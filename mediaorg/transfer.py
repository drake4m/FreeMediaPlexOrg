"""Execute a plan with journaled, non-overwriting moves; reconcile; undo.

Overwrite protection on SMB 2/3
-------------------------------
The Linux SMB2/3 client always sends rename requests with ReplaceIfExists = 1
(fs/smb/client/smb2inode.c), so renameat2(RENAME_NOREPLACE) is only a client-side
existence check and a file created by another SMB client (tMM, Windows) in between
would be replaced by the server.  Files are therefore moved in two steps:

  1. reserve:  open(dst, O_CREAT | O_EXCL)  - the server creates the name atomically
               (FILE_CREATE disposition) or fails with EEXIST, case-insensitively;
  2. rename:   rename(src, dst)              - replaces only our own empty placeholder.

If the rename fails and the source is still in place, the placeholder (journaled as
"reserved", empty) is removed.  Directories are only renamed into review folders that
this run created, with RENAME_NOREPLACE.

Deletions (`unlink` ops, planned explicitly and journaled like moves) are limited to:
  * identical-duplicate: a download whose bytes equal library file `ref` (compared during
    planning); right before the unlink both files must still have the size and mtime seen
    then, otherwise the item is not touched;
  * empty-placeholder:   a 0-byte library file of the episode/movie being filed; it must
    still be 0 bytes.  `undo` re-creates it (empty, same mtime).
Besides these, only our own empty reservation placeholder is ever deleted, and `rmdir` only
removes empty directories.  (The review purge is separate: see review.py.)

Journal
-------
Every operation is written and fsync'ed before (intent / reserved) and after (done /
failed / unknown).  The next run reconciles open records from the actual disk state.
An outcome that cannot be established (NAS unreachable) aborts the run.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import json
import logging
import os
import re
import stat
import time
from dataclasses import dataclass, field

from .model import ItemPlan, Op

log = logging.getLogger(__name__)

AT_FDCWD = -100
RENAME_NOREPLACE = 1
UNREACHABLE = {errno.EHOSTDOWN, errno.EHOSTUNREACH, errno.ETIMEDOUT, errno.ENOTCONN, errno.ECONNRESET,
               errno.ECONNABORTED, errno.EIO, errno.ENETDOWN, errno.ENETUNREACH, errno.ESTALE}

_libc = ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)
_renameat2 = getattr(_libc, "renameat2", None)
if _renameat2 is not None:
    _renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    _renameat2.restype = ctypes.c_int


class NoReplaceUnsupported(OSError):
    pass


class StorageUnavailable(Exception):
    """The outcome of an operation cannot be established; the run must stop."""


def rename_noreplace(src: str, dst: str) -> None:
    if _renameat2 is None:
        raise NoReplaceUnsupported(errno.ENOSYS, "renameat2 not available in libc")
    rc = _renameat2(AT_FDCWD, os.fsencode(src), AT_FDCWD, os.fsencode(dst), RENAME_NOREPLACE)
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), src, None, dst)


def make_dir(path: str) -> None:
    """mkdir with the permissions of the folder it is created in, so that a library shared with a media
    server (755, or a group-shared 2775) keeps its modes.  The mode is given to mkdir itself, without the
    set-id bits: the timers' sandbox (RestrictSUIDSGID=) refuses any mode that carries them, and the kernel
    passes a parent's setgid bit on to a new folder by itself.  Best effort: some network filesystems
    decide the modes themselves."""
    want = stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode)
    old = os.umask(0)
    try:
        os.mkdir(path, want & 0o1777)
    finally:
        os.umask(old)
    try:
        if stat.S_IMODE(os.stat(path).st_mode) != want and not want & (stat.S_ISUID | stat.S_ISGID):
            os.chmod(path, want)
    except OSError:
        pass


def reserve(dst: str) -> None:
    """Atomically create an empty placeholder at dst; EEXIST if anything has that name."""
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o640)
    os.close(fd)


def _exists(path: str) -> bool | None:
    """True / False, or None when the storage cannot answer."""
    try:
        os.lstat(path)
        return True
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return None


def _gone(path: str) -> bool:
    """True only when `path` certainly does not exist (a storage that does not answer is not "gone")."""
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False
    return False


def _release_placeholder(src: str, dst: str) -> bool:
    """Remove dst only if it is our empty placeholder and the move clearly did not happen."""
    if _exists(src) is not True:
        return False
    try:
        st = os.lstat(dst)
        if stat.S_ISREG(st.st_mode) and st.st_size == 0:
            os.unlink(dst)
            return True
    except OSError:
        pass
    return False


def _empty_file(path: str) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_size == 0


def tree_fingerprint(root: str) -> frozenset:
    """{(relpath, size, int mtime)} of regular files; symlinks/specials are listed as such."""
    out = set()
    for d, dirs, files in os.walk(root, followlinks=False):
        for n in files + [x for x in dirs if os.path.islink(os.path.join(d, x))]:
            p = os.path.join(d, n)
            st = os.lstat(p)
            rel = os.path.relpath(p, root)
            if stat.S_ISREG(st.st_mode):
                out.add((rel, st.st_size, int(st.st_mtime)))
            else:
                out.add((rel, "special", 0))
    return frozenset(out)


# Test-only fault injection: MEDIAORG_TEST_CRASH_AFTER=N kills the process right after
# the N-th successful rename (before the journal records it), like a power cut.
_CRASH_AFTER = int(os.environ.get("MEDIAORG_TEST_CRASH_AFTER", "0") or 0)


class Journal:
    def __init__(self, directory: str, run_id: str):
        os.makedirs(directory, exist_ok=True)
        self.path = os.path.join(directory, f"{run_id}.jsonl")
        self.run_id = run_id
        self._fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
        self.seq = 0

    def write(self, rec: dict) -> None:
        rec = {"t": round(time.time(), 3), **rec}
        os.write(self._fd, (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8"))
        os.fsync(self._fd)

    def intent(self, op: Op, item: str) -> int:
        self.seq += 1
        rec = {"seq": self.seq, "state": "intent", "op": op.kind, "src": op.src, "dst": op.dst,
               "role": op.role, "item": item, "size": op.expect_size}
        if op.kind in ("unlink", "create"):
            rec["mtime"] = op.expect_mtime  # undo re-creates an empty placeholder with this mtime
        if op.ref:
            rec["ref"] = op.ref
        if op.purge:
            rec["purge"] = True  # review.py may delete it after the retention
        self.write(rec)
        return self.seq

    def close(self) -> None:
        os.close(self._fd)


def read_journal(path: str) -> tuple[dict, dict[int, dict], dict | None]:
    """-> (begin record, {seq: merged op record}, end record or None)"""
    begin, ops, end = {}, {}, None
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue  # torn last line after a crash
            if rec.get("state") == "begin":
                begin = rec
            elif rec.get("state") == "end":
                end = rec
            elif "seq" in rec:
                merged = ops.setdefault(rec["seq"], {})
                if rec.get("state") == "reserved":
                    merged["reserved"] = True
                merged.update(rec)
    return begin, ops, end


_JOURNAL_NAME = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{4}(-undo)?\.jsonl$")


def prune_journals(journal_dir: str, retention_days: int, now: float | None = None) -> int:
    """Delete journals of finished runs older than retention_days (0 = keep all).  A journal that was
    not closed cleanly (an interrupted run that still needs reconciling) is never deleted.
    Returns the number removed."""
    if retention_days <= 0 or not os.path.isdir(journal_dir):
        return 0
    limit = (now if now is not None else time.time()) - retention_days * 86400
    removed = 0
    for name in os.listdir(journal_dir):
        if not _JOURNAL_NAME.match(name):
            continue
        path = os.path.join(journal_dir, name)
        try:
            if os.lstat(path).st_mtime >= limit:
                continue
            _, _, end = read_journal(path)
            if end is None or end.get("needs_reconcile"):
                continue
            os.unlink(path)
            removed += 1
        except (OSError, ValueError):
            continue
    return removed


class Unfinished:
    """Downloads items whose processing started but did not complete (crash, failed op).

    Their remains are routed to review/leftovers by a later run instead of being
    ignored forever once no video is left in them.
    """

    def __init__(self, state_dir: str):
        self.path = os.path.join(state_dir, "unfinished_items.json")
        try:
            with open(self.path, encoding="utf-8") as fh:
                self.items: dict[str, str] = json.load(fh)
        except (FileNotFoundError, ValueError):
            self.items = {}

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.items, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def add(self, item_path: str, run_id: str) -> None:
        if item_path not in self.items:
            self.items[item_path] = run_id
            self._save()

    def remove(self, item_path: str) -> None:
        if self.items.pop(item_path, None) is not None:
            self._save()

    def prune(self) -> None:
        gone = [p for p in self.items if _exists(p) is False]
        for p in gone:
            del self.items[p]
        if gone:
            self._save()


@dataclass
class ExecResult:
    done: int = 0
    failed: int = 0
    skipped_items: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    moved_paths: dict[str, set[str]] = field(default_factory=dict)
    vanished: list[str] = field(default_factory=list)  # items deleted from Downloads since the scan: nothing done


def _verify_source(op: Op) -> str | None:
    try:
        st = os.lstat(op.src)
    except FileNotFoundError:
        return "source vanished since the scan"
    if op.expect_tree is not None:
        if not stat.S_ISDIR(st.st_mode):
            if (op.src.rsplit("/", 1)[-1], st.st_size, int(st.st_mtime)) not in op.expect_tree:
                return "source changed since the scan"
            return None
        if tree_fingerprint(op.src) != op.expect_tree:
            return "folder content changed since the scan (still being written?)"
        return None
    if op.expect_size is not None and (
            st.st_size != op.expect_size or (op.expect_mtime and abs(st.st_mtime - op.expect_mtime) > 2)):
        return "source changed since the scan (still being written?)"
    return None


def _same_file_state(path: str, size: int | None, mtime: float | None) -> str | None:
    """None if path is a regular file with this size and (within 2 s) mtime, else what differs."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return "vanished"
    if not stat.S_ISREG(st.st_mode):
        return "is not a regular file"
    if (size is not None and st.st_size != size) or (mtime is not None and abs(st.st_mtime - mtime) > 2):
        return "changed"
    return None


