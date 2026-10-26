"""Read-only scan of the Downloads folder into Items."""

from __future__ import annotations

import logging
import os
import re
import stat

from .config import Config
from .model import FileEntry, Item

log = logging.getLogger(__name__)


def _ignored(name: str, cfg: Config) -> bool:
    if cfg.media.ignore_hidden and name.startswith("."):
        return True
    return name.casefold() in {n.casefold() for n in cfg.media.ignore_names}


# Files never examined by content: video containers keep their configured treatment ('.webm' or '.ts'
# stay IGNORED unless added to media.video_extensions), audio containers ('.mka' is Matroska too) are
# not videos.  Any other file of SNIFF_MIN_BYTES or more is examined: 'Show.S01E04' (no extension),
# 'Movie.2019.1080p.x264.AC3' (a release name ending in a known token).
_NEVER_SNIFFED = frozenset("""
mkv mp4 m4v avi mov wmv ts m2ts mts webm flv mpg mpeg vob ogm ogv 3gp divx rmvb rm asf f4v iso
mka m4a m4b weba oga opus ogg
""".split())
_ARCHIVE_PART = re.compile(r"^\.(?:r\d{2,3}|\d{3})$")  # rar volumes .r00, split archives .001
_ARCHIVES = frozenset(".rar .zip .7z .tar .gz .tgz .bz2 .xz .zst .lz .cab .par2".split())
SNIFF_MIN_BYTES = 1024 * 1024  # a video is never smaller; reading 12 bytes of a bigger file is cheap


def is_archive(name: str) -> bool:
    """An archive or archive volume ('.rar', '.r00', '.001', '.zip', ...)."""
    ext = os.path.splitext(name)[1].lower()
    return ext in _ARCHIVES or bool(_ARCHIVE_PART.match(ext))


def sniff_video(path: str) -> str | None:
    """'.mkv' / '.mp4' / '.avi' when the file starts like a Matroska / MP4 / AVI file, else None."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
    except OSError:
        return None
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return ".mkv"
    if head[4:8] == b"ftyp":
        return ".mp4"
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return ".avi"
    return None


def _file_entry(path: str, rel: str, st: os.stat_result, cfg: Config) -> FileEntry:
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    entry = FileEntry(path=path, rel=rel, name=name, ext=ext, size=st.st_size, mtime=st.st_mtime)
    skip = (ext[1:] in _NEVER_SNIFFED or ext in cfg.media.video_extensions or ext in cfg.media.subtitle_extensions
            or ext in cfg.completeness.temp_extensions)  # a partial download is never a video yet
    if not skip and st.st_size >= SNIFF_MIN_BYTES:
        # A file whose name has no video extension ('Show.S01E04', 'Movie.2020.1080p.x264.AC3'): a video
        # if its content says so; it is filed with the extension added.
        found = sniff_video(path)
        if found and found in cfg.media.video_extensions:
            entry.ext, entry.added_ext = found, found
    return entry


def _valid_name(name: str) -> bool:
    """False for names that are not valid UTF-8 (undecodable bytes -> surrogates). The SMB share
    always returns valid UTF-8; anything else is left untouched and reported."""
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _walk(item: Item, root: str, rel_prefix: str, cfg: Config) -> None:
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError as exc:
        item.problems.append(f"cannot list {root}: {exc.strerror}")
        return
    for e in entries:
        rel = f"{rel_prefix}{e.name}"
        if not _valid_name(e.name):
            item.problems.append(f"name is not valid UTF-8: {rel!a}")
            continue
        try:
            st = e.stat(follow_symlinks=False)
        except OSError as exc:
            item.problems.append(f"cannot stat {rel}: {exc.strerror}")
            continue
        if stat.S_ISLNK(st.st_mode):
            item.problems.append(f"symlink not followed: {rel}")
        elif stat.S_ISDIR(st.st_mode):
            item.dirs.append(e.path)
            item.newest_mtime = max(item.newest_mtime, st.st_mtime)
            _walk(item, e.path, rel + "/", cfg)
        elif stat.S_ISREG(st.st_mode):
            item.files.append(_file_entry(e.path, rel, st, cfg))
            item.newest_mtime = max(item.newest_mtime, st.st_mtime)
        else:
            item.problems.append(f"special file ignored: {rel}")


def scan_downloads(cfg: Config) -> tuple[list[Item], list[tuple[str, str]]]:
    """Return (items, ignored) where ignored is [(name, reason)]."""
    items: list[Item] = []
    ignored: list[tuple[str, str]] = []
    root = cfg.paths.downloads
    review = os.path.normpath(cfg.paths.review)
    for e in sorted(os.scandir(root), key=lambda e: e.name):  # OSError propagates: precondition
        if os.path.normpath(e.path) == review or _ignored(e.name, cfg):
            ignored.append((e.name, "ignored name"))
            continue
        if not _valid_name(e.name):
            ignored.append((ascii(e.name), "name is not valid UTF-8 (left untouched)"))
            continue
        try:
            st = e.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue  # removed while the folder was being listed (e.g. by the Freebox): not an error
        except OSError as exc:
            from .transfer import UNREACHABLE
            if exc.errno in UNREACHABLE:
                raise  # the NAS is not answering: the run stops (precondition)
            ignored.append((e.name, f"cannot be read ({exc.strerror or exc}); left untouched"))
            continue
        if stat.S_ISLNK(st.st_mode):
            ignored.append((e.name, "symlink (never followed)"))
            continue
        if stat.S_ISDIR(st.st_mode):
            item = Item(name=e.name, path=e.path, is_dir=True, newest_mtime=st.st_mtime)
            _walk(item, e.path, "", cfg)
        elif stat.S_ISREG(st.st_mode):
            item = Item(name=e.name, path=e.path, is_dir=False, newest_mtime=st.st_mtime)
            item.files.append(_file_entry(e.path, e.name, st, cfg))
        else:
            ignored.append((e.name, "not a regular file or directory"))
            continue
        items.append(item)
    return attach_loose_sidecars(items, cfg), ignored


def attach_loose_sidecars(items: list[Item], cfg: Config) -> list[Item]:
    """A subtitle lying loose in Downloads next to a loose video ('X.mkv' + 'X.en.srt')
    travels with that video instead of being stranded as an item without video."""
    videos = [i for i in items if not i.is_dir and i.files[0].ext in cfg.media.video_extensions]
    out = []
    for it in items:
        if it.is_dir or it.files[0].ext not in cfg.media.subtitle_extensions:
            out.append(it)
            continue
        # a video recognised by its content has no extension: its whole name is the stem
        stems = [(v.name if v.files[0].added_ext else os.path.splitext(v.name)[0], v) for v in videos]
        cands = [(st, v) for st, v in stems if it.name.casefold().startswith(st.casefold() + ".")]
        if cands:
            longest = max(len(st) for st, _ in cands)
            best = [v for st, v in cands if len(st) == longest]
            if len(best) == 1:
                best[0].files.append(it.files[0])
                best[0].newest_mtime = max(best[0].newest_mtime, it.newest_mtime)
                continue
        out.append(it)
    return out
