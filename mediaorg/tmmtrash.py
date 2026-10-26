"""Cleaning of tinyMediaManager's trash: <movies>/.deletedByTMM and <series>/.deletedByTMM.

tMM moves what it replaces or deletes (old artwork and NFO files when it renames, folders deleted in its
UI) into a ".deletedByTMM" folder at the root of each data source.  Its own option "Delete trash/backup
files folder on exit" only acts when tMM shuts down, which a container rarely does.

A file keeps its own modification time when tMM moves it there, so its age in the trash cannot be read
from the disk.  mediaorg counts how long it has seen each file there (tmm_trash.json in the state folder,
written by --apply runs): every run adds the time since the previous one, at most AGE_STEP_CAP (12 hours),
so a clock that jumps (corrected after running late, a virtual machine restored from a snapshot together
with this file) makes a file due at most that much early, and a longer stop adds only that much.  A file is
deleted once it has been seen for review.tmm_trash_retention_days, or _minutes (0 = in the run that first
sees it, -1 = never); a file whose size or modification time changed counts as new.  Only regular files and
symbolic links (the link itself, never its target) below these two folders are deleted, each re-checked
right before (same size and time, the same file as at the scan as far as the mount can tell, no folder on
its path turned into a link); then the folders below them that hold nothing: those this run emptied, and
others once they have not changed for 10 minutes (tMM may have just made one to move a file into).  The
two .deletedByTMM folders themselves and anything outside them are never touched.
"""

from __future__ import annotations

import errno
import json
import math
import os
import stat
from dataclasses import dataclass, field

from .config import Config

TRASH_NAME = ".deletedByTMM"
STATE_FILE = "tmm_trash.json"
STATE_VERSION = 2  # files: path -> [size, mtime, seconds seen, clock of the last count]
DIR_QUIET_SECONDS = 600  # an empty folder tMM made in the last 10 minutes may be about to receive a file
# The most one run adds to the age of a file: longer than any gap between runs of the schedules in
# a timer schedule (at most a night: 8 hours), so the retention lasts its days; a clock jump makes a file
# due at most this much early.
AGE_STEP_CAP = 12 * 3600


@dataclass
class TrashFile:
    path: str
    size: int
    mtime: int
    age: float  # seconds mediaorg has seen it in the trash, this run included
    link: bool = False
    ident: tuple[int, int] = (0, 0)  # (st_dev, st_ino) at the scan: a file replaced since then is left alone


@dataclass
class TrashScan:
    root: str  # <movies or series>/.deletedByTMM
    root_ident: tuple[int, int] = (0, 0)
    files: list[TrashFile] = field(default_factory=list)
    dirs: list[str] = field(default_factory=list)  # every real folder below root
    unreadable: list[str] = field(default_factory=list)  # left alone and reported
    prev_folder_errors: list[str] = field(default_factory=list)  # folders the last --apply run could not remove

    def due(self, seconds: int) -> list[TrashFile]:
        """Files seen for at least the retention (review.tmm_trash_retention_seconds; -1 = never)."""
        if seconds < 0:
            return []
        return [f for f in self.files if f.age >= seconds]

    def empty_dirs(self, gone: set[str] | frozenset[str] = frozenset()) -> list[str]:
        """Folders below root that hold no file (once those in `gone` are deleted) and no unreadable entry,
        deepest first."""
        busy: set[str] = set()
        held = [os.path.dirname(f.path) for f in self.files if f.path not in gone] + list(self.unreadable)
        for d in held:
            while d.startswith(self.root + os.sep) and d not in busy:
                busy.add(d)
                d = os.path.dirname(d)
        return sorted((d for d in self.dirs if d not in busy), key=lambda p: (-p.count(os.sep), p))


@dataclass
class TrashState:
    files: dict[str, tuple[int, int, float, float]] = field(default_factory=dict)
    saved_at: float | None = None  # clock of the last --apply run that recorded the trash (information)
    unreadable: list[str] = field(default_factory=list)  # as reported by that run
    folder_errors: list[str] = field(default_factory=list)  # empty folders that run could not remove


@dataclass
class PurgeResult:
    deleted: list[str] = field(default_factory=list)
    size: int = 0
    errors: list[str] = field(default_factory=list)  # files that could not be deleted: the run fails
    removed: list[str] = field(default_factory=list)  # empty folders removed
    folder_errors: list[tuple[str, str]] = field(default_factory=list)  # (path, reason) of folders left behind
    left: list[tuple[str, str]] = field(default_factory=list)  # (path, reason) of due files not deleted this run
    skipped: str | None = None  # why the whole trash folder was left alone in this run