def _verify_unlink(op: Op) -> str | None:
    """A planned deletion still deletes exactly what was planned."""
    what = "empty library file" if op.role == "empty-placeholder" else "file to delete"
    problem = _same_file_state(op.dst, op.expect_size, op.expect_mtime)
    if problem:
        return f"{what} {problem} since the scan"
    if op.role == "identical-duplicate":
        if not op.ref:
            return "no library copy recorded for a deletion"
        if not op.expect_size or op.ref_mtime is None:
            return f"no size or time recorded for the library copy {op.ref}"
        problem = _same_file_state(op.ref, op.expect_size, op.ref_mtime)  # same bytes, so the download's size
        if problem:
            return f"library copy {op.ref} {problem} since the scan"
    elif op.role == "same-duplicate":
        if not op.ref_files:
            return "no library copy recorded for a deletion"
        for path, size, mtime in op.ref_files:  # every library file holding the media, as planned
            if not size or mtime is None:
                return f"no size or time recorded for the library copy {path}"
            problem = _same_file_state(path, size, mtime)
            if problem:
                return f"library copy {path} {problem} since the scan"
    elif op.role != "empty-placeholder" or op.expect_size != 0:
        return f"refusing an unplanned kind of deletion ({op.role})"
    return None


def _verify_item(plan: ItemPlan) -> str | None:
    """A Downloads folder whose content changed since the scan (a late file, a growing file) is not
    touched at all this run, whatever its plan.  Raises StorageUnavailable when the NAS does not answer."""
    item = plan.item
    if not item.is_dir:
        return None  # loose files: every op re-checks its own file
    expected = frozenset((f.rel, f.size, int(f.mtime)) for f in item.files)
    try:
        if tree_fingerprint(item.path) != expected:
            return "folder content changed since the scan (still being written?)"
    except FileNotFoundError:
        return "folder vanished since the scan"
    except OSError as exc:
        if exc.errno in UNREACHABLE:
            raise StorageUnavailable(f"checking {item.path}: {exc}") from exc
        return f"cannot check {item.path}: {exc.strerror or exc}"
    return None


