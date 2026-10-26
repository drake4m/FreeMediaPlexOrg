"""Preconditions: the expected storage is mounted, the paths are usable, one run at a time."""

from __future__ import annotations

import errno
import fcntl
import os
import re
from dataclasses import dataclass

from .config import Config


class PreconditionError(Exception):
    pass


class LockBusy(Exception):
    pass


_OCTAL = re.compile(r"\\([0-7]{3})")


def _unescape(s: str) -> str:
    return _OCTAL.sub(lambda m: chr(int(m.group(1), 8)), s)


@dataclass(frozen=True)
class MountEntry:
    mountpoint: str
    fstype: str
    source: str
    options: str


def read_mountinfo(path: str = "/proc/self/mountinfo") -> list[MountEntry]:
    out = []
    with open(path, encoding="utf-8", errors="surrogateescape") as fh:
        for line in fh:
            left, sep, right = line.rstrip("\n").partition(" - ")
            if not sep:
                continue
            lf, rf = left.split(), right.split()
            if len(lf) < 6 or len(rf) < 2:
                continue
            out.append(MountEntry(_unescape(lf[4]), rf[0], _unescape(rf[1]), rf[2] if len(rf) > 2 else ""))
    return out


def check_mount(cfg: Config, mountinfo_path: str = "/proc/self/mountinfo") -> MountEntry | None:
    """The configured mountpoint must be the expected share or disk, right now.  None when the
    configuration has no [mount] check."""
    if cfg.mount is None:
        return None
    mp = cfg.mount.mountpoint
    try:
        os.stat(mp)  # also triggers an automount if one is configured
    except OSError as exc:
        raise PreconditionError(f"cannot access {mp}: {exc.strerror}") from exc
    entries = [e for e in read_mountinfo(mountinfo_path) if e.mountpoint == mp]
    if not entries:
        raise PreconditionError(f"{mp} is not a mount point (NAS not mounted?) - refusing to touch it")
    top = entries[-1]  # the last mount on a path is the visible one
    if top.fstype != cfg.mount.fstype:
        raise PreconditionError(f"{mp} is mounted as {top.fstype!r}, expected {cfg.mount.fstype!r}")
    if top.source.rstrip("/").casefold() != cfg.mount.source.casefold():
        raise PreconditionError(f"{mp} comes from {top.source!r}, expected {cfg.mount.source!r}")
    return top


def check_paths(cfg: Config) -> None:
    """All media paths exist, are real directories on one filesystem (the mount's, when one is
    configured), and are writable by this user."""
    ref = cfg.mount.mountpoint if cfg.mount is not None else cfg.paths.downloads
    if not os.path.isdir(ref):
        raise PreconditionError(f"{ref} is missing or not a directory")
    mp_dev = os.stat(ref).st_dev
    for key in ("downloads", "movies", "series"):
        p = getattr(cfg.paths, key)
        if os.path.islink(p) or not os.path.isdir(p):
            raise PreconditionError(f"paths.{key} {p} is missing or not a real directory")
        if os.path.realpath(p) != os.path.normpath(p):
            raise PreconditionError(f"paths.{key} {p} resolves elsewhere ({os.path.realpath(p)})")
        if os.stat(p).st_dev != mp_dev:
            raise PreconditionError(f"paths.{key} {p} is on a different filesystem than {ref}: files are moved "
                                    f"with renames, so Downloads and the library must share one filesystem")
        if not os.access(p, os.R_OK | os.W_OK | os.X_OK):
            raise PreconditionError(f"no read/write access to {p} for uid {os.getuid()}")
    review_parent = cfg.paths.review
    while not os.path.isdir(review_parent):
        review_parent = os.path.dirname(review_parent)
    if os.stat(review_parent).st_dev != mp_dev:
        raise PreconditionError("paths.review must be on the same filesystem as the library")
    os.makedirs(cfg.paths.state_dir, exist_ok=True)
    if not os.access(cfg.paths.state_dir, os.W_OK):
        raise PreconditionError(f"state_dir {cfg.paths.state_dir} is not writable")


class RunLock:
    """Exclusive, non-blocking flock; released automatically if the process dies."""

    def __init__(self, path: str):
        self.path = path
        self.fd: int | None = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o640)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self.fd)
            self.fd = None
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                raise LockBusy(f"another mediaorg run holds {self.path}") from exc
            raise
        os.ftruncate(self.fd, 0)
        os.write(self.fd, f"{os.getpid()}\n".encode())
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None
