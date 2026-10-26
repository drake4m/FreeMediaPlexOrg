"""`mediaorg config ...` and `mediaorg tmm set-key`: create, change and inspect the configuration.

Every change is validated as a whole before anything is written; the file is replaced in one step,
the previous version is kept as <file>.bak, and the result is readable by the service account only
(mode 0640, owner root:mediaorg).  Secrets never go through the command line: `tmm set-key` reads the
key from standard input or a prompt.
"""

from __future__ import annotations

import copy
import getpass
import json
import logging
import os
import re
import sys
import textwrap
import tomllib
import uuid

from . import __version__
from .config import DEFAULT_CONFIG, DEFAULT_STATE_DIR, SERVICE_USER, ConfigError, from_dict, load, read_raw
from .exitcodes import EXIT_OK, EXIT_PRECONDITION, EXIT_USAGE
from .schema import KEYS, describe, effective, find_key, parse_value, render_file, render_plain, toml_value
from .system import access_as, atomic_write, is_root, service_account

log = logging.getLogger("mediaorg")

# keys whose change needs new systemd units (`mediaorg service install` runs again by itself)
UNIT_KEYS = re.compile(r"^(paths\.|mount\.|schedule\.|logging\.file$|tmm\.enabled$)")


def config_path(args) -> str:
    return os.path.abspath(getattr(args, "config", None) or os.environ.get("MEDIAORG_CONFIG") or DEFAULT_CONFIG)


# ------------------------------------------------------------------------------- writing
def _validated(path: str, raw: dict) -> tuple[str, object]:
    """(file text, Config) for `raw`, validated as a whole; what is written is what was validated."""
    text = render_file(raw, __version__)
    return text, from_dict(tomllib.loads(text), source_file=path)


def _same_settings(a: dict, b: dict) -> bool:
    """True when two parsed files hold the same settings (empty sections do not count)."""
    def norm(d: dict) -> dict:
        return {k: v for k, v in d.items() if v != {}}
    return norm(a) == norm(b)


def write_config(path: str, raw: dict) -> tuple[str | None, object]:
    """Validate `raw` as a whole, then write it with its documentation.  -> (backup path, Config)"""
    text, cfg = _validated(path, raw)
    from .service import installed_config, render_units
    if installed_config() == os.path.abspath(path):
        render_units(cfg, os.path.abspath(path))  # the installed units must still be possible: refused before writing
    folder = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(folder):
        if not is_root():
            raise ConfigError(f"{folder} does not exist; create it, or run with sudo")
        os.makedirs(folder, mode=0o750)
    uid = gid = None
    if is_root():
        ids = service_account()
        uid, gid = 0, (ids[1] if ids else 0)
        if os.path.dirname(path) == os.path.dirname(DEFAULT_CONFIG):
            os.chown(folder, 0, gid)
            os.chmod(folder, 0o750)  # noqa: S103 - root:mediaorg, the service account must read the file
    try:
        backup = atomic_write(path, text.encode("utf-8"), 0o640, uid, gid, backup=path + ".bak")
    except PermissionError as exc:
        raise ConfigError(f"cannot write {path}: permission denied (run with sudo)") from exc
    return backup, cfg


def _read_existing(path: str) -> dict:
    _, raw = read_raw(path)
    return raw


def _show(v) -> str:
    return toml_value(v, 0).replace("\n", " ") if not isinstance(v, str) else repr(v)


def _refresh_units(cfg, path: str, changed_keys: list[str]) -> None:
    if not any(UNIT_KEYS.match(k) for k in changed_keys):
        return
    from .service import refresh_if_installed
    msg = refresh_if_installed(cfg, path)
    if msg:
        print(msg)


# ------------------------------------------------------------------------------- set / unset
def parse_assignments(items: list[str]) -> list[tuple[str, str]]:
    """`KEY=VALUE [KEY=VALUE ...]` or `KEY VALUE`."""
    if not items:
        raise ConfigError("nothing to set; example: mediaorg config set paths.movies=/srv/media/movies")
    if "=" not in items[0]:
        if len(items) != 2:
            raise ConfigError("write KEY=VALUE [KEY=VALUE ...] or KEY VALUE (quote values that contain spaces)")
        return [(items[0], items[1])]
    pairs = []
    for it in items:
        key, sep, value = it.partition("=")
        if not sep or not key:
            raise ConfigError(f"{it!r} is not KEY=VALUE")
        pairs.append((key.strip(), value))
    return pairs


