"""Retention of the review folders: review/duplicates/<run-id>/ and review/leftovers/<run-id>/.

A run folder is cleaned once the run that filled it is older than the configured number of days
(review.duplicates_retention_days / review.leftovers_retention_days; 0 = at the end of the same
run, -1 = never; with review.delete_duplicates, certain duplicates are deleted by the next run).
The age comes from the run id in the folder name (UTC start time of that run), not from file times,
which moves do not change.  Only folders whose name is exactly a run id are
considered; anything else in the review folder (your own notes, other folders) is never touched.
Cleaning happens only in --apply runs without --only, after the moves.

What is deleted: only what cannot be media or your own files.
  * duplicates/<run>/: only the files that this very run moved there as *certain* duplicates and
    journaled with "purge": true (the release year matches the library folder, not a REPACK/PROPER,
    not one part of a split file, not larger than the library copy), and their subtitles, as long as
    they still have the size recorded then.  Every other file there is kept;
  * leftovers/<run>/: release clutter only - .nfo/.txt/.sfv/.md5/.url/... and subtitles under 10 MiB,
    images under 1 MiB, and sample/trailer videos under 300 MiB (named so, or in a Sample/Trailers
    folder).
Everything else - any other video, any other file type (archives, programs, documents, files without
extension), anything that cannot be read - is kept and reported, and the folder stays until you
delete it.  Folders left empty are removed.
"""

from __future__ import annotations

import calendar
import errno
import logging
import os
import re
import stat
import time
from dataclasses import dataclass, field

from .config import Config

log = logging.getLogger(__name__)

RUN_DIR = re.compile(r"^(\d{8}T\d{6}Z)-[0-9a-f]{4}$")
KINDS = ("duplicates", "leftovers")
MIB = 1 << 20
JUNK_MAX = 10 * MIB  # release clutter (.nfo, .txt, subtitles ...) is small
IMAGE_MAX = 1 * MIB  # release artwork (poster, screenshots); photos are usually bigger
SMALL_EXTRA = 300 * MIB  # a sample/trailer video smaller than this is disposable
DUPLICATE_ROLES = ("duplicate", "duplicate-sidecar")
JUNK_EXTS = frozenset(
    ".nfo .txt .sfv .md5 .sha1 .sha256 .sha512 .par2 .url .lnk .htm .html .diz .log .xml .json .db .ini".split())
JUNK_NAMES = frozenset({".ds_store", "thumbs.db", "desktop.ini"})
IMAGE_EXTS = frozenset(".jpg .jpeg .png .gif .bmp .webp".split())
# Video containers (any of them, configured or not): such a file is never deleted automatically
# unless it is a journaled duplicate or a small sample/trailer.
VIDEO_EXTS = frozenset(
    ".mkv .mp4 .m4v .avi .mov .wmv .ts .m2ts .mts .webm .flv .mpg .mpeg .vob .ogm .ogv .3gp .divx "
    ".rmvb .rm .asf .f4v .iso".split())
_EXTRA_DIRS = frozenset({"sample", "samples", "trailer", "trailers"})


@dataclass
class Due:
    path: str
    kind: str  # duplicates | leftovers
    days: int  # configured retention
    age_days: float
    delete: list[tuple[str, int]] = field(default_factory=list)  # (path relative to `path`, bytes)
    keep: list[tuple[str, int]] = field(default_factory=list)  # never deleted automatically
    ident: tuple[int, int] | None = None  # (st_dev, st_ino) of the run folder when it was listed

    @property
    def files(self) -> int:
        return len(self.delete)

    @property
    def size(self) -> int:
        return sum(n for _, n in self.delete)

    @property
    def action(self) -> str:
        """'purge' when something is deleted (or only empty folders remain), 'kept' otherwise."""
        return "purge" if self.delete or not self.keep else "kept"

    def fingerprint(self) -> str:
        return "kept|" + "|".join(sorted(r for r, _ in self.keep))


def run_time(name: str) -> float | None:
    """UTC epoch of a run id like 20260115T101500Z-ab12, None if the name is not a run id."""
    m = RUN_DIR.match(name)
    if not m:
        return None
    try:
        return float(calendar.timegm(time.strptime(m.group(1), "%Y%m%dT%H%M%SZ")))
    except ValueError:
        return None


def video_like(name: str, cfg: Config) -> bool:
    ext = os.path.splitext(name)[1].lower()
    return ext in cfg.media.video_extensions or ext in VIDEO_EXTS


def extra_video(rel: str) -> bool:
    """A sample or trailer by its folder ('Item/Sample/x.mkv') or its name ('x-sample.mkv', 'x.Trailer.mp4').
    `rel` is relative to the review run folder: its first part is the download's own name, which does
    not count ('Sample/clip.ts' is a download named Sample, not a sample folder)."""
    from .identify import guess, is_sample
    from .library import is_library_extra

    parts = rel.split("/")
    if any(p.casefold() in _EXTRA_DIRS for p in parts[1:-1]):
        return True
    name = parts[-1]
    g = guess(name)
    others = g.get("other") or []
    others = others if isinstance(others, list) else [others]
    return is_library_extra(name) or is_sample(name, g) or "Trailer" in others


