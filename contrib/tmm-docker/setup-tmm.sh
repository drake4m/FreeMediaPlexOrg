#!/bin/bash
# Optional: run tinyMediaManager headless in Docker, next to mediaorg.
#
#   sudo bash setup-tmm.sh --movies /srv/media/movies --series /srv/media/tv [options]
#
# Options:
#   --movies DIR            movie library (required)            -> /media/movies in the container
#   --series DIR            TV library (required)               -> /media/series in the container
#   --data DIR              tMM settings and database, a folder of its own (default /srv/tmm/data) -> /data
#   --ui-bind ADDRESS       web UI address (default 127.0.0.1 = this machine only; 0.0.0.0 = the LAN)
#   --tz ZONE               container time zone (default: this machine's, e.g. Europe/Berlin)
#   --user NAME             account whose uid/gid tMM runs as (default mediaorg: same rights on the library)
#   --import-settings DIR   start from the settings of an existing tMM (tmm.json, movies.json,
#                           tvShows.json, scraper_*.conf from its data folder); the HTTP API is enabled
#                           with a NEW key, written to /etc/mediaorg/tmm.key
#   --pro                   with --import-settings: keep PRO-only scrapers (TVDB, fanart.tv)
#   --image IMAGE           another image (default: the tested version below, pinned by digest)
#
# Creates /etc/tmm/tmm.conf and /etc/tmm/container.env (root only), the data folder, and
# /etc/systemd/system/tinymediamanager.service. Does NOT start or enable the service. Refuses to run
# over existing tMM settings.
set -euo pipefail
umask 022

# tMM 5.3.4 (released 2026-10-04), pinned by its multi-arch digest from Docker Hub: the image cannot
# change underneath you. Pass --image to use another version.
TMM_VERSION=5.3.4
IMAGE="tinymediamanager/tinymediamanager@sha256:48c15784a12759f3e055492977f9b1dc9d80301f9b0286993c60ae0649e38db9"
DATA=/srv/tmm/data
CONF=/etc/tmm
SVC_USER=mediaorg
UI_BIND=127.0.0.1
TZ_NAME=""
MOVIES=""
SERIES=""
IMPORT=""
PRO=()
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
UNIT_SRC="$HERE/tinymediamanager.service"

die() { printf 'ERROR: %s\n' "$1" >&2; exit "${2:-1}"; }
usage() { sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --movies) MOVIES=${2:?--movies needs a folder}; shift 2 ;;
    --series) SERIES=${2:?--series needs a folder}; shift 2 ;;
    --data) DATA=${2:?--data needs a folder}; shift 2 ;;
    --ui-bind) UI_BIND=${2:?--ui-bind needs an address}; shift 2 ;;
    --tz) TZ_NAME=${2:?--tz needs a zone}; shift 2 ;;
    --user) SVC_USER=${2:?--user needs an account}; shift 2 ;;
    --import-settings) IMPORT=${2:?--import-settings needs a folder}; shift 2 ;;
    --pro) PRO=(--pro); shift ;;
    --image) IMAGE=${2:?--image needs an image}; TMM_VERSION="(custom image)"; shift 2 ;;
    -h|--help) usage 0 ;;
    *) die "unknown option: $1 (see --help)" 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "run as root (sudo)" 2
if [ -z "$MOVIES" ] || [ -z "$SERIES" ]; then die "--movies and --series are required (see --help)" 2; fi
[ -f "$UNIT_SRC" ] || die "unit file not found next to this script: $UNIT_SRC" 2

