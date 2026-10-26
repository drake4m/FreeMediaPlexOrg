#!/bin/bash
# mediaorg installer for Debian, Ubuntu and their derivatives.
#
#   sudo bash install.sh                  install, or upgrade in place (keeps configuration and state)
#   sudo bash install.sh --rollback       switch back to the previous release (/opt/mediaorg/app.old)
#   sudo bash install.sh --uninstall      remove the program and its timers; keep configuration and state
#   sudo bash install.sh --uninstall --purge   also remove /etc/mediaorg, the state, the logs and the account
#
# Needs: Python >= 3.11 with venv (python3-venv), systemd, util-linux (flock), and access to PyPI for
# the pinned, hash-checked dependencies (requirements.lock). Override the interpreter with PYTHON=...
# Never touches the media folders, tinyMediaManager, or whether the timers are enabled.
set -euo pipefail
umask 022

PREFIX=/opt/mediaorg
ETC=/etc/mediaorg
DEFAULT_STATE=/var/lib/mediaorg
LOG_DIR=/var/log/mediaorg
UNIT_DIR=/etc/systemd/system
SVC_USER=mediaorg
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$1" >&2; exit "${2:-1}"; }
version_of() { sed -n 's/^__version__ = "\(.*\)"/\1/p' "$1/mediaorg/__init__.py" 2>/dev/null || true; }
usage() { sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

MODE=install
PURGE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --rollback) MODE=rollback ;;
    --uninstall) MODE=uninstall ;;
    --purge) PURGE=1 ;;
    -h|--help) usage 0 ;;
    *) die "unknown argument: $1 (see --help)" 2 ;;
  esac
  shift
done
[ "$PURGE" = 0 ] || [ "$MODE" = uninstall ] || die "--purge goes with --uninstall" 2
[ "$(id -u)" -eq 0 ] || die "run as root: sudo bash install.sh" 2
command -v flock >/dev/null || die "flock not found: apt install util-linux" 3

# one installer at a time
exec 8>/run/lock/mediaorg-install.lock
flock -n 8 || die "another install.sh is running" 4

units_installed() { [ -f "$UNIT_DIR/mediaorg.service" ]; }

# the configuration the installed units were written from ("" without units; units of 1.x: the default one)
units_config() {
  local c=""
  units_installed || return 0
  # shellcheck disable=SC2016  # the backquotes are literal text of the unit header
  c=$(sed -n 's/^# Managed by mediaorg [^:]*: written from \(.*\) by `mediaorg service install`\.$/\1/p' \
    "$UNIT_DIR/mediaorg.service" | head -n 1)
  printf '%s\n' "${c:-$ETC/config.toml}"
}
UNIT_CFG=$(units_config)
MAIN_CFG=${UNIT_CFG:-$ETC/config.toml}

# state_dir (and so the run lock) as configured, default /var/lib/mediaorg
state_dir() {
  local s=""
  if [ -f "$MAIN_CFG" ] && [ -x "$PREFIX/venv/bin/python" ]; then
    s=$("$PREFIX/venv/bin/python" -I - "$MAIN_CFG" <<'PY' 2>/dev/null || true
import sys, tomllib
try:
    with open(sys.argv[1], "rb") as fh:
        print(tomllib.load(fh).get("paths", {}).get("state_dir", ""))
except Exception:
    print("")
PY
)
  fi
  printf '%s\n' "${s:-$DEFAULT_STATE}"
}

take_run_lock() {   # waits up to 15 min for a running job; released when this script exits
  local dir="$1" lock="$1/lock"
  [ -d "$dir" ] || return 0
  [ -e "$lock" ] || install -o "$SVC_USER" -g "$SVC_USER" -m 0640 /dev/null "$lock"
  exec 9<>"$lock"
  flock -w 900 9 || die "a mediaorg run has held $lock for 15 min; try again later" 4
}

