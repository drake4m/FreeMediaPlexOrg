#!/bin/bash
# fix-case-renames.sh - one-time repair of library folders that tinyMediaManager cannot rename.
#
# On Linux, with the library on an SMB/CIFS share, tMM cannot rename a folder whose new name differs from
# the current one only in letter case ("Rise of the Conqueror (2026)" -> "Rise Of The Conqueror (2026)"):
# its rename fails (NoSuchFileException, "Renaming aborted") and the movie keeps its names. New folders are
# protected by naming.tmm_staging (docs/TMM.md); this script repairs the folders that are already stuck.
#
# A folder is a candidate when its tMM .nfo (movie .nfo, or tvshow.nfo) names a title and year whose
# "Title (Year)" equals the folder name ignoring case but not exactly. Each candidate is renamed to
# "<name> [mediaorg]" (a name that differs by more than the case), then tMM is asked to update, scrape and
# rename what is new (`mediaorg tmm sync --apply`): tMM then gives the folder and its files their names.
# Folders without such an .nfo, or whose name differs otherwise, are left alone. Nothing is deleted or
# overwritten; mediaorg's run lock is held while folders are renamed.
#
#   sudo bash fix-case-renames.sh           dry-run: lists the candidates, changes nothing
#   sudo bash fix-case-renames.sh --apply   renames them, then sends the tMM calls
#
# The command that undoes each rename (useful only until tMM renamed the folder) is written to
# <paths.state_dir>/case-renames-<time>.log. Needs mediaorg 2.1.0 or later and tmm.enabled = true.
set -uo pipefail

APPLY=0
case "${1:-}" in
  --apply) APPLY=1 ;;
  ""|--dry-run) ;;
  -h|--help) sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "unknown argument: $1 (see --help)" >&2; exit 2 ;;
esac
die() { echo "fix-case-renames: $1" >&2; exit "${2:-1}"; }
[ "$(id -u)" = 0 ] || die "run it with sudo" 2
command -v mediaorg >/dev/null || die "mediaorg not found"
command -v python3 >/dev/null || die "python3 not found"
command -v flock >/dev/null || die "flock not found (util-linux)"
ver=$(mediaorg --version 2>/dev/null | awk '{print $2}')
IFS=. read -r major minor _ <<< "$ver"
if ! [[ "${major:-}" =~ ^[0-9]+$ && "${minor:-}" =~ ^[0-9]+$ ]] || [ "$major" -lt 2 ] \
    || { [ "$major" -eq 2 ] && [ "$minor" -lt 1 ]; }; then
  die "mediaorg ${ver:-?}: 2.1.0 or later is needed (older versions do not recognise the [mediaorg] tag)"
fi
get() { mediaorg config get "$1" </dev/null; }
MOVIES=$(get paths.movies) || die "cannot read paths.movies"
SERIES=$(get paths.series) || die "cannot read paths.series"
STATE=$(get paths.state_dir) || die "cannot read paths.state_dir"
[ "$(get tmm.enabled)" = true ] || die "tmm.enabled is false: only tMM's rename gives the folders their names"
for d in "$MOVIES" "$SERIES"; do
  [ -d "$d" ] || die "$d is not a directory (share not mounted?)"
done

