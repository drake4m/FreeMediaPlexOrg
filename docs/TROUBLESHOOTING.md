# Troubleshooting

Start with the read-only checks: `sudo mediaorg check` (every `[FAIL]` line names the problem and usually
the fix), `sudo mediaorg status`, then a dry-run, `sudo mediaorg run`, which shows the decision and the
reason for every entry of Downloads. Exit codes: [USAGE.md](USAGE.md#exit-codes).

- [Setup and permissions](#setup-and-permissions)
- [Storage and mounts](#storage-and-mounts)
- [Downloads that are not filed](#downloads-that-are-not-filed)
- [Review folder and deletions](#review-folder-and-deletions)
- [Timers](#timers)
- [Freebox](#freebox)
- [tinyMediaManager](#tinymediamanager)
- [Reporting a problem](#reporting-a-problem)

## Setup and permissions

| Symptom | Cause | Fix |
|---|---|---|
| `this changes /etc/mediaorg/config.toml: run it with sudo` | setup commands write root-owned files | use `sudo` |
| `cannot read /etc/mediaorg/config.toml: permission denied (use sudo)` | the configuration is readable by root and the `mediaorg` group only | use `sudo` |
| `config file not found` | no configuration yet | `sudo mediaorg config init` |
| `... is writable by every user; fix: sudo chmod o-w ...`, `... is readable by every user; fix: sudo chmod o-rwx ...` | the configuration, the tMM API key or the Freebox token could be changed (the key and token also read) by every user: such a file is refused whenever it is read | run the `chmod` shown |
| `WARNING: the service account 'mediaorg' cannot read and write: ...` (from `config init`), or `[FAIL] no read/write access to ...` | the `mediaorg` account has no access to a media folder | give it access: group and setgid folders, an ACL, or the `uid=`/`gid=` options of a network mount ([INSTALL.md](INSTALL.md#2-storage-access-for-the-mediaorg-account)) |
| `[FAIL] paths.movies ... is on a different filesystem than ...` | Downloads and the library are on different disks or shares | put them on one filesystem: files are moved with renames, never copied |
| `paths.downloads ... resolves elsewhere` | a path through a symbolic link | configure the real path (`readlink -f`) |
| `ERROR installation is broken (No module named ...)` (exit 2) | damaged virtual environment or a partial install | run the release's `install.sh` again (it recreates a damaged environment) |
| `configuration error with the new version: ...` and `the program and its units were not changed` during an upgrade | the new version does not accept a setting | the installed version still runs; fix the setting it names with `sudoedit /etc/mediaorg/config.toml` (from 2.x, `sudo mediaorg config set ...` works too), then run `install.sh` again |

## Storage and mounts

| Symptom | Cause | Fix |
|---|---|---|
| exit 3, `... is not a mount point (NAS not mounted?) - refusing to touch it` | the share is not mounted | `findmnt <mount point>`; `sudo mount <mount point>`; check the NAS, the network and the credentials |
| `Dependency failed for ...` in `sudo journalctl -u mediaorg.service`, a failed `.mount` unit in `systemctl --failed` | the run needs the mount (`RequiresMountsFor=`), which failed, so systemd did not start it | as above; the next timer run tries the mount again |
| exit 3, `... comes from '//other/share', expected ...`, `mounted as 'nfs4', expected 'cifs'` | another share or disk is mounted there | mount the right one, or update `mount.*` if the change is intended |
| exit 3, `NAS stopped answering during the run` | the share went away during a run | nothing: the next run reconciles the journal before doing anything else |
| exit 3, `precondition failed: only N MB free in /var/lib/mediaorg` | the local disk is almost full | free space; the threshold is `safety.min_free_mb` |
| `Read-only file system` or `Permission denied` only in timer runs, for a folder outside the configuration | the service may write only its state folder and the folder that holds the media folders (systemd sandbox) | configure that folder; after editing the file by hand, run `sudo mediaorg service install` (`config set` does it by itself) |
| `... have no common folder other than /` (`service install`, `config set`, `[FAIL]` in `check`) | Downloads, the libraries or the review folder sit in different top-level folders; the sandbox makes one folder holding them all writable, so that a move stays a rename inside one mount | put them in one folder (`/srv/media/downloads`, `/srv/media/movies` ...); manual runs work meanwhile |
| `[warn] the installed systemd units run another configuration: ...` | `service install` was run with another `--config` | `sudo mediaorg --config <the file you want> service install` |
| `[warn] systemd units differ from the configuration` | the configuration file was edited by hand | `sudo mediaorg service install` |

## Downloads that are not filed

| Symptom | Cause | Fix |
|---|---|---|
| `WAIT ... modified N min ago` | watched-folder mode: the download changed less than `completeness.min_quiet_minutes` ago | wait; lower the quiet time with `config set` (minimum 5) |
| `WAIT ... temporary download file present` | a `.part`, `.!qb` ... file in the download | let the client finish; if the file is a leftover of an abandoned download, delete it |
| `IGNORED ... no video files` | nothing in it has a video extension (an unfinished `X.mkv.part`, software, documents) | nothing; a finished `X.mkv` is filed after the quiet time. Other video types: `media.video_extensions` |
| `IGNORED ... named or filed like samples/trailers/extras` | the only video of the entry looks like a sample, trailer or bonus file | if it is the real episode or movie, rename it or file it by hand |
| `SKIP ... no year and no release information` | a loose `Some Name.mkv` may not be a movie at all | rename it `Title (Year).mkv`, or put it in a folder `Title (Year)` |
| `SKIP ... season and episode read from a bare number` | no `SxxEyy`, `NxNN` or season folder, only digits | rename it (`Show.S01E05.mkv`) or put it in a `Season N` folder |
| `SKIP ... several files of this download are SxxEyy` | parts, versions or bonus material named like the episode | keep the right file (file it by hand, or delete the other one) |
| `SKIP ... folder says '...' but the file says '...'` | a dated folder holding another film | rename the folder, or move the file out of it |
| `SKIP ... is filed from another download in this run` | two downloads of one episode or movie arrived together | nothing: the next run compares it with the filed copy |
| `PARTIAL` with `Recap 05.mkv`, `Deleted Scene 05.mkv` ... | bonus files of a season pack that are not named as extras | delete them, or move them into the pack's `Extras` folder; the pack then leaves Downloads |
| a show folder year conflict, several possible folders | the library has `Show (2002)` and the release says 2017, or two folders fit | rename or merge the library folders, or add the year to the release name |
| a movie went to `Movies/Title/` without year | no year in its name | nothing (tinyMediaManager adds it when it renames), or rename the folder |
| exit 6, `plan has N moves/deletions, above the safety cap` | many downloads at once, after a long pause | look at the dry-run, then `sudo mediaorg run --apply --max-moves N` once |
| exit 5 | the journal of an interrupted run contradicts the disk | look at the two paths named, fix by hand, then `sudo mediaorg recover` |
| exit 4 | another run holds the lock | nothing; wait for it |

`mediaorg identify "<name>" --folder "<folder>"` shows how a name is read; [How it works](HOW-IT-WORKS.md)
has the rules.

## Review folder and deletions

| Symptom | Cause | Fix |
|---|---|---|
| a download disappeared from Downloads but nothing new is in the library | it was byte-identical to a library file and deleted (`delete` in its run's report) | nothing to fix: the library has the same bytes. `sudo grep -rl identical-duplicate /var/lib/mediaorg/journal` lists the runs that did it; `review.delete_identical_duplicates = false` keeps such copies in review instead |
| a review folder is gone | cleaned after its retention | `review.*_retention_days` (-1 = never); to keep files, move them out of the `<run-id>` folder |
| a review folder stays, listed as `[KEPT]` | it holds files that are never deleted automatically (videos, uncertain duplicates, archives, documents, photos) | look at them, keep what you want, delete the folder |
| a duplicate stays in review after the retention | it was not certain; its line in the run's report says why (no year, REPACK/PROPER, part of a split file, bigger than the library copy) | compare it with the library copy, keep the better one |
| files disappear from `.deletedByTMM`, `[TRASH]` lines | tinyMediaManager's trash is emptied by retention | `review.tmm_trash_retention_days` (-1 = never); move a file out of `.deletedByTMM` to keep it |
| `could not remove the empty folder .../.deletedByTMM/...` | the storage refuses to remove it (permissions, an attribute) | remove it by hand; the warning comes back only when the list of such folders changes |
| `tMM trash: N entry(ies) cannot be read or are not files; left alone` | an entry of `.deletedByTMM` that cannot be listed, or is not a regular file or link | look at the paths named; fix or delete them by hand |
| a file of tMM's trash is due but logged as left, `No such file or directory` although `ls` shows it | on a Freebox share: a pending deletion ([FREEBOX.md](FREEBOX.md#freebox-restart-internet-outage)) | it goes when the other program closes it, at the latest when the Freebox restarts |

## Timers

| Symptom | Cause | Fix |
|---|---|---|
| the journal shows nothing for hours | normal: timer runs log only changes | `systemctl list-timers 'mediaorg*'`; `sudo mediaorg run` shows the full state |
| no run at all | timer not installed or not enabled | `sudo mediaorg service install --enable` |
| `systemd is not running on this machine` | a container or WSL without systemd | run `sudo mediaorg run --apply --quiet` from another scheduler (cron), as root or as `mediaorg` |
| runs at the wrong times | the interval is not one of the fixed-time values, so it counts from the end of the previous run | choose 1, 2, 3, 4, 5, 6, 10, 12, 15, 20 or 30 minutes, or 1, 2, 3, 4, 6, 8, 12 or 24 hours (60 to 1440 minutes), for fixed times |
| `schedule.run_every_minutes` has no effect; `check` warns `drop-in files set the timer schedule` | a drop-in (from 1.x, or `systemctl edit mediaorg.timer`) sets `OnCalendar=` and overrides the configuration | `systemctl cat mediaorg.timer` shows it; `sudo systemctl revert mediaorg.timer` removes the timer's drop-ins (the unit file stays; the same for `mediaorg-tmm-sync.timer`) |

## Freebox

| Symptom | Cause | Fix |
|---|---|---|
| everything stays `WAIT ... Freebox task is seeding` | the stop ratio is 0 (seed forever) or not reached (few peers) | `sudo mediaorg config set completeness.on_seeding=stop` ([FREEBOX.md](FREEBOX.md#stop-seeding-when-a-download-is-filed)); or a stop ratio above 0; or stop the task by hand at 100 % |
| `WAIT ... Freebox task N is still 'stopping' ...`, `... could not be stopped (...)` | `on_seeding = "stop"`: the Freebox did not confirm the stop within 15 s, or refused it | nothing for a slow stop (the next run files it); for a refusal, the app's download manager permission (`sudo mediaorg check`) |
| exit 3, `freebox.remote_downloads_dir is ... but the Freebox downloads into ...` | the Freebox download folder changed | `sudo mediaorg freebox status`, then set `freebox.remote_downloads_dir` as it shows |
| exit 3, `freebox.remote_downloads_dir is not set` | Freebox mode without the folder | `sudo mediaorg freebox status`, then `config set` as it shows |
| `no Freebox app token at ...` | not authorized yet | `sudo mediaorg freebox authorize` |
| `app is authorised but lacks the 'downloader' permission`, or `login failed: ...` (such as `invalid_token`) | the permission was removed, or the app revoked in Freebox OS | give the permission back, or delete the token file and authorize again |
| `cannot reach Freebox API at ...` | the machine is not on the Freebox's network, or cannot resolve `mafreebox.freebox.fr` | check the network and DNS; `freebox.base_url` |
| files copied by hand stay `WAIT ... is not a Freebox download task` | `completeness.untracked = "wait"` (default) | file them by hand, or `config set completeness.untracked=quiescence` |
| `[warn] Freebox app permissions: also [...]` | the app has more permissions than it needs | untick them in Freebox OS (Gestion des accès > Applications) |

## tinyMediaManager

| Symptom | Cause | Fix |
|---|---|---|
| `[FAIL] tMM: HTTP 403 on ...: {"message":"Invalid API key"}` | the key in `tmm.api_key_file` is not tMM's | `sudo mediaorg tmm set-key` (or `--from-tmm-json`) |
| `[FAIL] tMM: tMM not listening at ...`, `cannot reach tMM at ...` | tMM is not running, its HTTP API is off, or listens elsewhere | start tMM; enable the API; `tmm.base_url` |
| `[FAIL] tMM endpoint /api/movie listed in /api/docs (listed: [...])` | a tMM version with other endpoint names | set `tmm.movie_endpoint` / `tmm.tvshow_endpoint` to the names listed |
| `tmm.base_url ... http://` refused | plain HTTP to another machine would send the key unencrypted | use `https://`, or `tmm.allow_insecure_http = true` on a network you trust |
| a new item is not scraped or renamed | no result good enough for tMM, tMM still in its first media-information pass, or a later call made tMM forget it | scrape it once in tMM; [TMM.md](TMM.md#known-limitations) |
| a movie is scraped but keeps its folder and file names; tMM's log: `Rename problem ... [NoSuchFileException]`, `Renaming aborted` | on an SMB/CIFS share tMM (Linux) cannot rename a folder whose new name differs only in letter case | `sudo mediaorg config set naming.tmm_staging=true` for new folders; repair the stuck ones once with `sudo bash /opt/mediaorg/app/contrib/fix-case-renames.sh` ([TMM.md](TMM.md#library-on-an-smbcifs-share-staging-names)) |
| `[warn] N folder(s) still end with ' [mediaorg]' after a day` | tMM did not rename them: not scraped yet, folder renaming off in tMM's renamer settings, or tMM not reached | scrape and rename them in tMM; turn tMM's folder renaming on, or `naming.tmm_staging=false` and rename the folders by hand |
| tMM busy for hours right after its setup | its first media-information pass over the whole library | wait; do not restart tMM meanwhile |
| tMM's log: `UnknownHostException: api.themoviedb.org` (Docker helper) | the container has no working DNS | `TMM_DNS_OPTS` ([TMM.md](TMM.md#optional-tinymediamanager-in-docker)) |
| `systemctl status tinymediamanager` shows `activating (auto-restart)` | the library's mount is missing; tMM waits for it | mount the share |
| `TMM_UI_BIND must be an IPv4 address`, `TMM_DNS_OPTS must be empty or --dns <IPv4> pairs` | a wrong value in `/etc/tmm/tmm.conf` | fix the line, `sudo systemctl restart tinymediamanager` |

## Reporting a problem

Open an issue with: `mediaorg --version`, your distribution and Python version, the output of
`sudo mediaorg check` and of a dry-run limited to the entry concerned
(`sudo mediaorg run --only "<entry>"`), and the relevant `sudo journalctl -u mediaorg.service` lines. Remove
anything private first (host names, share names, titles you do not want to publish). Security problems:
see [SECURITY.md](../SECURITY.md), not a public issue.
