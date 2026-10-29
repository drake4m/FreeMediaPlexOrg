"""Is a Downloads item finished and no longer used by the download client?

Moving files while the torrent client still writes or seeds them makes the client re-create
them in Downloads (and download them again).  Hence:

  * method "freebox": the Freebox task status is authoritative for items it knows.
    Ready only when the task is `done`, or `stopped` at 100 %.  `seeding` waits
    (on_seeding = "wait"), or, with on_seeding = "stop", a run that files the item stops the
    task first (stop_seeding) and files it once the Freebox lists it as stopped.  Files the
    task did not fully receive are blocked.
  * items the Freebox does not list (copied manually) and method "quiescence":
    nothing modified for min_quiet_minutes and no temporary download extensions.
"""

from __future__ import annotations

import json
import logging
import os
import time
import unicodedata

from .config import Config
from .freebox import FreeboxApiError, FreeboxClient, FreeboxError, Task, TaskFile
from .model import Item, Readiness

log = logging.getLogger(__name__)

READY_STATUSES = {"done"}
STOP_CONFIRM_SECONDS = 15  # how long a run waits for the Freebox to list a stopped task as "stopped"
WAIT_REASON = {
    "seeding": "Freebox task is seeding (waits for 'done' at the Freebox stop ratio; "
               "completeness.on_seeding = \"stop\" stops it when it is filed)",
    "downloading": "Freebox task is downloading",
    "queued": "Freebox task is queued",
    "starting": "Freebox task is starting",
    "stopping": "Freebox task is stopping",
    "checking": "Freebox task is being verified",
    "repairing": "Freebox task is being repaired",
    "extracting": "Freebox task is extracting",
    "retry": "Freebox task is retrying",
    "error": "Freebox task is in error",
}


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def quiescence(item: Item, cfg: Config, now: float | None = None) -> Readiness:
    now = time.time() if now is None else now
    temp = [f.rel for f in item.files if f.ext in cfg.completeness.temp_extensions]
    if temp:
        return Readiness(False, f"temporary download file present: {temp[0]}")
    age_min = (now - item.newest_mtime) / 60
    if age_min < cfg.completeness.min_quiet_minutes:
        return Readiness(False, f"modified {age_min:.0f} min ago (< {cfg.completeness.min_quiet_minutes} min)")
    return Readiness(True, f"quiet for {age_min:.0f} min")