def _number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _strings(x) -> list[str]:
    return [s for s in x if isinstance(s, str)] if isinstance(x, list) else []


def load_state(state_dir: str) -> TrashState:
    """The recorded state; empty when missing, unreadable or of another version (everything then counts
    as new: the safe direction)."""
    try:
        with open(os.path.join(state_dir, STATE_FILE), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return TrashState()
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        return TrashState()
    raw = data.get("files")
    files = {}
    for p, v in (raw.items() if isinstance(raw, dict) else ()):
        if isinstance(p, str) and isinstance(v, list) and len(v) == 4 and all(_number(x) for x in v) and v[2] >= 0:
            files[p] = (int(v[0]), int(v[1]), float(v[2]), float(v[3]))
    saved = data.get("saved_at")
    return TrashState(files, float(saved) if _number(saved) else None, _strings(data.get("unreadable")),
                      _strings(data.get("folder_errors")))


def save_state(state_dir: str, scans: list[TrashScan], deleted: set[str], now: float,
               folder_errors: list[str] = ()) -> None:
    """Record what is still in the trash, how long each file has been seen, and the folders that could not
    be removed (raises OSError)."""
    files = {f.path: [f.size, f.mtime, f.age, now] for sc in scans for f in sc.files if f.path not in deleted}
    path = os.path.join(state_dir, STATE_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", errors="backslashreplace") as fh:
        json.dump({"version": STATE_VERSION, "saved_at": now, "files": files,
                   "unreadable": sorted(p for sc in scans for p in sc.unreadable),
                   "folder_errors": sorted(folder_errors)}, fh, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def roots(cfg: Config) -> list[str]:
    return [os.path.join(cfg.paths.movies, TRASH_NAME), os.path.join(cfg.paths.series, TRASH_NAME)]


def scan(cfg: Config, state: TrashState, now: float) -> list[TrashScan]:
    """Read-only walk of the two trash folders.  Raises transfer.StorageUnavailable when the NAS stops
    answering."""
    from .transfer import UNREACHABLE, StorageUnavailable

    out: list[TrashScan] = []
    for root in roots(cfg):
        try:
            st = os.lstat(root)
        except FileNotFoundError:
            continue
        except OSError as exc:
            if exc.errno in UNREACHABLE:
                raise StorageUnavailable(f"reading {root}: {exc}") from exc
            raise
        if not stat.S_ISDIR(st.st_mode):  # a file or a symlink with that name: not tMM's trash folder
            continue
        sc = TrashScan(root, (st.st_dev, st.st_ino),
                       prev_folder_errors=[p for p in state.folder_errors if p.startswith(root + os.sep)])

        def onerror(exc: OSError, sc=sc, root=root) -> None:
            if exc.errno in UNREACHABLE:
                raise StorageUnavailable(f"listing {exc.filename}: {exc}") from exc
            sc.unreadable.append(exc.filename or root)

        for d, dirs, names in os.walk(root, onerror=onerror, followlinks=False):
            linked = {x for x in dirs if os.path.islink(os.path.join(d, x))}
            sc.dirs.extend(os.path.join(d, x) for x in dirs if x not in linked)
            for n in sorted([*names, *linked]):
                p = os.path.join(d, n)
                try:
                    fst = os.lstat(p)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    if exc.errno in UNREACHABLE:
                        raise StorageUnavailable(f"reading {p}: {exc}") from exc
                    sc.unreadable.append(p)
                    continue
                link = stat.S_ISLNK(fst.st_mode)
                if not link and not stat.S_ISREG(fst.st_mode):
                    sc.unreadable.append(p)  # device, socket ...: not ours to delete
                    continue
                size, mtime = (0, 0) if link else (fst.st_size, int(fst.st_mtime))
                prev = state.files.get(p)
                age = 0.0
                if prev and prev[0] == size and prev[1] == mtime:
                    step = now - prev[3]  # negative when the clock went back: no time counted
                    age = prev[2] + min(max(step, 0.0), AGE_STEP_CAP)
                sc.files.append(TrashFile(p, size, mtime, age, link, (fst.st_dev, fst.st_ino)))
        out.append(sc)
    return out


def retention_text(seconds: int) -> str:
    if seconds % 86400 == 0:
        return f"{seconds // 86400} day(s)"
    if seconds % 60 == 0:
        return f"{seconds // 60} minute(s)"
    return f"{seconds} second(s)"


def purge(sc: TrashScan, seconds: int, now: float) -> PurgeResult:
    """Delete the due files of one trash folder, then the empty, quiet folders below it.  Raises
    transfer.StorageUnavailable when the NAS stops answering."""
    from .transfer import UNREACHABLE, StorageUnavailable

    res = PurgeResult()
    try:
        rst = os.lstat(sc.root)
        if not stat.S_ISDIR(rst.st_mode) or (rst.st_dev, rst.st_ino) != sc.root_ident:
            # replaced since the scan, or (noserverino) its cached inode number was renewed: next run
            res.skipped = "not the folder seen at the scan any more; left alone in this run"
            return res
        real_root = os.path.realpath(sc.root)
    except FileNotFoundError:
        return res
    except OSError as exc:
        if exc.errno in UNREACHABLE:
            raise StorageUnavailable(f"reading {sc.root}: {exc}") from exc
        res.errors.append(f"{sc.root}: {exc.strerror or exc}")
        return res
    plain: dict[str, bool] = {}

    def plain_path(d: str) -> bool:
        """True when no folder from below the trash root down to d (included) is a symbolic link."""
        if d not in plain:
            rel = os.path.relpath(d, sc.root)
            plain[d] = os.path.realpath(d) == (real_root if rel == "." else os.path.join(real_root, rel))
        return plain[d]

    emptied: set[str] = set()  # folders whose content this run removed: their new mtime is ours
    for f in sc.due(seconds):
        try:
            if not plain_path(os.path.dirname(f.path)):
                res.errors.append(f"{f.path}: a folder on its path is now a link; left alone")
                continue
            st = os.lstat(f.path)
            if (st.st_dev, st.st_ino) != f.ident:
                res.left.append((f.path, "another file under that name since the scan"))
                continue  # seen again by the next run
            if f.link:
                if not stat.S_ISLNK(st.st_mode):
                    res.left.append((f.path, "no longer a link"))
                    continue
            elif not (stat.S_ISREG(st.st_mode) and st.st_size == f.size and int(st.st_mtime) == f.mtime):
                res.left.append((f.path, f"changed since the scan (size {f.size} -> {st.st_size}, "
                                         f"time {f.mtime} -> {int(st.st_mtime)}): counted as new"))
                continue
            os.unlink(f.path)
            res.deleted.append(f.path)
            res.size += f.size
            emptied.add(os.path.dirname(f.path))
        except FileNotFoundError:
            # removed meanwhile, or a deletion still pending on the NAS because another program keeps the
            # file open: the NAS then refuses to open it, and removes it once that program closes it
            res.left.append((f.path, "no longer readable: deleted elsewhere, or still open in another program"))
            continue
        except OSError as exc:
            if exc.errno in UNREACHABLE:
                raise StorageUnavailable(f"deleting {f.path}: {exc}") from exc
            res.errors.append(f"{f.path}: {exc.strerror or exc}")
    for d in sc.empty_dirs(set(res.deleted)):  # deepest first; never the trash root
        try:
            st = os.lstat(d)
            if not stat.S_ISDIR(st.st_mode) or (d not in emptied and now - st.st_mtime < DIR_QUIET_SECONDS):
                continue
            if not plain_path(d):
                continue
            os.rmdir(d)  # only ever succeeds on an empty folder
            res.removed.append(d)
            emptied.add(os.path.dirname(d))
        except OSError as exc:
            if exc.errno in UNREACHABLE:
                raise StorageUnavailable(f"removing {d}: {exc}") from exc
            if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT):
                res.folder_errors.append((d, str(exc.strerror or exc)))
    return res


def describe(sc: TrashScan, seconds: int, base: str) -> str | None:
    """One report line for a trash folder with due files, else None."""
    from .review import human

    due = sc.due(seconds)
    if not due:
        return None
    rel = os.path.relpath(sc.root, base) if sc.root.startswith(base + "/") else sc.root
    names = [os.path.relpath(f.path, sc.root) for f in due]
    shown = ", ".join(repr(n) for n in names[:4]) + (f" (+{len(names) - 4} more)" if len(names) > 4 else "")
    rule = "in the run that first sees them" if seconds == 0 else f"once seen there for {retention_text(seconds)}"
    waiting = len(sc.files) - len(due)
    return (f"{rel}  -- deleting {len(due)} file(s), {human(sum(f.size for f in due))}: {shown} (tMM's trash is "
            f"cleaned {rule})" + (f"; {waiting} newer file(s) wait" if waiting else ""))