check_dir() {  # absolute, existing, and usable in a docker volume (no ':' or ',')
  case "$1" in /*) ;; *) die "$2 must be an absolute path: $1" 2 ;; esac
  case "$1" in *:*|*,*) die "$2 must not contain ':' or ',': $1" 2 ;; esac
  [ -d "$1" ] || die "$2 does not exist: $1" 3
}
check_dir "$MOVIES" --movies
check_dir "$SERIES" --series
case "$DATA" in /*) ;; *) die "--data must be an absolute path: $DATA" 2 ;; esac
case "$DATA" in *:*|*,*) die "--data must not contain ':' or ',': $DATA" 2 ;; esac
# the data folder is given to tMM's account (chown -R below): it must be a folder of its own
DATA=$(realpath -m -- "$DATA")
case "$DATA" in /*/*) ;; *) die "--data must be a folder of its own, at least two levels deep (e.g. /srv/tmm/data): $DATA" 2 ;; esac
for sys in /etc /usr /bin /sbin /lib /lib32 /lib64 /libx32 /boot /dev /proc /sys /run /root /tmp /var/tmp \
           /opt/mediaorg /var/lib/mediaorg /var/log/mediaorg; do
  case "$DATA/" in "$sys"/*) die "--data must not be inside $sys: $DATA" 2 ;; esac
done
for lib in "$MOVIES" "$SERIES"; do
  lib=$(realpath -- "$lib")
  case "$DATA/" in "$lib"/*) die "--data must not be inside a library folder ($lib): $DATA" 2 ;; esac
  case "$lib/" in "$DATA"/*) die "--data must not contain a library folder ($lib): $DATA" 2 ;; esac
done
if [ -d "$DATA" ]; then
  OTHER=$(find "$DATA" -mindepth 1 -maxdepth 1 ! -name data ! -name logs ! -name cache ! -name backup -printf '%f' -quit)
  [ -z "$OTHER" ] || die "--data $DATA already holds other files ('$OTHER' ...): give tMM a folder of its own" 3
fi
printf '%s' "$UI_BIND" | grep -zEqx '((25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])[.]){3}(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])' \
  || die "--ui-bind must be an IPv4 address (127.0.0.1, 0.0.0.0 or this machine's LAN address)" 2
if [ -n "$IMPORT" ]; then
  [ -d "$IMPORT" ] || die "--import-settings: $IMPORT is not a folder" 2
  for f in tmm.json movies.json tvShows.json; do
    [ -f "$IMPORT/$f" ] || die "--import-settings: $IMPORT/$f not found" 2
  done
fi

# --- preconditions (read-only) --------------------------------------------------------------------
for c in docker ss findmnt python3; do command -v "$c" >/dev/null || die "$c not found" 3; done
docker info >/dev/null 2>&1 || die "the docker daemon is not running (systemctl start docker)" 3
id "$SVC_USER" >/dev/null 2>&1 || die "account $SVC_USER missing: install mediaorg first, or pass --user" 3
for p in 4000 7878; do
  if ss -Hltn "sport = :$p" | grep -q .; then die "TCP port $p is already in use on this host" 3; fi
done
if [ -d "$DATA/data" ] && [ -n "$(ls -A "$DATA/data" 2>/dev/null)" ]; then
  die "$DATA/data already holds tMM settings; this setup runs only once (see docs/TMM.md, reset)" 3
fi
UID_N=$(id -u "$SVC_USER"); GID_N=$(id -g "$SVC_USER")
REQUIRE_MOUNT=$(findmnt -n -o TARGET --target "$MOVIES" || true)
[ "$REQUIRE_MOUNT" = "/" ] && REQUIRE_MOUNT=""
if [ -z "$TZ_NAME" ]; then
  TZ_NAME=$(timedatectl show -p Timezone --value 2>/dev/null || true)
  [ -n "$TZ_NAME" ] || TZ_NAME=$(cat /etc/timezone 2>/dev/null || echo Etc/UTC)
fi
printf '%s' "$TZ_NAME" | grep -Eqx '[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*' || die "--tz: invalid zone '$TZ_NAME'" 2

echo "== pulling tMM $TMM_VERSION ($IMAGE)"
docker pull --quiet "$IMAGE" >/dev/null
docker image inspect "$IMAGE" >/dev/null || die "image not available after pull" 3

# tMM must resolve api.themoviedb.org. A host resolving through a resolver on 127.0.0.1 (dnsmasq, a local
# DNS-over-HTTPS proxy, systemd-resolved's stub in some setups) leaves a container without DNS: then tMM
# gets public resolvers of its own (TMM_DNS_OPTS in tmm.conf; the rest of the host is unchanged).
echo "== DNS inside a container"
DNS_OPTS=""
if timeout 40 docker run --rm --pull=never --entrypoint getent "$IMAGE" hosts api.themoviedb.org >/dev/null 2>&1; then
  echo "   ok with Docker's default DNS"
elif timeout 40 docker run --rm --pull=never --dns 1.1.1.1 --dns 8.8.8.8 --entrypoint getent "$IMAGE" \
       hosts api.themoviedb.org >/dev/null 2>&1; then
  DNS_OPTS="--dns 1.1.1.1 --dns 8.8.8.8"
  echo "   NOTE: Docker's default DNS does not work in a container here; tMM uses $DNS_OPTS (TMM_DNS_OPTS)"
else
  echo "   WARNING: a container cannot resolve api.themoviedb.org, not even through 1.1.1.1 / 8.8.8.8:"
  echo "            tMM will not be able to scrape. Allow DNS and HTTPS from this host, then set"
  echo "            TMM_DNS_OPTS in $CONF/tmm.conf (docs/TMM.md)."
fi

echo "== data folder $DATA"
# only the data folder gets tMM's owner and mode; an existing parent (/srv, /var/lib ...) is left as it is
if [ ! -d "$(dirname "$DATA")" ]; then install -d -o root -g root -m 0755 "$(dirname "$DATA")"; fi
install -d -o "$UID_N" -g "$GID_N" -m 0700 "$DATA"
if [ -n "$IMPORT" ]; then
  echo "== tMM settings from $IMPORT"
  install -d -o root -g "$SVC_USER" -m 0750 /etc/mediaorg
  python3 -I "$HERE/tmm_migrate.py" --backup "$IMPORT" --out "$DATA/data" \
    --api-key-file /etc/mediaorg/tmm.key --key-group "$SVC_USER" "${PRO[@]}"
fi
chown -R "$UID_N:$GID_N" "$DATA"
[ -d "$DATA/data" ] && chmod 0700 "$DATA/data"

echo "== $CONF"
install -d -o root -g root -m 0700 "$CONF"
for f in "$CONF/tmm.conf" "$CONF/container.env"; do   # left over from an earlier attempt: keep a copy
  if [ -e "$f" ]; then mv "$f" "$f.bak-$(date +%Y%m%d%H%M%S)"; fi
done
umask 077
cat > "$CONF/tmm.conf" <<CONF
# tinyMediaManager $TMM_VERSION - read by tinymediamanager.service. After a change:
#   sudo systemctl restart tinymediamanager
TMM_IMAGE=$IMAGE
TMM_DATA=$DATA
TMM_MOVIES=$MOVIES
TMM_SERIES=$SERIES
# tMM starts only while this is mounted ("" = no check)
TMM_REQUIRE_MOUNT=$REQUIRE_MOUNT
# Web UI (port 4000): 127.0.0.1 = this machine only (SSH tunnel); 0.0.0.0 or a LAN address = the LAN.
TMM_UI_BIND=$UI_BIND
# DNS servers of the container as "--dns <IPv4>" pairs; empty = Docker's default.
TMM_DNS_OPTS=$DNS_OPTS
CONF
VNC_PW=$(head -c 18 /dev/urandom | base64 | tr -d '/+=' | cut -c1-20)
cat > "$CONF/container.env" <<ENV
USER_ID=$UID_N
GROUP_ID=$GID_N
PASSWORD=$VNC_PW
TZ=$TZ_NAME
LC_ALL=en_US.UTF-8
LANG=en_US.UTF-8
ENV
chmod 0600 "$CONF/tmm.conf" "$CONF/container.env"
umask 022

install -o root -g root -m 0644 "$UNIT_SRC" /etc/systemd/system/tinymediamanager.service
systemctl daemon-reload

ADMIN=${SUDO_USER:-$(logname 2>/dev/null || echo root)}
HOST=$(hostname)
cat <<MSG
== tMM SETUP OK (service installed, not started)
   web UI password (also in $CONF/container.env): $VNC_PW
   start:   sudo systemctl enable --now tinymediamanager
   web UI:  from your PC, through SSH (nothing opened on the LAN):
              ssh -L 4000:127.0.0.1:4000 $ADMIN@$HOST     then open http://localhost:4000
MSG
if [ -n "$IMPORT" ]; then
  cat <<MSG
   mediaorg: the API key is in /etc/mediaorg/tmm.key already; enable the calls with
              sudo mediaorg config set tmm.enabled=true && sudo mediaorg tmm status
MSG
else
  cat <<MSG
   then, in the web UI: finish the first-start wizard with the data sources /media/movies (movies)
   and /media/series (TV shows), enable the HTTP API (port 7878) with an API key, and:
              sudo mediaorg tmm set-key --from-tmm-json $DATA/data/tmm.json
              sudo mediaorg config set tmm.enabled=true && sudo mediaorg tmm status
MSG
fi