def cmd_set(args) -> int:
    path = config_path(args)
    raw = _read_existing(path)
    before = copy.deepcopy(raw)
    changes, keys = [], []
    for dotted, text in parse_assignments(args.assignments):
        key = find_key(dotted)
        body = raw.setdefault(key.section, {})
        old = body.get(key.name, None)
        if key.kind == "path" and not text.strip():
            value = ""
            body.pop(key.name, None)  # empty path = back to its default
        else:
            value = parse_value(key, text)
            body[key.name] = value
        if key.dotted == "review.tmm_trash_retention_minutes":
            body.pop("tmm_trash_retention_days", None)
        if key.dotted == "review.tmm_trash_retention_days":
            body.pop("tmm_trash_retention_minutes", None)
        changes.append(f"{key.dotted}: {_show(old) if old is not None else '(default)'} -> {_show(value)}")
        keys.append(key.dotted)
    if _same_settings(raw, before):  # rewriting would replace the .bak with a copy of this very file
        _validated(path, raw)
        print("\n".join(changes) + f"\nnothing changed: {path} left as it is")
        return EXIT_OK
    backup, cfg = write_config(path, raw)
    for c in changes:
        print(c)
    print(f"saved {path}" + (f" (previous version: {backup})" if backup else ""))
    if "paths.state_dir" in keys:
        _prepare_state_dir(cfg.paths.state_dir)
    _refresh_units(cfg, path, keys)
    return EXIT_OK


def cmd_unset(args) -> int:
    path = config_path(args)
    raw = _read_existing(path)
    before = copy.deepcopy(raw)
    for dotted in args.keys:
        key = find_key(dotted)
        body = raw.get(key.section) or {}
        if key.name not in body:
            print(f"{key.dotted}: not set (already the default)")
            continue
        del body[key.name]
        if not body:
            raw.pop(key.section, None)
        print(f"{key.dotted}: back to its default")
    if _same_settings(raw, before):
        print(f"nothing changed: {path} left as it is")
        return EXIT_OK
    backup, cfg = write_config(path, raw)
    print(f"saved {path}" + (f" (previous version: {backup})" if backup else ""))
    _refresh_units(cfg, path, list(args.keys))
    return EXIT_OK


# ------------------------------------------------------------------------------- read-only commands
def cmd_get(args) -> int:
    cfg = load(config_path(args))
    key = find_key(args.key)
    data = effective(cfg)
    if key.section not in data:
        print("")  # e.g. [mount] not configured
        return EXIT_OK
    v = data[key.section].get(key.name)
    print(v if isinstance(v, str) else ("" if v is None else toml_value(v, 0)))
    return EXIT_OK


def cmd_show(args) -> int:
    path = config_path(args)
    if args.file:
        with open(path, encoding="utf-8") as fh:
            sys.stdout.write(fh.read())
        return EXIT_OK
    cfg = load(path)
    print(f"# configuration in force (defaults included), from {path}")
    print(render_plain(effective(cfg)))
    return EXIT_OK


def cmd_validate(args) -> int:
    path = config_path(args)
    cfg = load(path)
    print(f"{path}: valid (mode {cfg.completeness.method}, downloads {cfg.paths.downloads})")
    return EXIT_OK


def cmd_path(args) -> int:
    print(config_path(args))
    return EXIT_OK


def cmd_keys(args) -> int:
    pattern = (args.pattern or "").lower()
    width = 100
    for key in KEYS:
        if key.advanced and not args.all and not pattern:
            continue
        if pattern and pattern not in key.dotted.lower():
            continue
        print(describe(key))
        doc = key.doc + (f" Example: {toml_value(key.example, 0)}" if key.example is not None else "")
        print(textwrap.indent(textwrap.fill(doc, width - 4), "    "))
    if not args.all and not pattern:
        print("\n(advanced keys hidden; `mediaorg config keys --all` lists every key)")
    return EXIT_OK


# ------------------------------------------------------------------------------- init
def detect_mount(path: str, mountinfo: str = "/proc/self/mountinfo"):
    """The mount holding `path` when it is not the root filesystem, else None."""
    from .guards import read_mountinfo

    os.stat(path)  # triggers an automount first
    real = os.path.realpath(path)
    best = None
    for e in read_mountinfo(mountinfo):
        if real == e.mountpoint or real.startswith(e.mountpoint.rstrip("/") + "/"):
            if best is None or len(e.mountpoint) >= len(best.mountpoint):
                best = e
    if best is None or best.mountpoint == "/":
        return None
    return best


def _ask(prompt: str, default: str | None = None, check=None) -> str:
    while True:
        shown = f" [{default}]" if default else ""
        try:
            answer = input(f"{prompt}{shown}: ").strip()
        except EOFError:
            raise ConfigError("no answer (end of input)") from None
        answer = answer or (default or "")
        if not answer:
            print("  a value is needed")
            continue
        if check is not None:
            err = check(answer)
            if err:
                print(f"  {err}")
                continue
        return answer