# --- rollback -----------------------------------------------------------------------------------------
if [ "$MODE" = rollback ]; then
  [ -d "$PREFIX/app.old" ] || die "no previous version in $PREFIX/app.old" 3
  CUR=$(version_of "$PREFIX/app")
  take_run_lock "$(state_dir)"
  rm -rf "$PREFIX/app.rollback"
  [ -d "$PREFIX/app" ] && mv -T "$PREFIX/app" "$PREFIX/app.rollback"
  mv -T "$PREFIX/app.old" "$PREFIX/app"
  [ -d "$PREFIX/app.rollback" ] && mv -T "$PREFIX/app.rollback" "$PREFIX/app.old"
  say "== ROLLBACK OK: $CUR -> $("$PREFIX/bin/mediaorg" --version) (the rolled-back version is now in app.old)"
  say "   If the configuration was changed for $CUR, restore $ETC/config.toml.bak first."
  if units_installed && [ -d /run/systemd/system ]; then
    if [ -f "$PREFIX/app/systemd/mediaorg.service" ]; then   # 1.x shipped fixed unit files
      for u in mediaorg.service mediaorg.timer mediaorg-tmm-sync.service mediaorg-tmm-sync.timer; do
        if [ -f "$PREFIX/app/systemd/$u" ]; then install -o root -g root -m 0644 "$PREFIX/app/systemd/$u" "$UNIT_DIR/$u"; fi
      done
      systemctl daemon-reload
      say "   systemd units of the rolled-back version restored (enabled timers stay enabled)"
    elif "$PREFIX/bin/mediaorg" --config "$MAIN_CFG" service install >/dev/null 2>&1; then
      say "   systemd units rewritten by the rolled-back version"
    else
      say "   WARNING: check the systemd units: systemctl cat mediaorg.service"
    fi
  fi
  exit 0
fi