def _verify(op: Op) -> str | None:
    """Check an operation against the scan right before executing it.  Raises StorageUnavailable
    when the storage does not answer."""
    try:
        if op.kind == "rename":
            return _verify_source(op)
        if op.kind == "unlink":
            return _verify_unlink(op)
    except OSError as exc:
        if exc.errno in UNREACHABLE:
            raise StorageUnavailable(f"checking {op.src or op.dst}: {exc}") from exc
        return f"cannot check {op.src or op.dst}: {exc.strerror or exc}"
    return None


def _move(op: Op, journal: Journal, seq: int) -> None:
    if os.path.isdir(op.src) and not os.path.islink(op.src):
        rename_noreplace(op.src, op.dst)
        return
    os.lstat(op.src)  # a source that is gone fails here, before any placeholder exists
    reserve(op.dst)
    journal.write({"seq": seq, "state": "reserved"})
    try:
        os.rename(op.src, op.dst)
    except OSError:
        src = _exists(op.src)
        if src is True:
            _release_placeholder(op.src, op.dst)
        elif src is False and op.expect_size and _empty_file(op.dst):
            # the source vanished between the check and the rename: what is there is our empty reservation
            # (the moved file would have expect_size bytes)
            try:
                os.unlink(op.dst)
            except OSError:
                pass
        raise