def disposable(kind: str, rel: str, size: int, cfg: Config, journaled: dict[str, int | None],
               full_path: str) -> bool:
    """May the review cleaning delete this file?  `rel` is relative to the review run folder."""
    if kind == "duplicates":
        return full_path in journaled and journaled[full_path] in (None, size)
    name = rel.rsplit("/", 1)[-1]
    ext = os.path.splitext(name)[1].lower()
    if video_like(name, cfg):
        return size < SMALL_EXTRA and extra_video(rel)
    if name.casefold() in JUNK_NAMES or ext in JUNK_EXTS or ext in cfg.media.subtitle_extensions:
        return size < JUNK_MAX
    if ext in IMAGE_EXTS:
        return size < IMAGE_MAX
    return False  # archives, programs, documents, files without extension: yours to decide


def journaled_duplicates(journal_dir: str, run_id: str) -> dict[str, int | None]:
    """{destination path: size} of the files run `run_id` moved to review/duplicates as certain
    duplicates ("purge": true) ({} if its journal is missing or unreadable, or for runs of 1.0.0 and
    of the first 1.0.1 builds, which did not record it: nothing is then deleted)."""
    from .transfer import read_journal

    path = os.path.join(journal_dir, f"{run_id}.jsonl")
    try:
        _, ops, _ = read_journal(path)
    except (OSError, ValueError):
        return {}
    return {r["dst"]: r.get("size") for r in ops.values()
            if r.get("op") == "rename" and r.get("role") in DUPLICATE_ROLES and r.get("state") == "done"
            and r.get("purge") is True and isinstance(r.get("dst"), str)}


def _classify(root: str, kind: str, journaled: dict[str, int | None], cfg: Config
              ) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    delete: list[tuple[str, int]] = []
    keep: list[tuple[str, int]] = []

    def unreadable(exc: OSError) -> None:
        rel = os.path.relpath(exc.filename, root) if exc.filename else "?"
        keep.append((rel + "/ (cannot be listed)", 0))

    for d, dirs, names in os.walk(root, onerror=unreadable, followlinks=False):
        linked = [x for x in dirs if os.path.islink(os.path.join(d, x))]
        for n in sorted(names + linked):
            p = os.path.join(d, n)
            rel = os.path.relpath(p, root)
            try:
                st = os.lstat(p)
            except OSError:
                keep.append((rel, 0))
                continue
            if stat.S_ISLNK(st.st_mode):
                delete.append((rel, 0))  # the link itself (never its target)
            elif not stat.S_ISREG(st.st_mode):
                keep.append((rel, 0))
            elif disposable(kind, rel, st.st_size, cfg, journaled, p):
                delete.append((rel, st.st_size))
            else:
                keep.append((rel, st.st_size))
    return delete, keep


def due(cfg: Config, now: float | None = None) -> list[Due]:
    """Review run folders whose retention has expired, with what would be deleted and kept (read-only)."""
    now = time.time() if now is None else now
    journal_dir = os.path.join(cfg.paths.state_dir, "journal")
    days_for = {"duplicates": cfg.review.duplicates_retention, "leftovers": cfg.review.leftovers_retention_days}
    out: list[Due] = []
    for kind in KINDS:
        days = days_for[kind]
        if days < 0:
            continue
        base = os.path.join(cfg.paths.review, kind)
        try:
            names = sorted(os.listdir(base))
        except FileNotFoundError:
            continue
        for name in names:
            t = run_time(name)
            if t is None:
                continue
            path = os.path.join(base, name)
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(st.st_mode):  # a symlink or a file with a run-id name: not ours
                continue
            age = (now - t) / 86400
            if age < 0 or age < days:  # a run id in the future means a wrong clock: wait
                continue
            journaled = journaled_duplicates(journal_dir, name) if kind == "duplicates" else {}
            delete, keep = _classify(path, kind, journaled, cfg)
            out.append(Due(path, kind, days, age, delete, keep, (st.st_dev, st.st_ino)))
    return out


# Deletions go through directory file descriptors opened without following links, starting from the
# run folder as it was listed: a folder replaced by a link since the listing is refused (ELOOP/ENOTDIR),
# never followed, so nothing outside the review folder can be deleted.
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_LINKED = (errno.ELOOP, errno.ENOTDIR)


def _open_below(root_fd: int, parts: list[str]) -> int:
    """A descriptor of the folder root/parts[0]/parts[1]/..., refusing any link on the way."""
    fd = os.dup(root_fd)
    try:
        for part in parts:
            if part in ("", ".", ".."):
                raise OSError(errno.EINVAL, "unexpected path component", part)
            nfd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = nfd
    except BaseException:
        os.close(fd)
        raise
    return fd


def _remove(name: str, dir_fd: int) -> None:
    os.unlink(name, dir_fd=dir_fd)