# candidates, one per line: <library folder>\t<folder name>\t<title (year) from the .nfo>
list_candidates() {
  python3 -I - "$MOVIES" "$SERIES" <<'PY'
import os
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET

TAG = re.compile(r"\s*\[mediaorg\]\s*$", re.I)


def nfc(s):
    return unicodedata.normalize("NFC", s)


def nfo_title_year(folder, show):
    names = ["tvshow.nfo"] if show else sorted(n for n in os.listdir(folder) if n.lower().endswith(".nfo"))
    for n in names:
        p = os.path.join(folder, n)
        if not os.path.isfile(p):
            continue
        try:
            root = ET.parse(p).getroot()
        except (ET.ParseError, OSError):
            continue
        if root.tag not in ("movie", "tvshow"):
            continue
        title = (root.findtext("title") or "").strip()
        year = (root.findtext("year") or "").strip()
        if title and re.fullmatch(r"\d{4}", year):
            return title, year
    return None


for lib, show in ((sys.argv[1], False), (sys.argv[2], True)):
    for name in sorted(os.listdir(lib)):
        folder = os.path.join(lib, name)
        if name.startswith(".") or not os.path.isdir(folder) or TAG.search(name):
            continue
        try:
            ty = nfo_title_year(folder, show)
        except OSError:
            continue
        if not ty:
            continue
        # tMM's default folder pattern "${title} (${year})"; ':' becomes ' - ', characters Windows forbids go
        want = re.sub(r"\s*:\s*", " - ", f"{ty[0]} ({ty[1]})")
        want = re.sub(r'[\\/*?"<>|]', "", want)
        want = re.sub(r"\s+", " ", want).strip()
        if nfc(want) != nfc(name) and nfc(want).casefold() == nfc(name).casefold():
            print(f"{lib}\t{name}\t{want}")
PY
}

mapfile -t CANDS < <(list_candidates)
echo "== folders tMM cannot rename because only the letter case differs ($([ "$APPLY" = 1 ] && echo apply || echo dry-run))"
if [ "${#CANDS[@]}" -eq 0 ]; then
  echo "none found; nothing to do"
  exit 0
fi
for c in "${CANDS[@]}"; do
  IFS=$'\t' read -r lib name want <<< "$c"
  printf '  %s/%s\n      tMM wants: %s\n      renamed to: %s [mediaorg]\n' "$(basename "$lib")" "$name" "$want" "$name"
done
if [ "$(get naming.tmm_staging)" != true ]; then
  echo "note: naming.tmm_staging is off, so new folders can get stuck the same way (sudo mediaorg config set naming.tmm_staging=true)"
fi
if [ "$APPLY" = 0 ]; then
  echo "== DRY-RUN: nothing was changed. Run again with --apply."
  exit 0
fi

LOG="$STATE/case-renames-$(date +%Y%m%d-%H%M%S).log"
FAILS=0
DONE=0
cd / || exit 1
exec 9>>"$STATE/lock"
flock -w 300 9 || die "a mediaorg run held the lock for 5 minutes; nothing was changed"
for c in "${CANDS[@]}"; do
  IFS=$'\t' read -r lib name want <<< "$c"
  src="$lib/$name"
  dst="$lib/$name [mediaorg]"
  if [ -e "$dst" ]; then
    echo "  [skip] $dst exists"; FAILS=$((FAILS + 1)); continue
  fi
  if runuser -u mediaorg -- mv -n -T -- "$src" "$dst" && [ -d "$dst" ] && [ ! -e "$src" ]; then
    echo "  [ok]   $(basename "$lib")/$name -> $name [mediaorg]"
    printf 'mv -n -T -- %q %q\n' "$dst" "$src" >> "$LOG"
    DONE=$((DONE + 1))
  else
    echo "  [FAIL] $src was not renamed"; FAILS=$((FAILS + 1))
  fi
done
flock -u 9
exec 9>&-
[ -f "$LOG" ] && echo "undo commands (only until tMM renamed the folders): $LOG"
if [ "$DONE" -eq 0 ]; then
  echo "== nothing renamed ($FAILS problem(s)); tMM not called"
  exit 1
fi
echo "-- asking tMM to update, scrape and rename new items"
if mediaorg tmm sync --apply </dev/null; then
  echo "== DONE: $DONE folder(s) renamed, $FAILS problem(s); tMM renames them within minutes (its log: 'Renaming movie: ...')"
else
  echo "== the tMM calls failed: the folders keep the tag until the next sync (nightly), or: sudo mediaorg tmm sync --apply"
  FAILS=$((FAILS + 1))
fi
[ "$FAILS" -eq 0 ]