def _move_landed(op: Op) -> bool:
    """After a failed rename call: does the target hold what was moved (a server that did the rename
    although the call failed), rather than nothing or our empty reservation?"""
    try:
        st = os.lstat(op.dst)
    except OSError:
        return False
    if stat.S_ISDIR(st.st_mode):
        return True
    if op.expect_size is not None:
        return stat.S_ISREG(st.st_mode) and st.st_size == op.expect_size
    return not (stat.S_ISREG(st.st_mode) and st.st_size == 0)


def execute(plans: list[ItemPlan], journal: Journal, unfinished: Unfinished | None = None) -> ExecResult:
    res = ExecResult()
    renames_done = 0
    created: set[str] = set()  # directories created by this run
    for plan in plans:
        if not plan.ops:
            continue
        if _gone(plan.item.path):
            # deleted since the scan (e.g. removed in Freebox OS together with its files): nothing to do, not
            # an error; checked before any of its operations, so no library folder is created for it either.
            # Unless the folder that held it is gone too: then the share is (no longer) mounted.
            root = os.path.dirname(plan.item.path)
            if _gone(root) or not os.path.isdir(root):
                raise StorageUnavailable(f"{root} is gone (the share is no longer mounted?)")
            journal.write({"state": "item_aborted", "item": plan.item.name, "reason": "vanished since the scan"})
            res.vanished.append(plan.item.name)
            res.skipped_items.append(plan.item.name)
            continue
        problem = _verify_item(plan)
        if problem:
            res.failed += 1
            res.errors.append(f"{plan.item.name}: {problem}: {plan.item.path}")
            journal.write({"state": "item_aborted", "item": plan.item.name, "reason": problem})
            res.skipped_items.append(plan.item.name)
            continue
        item_failed = False
        for op in plan.ops:
            problem = _verify(op)
            if problem:
                res.failed += 1
                res.errors.append(f"{plan.item.name}: {problem}: {op.src or op.dst}")
                journal.write({"state": "item_aborted", "item": plan.item.name, "reason": problem})
                item_failed = True
                break
            touched = op.src if op.kind == "rename" else op.dst if op.kind == "unlink" else None
            if unfinished is not None and touched and (
                    touched == plan.item.path or touched.startswith(plan.item.path + "/")):
                unfinished.add(plan.item.path, journal.run_id)  # before the change: survives a crash
            seq = journal.intent(op, plan.item.name)
            try:
                if op.kind == "mkdir":
                    try:
                        make_dir(op.dst)
                        created.add(op.dst)
                        outcome = "done"
                    except FileExistsError:
                        if op.dst in created:
                            outcome = "exists"
                        else:
                            raise FileExistsError(errno.EEXIST, "folder appeared since planning", op.dst) from None
                elif op.kind == "rename":
                    _move(op, journal, seq)
                    outcome = "done"
                elif op.kind == "rmdir":
                    os.rmdir(op.dst)  # ENOTEMPTY instead of deleting content
                    outcome = "done"
                elif op.kind == "unlink":
                    os.unlink(op.dst)  # verified just above: exactly the planned file
                    outcome = "done"
                else:
                    raise ValueError(f"unknown op {op.kind}")
            except OSError as exc:
                if op.kind == "rename":
                    s, d = _exists(op.src), _exists(op.dst)
                    if s is False and d is True and _move_landed(op):  # e.g. a `soft` mount timeout after the
                        # server did the rename; an empty target is our reservation, not the moved file
                        journal.write({"seq": seq, "state": "done", "note": f"completed despite {exc}"})
                        res.done += 1
                        continue
                    if s is None or d is None or exc.errno in UNREACHABLE:
                        journal.write({"seq": seq, "state": "unknown", "error": str(exc)})
                        raise StorageUnavailable(f"{op.src} -> {op.dst}: {exc}") from exc
                elif op.kind == "unlink":
                    d = _exists(op.dst)
                    if d is False:  # gone: the deletion happened (or someone else did it)
                        journal.write({"seq": seq, "state": "done", "note": f"completed despite {exc}"})
                        res.done += 1
                        continue
                    if d is None or exc.errno in UNREACHABLE:
                        journal.write({"seq": seq, "state": "unknown", "error": str(exc)})
                        raise StorageUnavailable(f"unlink {op.dst}: {exc}") from exc
                elif exc.errno in UNREACHABLE:
                    journal.write({"seq": seq, "state": "unknown", "error": str(exc)})
                    raise StorageUnavailable(f"{op.kind} {op.dst}: {exc}") from exc
                journal.write({"seq": seq, "state": "failed", "error": str(exc)})
                res.failed += 1
                res.errors.append(f"{plan.item.name}: {op.kind} {op.dst}: {exc.strerror or exc}")
                item_failed = True
                break  # stop this item; never clean up an item whose moves failed
            if op.kind == "rename":
                renames_done += 1
                if _CRASH_AFTER and renames_done >= _CRASH_AFTER:
                    os._exit(137)
            journal.write({"seq": seq, "state": outcome})
            res.done += outcome == "done"
            if op.kind == "rename" and op.role == "media":
                for kind, paths in plan.tmm_paths.items():
                    for p in paths:
                        if op.dst.startswith(p + os.sep):
                            res.moved_paths.setdefault(kind, set()).add(p)
        if item_failed:
            res.skipped_items.append(plan.item.name)
        elif unfinished is not None:
            unfinished.remove(plan.item.path)
    return res