def _folder_problem(p: str) -> str | None:
    if not os.path.isabs(p):
        return "give an absolute path (starting with /)"
    if not os.path.isdir(p):
        return f"{p} does not exist or is not a folder (create it first)"
    if os.path.realpath(p) != os.path.normpath(p):
        return f"{p} is (or goes through) a symbolic link to {os.path.realpath(p)}; give the real path"
    return None


def cmd_init(args) -> int:
    path = config_path(args)
    if os.path.exists(path) and not args.force:
        print(f"{path} exists already. Change it with `mediaorg config set KEY=VALUE`, or start over with "
              f"`mediaorg config init --force ...` (the old file is kept as {path}.bak).", file=sys.stderr)
        return EXIT_USAGE
    interactive = sys.stdin.isatty() and not args.non_interactive
    folders = {}
    labels = {"downloads": "Downloads folder (where completed downloads appear)",
              "movies": "Movie library folder", "series": "TV library folder"}
    missing = []
    for name, label in labels.items():
        value = getattr(args, name)
        if not value and interactive:
            value = _ask(label, check=_folder_problem)
        if not value:
            missing.append(f"--{name}")
            continue
        err = _folder_problem(value)
        if err:
            raise ConfigError(f"--{name}: {err}")
        folders[name] = os.path.normpath(value)
    if missing:
        raise ConfigError(f"missing {', '.join(missing)} (or run it in a terminal to be asked)")

    devs = {name: os.stat(p).st_dev for name, p in folders.items()}
    if len(set(devs.values())) != 1:
        raise ConfigError("Downloads, movies and TV library must be on the same filesystem (files are moved with "
                          "renames, never copied): " + ", ".join(f"{n} {folders[n]}" for n in folders))

    mode = args.mode
    if mode is None and interactive:
        mode = _ask("How are finished downloads recognised: 'watch' (files unchanged for a while; any download "
                    "client) or 'freebox' (the Freebox download manager)", "watch",
                    lambda a: None if a in ("watch", "freebox") else "answer watch or freebox")
    mode = mode or "watch"
    raw: dict = {"paths": dict(folders), "completeness": {"method": "freebox" if mode == "freebox" else "quiescence"}}
    if args.review:
        raw["paths"]["review"] = os.path.normpath(args.review)
    if args.state_dir:
        raw["paths"]["state_dir"] = os.path.normpath(args.state_dir)
    if args.quiet_minutes is not None:
        raw["completeness"]["min_quiet_minutes"] = args.quiet_minutes
    if mode == "freebox" and args.freebox_downloads_dir:
        raw["freebox"] = {"remote_downloads_dir": args.freebox_downloads_dir}

    mount_note = "no mount check (the media folders are on the root filesystem)"
    if args.mount != "none":
        entry = detect_mount(folders["movies"])
        if entry is not None:
            raw["mount"] = {"mountpoint": entry.mountpoint, "fstype": entry.fstype, "source": entry.source}
            mount_note = f"mount check: {entry.mountpoint} must be mounted from {entry.source} ({entry.fstype})"
    else:
        mount_note = "no mount check (--mount none)"

    if args.tmm or args.tmm_url or args.tmm_path_map:
        tmm: dict = {"enabled": True}
        if args.tmm_url:
            tmm["base_url"] = args.tmm_url
        if args.tmm_path_map:
            tmm["path_map"] = parse_value(find_key("tmm.path_map"), ",".join(args.tmm_path_map))
        raw["tmm"] = tmm
    if args.every is not None:
        raw["schedule"] = {"run_every_minutes": args.every}

    backup, cfg = write_config(path, raw)
    _prepare_state_dir(cfg.paths.state_dir)
    print(f"configuration written to {path}" + (f" (previous version: {backup})" if backup else ""))
    print(f"  downloads  {cfg.paths.downloads}\n  movies     {cfg.paths.movies}\n  tv         {cfg.paths.series}\n"
          f"  review     {cfg.paths.review}\n  state      {cfg.paths.state_dir}\n  {mount_note}\n"
          f"  finished downloads: " + ("Freebox download manager" if cfg.completeness.method == "freebox" else
                                       f"unchanged for {cfg.completeness.min_quiet_minutes} min"))
    _report_access(cfg)
    steps = ["sudo mediaorg check                       # read-only: paths, permissions, mount"]
    if cfg.completeness.method == "freebox":
        steps += ["sudo mediaorg freebox authorize            # confirm on the Freebox display",
                  "sudo mediaorg freebox status               # shows the Freebox download folder",
                  "sudo mediaorg config set freebox.remote_downloads_dir=<that folder>"]
    if cfg.tmm.enabled:
        steps += ["sudo mediaorg tmm set-key                  # paste the key of tMM's HTTP API"]
    steps += ["sudo mediaorg run                         # dry-run: shows what would happen, changes nothing",
              "sudo mediaorg service install --enable    # then every "
              f"{cfg.schedule.run_every_minutes} min, also after a reboot"]
    print("next:\n" + "\n".join(f"  {i}. {s}" for i, s in enumerate(steps, 1)))
    return EXIT_OK