class FilesCache:
    """File lists of fully received Freebox tasks, kept between runs (<state_dir>/freebox_files.json),
    so that a run asks the Freebox only for the files of tasks that are still changing.
    An entry is used only while the task has the same id, creation time, name, status, size and
    progress; anything else is asked again."""

    def __init__(self, path: str | None):
        self.path = path
        self.old: dict[str, list[dict]] = {}
        self.new: dict[str, list[dict]] = {}
        if path:
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    self.old = {k: v for k, v in data.items() if isinstance(v, list)}
            except (OSError, ValueError):
                self.old = {}

    @staticmethod
    def key(t: Task) -> str:
        return f"{t.id}|{t.created_ts}|{t.name}|{t.status}|{t.size}|{t.rx_pct}"

    def get(self, t: Task) -> list[TaskFile] | None:
        rows = self.old.get(self.key(t))
        if not rows:
            return None
        try:
            files = [TaskFile(**r) for r in rows]
        except TypeError:
            return None
        self.new[self.key(t)] = rows
        return files

    def put(self, t: Task, files: list[TaskFile]) -> None:
        if t.rx_pct >= 10000 and files:  # only finished data: the list no longer changes
            self.new[self.key(t)] = [f.__dict__.copy() for f in files]

    def save(self) -> None:
        if not self.path or self.new == self.old:
            return
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8", errors="backslashreplace") as fh:
                json.dump(self.new, fh, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError as exc:  # a cache: never fatal
            log.debug("Freebox file cache not saved: %s", exc)


class FreeboxState:
    """Snapshot of Freebox download tasks for one run.

    Every task whose download_dir is the Downloads folder or any folder below it is mapped to the
    top-level Downloads entry (or entries) holding its files (downloads/{id}/files).  A task's
    *name* can differ from what it writes, e.g. a magnet whose display name lacks the ".mkv" of
    its single file (seen with a real Freebox), so the name is used only for a task whose
    files map to no entry present in Downloads (file list not known yet, or files no longer
    there): as the entry name, and for a loose video or subtitle whose name is that task name plus
    a media extension.  An entry is ready only when all tasks writing into it are finished.
    """

    def __init__(self, cfg: Config, client: FreeboxClient, files_cache: FilesCache | None = None,
                 present: set[str] | None = None):
        self.cfg = cfg
        self.client = client
        self.cache = files_cache
        self.remote_dir = _nfc(cfg.freebox.remote_downloads_dir.rstrip("/"))
        self.by_entry: dict[str, list[Task]] = {}
        self._by_name_only: dict[str, list[Task]] = {}  # tasks whose files map to no present entry
        self._files: dict[int, list[TaskFile]] = {}
        if present is None:  # NFC + casefolded names of the entries now in Downloads
            try:
                present = {_nfc(n).casefold() for n in os.listdir(cfg.paths.downloads)}
            except OSError:
                present = None  # unknown: every entry named by a task's files counts as present
        for t in client.tasks():
            ddir = _nfc(t.download_dir.rstrip("/"))
            if ddir != self.remote_dir and not ddir.startswith(self.remote_dir + "/"):
                continue
            entries: set[str] = set()
            for f in self._task_files(t):
                fp = _nfc(f.filepath)
                if fp.startswith(self.remote_dir + "/"):
                    entries.add(fp[len(self.remote_dir) + 1:].split("/")[0].casefold())
            if not (entries if present is None else entries & present):
                rel = f"{ddir[len(self.remote_dir):].strip('/')}/{_nfc(t.name)}".strip("/")
                if rel:
                    key = rel.split("/")[0].casefold()
                    entries.add(key)
                    self._by_name_only.setdefault(key, []).append(t)
            for e in entries:
                self.by_entry.setdefault(e, []).append(t)

    def _task_files(self, t: Task) -> list[TaskFile]:
        if t.id not in self._files:
            files = self.cache.get(t) if self.cache else None
            if files is None:
                try:
                    files = self.client.task_files(t.id)
                except FreeboxApiError as exc:
                    # the box answered but refused this one list (task in an odd state): the task is then
                    # mapped by its name, and its status still decides; a box that does not answer at
                    # all (FreeboxError) still stops the run
                    log.warning("Freebox task %s (%r): file list not available (%s); mapped by its name",
                                t.id, t.name, exc)
                    files = []
                else:
                    if self.cache:
                        self.cache.put(t, files)
            self._files[t.id] = files
        return self._files[t.id]

    def _tasks_for(self, name: str, loose: bool = False) -> list[Task]:
        key = _nfc(name).casefold()
        tasks = self.by_entry.get(key)
        if tasks:
            return tasks
        if not loose:
            return []
        stem, ext = os.path.splitext(key)
        media = (*self.cfg.media.video_extensions, *self.cfg.media.subtitle_extensions)
        return self._by_name_only.get(stem, []) if stem and ext in media else []

    def to_local(self, remote_path: str) -> str | None:
        rp = _nfc(remote_path)
        if not (rp == self.remote_dir or rp.startswith(self.remote_dir + "/")):
            return None
        return _nfc(self.cfg.paths.downloads + rp[len(self.remote_dir):])

    def _stoppable(self, t: Task) -> bool:
        """A finished download that is only seeding, and on_seeding = "stop" lets a run stop it."""
        return self.cfg.completeness.on_seeding == "stop" and t.status == "seeding" and t.rx_pct >= 10000

    def _task_check(self, tasks: list[Task], blocked: set[str], stop: set[int]) -> str | None:
        for t in tasks:
            if self._stoppable(t):
                stop.add(t.id)
            elif not (t.status in READY_STATUSES or (t.status == "stopped" and t.rx_pct >= 10000)):
                return WAIT_REASON.get(t.status, f"Freebox task status '{t.status}'")
            for f in self._task_files(t):
                if f.priority == "no_dl" or f.rx < f.size:
                    local = self.to_local(f.filepath)
                    if local:
                        blocked.add(os.path.normpath(local))
        return None

    def assess(self, item: Item, now: float | None = None) -> Readiness:
        now = time.time() if now is None else now
        blocked: set[str] = set()
        stop: set[int] = set()
        tasks = self._tasks_for(item.name, loose=not item.is_dir)
        if tasks:
            why = self._task_check(tasks, blocked, stop)
            if why:
                return Readiness(False, why)
            reason = "Freebox: " + ", ".join(sorted({t.status for t in tasks}))
        elif self.cfg.completeness.untracked == "quiescence":
            r = quiescence(item, self.cfg, now)
            if not r.ready:
                return Readiness(False, f"not a Freebox task; {r.reason}")
            reason = f"not a Freebox task; {r.reason}"
        else:
            return Readiness(False, f"{item.name!r} is not a Freebox download task "
                                    "(completeness.untracked = \"wait\")")
        # loose sidecars attached to a loose video: their own task must be finished, or
        # (if they are not tasks) they must have been quiet for min_quiet_minutes
        for f in item.files if not item.is_dir else []:
            if f.name == item.name:
                continue
            extra = self._tasks_for(f.name, loose=True)
            if extra:
                why = self._task_check(extra, blocked, stop)
                if why:
                    return Readiness(False, f"attached {f.name!r}: {why}")
            elif (now - f.mtime) / 60 < self.cfg.completeness.min_quiet_minutes:
                return Readiness(False, f"attached {f.name!r} modified recently")
        return Readiness(True, reason, frozenset(blocked), frozenset(stop))


def _finished(t: Task) -> bool:
    return t.status in ("stopped", *READY_STATUSES) and t.rx_pct >= 10000


def stop_seeding(client: FreeboxClient, task_ids, timeout_s: float | None = None, sleep=None,
                 names: dict[int, str] | None = None) -> tuple[bool, str]:
    """Stop Freebox tasks that are seeding a finished download (completeness.on_seeding = "stop"), then wait
    until the Freebox lists each one as stopped (or done) at 100 %.  The state is read again first: only a
    task that is still seeding at 100 % is stopped (never one that went back to downloading or checking).
    -> (True, what was done) or (False, why the item waits: it is not filed in this run).
    FreeboxError (the box does not answer) is left to the caller."""
    ids = sorted(set(task_ids))
    names = names or {}
    timeout_s = STOP_CONFIRM_SECONDS if timeout_s is None else timeout_s
    sleep = sleep or time.sleep

    def listed() -> tuple[dict[int, Task], str | None]:
        now = {t.id: t for t in client.tasks() if t.id in ids}
        gone = [tid for tid in ids if tid not in now]
        return now, (f"Freebox task {gone[0]} is gone (removed meanwhile?); the next run looks again" if gone
                     else None)

    now, why = listed()
    if why:
        return False, why
    # all or nothing as far as the box allows: no task is stopped unless every task of the item is finished
    # or seeding at 100 % now
    for tid in ids:
        t = now[tid]
        if not (_finished(t) or (t.status == "seeding" and t.rx_pct >= 10000)):
            return False, f"Freebox task {tid} is now '{t.status}'; the next run looks again"
    stopped: list[int] = []
    for tid in (i for i in ids if not _finished(now[i])):
        try:
            client.stop_task(tid)
        except FreeboxApiError as exc:
            if stopped:
                log.warning("Freebox task(s) %s stopped, but not task %s (%s): the item waits; stopped tasks stay "
                            "stopped", ", ".join(map(str, stopped)), tid, exc)
            if exc.code == "task_not_found":
                return False, f"Freebox task {tid} is gone (removed meanwhile?); the next run looks again"
            return False, f"Freebox task {tid} could not be stopped ({exc}); it keeps seeding"
        stopped.append(tid)
        log.info("Freebox task %s (%r) was seeding a finished download: stop requested", tid, names.get(tid, "?"))
    if stopped:
        now, why = listed()
        if why:
            return False, why
    deadline = time.monotonic() + timeout_s
    while True:
        pending = [t for t in now.values() if not _finished(t)]
        if not pending:
            return True, f"Freebox task {', '.join(map(str, ids))} was seeding: stopped"
        if time.monotonic() >= deadline:
            t = pending[0]
            return False, (f"Freebox task {t.id} is still '{t.status}' {timeout_s:g} s after the stop request; "
                           "filed by a later run once it is stopped")
        sleep(1)
        now, why = listed()
        if why:
            return False, why


def freebox_session(cfg: Config, client_factory=None) -> FreeboxClient:
    """A logged-in Freebox client (the caller logs out)."""
    from .freebox import load_token

    token = load_token(cfg.freebox.token_file)
    client = client_factory() if client_factory else FreeboxClient(
        cfg.freebox.base_url, cfg.freebox.ca_file, cfg.freebox.timeout_seconds)
    try:
        client.login(token["app_id"], token["app_token"])
    except FreeboxError:
        client.logout()  # a session opened without the downloader permission is closed too
        raise
    return client


def make_assessor(cfg: Config, client_factory=None):
    """Return (assess(item) -> Readiness, close())."""
    if cfg.completeness.method == "quiescence":
        return (lambda item: quiescence(item, cfg)), (lambda: None)
    from .config import ConfigError

    if not cfg.freebox.remote_downloads_dir:
        raise ConfigError("freebox.remote_downloads_dir is not set (see `sudo mediaorg freebox status`)")
    client = freebox_session(cfg, client_factory)
    cache = FilesCache(os.path.join(cfg.paths.state_dir, "freebox_files.json"))
    try:
        # Fail closed if the configured folder is not the Freebox's download folder: otherwise
        # no task would map to Downloads and seeding torrents would look "untracked".
        from .freebox import same_remote_dir
        fb_dir = client.download_config()["download_dir"]
        if not same_remote_dir(fb_dir, cfg.freebox.remote_downloads_dir):
            raise FreeboxError(f"freebox.remote_downloads_dir is {cfg.freebox.remote_downloads_dir!r} but the "
                               f"Freebox downloads into {fb_dir!r}; fix the config "
                               "(see `sudo mediaorg freebox status`)")
        state = FreeboxState(cfg, client, cache)
    except FreeboxError:
        client.logout()
        raise

    def close() -> None:
        cache.save()
        client.logout()

    return state.assess, close