# --- uninstall ----------------------------------------------------------------------------------------
if [ "$MODE" = uninstall ]; then
  STATE=$(state_dir)   # read now: the program that reads the configuration is removed below
  take_run_lock "$STATE"
  if [ -d /run/systemd/system ]; then
    for t in mediaorg.timer mediaorg-tmm-sync.timer; do
      systemctl disable --now "$t" >/dev/null 2>&1 || true
    done
  fi
  for u in mediaorg.service mediaorg.timer mediaorg-tmm-sync.service mediaorg-tmm-sync.timer; do
    rm -f "$UNIT_DIR/$u"
  done
  [ -d /run/systemd/system ] && systemctl daemon-reload
  rm -f /usr/local/bin/mediaorg
  rm -rf "$PREFIX"
  say "== removed the program, its timers and /usr/local/bin/mediaorg (media folders untouched)"
  if [ "$PURGE" = 1 ]; then
    rm -rf "$ETC" "$LOG_DIR"
    PURGED="configuration and logs"
    case "$STATE" in
      /var/lib/mediaorg|/var/lib/mediaorg/*) rm -rf "$STATE"; PURGED="configuration, state and logs" ;;
      *) say "   state left in $STATE (not the default location; delete it yourself when you no longer need it)" ;;
    esac
    if id "$SVC_USER" >/dev/null 2>&1; then
      if userdel "$SVC_USER" 2>/dev/null; then PURGED="$PURGED, and the $SVC_USER account"; else say "   account $SVC_USER kept (in use?)"; fi
    fi
    say "== purged $PURGED"
  else
    say "   kept: $ETC (configuration, keys), $STATE (journals, reports, Freebox token), $LOG_DIR"
  fi
  exit 0
fi

# --- install / upgrade --------------------------------------------------------------------------------
[ "$HERE" != "$PREFIX" ] || die "this installed copy only supports --rollback and --uninstall; run install.sh from an extracted release" 2
for f in bin/mediaorg mediaorg/__init__.py mediaorg/cli.py requirements.lock config/freebox-ca.pem; do
  [ -e "$HERE/$f" ] || die "package file missing: $f (incomplete package?)" 2
done
VER=$(version_of "$HERE")
[ -n "$VER" ] || die "cannot read the package version" 2

if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  case " ${ID:-} ${ID_LIKE:-} " in
    *" debian "*|*" ubuntu "*) ;;
    *) say "WARNING: ${PRETTY_NAME:-this system} is not Debian/Ubuntu-based; continuing (only Debian and Ubuntu are tested)" ;;
  esac
fi
[ -d /run/systemd/system ] || say "WARNING: systemd is not running: the timers cannot be used (manual runs only)"

# Python >= 3.11 with venv: $PYTHON when given (and nothing else), else python3, else the newest python3.X
pick_python() {
  local c
  local -a candidates=(python3 python3.14 python3.13 python3.12 python3.11)
  [ -z "${PYTHON:-}" ] || candidates=("$PYTHON")
  for c in "${candidates[@]}"; do
    command -v "$c" >/dev/null 2>&1 || continue
    "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null || continue
    "$c" -c 'import ensurepip, venv' 2>/dev/null || continue
    command -v "$c"
    return 0
  done
  return 1
}
if [ -n "${PYTHON:-}" ] && ! PY=$(pick_python); then
  die "PYTHON=$PYTHON is not a Python >= 3.11 with venv (python3-venv or python3.X-venv)" 3
fi
if [ -z "${PYTHON:-}" ] && ! PY=$(pick_python); then
  FOUND=$(python3 -V 2>&1 || echo "no python3")
  die "Python >= 3.11 with venv is required (found: $FOUND).
       Debian 12 / Ubuntu 24.04 and later:  sudo apt install python3 python3-venv
       Ubuntu 22.04:                         sudo apt install python3.11 python3.11-venv" 3
fi
say "== mediaorg $VER: install/upgrade (Python: $PY, $("$PY" -V 2>&1))"

# --- service account and folders -------------------------------------------------------------------------
if ! id "$SVC_USER" >/dev/null 2>&1; then
  useradd --system --user-group --no-create-home --home-dir "$DEFAULT_STATE" --shell /usr/sbin/nologin "$SVC_USER"
  say "   created system account $SVC_USER ($(id "$SVC_USER"))"
fi
install -d -o root -g root -m 0755 "$PREFIX" "$PREFIX/bin"
install -d -o root -g "$SVC_USER" -m 0750 "$ETC"
install -d -o "$SVC_USER" -g "$SVC_USER" -m 0750 "$DEFAULT_STATE" "$LOG_DIR"

# --- stage the new code ----------------------------------------------------------------------------------
rm -rf "$PREFIX/app.new"
trap 'rm -rf "$PREFIX/app.new"' EXIT   # a failed attempt leaves no staging copy behind
install -d -o root -g root -m 0755 "$PREFIX/app.new"
for d in mediaorg tests config docs contrib; do
  if [ -d "$HERE/$d" ]; then cp -r "$HERE/$d" "$PREFIX/app.new/"; fi
done
find "$PREFIX/app.new" -name '__pycache__' -prune -exec rm -rf {} +
chown -R root:root "$PREFIX/app.new"
chmod -R u=rwX,go=rX "$PREFIX/app.new"

# --- validate the existing configuration(s) with the NEW code (before the program or the units change) ---------
CFGS=()
if [ -f "$ETC/config.toml" ]; then CFGS+=("$ETC/config.toml"); fi
if [ -n "$UNIT_CFG" ] && [ -f "$UNIT_CFG" ] && [ "$UNIT_CFG" != "$ETC/config.toml" ]; then CFGS+=("$UNIT_CFG"); fi
for cfg in "${CFGS[@]}"; do
  runuser -u "$SVC_USER" -- "$PY" -I - "$PREFIX/app.new" "$cfg" <<'PY' \
    || die "the existing $cfg is not valid for $VER; the program and its units were not changed (fix it, run again)" 2
import sys
sys.path.insert(0, sys.argv[1])
from mediaorg.config import load, ConfigError
try:
    load(sys.argv[2])
except ConfigError as exc:
    sys.exit(f"configuration error with the new version: {exc}")
PY
done

# --- under the run lock: dependencies, swap ------------------------------------------------------------------
take_run_lock "$(state_dir)"
venv_ok() {
  [ -x "$PREFIX/venv/bin/python" ] \
    && "$PREFIX/venv/bin/python" -I -c 'import sys, guessit, rebulk, babelfish, dateutil, six; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null
}
if [ -x "$PREFIX/venv/bin/python" ] && ! venv_ok; then
  say "   virtual environment damaged or too old: recreating it"
  rm -rf "$PREFIX/venv"
fi
[ -x "$PREFIX/venv/bin/python" ] || "$PY" -m venv "$PREFIX/venv"
install -m 0644 "$HERE/requirements.lock" "$PREFIX/requirements.lock"
"$PREFIX/venv/bin/pip" install --quiet --disable-pip-version-check --require-hashes --no-deps \
  --only-binary=:all: -r "$PREFIX/requirements.lock"
venv_ok || die "dependencies not importable after install" 3

CUR=$(version_of "$PREFIX/app")
if [ -d "$PREFIX/app" ]; then
  if [ "$CUR" = "$VER" ] && [ -d "$PREFIX/app.old" ]; then
    rm -rf "$PREFIX/app"                       # same version again: keep the older release in app.old
  else
    rm -rf "$PREFIX/app.old"
    mv -T "$PREFIX/app" "$PREFIX/app.old"
  fi
fi
mv -T "$PREFIX/app.new" "$PREFIX/app"
install -m 0644 "$HERE/README.md" "$PREFIX/README.md" 2>/dev/null || true
install -m 0755 "$HERE/bin/mediaorg" "$PREFIX/bin/mediaorg"
install -m 0755 "$HERE/install.sh" "$PREFIX/install.sh"     # kept for --rollback and --uninstall
ln -sfn "$PREFIX/bin/mediaorg" /usr/local/bin/mediaorg
"$PREFIX/venv/bin/python" -m compileall -q "$PREFIX/app/mediaorg"
[ "$("$PREFIX/bin/mediaorg" --version)" = "mediaorg $VER" ] || die "the installed launcher does not report $VER" 3

# --- reference files ------------------------------------------------------------------------------------------
install -o root -g "$SVC_USER" -m 0644 "$HERE/config/freebox-ca.pem" "$ETC/freebox-ca.pem"

# --- systemd units: rewritten from the configuration when they are installed (enabled state is kept) ----------
UNITS_NOTE=""
if [ -n "$UNIT_CFG" ] && [ -f "$UNIT_CFG" ] && [ -d /run/systemd/system ]; then
  if /usr/local/bin/mediaorg --config "$UNIT_CFG" service install >/dev/null; then
    UNITS_NOTE="systemd units rewritten from $UNIT_CFG"
  else
    UNITS_NOTE="WARNING: could not rewrite the systemd units; run: sudo mediaorg --config $UNIT_CFG service install"
  fi
fi
TIMERS="(systemd not running)"
if [ -d /run/systemd/system ]; then
  TIMERS=$(systemctl is-enabled mediaorg.timer mediaorg-tmm-sync.timer 2>/dev/null | paste -sd' ' || true)
fi

say "== INSTALL OK: $("$PREFIX/bin/mediaorg" --version)"
[ -d "$PREFIX/app.old" ] && say "   previous release kept in $PREFIX/app.old ($(version_of "$PREFIX/app.old")); undo: sudo bash $PREFIX/install.sh --rollback"
[ -n "$UNITS_NOTE" ] && say "   $UNITS_NOTE"
say "   timers (mediaorg, mediaorg-tmm-sync): ${TIMERS:-not installed}"
if [ ! -f "$ETC/config.toml" ]; then
  say "   next: sudo mediaorg config init      (asks for your folders; see docs/INSTALL.md)"
else
  say "   next: sudo mediaorg check"
fi
exit 0
