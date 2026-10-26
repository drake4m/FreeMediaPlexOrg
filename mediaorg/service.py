"""systemd units, written from the configuration by `sudo mediaorg service install`.

The units carry the folders they may write and the mounts they need, the schedule and a strict
sandbox; they are rewritten whenever a setting they depend on changes.  Local additions belong in
drop-ins (`systemctl edit mediaorg.service`), which are kept.
"""

from __future__ import annotations

import os
import re
import sys

from . import __version__
from .config import DEFAULT_CONFIG, SERVICE_USER, Config, ConfigError
from .exitcodes import EXIT_OK, EXIT_PRECONDITION
from .system import atomic_write, is_root, service_account, systemctl, systemd_running

UNIT_DIR = "/etc/systemd/system"
EXE = "/usr/local/bin/mediaorg"
MARK = "# Managed by mediaorg"
RUN_UNITS = ("mediaorg.service", "mediaorg.timer")
SYNC_UNITS = ("mediaorg-tmm-sync.service", "mediaorg-tmm-sync.timer")
DOC_URL = "https://github.com/drake4m/FreeMediaPlexOrg"

_HARDENING = """\
UMask=0022
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome={protect_home}
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
ProtectProc=invisible
ProcSubset=pid
RestrictSUIDSGID=yes
RestrictRealtime=yes
RestrictNamespaces=yes
LockPersonality=yes
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
CapabilityBoundingSet=
LogRateLimitIntervalSec=30s
LogRateLimitBurst=1000"""


def quote(word: str) -> str:
    """A path for a unit file: specifiers escaped, quoted when it holds spaces or quotes."""
    w = word.replace("%", "%%")
    if any(c.isspace() or c in "\"'\\" for c in w):
        w = '"' + w.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return w


def _literal(path: str) -> str:
    """For settings that take the rest of the line as one path (ConditionPathExists=): no quotes."""
    return path.replace("%", "%%")


def _exec_quote(word: str) -> str:
    return quote(word).replace("$", "$$")


def _under_home(path: str) -> bool:
    return any(path == h or path.startswith(h + "/") for h in ("/home", "/root", "/run/user"))


def _header(config_path: str) -> str:
    return (f"{MARK} {__version__}: written from {config_path} by `mediaorg service install`.\n"
            f"# Do not edit: change the configuration (mediaorg config set ...), then run\n"
            f"# `sudo mediaorg service install` again. Local additions: `sudo systemctl edit <unit>`.\n")