def purge(entry: Due) -> tuple[int, int, list[str], list[str]]:
    """Delete the disposable files of one expired review folder, then the folders left empty.
    A file that changed since `due()` listed it is kept, and nothing is reached through a link.
    -> (files deleted, bytes, errors, deleted paths relative to the folder).  Raises
    transfer.StorageUnavailable when the NAS stops answering."""
    from .transfer import UNREACHABLE, StorageUnavailable

    done = size = 0
    errors: list[str] = []
    deleted: list[str] = []
    try:
        root_fd = os.open(entry.path, _DIR_FLAGS)
    except FileNotFoundError:
        return 0, 0, [], []
    except OSError as exc:
        if exc.errno in UNREACHABLE:
            raise StorageUnavailable(f"opening {entry.path}: {exc}") from exc
        if exc.errno in _LINKED:
            return 0, 0, [f"{entry.path}: now a link, not the folder that was listed; left alone"], []
        return 0, 0, [f"{entry.path}: {exc.strerror or exc}"], []
    try:
        rst = os.fstat(root_fd)
        if entry.ident is not None and (rst.st_dev, rst.st_ino) != entry.ident:
            log.warning("%s is not the folder listed at the start of this run; not cleaned in this run", entry.path)
            return 0, 0, [], []
        for rel, n in entry.delete:
            parts = rel.split("/")
            p = os.path.join(entry.path, rel)
            try:
                dfd = _open_below(root_fd, parts[:-1])
                try:
                    st = os.stat(parts[-1], dir_fd=dfd, follow_symlinks=False)
                    if not stat.S_ISLNK(st.st_mode) and not (stat.S_ISREG(st.st_mode) and st.st_size == n):
                        continue  # changed since it was listed: left alone
                    _remove(parts[-1], dfd)  # a link is removed itself, never its target
                finally:
                    os.close(dfd)
                done += 1
                size += n
                deleted.append(rel)
            except FileNotFoundError:
                continue
            except OSError as exc:
                if exc.errno in UNREACHABLE:
                    raise StorageUnavailable(f"deleting {p}: {exc}") from exc
                if exc.errno in _LINKED:
                    errors.append(f"{p}: a folder on its path is now a link; left alone")
                else:
                    errors.append(f"{p}: {exc.strerror or exc}")
        # folders left empty, deepest first; fwalk never descends into a link
        for dirpath, dirnames, _files, dfd in os.fwalk(".", topdown=False, follow_symlinks=False, dir_fd=root_fd):
            for name in dirnames:
                try:
                    os.rmdir(name, dir_fd=dfd)  # only ever succeeds on an empty folder (ENOTDIR on a link)
                except OSError as exc:
                    if exc.errno in UNREACHABLE:
                        raise StorageUnavailable(f"removing {os.path.join(entry.path, dirpath, name)}: {exc}"
                                                 ) from exc
                    if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT, errno.ENOTDIR):
                        errors.append(f"{os.path.normpath(os.path.join(entry.path, dirpath, name))}: "
                                      f"{exc.strerror or exc}")
        _remove_root(entry.path, rst, errors)
    finally:
        os.close(root_fd)
    return done, size, errors, deleted


def _remove_root(path: str, rst: os.stat_result, errors: list[str]) -> None:
    """Remove the run folder itself once empty, only if its name still points to the folder cleaned."""
    from .transfer import UNREACHABLE, StorageUnavailable

    parent, name = os.path.split(path)
    try:
        pfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError as exc:
        if exc.errno in UNREACHABLE:
            raise StorageUnavailable(f"opening {parent}: {exc}") from exc
        return
    try:
        st = os.stat(name, dir_fd=pfd, follow_symlinks=False)
        if (st.st_dev, st.st_ino) == (rst.st_dev, rst.st_ino):
            os.rmdir(name, dir_fd=pfd)
    except OSError as exc:
        if exc.errno in UNREACHABLE:
            raise StorageUnavailable(f"removing {path}: {exc}") from exc
        if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT, errno.ENOTDIR):
            errors.append(f"{path}: {exc.strerror or exc}")
    finally:
        os.close(pfd)


def human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def _list(entries: list[tuple[str, int]], n: int = 4) -> str:
    shown = ", ".join(f"{r!r} ({human(s)})" if s else repr(r) for r, s in entries[:n])
    return shown + (f" (+{len(entries) - n} more)" if len(entries) > n else "")


def describe(entry: Due, base: str) -> str:
    rel = os.path.relpath(entry.path, base) if entry.path.startswith(base + "/") else entry.path
    rule = "at the end of their run" if entry.days == 0 else f"{entry.days} day(s) after their run"
    kept = (f"{len(entry.keep)} file(s) kept for you (never deleted automatically): {_list(entry.keep)}"
            if entry.keep else "")
    if entry.action == "kept":
        return f"{rel}  -- {entry.age_days:.1f} day(s) old; {kept}; delete the folder yourself when done"
    what = (f"deleting {entry.files} file(s), {human(entry.size)}: {_list(entry.delete)}" if entry.delete
            else "removing empty folders")
    return (f"{rel}  -- {what}; {entry.age_days:.1f} day(s) old ({entry.kind} are cleaned {rule})"
            + (f"; {kept}" if kept else ""))