def _prepare_state_dir(state_dir: str) -> None:
    if os.path.isdir(state_dir) or not is_root():
        return
    ids = service_account()
    os.makedirs(state_dir, mode=0o750)
    if ids:
        os.chown(state_dir, *ids)


def _report_access(cfg) -> None:
    p = cfg.paths
    rw = os.R_OK | os.W_OK | os.X_OK
    checks = [(p.downloads, rw), (p.movies, rw), (p.series, rw)]
    if os.path.isdir(p.review):
        checks.append((p.review, rw))
    if os.path.isdir(p.state_dir):
        checks.append((p.state_dir, rw))
    from .service import move_root
    try:
        move_root(cfg)
    except ConfigError as exc:  # manual runs work; the timers' sandbox cannot serve such a layout
        print(f"WARNING: {exc}")
    bad = access_as(SERVICE_USER, checks)
    if bad is None:
        return
    if bad:
        print(f"WARNING: the service account '{SERVICE_USER}' cannot read and write: {', '.join(bad)}\n"
              f"  give it access (group membership, ACL, or the uid/gid options of a network mount); "
              f"`sudo mediaorg check` tells when it is right.")
    else:
        print(f"  the service account '{SERVICE_USER}' can read and write all media folders")


# ------------------------------------------------------------------------------- tMM key
_KEY_CHARS = re.compile(r"^[\x21-\x7e]{8,256}$")


def cmd_tmm_set_key(args) -> int:
    path = config_path(args)
    key_file = "/etc/mediaorg/tmm.key"
    try:
        key_file = load(path).tmm.api_key_file
    except ConfigError as exc:
        if os.path.exists(path):
            raise
        log.warning("%s; using the default key file %s", exc, key_file)
    if args.generate:
        key = str(uuid.uuid4())
    elif args.from_tmm_json:
        try:
            with open(args.from_tmm_json, encoding="utf-8") as fh:
                key = str(json.load(fh).get("httpApiKey") or "")
        except (OSError, ValueError, AttributeError) as exc:
            raise ConfigError(f"cannot read httpApiKey from {args.from_tmm_json}: {exc}") from exc
    elif sys.stdin.isatty():
        key = getpass.getpass("tMM API key (input hidden): ").strip()
        if getpass.getpass("again: ").strip() != key:
            raise ConfigError("the two entries differ; nothing changed")
    else:
        key = sys.stdin.read().strip()
    if not _KEY_CHARS.match(key):
        raise ConfigError("the key must be 8-256 printable characters without spaces; nothing changed")
    uid = gid = None
    if is_root():
        ids = service_account()
        uid, gid = 0, (ids[1] if ids else 0)
    folder = os.path.dirname(key_file)
    if not os.path.isdir(folder):
        if not is_root():
            raise ConfigError(f"{folder} does not exist (run with sudo)")
        os.makedirs(folder, mode=0o750)
    try:
        atomic_write(key_file, (key + "\n").encode("ascii"), 0o640, uid, gid)
    except PermissionError as exc:
        raise ConfigError(f"cannot write {key_file}: permission denied (run with sudo)") from exc
    print(f"tMM API key saved to {key_file} (mode 0640, readable by the service account only)")
    if args.generate:
        print(f"new key: {key}\nenter it as the API key in tinyMediaManager's HTTP API settings, then check "
              "with: sudo mediaorg tmm status")
    else:
        print("check it with: sudo mediaorg tmm status")
    return EXIT_OK


def guard_admin(path: str) -> int | None:
    """Setup commands need root, unless the configuration lives somewhere this user may write."""
    folder = os.path.dirname(os.path.abspath(path))
    if is_root() or (os.access(folder, os.W_OK) and (not os.path.exists(path) or os.access(path, os.W_OK))):
        return None
    print(f"this changes {path}: run it with sudo", file=sys.stderr)
    return EXIT_PRECONDITION


__all__ = ["cmd_init", "cmd_set", "cmd_unset", "cmd_get", "cmd_show", "cmd_validate", "cmd_path", "cmd_keys",
           "cmd_tmm_set_key", "config_path", "write_config", "detect_mount", "guard_admin", "DEFAULT_STATE_DIR"]