# ---------------------------------------------------------------------------- recovery
OPEN_STATES = {"intent", "reserved", "unknown"}


def reconcile(journal_dir: str, dry_run: bool = False) -> tuple[int, list[str]]:
    """Resolve records left open by interrupted runs from the actual disk state.

    Returns (resolved_count, problems).  Problems need a human (or a reachable NAS) and
    are never auto-fixed.  With dry_run nothing is written.
    """
    resolved, problems = 0, []
    if not os.path.isdir(journal_dir):
        return 0, []
    for name in sorted(os.listdir(journal_dir)):
        if not name.endswith(".jsonl"):
            continue
        path = os.path.join(journal_dir, name)
        begin, ops, end = read_journal(path)
        if end is not None and not end.get("needs_reconcile"):
            continue
        updates, file_problems = [], []
        for seq, rec in sorted(ops.items()):
            if rec.get("state") not in OPEN_STATES:
                continue
            kind, src, dst = rec.get("op"), rec.get("src"), rec.get("dst")
            if kind == "rename":
                s, d = _exists(src), _exists(dst)
                if s is None or d is None:
                    file_problems.append(f"{name} seq {seq}: storage not answering for {src} / {dst}")
                elif d and not s:
                    updates.append({"seq": seq, "state": "done", "note": "reconciled: move had completed"})
                elif s and not d:
                    updates.append({"seq": seq, "state": "not_done", "note": "reconciled: move never happened"})
                elif s and d and (rec.get("reserved") or _empty_file(dst)):
                    # our exclusive-create placeholder; without its "reserved" record when the run stopped
                    # between creating it and journaling it (an empty file is never media)
                    if dry_run or _release_placeholder(src, dst):
                        updates.append({"seq": seq, "state": "not_done",
                                        "note": "reconciled: removed our empty placeholder"})
                    else:
                        file_problems.append(f"{name} seq {seq}: {dst} exists and is not an empty placeholder")
                else:
                    file_problems.append(f"{name} seq {seq}: move {src} -> {dst}: source "
                                         f"{'present' if s else 'missing'}, target {'present' if d else 'missing'}")
            elif kind == "mkdir":
                e = _exists(dst)
                if e is None:
                    file_problems.append(f"{name} seq {seq}: storage not answering for {dst}")
                else:
                    updates.append({"seq": seq, "state": "done" if e else "not_done", "note": "reconciled"})
            elif kind in ("rmdir", "unlink"):
                e = _exists(dst)
                if e is None:
                    file_problems.append(f"{name} seq {seq}: storage not answering for {dst}")
                else:
                    updates.append({"seq": seq, "state": "not_done" if e else "done", "note": "reconciled"})
            elif kind == "create":  # undo re-creating an empty placeholder
                e = _exists(dst)
                if e is None:
                    file_problems.append(f"{name} seq {seq}: storage not answering for {dst}")
                else:
                    updates.append({"seq": seq, "state": "done" if e else "not_done", "note": "reconciled"})
        if file_problems:
            problems.extend(file_problems)
            continue
        resolved += len(updates) or 1
        if dry_run:
            continue
        with open(path, "a", encoding="utf-8") as fh:
            for u in updates:
                fh.write(json.dumps({"t": round(time.time(), 3), **u}, ensure_ascii=False) + "\n")
            fh.write(json.dumps({"t": round(time.time(), 3), "state": "end", "status": "interrupted",
                                 "reconciled": len(updates)}) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    return resolved, problems


def undo_plan(journal_path: str) -> tuple[list[Op], list[str]]:
    """Inverse operations for everything a run actually did, newest first, and notes about what
    cannot be reversed (deleted duplicates: a byte-identical one's content is the library copy; one
    deleted by review.delete_duplicates is gone, the library copy holds the same media)."""
    _, ops, _ = read_journal(journal_path)
    inverse, notes = [], []
    for seq in sorted(ops, reverse=True):
        rec = ops[seq]
        if rec.get("state") != "done":
            continue
        if rec["op"] == "rename":
            inverse.append(Op("rename", src=rec["dst"], dst=rec["src"], role="undo", expect_size=rec.get("size")))
        elif rec["op"] == "mkdir":
            inverse.append(Op("rmdir", dst=rec["dst"], role="undo"))
        elif rec["op"] == "rmdir":
            inverse.append(Op("mkdir", dst=rec["dst"], role="undo"))
        elif rec["op"] == "unlink" and rec.get("role") == "empty-placeholder":
            inverse.append(Op("create", dst=rec["dst"], role="undo", expect_size=0, expect_mtime=rec.get("mtime")))
        elif rec["op"] == "unlink" and rec.get("role") == "same-duplicate":
            notes.append(f"not restored: {rec['dst']} was deleted as a duplicate of {rec.get('ref')} "
                         "(review.delete_duplicates)")
        elif rec["op"] == "unlink":
            notes.append(f"not restored: {rec['dst']} was deleted as a byte-identical copy of {rec.get('ref')}")
    return inverse, notes


def undo_ops(journal_path: str) -> list[Op]:
    """Inverse operations for everything a run actually did, newest first."""
    return undo_plan(journal_path)[0]


def recreate_empty(path: str, mtime: float | None) -> None:
    """Undo of an empty-placeholder deletion: an empty file again, never over an existing name."""
    reserve(path)
    if mtime:
        try:
            os.utime(path, (mtime, mtime), follow_symlinks=False)
        except OSError as exc:  # the file is back; its old time is cosmetic
            log.warning("re-created %s but could not restore its time: %s", path, exc)


def run_undo(journal_path: str, apply: bool, journal_dir: str, undo_run_id: str) -> tuple[list[Op], list[str]]:
    """Reverse a run.  Never creates missing parents and never overwrites; stops if the NAS stops answering."""
    inverse = undo_ops(journal_path)
    errors: list[str] = []
    if not apply:
        return inverse, errors
    j = Journal(journal_dir, undo_run_id)
    j.write({"state": "begin", "kind": "undo", "of": os.path.basename(journal_path)})
    status = "ok"
    for op in inverse:
        seq = j.intent(op, "undo")
        try:
            if op.kind == "rename":
                _move(op, j, seq)
            elif op.kind == "mkdir":
                make_dir(op.dst)
            elif op.kind == "create":
                recreate_empty(op.dst, op.expect_mtime)
            else:
                os.rmdir(op.dst)
            j.write({"seq": seq, "state": "done"})
        except OSError as exc:
            if exc.errno in UNREACHABLE or _exists(op.dst) is None:
                j.write({"seq": seq, "state": "unknown", "error": str(exc)})
                errors.append(f"storage stopped answering at {op.dst}: {exc}; undo aborted")
                status = "aborted"
                break
            j.write({"seq": seq, "state": "failed", "error": str(exc)})
            errors.append(f"{op.kind} {op.src + ' -> ' if op.src else ''}{op.dst}: {exc.strerror or exc}")
    j.write({"state": "end", "status": status if not errors or status == "aborted" else "errors",
             "needs_reconcile": status == "aborted"})
    j.close()
    return inverse, errors
