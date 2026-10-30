"""Is a Downloads item finished and no longer used by the download client?

Moving files while the torrent client still writes or seeds them makes the client re-create
them in Downloads (and download them again).  Hence:

  * method "freebox": the Freebox task status is authoritative for items it knows.
    Ready only when the task is `done`, or `stopped` at 100 %.  `seeding` waits
    (on_seeding = "wait"), or, with on_seeding = "stop", a run that files the item stops the
    task first (stop_seeding) and files it once the Freebox lists it as stopped.  Files the
    task did not fully receive are blocked.
  * an item whose task is gone from the Freebox (removed in Freebox OS, its files kept): what the
    Freebox last said about it decides (SeenTasks): ready if the task had received everything, never
    filed if it had not (the files are incomplete), until a task writing all of its files is added again.
    A recorded task the Freebox still lists, but whose files were not matched to the item in this run,
    makes the item wait unless it is finished.
  * items the Freebox never listed (copied manually) and method "quiescence":
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


SEEN_SKEW_SECONDS = 120  # the share's clock and this machine's may differ a little


def _complete(info: dict) -> bool:
    """Would a task in this state make its download ready (finished and no longer written)?"""
    return info.get("status") == "done" or (info.get("status") in ("stopped", "seeding")
                                             and info.get("pct", 0) >= 10000)


def _covered(info: dict, files: set[str], names: set[str]) -> bool:
    """Is a recorded task replaced by the tasks now writing into its entry (the download added again in
    Freebox OS): do they write every file it wrote (or, its files unknown, carry its name)?"""
    if info.get("files"):
        return set(info["files"]) <= files
    return bool(info.get("name")) and _nfc(info["name"]).casefold() in names


class SeenTasks:
    """What the Freebox last said about the tasks of each Downloads entry (<state_dir>/freebox_seen.json),
    for entries whose task disappears from the Freebox (removed in Freebox OS, the files kept):
    {entry key (NFC, casefolded name): {"t": epoch of that run, "tasks": {"<id>": {"pct": 0..10000,
    "status": str, "name": str, "created": epoch, "files": [local paths it writes], "blocked": [those it did
    not fully receive, e.g. deselected ones]}}}}.
    A task that no longer writes into its entry (removed in Freebox OS, or not matched in a run) stays
    recorded, with the files it never fully received, until tasks writing all of its files are listed again
    (the download added again).  Entries gone from Downloads are forgotten."""

    def __init__(self, path: str | None):
        self.path = path
        self.old: dict[str, dict] = {}
        self.new: dict[str, dict] = {}
        if path:
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    self.old = {k: v for k, v in data.items() if self._valid(v)}
            except (OSError, ValueError):
                self.old = {}

    @staticmethod
    def _valid(rec) -> bool:
        if not (isinstance(rec, dict) and isinstance(rec.get("t"), (int, float))
                and isinstance(rec.get("tasks"), dict) and rec["tasks"]):
            return False

        def paths(v) -> bool:
            return isinstance(v, list) and all(isinstance(p, str) and "\x00" not in p for p in v)

        return all(isinstance(k, str) and k.isascii() and k.isdecimal() and isinstance(v, dict)
                   and isinstance(v.get("pct"), int) and isinstance(v.get("status"), str)
                   and isinstance(v.get("name", ""), str) and isinstance(v.get("created", 0), int)
                   and paths(v.get("files", [])) and paths(v.get("blocked", []))
                   for k, v in rec["tasks"].items())

    @staticmethod
    def info(state: FreeboxState, t: Task) -> dict:
        """What is recorded of task `t`."""
        files, blocked = set(), set()
        for f in state._task_files(t):
            local = state.to_local(f.filepath)
            if local:
                files.add(os.path.normpath(local))
                if f.priority == "no_dl" or f.rx < f.size:
                    blocked.add(os.path.normpath(local))
        rec = {"pct": int(t.rx_pct), "status": t.status, "name": _nfc(t.name)}
        if isinstance(t.created_ts, int):
            rec["created"] = t.created_ts
        if files:
            rec["files"] = sorted(files)
        if blocked:
            rec["blocked"] = sorted(blocked)
        return rec

    def get(self, key: str) -> dict | None:
        return self.old.get(key)

    def observe(self, state: FreeboxState) -> None:
        """Remember the tasks of every entry now in Downloads; keep the last record of an entry that has no task
        now; forget entries no longer in Downloads."""
        if state.present is None:  # Downloads could not be listed: forget nothing
            self.new = dict(self.old)
        now = int(time.time())
        for key in (state.present if state.present is not None else state.by_entry):
            tasks = state.file_tasks(key)  # not a task linked by its name only (its files may go elsewhere)
            old = self.old.get(key)
            if tasks:
                infos = {str(t.id): self.info(state, t) for t in tasks}
                if old:  # recorded tasks no longer writing into the entry are kept unless replaced
                    files = {p for i in infos.values() for p in i.get("files", [])}
                    names = {_nfc(t.name).casefold() for t in tasks}
                    for tid, info in old["tasks"].items():
                        if tid in infos or _covered(info, files, names):
                            continue
                        infos[tid] = self._refreshed(state, tid, info)
                self.new[key] = {"t": now, "tasks": infos}
            elif old:  # no task writes into it now: the record stays, with the state now of tasks still listed
                self.new[key] = {**old, "tasks": {tid: self._refreshed(state, tid, info)
                                                  for tid, info in old["tasks"].items()}}

    @staticmethod
    def _refreshed(state: FreeboxState, tid: str, info: dict) -> dict:
        lt = state.listed_task(tid, info)
        return info if lt is None else {**info, "pct": int(lt.rx_pct), "status": lt.status}

    def save(self) -> None:
        if not self.path or self.new == self.old:
            return
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8", errors="backslashreplace") as fh:
                json.dump(self.new, fh, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError as exc:  # only a memory: never fatal
            log.debug("Freebox task memory not saved: %s", exc)


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
                 present: set[str] | None = None, seen: SeenTasks | None = None):
        self.cfg = cfg
        self.client = client
        self.cache = files_cache
        self.seen = seen
        self.remote_dir = _nfc(cfg.freebox.remote_downloads_dir.rstrip("/"))
        self.by_entry: dict[str, list[Task]] = {}
        self._by_name_only: dict[str, list[Task]] = {}  # tasks whose files map to no present entry
        self._file_entries: dict[int, set[str]] = {}  # entries each task's files map to
        self._files: dict[int, list[TaskFile]] = {}
        if present is None:  # NFC + casefolded names of the entries now in Downloads
            try:
                present = {_nfc(n).casefold() for n in os.listdir(cfg.paths.downloads)}
            except OSError:
                present = None  # unknown: every entry named by a task's files counts as present
        self.present = present
        tasks = client.tasks()
        self.listed = {t.id: t for t in tasks}  # every task the Freebox lists now, wherever it downloads
        for t in tasks:
            ddir = _nfc(t.download_dir.rstrip("/"))
            if ddir != self.remote_dir and not ddir.startswith(self.remote_dir + "/"):
                continue
            entries: set[str] = set()
            for f in self._task_files(t):
                fp = _nfc(f.filepath)
                if fp.startswith(self.remote_dir + "/"):
                    entries.add(fp[len(self.remote_dir) + 1:].split("/")[0].casefold())
            self._file_entries[t.id] = set(entries)
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

    def file_tasks(self, key: str) -> list[Task]:
        """The tasks whose files map to the entry `key` (NFC, casefolded name)."""
        return [t for t in self.by_entry.get(key, []) if key in self._file_entries.get(t.id, ())]

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

    def listed_task(self, tid: str, info: dict) -> Task | None:
        """The task the Freebox still lists as recorded task `tid` (same creation time, when known), or None."""
        t = self.listed.get(int(tid))
        if t is None or (isinstance(info.get("created"), int) and isinstance(t.created_ts, int)
                         and t.created_ts != info["created"]):
            return None
        return t

    def _unfinished(self, seen: dict, skip=lambda tid, info: False) -> str | None:
        """Why an entry waits for a recorded task that is not matched in this run (`skip` leaves some out):
        one the Freebox still lists and that is not finished (it may still write or seed), or one that is
        gone and had not received everything.  None if there is none."""
        tasks = sorted(seen["tasks"].items(), key=lambda x: int(x[0]))
        for tid, info in tasks:
            lt = self.listed_task(tid, info)
            if lt is not None and not _finished(lt) and not skip(tid, info):
                return (f"Freebox task {tid} is still listed ('{lt.status}' at {lt.rx_pct / 100:.1f} %), but none of "
                        "its files could be matched to this entry in this run; the next run looks again")
        for tid, info in tasks:
            if self.listed_task(tid, info) is None and not _complete(info) and not skip(tid, info):
                return self._gone_reason(tid, info)
        return None

    def _others(self, seen: dict, tasks: list[Task], blocked: set[str]) -> str | None:
        """An entry some tasks matched in this run: its recorded tasks that none matched (removed, or not
        matched this time) and that were not replaced by the download added again.  -> why it waits, or None
        (then the files those tasks never fully received are added to `blocked`)."""
        matched = {t.id for t in tasks}
        files = {os.path.normpath(local) for t in tasks for f in self._task_files(t)
                 if (local := self.to_local(f.filepath))}
        names = {_nfc(t.name).casefold() for t in tasks}

        def skip(tid: str, info: dict) -> bool:
            return int(tid) in matched or _covered(info, files, names)

        why = self._unfinished(seen, skip)
        if why is None:  # what a removed task never fully received stays blocked, unless a task now has it in full
            received = {os.path.normpath(local) for t in tasks for f in self._task_files(t)
                        if f.priority != "no_dl" and f.rx >= f.size and (local := self.to_local(f.filepath))}
            blocked.update(p for tid, info in seen["tasks"].items() if not skip(tid, info)
                           for p in info.get("blocked", []) if p not in received)
        return why

    def _from_memory(self, name: str, newest_mtime: float, files: list, seen: dict, blocked: set[str]
                     ) -> Readiness | str | None:
        """An entry (or attached file) no task matched in this run, judged by what the Freebox last said about
        its tasks: a Readiness that is not ready, the reason it is ready, or None when the record does not
        apply (then completeness.untracked decides)."""
        why = self._unfinished(seen)
        if why:  # still listed and unfinished, or never filed: written up to its removal, then left incomplete
            return Readiness(False, why)
        if newest_mtime > seen["t"] + SEEN_SKEW_SECONDS:
            return None  # finished when last seen, yet written since: a new copy of that name
        temp = [f.rel for f in files if f.ext in self.cfg.completeness.temp_extensions]
        if temp:
            return Readiness(False, f"temporary download file present: {temp[0]}")
        # nothing writes or seeds these files any more; files a task never fully received (deselected ones)
        # stay blocked
        blocked.update(p for info in seen["tasks"].values() for p in info.get("blocked", []))
        listed = [(tid, t) for tid, info in sorted(seen["tasks"].items(), key=lambda x: int(x[0]))
                  if (t := self.listed_task(tid, info)) is not None]
        if listed:
            tid, t = listed[0]
            return (f"Freebox task {tid} is '{t.status}' (finished); its files were matched to {name!r} in an "
                    "earlier run")
        states = ", ".join(sorted({i["status"] for i in seen["tasks"].values()}))
        return f"its Freebox task is gone, removed after it finished (last seen '{states}')"

    @staticmethod
    def _gone_reason(tid: str, info: dict) -> str:
        return (f"its Freebox task {tid} is gone; it was last seen '{info['status']}' at {info['pct'] / 100:.1f} %, so "
                "its files may be incomplete: add the download again in Freebox OS, into the same folder (it checks "
                "the files and completes them), or file it by hand")

    def assess(self, item: Item, now: float | None = None) -> Readiness:
        now = time.time() if now is None else now
        blocked: set[str] = set()
        stop: set[int] = set()
        tasks = self._tasks_for(item.name, loose=not item.is_dir)
        seen = self.seen.get(_nfc(item.name).casefold()) if self.seen else None
        if tasks:
            why = self._task_check(tasks, blocked, stop) or (self._others(seen, tasks, blocked) if seen else None)
            if why:
                return Readiness(False, why)
            reason = "Freebox: " + ", ".join(sorted({t.status for t in tasks}))
        else:
            remembered = (self._from_memory(item.name, item.newest_mtime, item.files, seen, blocked)
                          if seen is not None else None)
            if isinstance(remembered, Readiness):
                return remembered
            if remembered is not None:
                reason = remembered
            elif self.cfg.completeness.untracked == "quiescence":
                r = quiescence(item, self.cfg, now)
                if not r.ready:
                    return Readiness(False, f"not a Freebox task; {r.reason}")
                reason = f"not a Freebox task; {r.reason}"
            else:
                return Readiness(False, f"{item.name!r} is not a Freebox download task "
                                        "(completeness.untracked = \"wait\")")
        # loose sidecars attached to a loose video: their own task must be finished; a task of theirs that is
        # gone decides as for the video; files no task ever wrote must have been quiet for min_quiet_minutes
        for f in item.files if not item.is_dir else []:
            if f.name == item.name:
                continue
            extra = self._tasks_for(f.name, loose=True)
            rec = self.seen.get(_nfc(f.name).casefold()) if self.seen else None
            if extra:
                why = self._task_check(extra, blocked, stop) or (self._others(rec, extra, blocked) if rec else None)
                if why:
                    return Readiness(False, f"attached {f.name!r}: {why}")
                continue
            r = self._from_memory(f.name, f.mtime, [f], rec, blocked) if rec is not None else None
            if isinstance(r, Readiness):
                return Readiness(False, f"attached {f.name!r}: {r.reason}")
            if r is None and (now - f.mtime) / 60 < self.cfg.completeness.min_quiet_minutes:
                return Readiness(False, f"attached {f.name!r} modified recently")
        return Readiness(True, reason, frozenset(blocked), frozenset(stop))


def forget_absent(cfg: Config) -> None:
    """Drop the task records of entries no longer in Downloads; every run, also one that does not ask the
    Freebox anything (a later download of the same name must not be judged by an old record)."""
    path = os.path.join(cfg.paths.state_dir, "freebox_seen.json")
    if not os.path.exists(path):
        return
    try:
        present = {_nfc(n).casefold() for n in os.listdir(cfg.paths.downloads)}
    except OSError:
        return
    seen = SeenTasks(path)
    seen.new = {k: v for k, v in seen.old.items() if k in present}
    seen.save()


def _finished(t: Task) -> bool:
    """Ready as far as this task goes: the same rule as FreeboxState._task_check."""
    return t.status in READY_STATUSES or (t.status == "stopped" and t.rx_pct >= 10000)


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

    def refused(tid: int, why: str) -> tuple[bool, str]:
        if stopped:
            log.warning("Freebox task(s) %s stopped, but not task %s (%s): the item waits; stopped tasks stay "
                        "stopped", ", ".join(map(str, stopped)), tid, why)
        return False, why

    for tid in ids:
        if _finished(now[tid]):
            continue
        try:
            client.stop_task(tid)
        except FreeboxApiError as exc:
            # refused: the task may have reached its stop ratio ('done'), been paused or removed meanwhile
            now, gone = listed()
            if gone:
                return refused(tid, gone)
            t = now[tid]
            if not _finished(t):
                return refused(tid, f"Freebox task {tid} could not be stopped ({exc}); it keeps seeding")
            log.info("Freebox task %s is now '%s' at 100 %%: nothing left to stop", tid, t.status)
            for other in ids:  # the others must still be finished or seeding at 100 %
                o = now[other]
                if other in stopped:  # stopped by this run: confirmed below
                    continue
                if not (_finished(o) or (o.status == "seeding" and o.rx_pct >= 10000)):
                    return refused(other, f"Freebox task {other} is now '{o.status}'; the next run looks again")
            continue
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
            if stopped:
                return True, f"Freebox task {', '.join(map(str, stopped))} was seeding: stopped"
            states = ", ".join(sorted({t.status for t in now.values()}))
            return True, f"Freebox task {', '.join(map(str, ids))} is now '{states}' at 100 %: nothing to stop"
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
    seen = SeenTasks(os.path.join(cfg.paths.state_dir, "freebox_seen.json"))
    try:
        # Fail closed if the configured folder is not the Freebox's download folder: otherwise
        # no task would map to Downloads and seeding torrents would look "untracked".
        from .freebox import same_remote_dir
        fb_dir = client.download_config()["download_dir"]
        if not same_remote_dir(fb_dir, cfg.freebox.remote_downloads_dir):
            raise FreeboxError(f"freebox.remote_downloads_dir is {cfg.freebox.remote_downloads_dir!r} but the "
                               f"Freebox downloads into {fb_dir!r}; fix the config "
                               "(see `sudo mediaorg freebox status`)")
        state = FreeboxState(cfg, client, cache, seen=seen)
        seen.observe(state)
    except FreeboxError:
        client.logout()
        raise

    def close() -> None:
        cache.save()
        seen.save()
        client.logout()

    return state.assess, close