def _timer_spec(minutes: int) -> str:
    if minutes < 60 and 60 % minutes == 0:
        return f"OnCalendar=*:0/{minutes}\nAccuracySec=30s"
    if minutes == 1440:
        return "OnCalendar=*-*-* 00:00:00\nAccuracySec=1min"
    if minutes % 60 == 0 and 24 % (minutes // 60) == 0:
        return f"OnCalendar=0/{minutes // 60}:00:00\nAccuracySec=1min"
    return f"OnBootSec=2min\nOnUnitInactiveSec={minutes}min\nAccuracySec=30s"


def move_root(cfg: Config) -> str:
    """The folder that holds Downloads, both libraries and the review folder: writable as one, so that a move
    between them is a rename within one mount of the service's sandbox."""
    p = cfg.paths
    root = os.path.commonpath([p.downloads, p.movies, p.series, p.review])
    if root == "/":
        raise ConfigError("the downloads, movie, TV and review folders have no common folder other than /: the "
                          "systemd sandbox lets the service move files only inside one folder; put them in one "
                          "folder, for example /srv/media/downloads, /srv/media/movies and /srv/media/tv")
    return root


def render_units(cfg: Config, config_path: str, exe: str = EXE) -> dict[str, str]:
    """Unit name -> content.  The nightly tMM units only when tMM is enabled and a time is set."""
    if any(ord(c) < 32 or ord(c) == 127 for c in config_path):
        raise ConfigError(f"the configuration path {config_path!r} contains control characters")
    p = cfg.paths
    media = [p.downloads, p.movies, p.series]
    # Every move must stay inside ONE writable folder: systemd makes each ReadWritePaths= entry a bind mount
    # of its own, and a rename from one mount to another fails with EXDEV, even on one filesystem.
    moves = move_root(cfg)
    rw = [p.state_dir, f"-{moves}"]
    log_dir = os.path.dirname(cfg.logging.file) if cfg.logging.file else ""
    if log_dir:
        rw.append(f"-{log_dir}")
    mounts = [cfg.mount.mountpoint] if cfg.mount is not None else media
    every = [config_path, p.state_dir, *media, p.review, cfg.tmm.api_key_file, log_dir]
    protect_home = "read-only" if any(_under_home(x) for x in every if x) else "yes"
    hardening = _HARDENING.format(protect_home=protect_home)
    head = _header(config_path)
    cfgq = _exec_quote(config_path)
    units = {
        "mediaorg.service": f"""{head}[Unit]
Description=mediaorg: file completed downloads into the movie and TV library
Documentation={DOC_URL}
Wants=network-online.target
After=network-online.target
RequiresMountsFor={' '.join(quote(m) for m in mounts)}
ConditionPathExists={_literal(config_path)}

[Service]
Type=oneshot
User={SERVICE_USER}
Group={SERVICE_USER}
ExecStart={_exec_quote(exe)} --config {cfgq} run --apply --quiet
# exit 4: another run (a manual one) holds the lock - not a failure
SuccessExitStatus=4
TimeoutStartSec=1h
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
ReadWritePaths={' '.join(quote(x) for x in rw)}
{hardening}
""",
        "mediaorg.timer": f"""{head}[Unit]
Description=Run mediaorg every {cfg.schedule.run_every_minutes} minute(s)
Documentation={DOC_URL}

[Timer]
{_timer_spec(cfg.schedule.run_every_minutes)}
# A run missed while the machine was off runs at the next boot (fixed times; otherwise OnBootSec does it).
Persistent=true

[Install]
WantedBy=timers.target
""",
    }
    if cfg.tmm.enabled and cfg.schedule.tmm_sync_time:
        sync_rw = f"ReadWritePaths={quote('-' + log_dir)}\n" if log_dir else ""
        units["mediaorg-tmm-sync.service"] = f"""{head}[Unit]
Description=mediaorg: nightly tinyMediaManager update, scrape and rename of new items
Documentation={DOC_URL}
Wants=network-online.target
After=network-online.target tinymediamanager.service
ConditionPathExists={_literal(config_path)}
# tMM may still be starting after a boot: retry up to 3 times, 5 minutes apart
StartLimitIntervalSec=1h
StartLimitBurst=4

[Service]
Type=oneshot
Restart=on-failure
RestartSec=5min
User={SERVICE_USER}
Group={SERVICE_USER}
ExecStart={_exec_quote(exe)} --config {cfgq} tmm sync --apply
TimeoutStartSec=5min
{sync_rw}{hardening}
"""
        units["mediaorg-tmm-sync.timer"] = f"""{head}[Unit]
Description=Nightly tinyMediaManager sync by mediaorg
Documentation={DOC_URL}

[Timer]
OnCalendar=*-*-* {cfg.schedule.tmm_sync_time}:00
RandomizedDelaySec=10min
# Caught up after the machine was off at that time.
Persistent=true

[Install]
WantedBy=timers.target
"""
    return units


def _installed(unit_dir: str, name: str) -> str | None:
    try:
        with open(os.path.join(unit_dir, name), encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


_WRITTEN_FROM = re.compile(r"^# Managed by mediaorg \S+: written from (.*) by `mediaorg service install`\.$", re.M)


def installed_config(unit_dir: str | None = None) -> str | None:
    """The configuration file the installed units were written from (None: no units).  Units of 1.x
    carry no header and always used the default configuration file."""
    text = _installed(unit_dir or UNIT_DIR, "mediaorg.service")
    if text is None:
        return None
    m = _WRITTEN_FROM.search(text)
    return m.group(1) if m else DEFAULT_CONFIG


_SCHEDULE_KEYS = ("OnCalendar=", "OnBootSec=", "OnUnitInactiveSec=", "OnUnitActiveSec=", "OnActiveSec=")


def dropins(unit_dir: str | None = None) -> tuple[list[str], list[str]]:
    """Local drop-in files of the mediaorg units: (timer drop-ins that change the schedule, the others).
    A schedule set in a drop-in (as 1.x documented) overrides schedule.* without notice."""
    unit_dir = unit_dir or UNIT_DIR
    schedule, other = [], []
    for name in (*RUN_UNITS, *SYNC_UNITS):
        folder = os.path.join(unit_dir, name + ".d")
        try:
            files = sorted(f for f in os.listdir(folder) if f.endswith(".conf"))
        except OSError:
            continue
        for f in files:
            path = os.path.join(folder, f)
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                text = ""
            sets_schedule = name.endswith(".timer") and any(
                line.strip().startswith(_SCHEDULE_KEYS) for line in text.splitlines())
            (schedule if sets_schedule else other).append(path)
    return schedule, other


def units_up_to_date(cfg: Config, config_path: str, unit_dir: str | None = None) -> tuple[bool, list[str]]:
    """(installed at all, names of installed units that differ from what the configuration gives)."""
    unit_dir = unit_dir or UNIT_DIR
    wanted = render_units(cfg, config_path)
    present = {n: _installed(unit_dir, n) for n in (*RUN_UNITS, *SYNC_UNITS)}
    if present["mediaorg.service"] is None:
        return False, []
    stale = [n for n, text in wanted.items() if present[n] != text]
    stale += [n for n in SYNC_UNITS if n not in wanted and present[n] is not None]
    return True, stale


def ensure_folders(cfg: Config) -> list[str]:
    """Create the state and review folders when missing, for the service account (root only): the sandbox
    lets the service write only folders that exist when it starts."""
    if not is_root():
        return []
    ids = service_account()
    lines = []
    for path, mode in ((cfg.paths.state_dir, 0o750), (cfg.paths.review, None)):
        parent = os.path.dirname(path)
        if os.path.lexists(path) or not os.path.isdir(parent):
            continue
        os.mkdir(path, 0o700)
        os.chmod(path, mode if mode is not None else os.stat(parent).st_mode & 0o7777)  # review: like its parent
        if ids:
            try:
                os.chown(path, *ids)
            except OSError:  # a network share may decide the owner itself
                pass
        lines.append(f"created {path}")
    return lines


def install(cfg: Config, config_path: str, unit_dir: str | None = None, enable: bool = False) -> list[str]:
    """Write the units (only those that changed), reload systemd, optionally enable the timers."""
    real = unit_dir is None
    unit_dir = unit_dir or UNIT_DIR
    wanted = render_units(cfg, config_path)
    lines = list(ensure_folders(cfg)) if real else []
    added_timers = []  # timers that did not exist before (the nightly tMM sync, once tMM is enabled)
    for name, text in wanted.items():
        old = _installed(unit_dir, name)
        if old != text:
            atomic_write(os.path.join(unit_dir, name), text.encode("utf-8"), 0o644, 0 if is_root() else None,
                         0 if is_root() else None)
            lines.append(f"wrote {os.path.join(unit_dir, name)}")
            if old is None and name.endswith(".timer") and name != "mediaorg.timer":
                added_timers.append(name)
    for name in SYNC_UNITS:  # tMM sync no longer wanted
        if name not in wanted and _installed(unit_dir, name) is not None:
            if name.endswith(".timer") and systemd_running() and real:
                systemctl("disable", "--now", name)
            os.unlink(os.path.join(unit_dir, name))
            lines.append(f"removed {os.path.join(unit_dir, name)} (tMM sync not configured)")
    if systemd_running() and real:
        systemctl("daemon-reload", check=True)
        if enable:
            timers = [n for n in wanted if n.endswith(".timer")]
            r = systemctl("enable", "--now", *timers)
            if r.returncode != 0:
                raise OSError(f"systemctl enable failed: {r.stderr.strip()}")
            lines.append(f"enabled and started: {', '.join(timers)}")
        elif added_timers and systemctl("is-enabled", "mediaorg.timer").stdout.strip() == "enabled":
            # a timer added by a configuration change follows the main timer: enabled when it is
            r = systemctl("enable", "--now", *added_timers)
            if r.returncode != 0:
                raise OSError(f"systemctl enable failed: {r.stderr.strip()}")
            lines.append(f"enabled and started: {', '.join(added_timers)} (as mediaorg.timer is enabled)")
    return lines


def refresh_if_installed(cfg: Config, config_path: str) -> str | None:
    """After a configuration change: rewrite the units if they are installed (as root), else say how."""
    installed, stale = units_up_to_date(cfg, config_path)
    if not installed or not stale:
        return None
    owner = installed_config()
    if owner != config_path:  # the timers run another configuration: never take them over silently
        return (f"(the installed systemd units run {owner}, not {config_path}: left unchanged; "
                f"`sudo mediaorg --config {config_path} service install` would switch them)")
    if not is_root():
        return "the systemd units no longer match the configuration: run `sudo mediaorg service install`"
    for line in install(cfg, config_path):
        print(line)
    return "systemd units updated"


def remove(unit_dir: str | None = None) -> list[str]:
    real = unit_dir is None
    unit_dir = unit_dir or UNIT_DIR
    lines = []
    if systemd_running() and real:
        for timer in ("mediaorg.timer", "mediaorg-tmm-sync.timer"):
            if _installed(unit_dir, timer) is not None:
                systemctl("disable", "--now", timer)
    for name in (*RUN_UNITS, *SYNC_UNITS):
        path = os.path.join(unit_dir, name)
        if os.path.exists(path):
            os.unlink(path)
            lines.append(f"removed {path}")
    if systemd_running() and real:
        systemctl("daemon-reload")
    return lines


def status_lines() -> list[str]:
    if not systemd_running():
        return ["systemd is not running here: no timers"]
    out = []
    for name in (*RUN_UNITS, *SYNC_UNITS):
        r = systemctl("show", name, "-p", "LoadState,UnitFileState,ActiveState,Result,NextElapseUSecRealtime,"
                                         "LastTriggerUSec,ExecMainExitTimestamp,ExecMainStatus")
        props = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
        if props.get("LoadState") == "not-found":
            out.append(f"{name:<28} not installed")
            continue
        extra = []
        if name.endswith(".timer"):
            if props.get("NextElapseUSecRealtime"):
                extra.append(f"next {props['NextElapseUSecRealtime']}")
            if props.get("LastTriggerUSec") and props["LastTriggerUSec"] not in ("n/a", "0"):
                extra.append(f"last {props['LastTriggerUSec']}")
        elif props.get("ExecMainExitTimestamp"):
            extra.append(f"last run ended {props['ExecMainExitTimestamp']} "
                         f"(exit {props.get('ExecMainStatus', '?')}, {props.get('Result', '?')})")
        out.append(f"{name:<28} {props.get('UnitFileState', '?'):<9} {props.get('ActiveState', '?'):<9} "
                   + "; ".join(extra))
    return out


# ------------------------------------------------------------------------------- command
def cmd_service(cfg_loader, args) -> int:
    sub = args.service_cmd
    if sub == "status":
        print("\n".join(status_lines()))
        return EXIT_OK
    if sub == "remove":
        if not is_root():
            print("run it with sudo", file=sys.stderr)
            return EXIT_PRECONDITION
        lines = remove()
        print("\n".join(lines) if lines else "no mediaorg units installed")
        return EXIT_OK
    cfg, path = cfg_loader()
    if sub == "show":
        for name, text in render_units(cfg, path).items():
            print(f"### {name}\n{text}")
        return EXIT_OK
    # install
    if not is_root():
        print("run it with sudo", file=sys.stderr)
        return EXIT_PRECONDITION
    if not systemd_running():
        print("systemd is not running on this machine: nothing installed (use `mediaorg service show` to see "
              "the units)", file=sys.stderr)
        return EXIT_PRECONDITION
    lines = install(cfg, path, enable=args.enable)
    print("\n".join(lines) if lines else "units already up to date")
    print("\n".join(status_lines()))
    if not args.enable:
        r = systemctl("is-enabled", "mediaorg.timer")
        if r.stdout.strip() != "enabled":
            print("timers not enabled yet; after a successful dry-run: sudo mediaorg service install --enable")
    return EXIT_OK
