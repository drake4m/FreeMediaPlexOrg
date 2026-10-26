"""Privileges and safe file writing.

Media commands run as the unprivileged service account: started with sudo, mediaorg becomes that
account for good before it reads the configuration or touches a file.  Setup commands (config, keys,
systemd units) stay root, and write their files atomically with explicit owner and mode.
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import stat
import subprocess
import tempfile

from .config import SERVICE_USER


class PrivilegeError(Exception):
    pass


def is_root() -> bool:
    return os.geteuid() == 0


def service_account(user: str = SERVICE_USER) -> tuple[int, int] | None:
    """(uid, gid) of the service account, or None when it does not exist."""
    try:
        pw = pwd.getpwnam(user)
    except KeyError:
        return None
    return pw.pw_uid, pw.pw_gid


def drop_to_service_user(user: str = SERVICE_USER) -> None:
    """Called as root before a media command: become the service account, irreversibly."""
    try:
        pw = pwd.getpwnam(user)
    except KeyError:
        raise PrivilegeError(f"the service account {user!r} does not exist; install mediaorg with install.sh "
                             f"first") from None
    if pw.pw_uid == 0:
        raise PrivilegeError(f"the service account {user!r} must not be root")
    os.setgroups([])
    os.initgroups(user, pw.pw_gid)
    os.setgid(pw.pw_gid)
    os.setuid(pw.pw_uid)
    if os.getuid() == 0 or os.geteuid() == 0:
        raise PrivilegeError("could not drop root privileges")
    os.environ.update(HOME=pw.pw_dir or "/", USER=user, LOGNAME=user)
    os.umask(0o022)  # state files get explicit private modes; new media folders copy their parent's
    os.chdir("/")


def access_as(user: str, checks: list[tuple[str, int]]) -> list[str] | None:
    """As root: the paths among `checks` [(path, os.R_OK | ...)] that `user` cannot access, tested in a
    child process running as that user.  None when the account does not exist or we are not root."""
    ids = service_account(user)
    if ids is None or not is_root():
        return None
    uid, gid = ids
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:  # child: never returns
        code = 0
        try:
            os.close(r)
            os.setgroups([])
            os.initgroups(user, gid)
            os.setgid(gid)
            os.setuid(uid)
            bad = [p for p, mode in checks if not os.access(p, mode)]
            os.write(w, json.dumps(bad).encode())
        except BaseException:
            code = 1
        finally:
            os._exit(code)
    os.close(w)
    chunks = []
    while True:
        b = os.read(r, 65536)
        if not b:
            break
        chunks.append(b)
    os.close(r)
    _, status = os.waitpid(pid, 0)
    if status != 0:
        return [p for p, _ in checks]
    return json.loads(b"".join(chunks) or b"[]")


def atomic_write(path: str, data: bytes, mode: int, uid: int | None = None, gid: int | None = None,
                 backup: str | None = None) -> str | None:
    """Write `path` in one step (temporary file in the same folder, fsync, rename): readers see the old
    or the new content, never a mix.  `backup`: also keep the previous file under this name.
    Returns the backup path when one was written."""
    folder = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=folder)
    try:
        os.fchmod(fd, mode)
        if uid is not None or gid is not None:
            os.fchown(fd, -1 if uid is None else uid, -1 if gid is None else gid)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        saved = _keep_backup(path, backup) if backup else None
        os.replace(tmp, path)
        dfd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
        return saved
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _keep_backup(path: str, backup: str) -> str | None:
    """Copy the current regular file `path` to `backup` with its mode and owner.  The copy goes to a new
    temporary file that is then renamed over `backup`: whatever has that name (a link planted there
    included) is replaced, never written through."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    folder = os.path.dirname(os.path.abspath(backup))
    bfd, btmp = tempfile.mkstemp(prefix=f".{os.path.basename(backup)}.", suffix=".tmp", dir=folder)
    try:
        os.fchmod(bfd, stat.S_IMODE(st.st_mode))
        try:
            os.fchown(bfd, st.st_uid, st.st_gid)
        except PermissionError:
            pass
        with os.fdopen(bfd, "wb") as out, open(path, "rb") as src:
            shutil.copyfileobj(src, out)
            out.flush()
            os.fsync(out.fileno())
        os.utime(btmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        os.replace(btmp, backup)
    except BaseException:
        try:
            os.unlink(btmp)
        except FileNotFoundError:
            pass
        raise
    return backup


def systemd_running() -> bool:
    return os.path.isdir("/run/systemd/system")


def systemctl(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    exe = shutil.which("systemctl") or "/usr/bin/systemctl"
    return subprocess.run([exe, *args], capture_output=True, text=True, check=check)
