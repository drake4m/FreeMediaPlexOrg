# tinyMediaManager

[tinyMediaManager](https://www.tinymediamanager.org) (tMM) scrapes metadata and artwork and renames media
to its own patterns. mediaorg can tell it, after each run that filed something, to update its library and
then scrape and rename only the new items. The integration is optional and off by default.

- [What mediaorg sends](#what-mediaorg-sends)
- [Set up the integration](#set-up-the-integration)
- [The calls in detail](#the-calls-in-detail)
- [Changing the calls](#changing-the-calls)
- [Trash cleaning](#trash-cleaning)
- [Optional: tinyMediaManager in Docker](#optional-tinymediamanager-in-docker)
- [Known limitations](#known-limitations)

## What mediaorg sends

- After a run that filed something: one request per library that received files, **TV shows first, then
  movies**, each holding three actions in one call: `update` (scope `all`), `scrape` (scope `new`),
  `rename` (scope `new`). tMM queues the calls and answers at once; the run logs
  `tMM tvshow refresh queued ...`, `tMM movie refresh queued ...`.
- Every night at `schedule.tmm_sync_time` (03:37 local time by default, plus up to 10 minutes): the same
  calls for both libraries, from `mediaorg-tmm-sync.timer`. They pick up files you put into the library by
  hand. A night missed while the machine was off is caught up at the next boot; when tMM does not answer
  (still starting after a boot), the sync is retried up to 3 times, 5 minutes apart.
- When tMM cannot be reached after a run, the libraries that received files are remembered
  (`/var/lib/mediaorg/tmm_pending.json`) and sent with the next calls; `mediaorg status` shows them.
- With tMM 5.3.5 or later (which has a status endpoint), a run moves nothing while tMM is busy
  (`tmm.defer_moves_while_busy`), so a move cannot meet a folder tMM is renaming; the next run tries again.

The API key is sent in the `api-key` header. Plain `http://` is accepted only to this machine (a loopback
address such as `127.0.0.1`, or `localhost`); for a tMM on another machine use `https://`, or set
`tmm.allow_insecure_http = true` knowing that the key then travels unencrypted.

## Set up the integration

1. **tMM must know the library.** Its data sources must be the movie and TV library folders (as tMM sees
   them). Let tMM finish its first import of the existing library before you enable the calls: on an
   empty tMM database the first `update` would make every movie and show "new", and the calls would then
   scrape and rename all of them.
2. **Enable tMM's HTTP API** in tMM's settings (enable it, choose the port, 7878 by default here, and an API
   key). tMM's own documentation: <https://www.tinymediamanager.org/docs/http-api>.
3. **Give mediaorg the key** (stored in `tmm.api_key_file`, `/etc/mediaorg/tmm.key`, mode 0640
   root:mediaorg; never on a command line):

   ```bash
   sudo mediaorg tmm set-key                         # paste it (hidden input, asked twice)
   sudo mediaorg tmm set-key < /path/to/keyfile      # or from standard input
   sudo mediaorg tmm set-key --from-tmm-json /path/to/tmm/data/tmm.json   # read it from tMM's settings
   sudo mediaorg tmm set-key --generate              # or create one, then enter it in tMM
   ```

4. **Point mediaorg at tMM and switch the calls on:**

   ```bash
   sudo mediaorg config set tmm.base_url=http://127.0.0.1:7878 tmm.enabled=true
   ```

   When the units are installed, enabling tMM also adds the nightly sync timer, enabled when
   `mediaorg.timer` is.

5. **Check:**

   ```bash
   sudo mediaorg check            # [ok] tMM reachable ...; [ok] tMM endpoint /api/tvshow listed in /api/docs
   sudo mediaorg tmm status       # tMM's own status (tMM 5.3.5 or later)
   sudo mediaorg tmm sync         # prints the nightly calls; --apply sends them once
   ```

   `check` reads tMM's API description (`/api/docs`, tMM 5.3.3 or later), so `[ok]` there proves that the
   key works and that the endpoint names are right.

`tmm.path_map` is needed only when you write calls of your own that use `{paths}` (the library folders as
tMM sees them, for example inside a container): `[["/srv/media/movies", "/media/movies"], ...]`. The
default calls do not use it.

## The calls in detail

- **update all**: tMM scans its data sources for added, changed and removed files. It reads the media
  information (resolution, codecs, duration) of files it has not analysed yet; right after tMM was set up
  on an existing library that can be every file, which takes hours over a network share, once. Queued
  calls wait meanwhile; do not restart tMM during that pass.
- **scrape new**: only the items this update found. tMM searches its scraper (TMDB by default) and takes
  the best result when it is good enough (for movies, the score threshold of tMM's automatic scraper
  settings); otherwise, or when several results match equally well, the item stays unscraped for you.
  Episodes of a show tMM already knows need no search.
- **rename new**: the same items get tMM's renamer patterns (folders and files). If tMM's own "automatic
  rename after scrape" already did it, renaming again changes nothing.
- Files tMM replaces when it renames (old NFO or artwork names) go to its trash, `.deletedByTMM`, which
  mediaorg empties by retention ([Trash cleaning](#trash-cleaning)).

tMM's documentation says that API calls are queued and run one after another, that the actions of one
call run in a fixed order (update first, then scrape, then rename ...), and that a new call drops the
"new" marks of previous calls. That is why mediaorg sends the three actions in one call.

An item that is not scraped automatically is scraped once by hand in tMM (search and scrape, pick the
match): its folder and files are renamed then.

## Changing the calls

The calls are configuration (`tmm.after_move_movie`, `tmm.after_move_tvshow`, `tmm.full_sync_movie`,
`tmm.full_sync_tvshow`), written as TOML lists of `{action, scope}`. If new episodes are never scraped
automatically although each one matches when scraped by hand, scrape every still-unscraped show and episode
at each call instead of only the new ones:

```bash
sudo mediaorg config set 'tmm.after_move_tvshow=[{action="update", scope={name="all"}}, {action="scrape", scope={name="unscraped"}}, {action="rename", scope={name="new"}}]'
sudo mediaorg config set 'tmm.full_sync_tvshow=[{action="update", scope={name="all"}}, {action="scrape", scope={name="unscraped"}}, {action="rename", scope={name="new"}}]'
sudo mediaorg tmm sync          # prints the calls
```

`sudo mediaorg config unset tmm.after_move_tvshow tmm.full_sync_tvshow` goes back to the defaults. Only
the shape of the values is checked (a list of tables, each with an `action`); the actions and scopes are
tMM's ([its API documentation](https://www.tinymediamanager.org/docs/http-api)), so look at what
`tmm sync` prints before relying on a change.

## Trash cleaning

tMM moves what it replaces or deletes into `.deletedByTMM` at the root of each library. tMM's own option to
delete that folder acts only when tMM shuts down, which a long-running tMM rarely does, so the trash grows.
mediaorg deletes each file there once it has seen it there for the retention:

```bash
sudo mediaorg config set review.tmm_trash_retention_days=7       # default; 0 = in the first run, -1 = never
sudo mediaorg config set review.tmm_trash_retention_minutes=30   # or in minutes (set one of the two)
```

The time is counted from runs (at most 12 hours per run), not from file dates, which tMM's moves do not
change. Each file is re-checked right before it is deleted; links are removed, never followed. `[TRASH]`
lines in the report name the files, and a due file that could not be deleted is logged with the reason. To
keep a file, move it out of `.deletedByTMM`. This works whether tMM's calls are enabled or not.

## Optional: tinyMediaManager in Docker

`contrib/tmm-docker/` (installed in `/opt/mediaorg/app/contrib/tmm-docker/`) runs the official tMM Docker
image as a systemd service on the same machine: the HTTP API on `127.0.0.1:7878` only, the web UI on port
4000, the library mounted as `/media/movies` and `/media/series`, tMM's data in `/srv/tmm/data`.

Requirements: Docker Engine with a running daemon (`docker.io` or Docker CE); ports 4000 and 7878 free.

```bash
sudo bash /opt/mediaorg/app/contrib/tmm-docker/setup-tmm.sh --movies /srv/media/movies --series /srv/media/tv
sudo systemctl enable --now tinymediamanager
sudo journalctl -u tinymediamanager -f   # Ctrl-C to stop following
```

| Option | Meaning |
|---|---|
| `--movies DIR`, `--series DIR` | the libraries (required); `/media/movies` and `/media/series` in the container |
| `--data DIR` | tMM's settings and database (default `/srv/tmm/data`): a folder of its own, given to tMM's account; refused inside a system folder, `/tmp`, mediaorg's folders or a library folder, and when it already holds other files |
| `--ui-bind ADDRESS` | web UI address: `127.0.0.1` (default, this machine only: use an SSH tunnel), the machine's LAN address, or `0.0.0.0` |
| `--tz ZONE` | container time zone (default: the machine's) |
| `--user NAME` | account whose ids tMM runs with (default `mediaorg`: the same rights on the library) |
| `--import-settings DIR` | start from the settings of an existing tMM (`tmm.json`, `movies.json`, `tvShows.json`, `scraper_*.conf`); the HTTP API is enabled with a new key, written to `/etc/mediaorg/tmm.key` |
| `--pro` | with `--import-settings`: keep the scrapers that need a tMM PRO licence |
| `--image IMAGE` | another image (default: tMM 5.3.4, pinned by digest) |

The script refuses to run over existing tMM settings, writes `/etc/tmm/tmm.conf` and
`/etc/tmm/container.env` (root only; the web-UI password is in the latter) and the unit, and starts
nothing. Then, in the web UI: finish tMM's first-start wizard with the data sources `/media/movies` and
`/media/series`, enable the HTTP API (port 7878) with a key, let the first import finish, and:

```bash
sudo mediaorg tmm set-key --from-tmm-json /srv/tmm/data/data/tmm.json
sudo mediaorg config set tmm.enabled=true && sudo mediaorg check
```

**Web UI.** tMM is a desktop application; the image shows its window in a browser (noVNC, plain HTTP) on
port 4000, behind the web-UI password. With the default `--ui-bind 127.0.0.1`, open it from your computer
through an SSH tunnel: `ssh -N -L 4000:127.0.0.1:4000 <you>@<server>`, then `http://localhost:4000`. To
reach it from the LAN instead, set `TMM_UI_BIND` in `/etc/tmm/tmm.conf` to the machine's LAN address and
restart the service; know that the page is not encrypted, and that Docker publishes ports through its own
firewall rules, which `ufw` does not filter. The HTTP API stays on `127.0.0.1` either way.

**Name resolution in the container.** tMM must resolve `api.themoviedb.org`. When the machine resolves
names through a resolver on `127.0.0.1` (dnsmasq, a local DNS-over-HTTPS proxy ...), that address means
the container itself inside it, and every search finds nothing (`UnknownHostException` in tMM's log). The
setup tests this and, when needed, gives the container public resolvers in `/etc/tmm/tmm.conf`:

```text
TMM_DNS_OPTS=--dns 1.1.1.1 --dns 8.8.8.8
```

Only `--dns <IPv4>` pairs are accepted there. Check: `docker exec tinymediamanager getent hosts api.themoviedb.org`.

**Logs.** The container output goes to the journal (`sudo journalctl -u tinymediamanager`; Docker keeps no
second copy: log driver `none`). tMM's own log files are in `<data>/logs`. tMM 5.3.4's logging
configuration keeps at most 80 MB of old `tmm.log` files and 150 MB of old trace logs, plus the current
files (a `trace-<time>.log` of up to 50 MB per start of tMM) and a small `startup.log`: plan for about
250 MB, and look now and then (`sudo du -sh <data>/logs`), because the trace file of each start is a new file.

**Restarts.** The service restarts tMM on failure, and waits while the library's mount is missing (it
never starts tMM on an empty mount point).

**Upgrade tMM.** Find the digest of the new version on Docker Hub (`tinymediamanager/tinymediamanager`, tag
`X.Y.Z`) and read tMM's changelog. Then, replacing the digest:

```bash
sudo bash -euo pipefail <<'EOF'
NEW_DIGEST='sha256:PASTE-THE-64-HEX-DIGEST-HERE'
[[ "$NEW_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "NEW_DIGEST is not a sha256 digest"; exit 1; }
IMG="tinymediamanager/tinymediamanager@$NEW_DIGEST"
DATA=$(sed -n 's/^TMM_DATA=//p' /etc/tmm/tmm.conf)
docker pull --quiet "$IMG"
systemctl stop tinymediamanager
tar -C "$(dirname "$DATA")" -czf "/root/tmm-data-$(date +%Y%m%d%H%M%S).tar.gz" "$(basename "$DATA")"
cp -p /etc/tmm/tmm.conf "/etc/tmm/tmm.conf.bak-$(date +%Y%m%d%H%M%S)"
sed -i "s#^TMM_IMAGE=.*#TMM_IMAGE=$IMG#" /etc/tmm/tmm.conf
systemctl start tinymediamanager
EOF
```

To go back: stop the service, restore the newest `/root/tmm-data-*.tar.gz` and `/etc/tmm/tmm.conf.bak-*`,
start it again. **Back up** `/etc/tmm` and tMM's data folder with the service stopped.

**Start over.** Stop the service, move the data folder aside, run `setup-tmm.sh` again.

## Known limitations

- tMM scrapes automatically only a result that is good enough for it; the threshold is a setting for movies
  only. Such items stay unscraped until you scrape them in tMM.
- A new API call drops the "new" marks of previous calls (tMM's documentation). Whether a call already
  queued keeps them is not documented: in the worst case, two downloads filed within minutes while tMM is
  busy leave the first one unscraped. The nightly sync does not scrape it either (it is not "new" any more);
  the `unscraped` scope above does.
- Files filed while tMM was stopped are found by tMM's own update at its start, if that option is on: they
  are then "new" for no mediaorg call. The pending list and the nightly sync catch up the update, the
  `unscraped` scope catches up the scrape.
- If the library disappears in the middle of a tMM update (a NAS restart), tMM 5.3.4 removes from its
  database the movies and shows whose folders it can no longer see: its clean-up takes an unreadable
  folder for a deleted one (read in tMM's source, `MovieUpdateDatasourceTask` and
  `TvShowUpdateDatasourceTask`). The files are not touched; the next update adds them back from their `.nfo`
  files and the calls scrape and rename them again as new items. tMM skips a data source that is
  unavailable when an update starts.
- tMM's Java heap is 512 MB by default. If its log shows `OutOfMemoryError`, raise it as tMM's Docker
  documentation explains; the unit does not set that up.
