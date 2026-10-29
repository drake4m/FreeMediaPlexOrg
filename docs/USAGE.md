# Daily use

- [At a glance](#at-a-glance)
- [Reading a report](#reading-a-report)
- [What the timer logs](#what-the-timer-logs)
- [The review folder](#the-review-folder)
- [Skipped downloads](#skipped-downloads)
- [Undo and recovery](#undo-and-recovery)
- [Schedule: change, pause, run now](#schedule-change-pause-run-now)
- [Logs and reports](#logs-and-reports)
- [Disk usage stays bounded](#disk-usage-stays-bounded)
- [Exit codes](#exit-codes)
- [What to back up](#what-to-back-up)

The examples use the layout `/srv/media/{downloads,movies,tv}`. Reports, `runs`, `undo` and `identify`
below are real outputs of mediaorg 2.0.0 on a test tree with fictional names (paths shortened); the
systemd lines of `status` show the format only.

## At a glance

```console
$ sudo mediaorg status
mediaorg 2.0.0 (Python 3.13.16)
configuration: /etc/mediaorg/config.toml
  downloads  /srv/media/downloads
  movies     /srv/media/movies
  tv         /srv/media/tv
  review     /srv/media/downloads/_review
  state      /var/lib/mediaorg
  finished downloads: unchanged for 10 min
  mount check: none
  tinyMediaManager: disabled
systemd:
  mediaorg.service             static    inactive  last run ended Mon 2026-10-26 10:35:01 UTC (exit 0, success)
  mediaorg.timer               enabled   active    next Mon 2026-10-26 10:40:00 UTC; last Mon 2026-10-26 10:35:00 UTC
  mediaorg-tmm-sync.service    not installed
  mediaorg-tmm-sync.timer      not installed
state: 2 report(s) (7 KiB), 1 journal(s), 28918 MB free
latest report: 20261026T103500Z-96c2 (apply): items ignored 2, ready 2, skip 1; renames 3
```

To see everything mediaorg would do right now, without changing anything: `sudo mediaorg run`.

## Reading a report

Every run prints a report: one block per entry of Downloads, then a summary line. A dry-run
(`sudo mediaorg run`) ends with `DRY-RUN (nothing changed ...)`, an applied run with `APPLY:`.

```text
[READY] A.Film.2023.1080p.BluRay.x264  -- 2 leftover file(s) -> review/leftovers; sample/trailer video(s), deleted with the leftovers: 'Sample/a.film.2023.sample.mkv'
    move      A.Film.2023.1080p.BluRay.x264.mkv -> movies/A Film (2023)/A.Film.2023.1080p.BluRay.x264.mkv
              A Film (2023): new movie folder 'A Film (2023)'
    rename    downloads/A.Film.2023.1080p.BluRay.x264 -> downloads/_review/leftovers/20261026T004209Z-96c2/A.Film.2023.1080p.BluRay.x264  (leftovers)
[IGNORED] Other.Show.S01E01.1080p.WEB.mkv.part  -- no video files
[SKIP] Some Name.mkv  -- 1 of 1 main video(s) not handled; item stays in Downloads
    skip      Some Name.mkv
              not identified: 'Some Name': no year and no release information (resolution, source, codec) in the name; not filed as a movie
[READY] Some.Show.S02E05.1080p.WEB.h264-GRP.mkv
    move      Some.Show.S02E05.1080p.WEB.h264-GRP.mkv -> tv/Some Show (2019)/Season 2/Some.Show.S02E05.1080p.WEB.h264-GRP.mkv
              Some Show S02E05: show folder 'Some Show (2019)'
[IGNORED] Tool Setup  -- no video files

APPLY: items: ignored 2, ready 2, skip 1 | renames 3 (media 2, subtitles 0, duplicates 0, leftovers 1), deletes 0 (identical downloads 0, empty library files 0), mkdir 5, rmdir 0
INFO report saved to /var/lib/mediaorg/runs/20261026T004209Z-96c2.json
INFO run 20261026T004209Z-96c2: 8 operation(s) done, 0 failed
```

| Status | Meaning | What happens to the entry |
|---|---|---|
| `READY` | identified and finished | filed; what remains of it (`.nfo`, samples, extras) goes to the review folder, so the entry leaves Downloads |
| `PARTIAL` | some of its videos are identified, others are not | the identified ones are filed; the entry stays in Downloads with the rest |
| `WAIT` | not finished: still downloading, still seeding (Freebox; see `completeness.on_seeding`), or changed less than `completeness.min_quiet_minutes` ago | nothing; looked at again in the next run |
| `SKIP` | not identified with certainty; the reason is given | stays in Downloads, untouched |
| `IGNORED` | not a video download (software, documents, an unfinished `.part` file, a lone sample) | never touched |

The operations of a block: `move` (into the library), `rename` (whole folder to the review folder),
`duplicate` (to the review folder: the library has this episode or movie in another file), `delete` (only
a byte-identical copy of a library file, or an empty 0-byte library file it replaces) and `skip`.
[How it works](HOW-IT-WORKS.md) explains the rules. The same report is saved as JSON in
`/var/lib/mediaorg/runs/<run-id>.json`.

`--only NAME ...` restricts a run to the named entries of Downloads (exact names, any letter case). Such a
run cleans neither the review folder nor tMM's trash; the next full run does:

```bash
sudo mediaorg run --apply --only "Some.Show.S02E05.1080p.WEB.h264-GRP.mkv"
```

## What the timer logs

The timer runs `mediaorg run --apply --quiet`. A quiet run prints only:

- the entries it acts on (`READY`, `PARTIAL`);
- entries that are new or whose state changed since the previous timer run (a new download appears as
  `WAIT`, a `WAIT` becomes `SKIP` ...): a skipped entry is reported once, not every 5 minutes;
- review folders being cleaned (`[PURGE]`) or kept for you (`[KEPT]`, once), tMM's trash (`[TRASH]`);
- warnings and errors.

A run in which nothing changed prints nothing and saves no report. So the journal can stay silent for
hours: that is normal. The full picture is always one dry-run away (`sudo mediaorg run`); a dry-run never
hides a change from the next timer run.

## The review folder

`paths.review` (default `<downloads>/_review`) receives, in one sub-folder per run:

- `duplicates/<run-id>/`: downloads of an episode or movie the library already has in another file (another
  quality, a REPACK, the same size but different bytes), with their subtitles;
- `leftovers/<run-id>/`: what remained of a filed download: `.nfo`, images, samples, extras and bonus
  videos, subtitles that could not follow their video, videos of other types.

A download that is **byte-for-byte identical** to the library file is not kept: it is deleted (`delete` in
the report; `review.delete_identical_duplicates = false` sends it to review instead).

To keep no duplicates at all, `sudo mediaorg config set review.delete_duplicates=true`: a download of an
episode or movie the library already has is then deleted in the run that finds it, even when its bytes
differ, as long as it is the same episode (or the same film of the same year), not a REPACK/PROPER, not a
special edition (Extended, Director's Cut...), not one part of a split file, and not larger than the library
copy (`delete ... not larger than the library copy: deleted from Downloads (review.delete_duplicates)`).
Right before the deletion every library file of that episode or film must still be there, unchanged. A
bigger or repaired copy may be the better one: it goes to review and is kept until you decide. Its subtitles
are not put next to the library copy (they may not match it): they go to `leftovers`. Certain duplicates
that earlier runs moved to review are deleted by the next run.

What "the same" means, so that you know what you give up:

- An episode is matched by its show's title: its show folder is the one folder of that title, so a release
  without a year is deleted. A release that names a year is kept unless the show folder has that same year
  (an undated folder may hold another show of that title).
- A film needs the year in the release name, equal to the movie folder's.
- Languages and qualities are not compared: a smaller French copy of a film you have in English is deleted.
  A copy with more audio tracks (`MULTi`) is usually bigger, and then kept.
- `review.delete_identical_duplicates = false` cannot be combined with it (that would keep the identical
  copies while deleting the others).

Example, a second copy and a REPACK of an episode that is already filed:

```text
[READY] Some.Show.S02E05.1080p.WEB.h264-GRP.mkv
    delete    Some.Show.S02E05.1080p.WEB.h264-GRP.mkv
              Some Show S02E05: already in library: S02E05 as 'Some.Show.S02E05.1080p.WEB.h264-GRP.mkv'; byte-identical copy, deleted from Downloads
              note: show folder 'Some Show (2019)'
[READY] Some.Show.S02E05.REPACK.1080p.WEB.h264-OTHER.mkv
    duplicate Some.Show.S02E05.REPACK.1080p.WEB.h264-OTHER.mkv -> downloads/_review/duplicates/20261026T004226Z-a5b4/Some.Show.S02E05.REPACK.1080p.WEB.h264-OTHER.mkv
              Some Show S02E05: already in library: S02E05 as 'Some.Show.S02E05.1080p.WEB.h264-GRP.mkv' (this release is a REPACK/PROPER)
              note: show folder 'Some Show (2019)'
              note: kept in review until you decide: it is a REPACK/PROPER release
```

**Cleaning.** A run folder is cleaned once its run is older than the retention (`review.duplicates_retention_days`,
`review.leftovers_retention_days`, 7 days by default; 0 = at the end of the same run, -1 = never):

- in `duplicates/`, only the duplicates the run marked as **certain** are deleted: the release names the
  same year as the library folder, it is not a REPACK/PROPER, not one part of a split file, and not larger
  than the library copy. Any other duplicate is kept; its report line says why;
- in `leftovers/`, only release clutter is deleted: `.nfo .txt .sfv .md5 .sha1 .sha256 .sha512 .par2 .url
  .lnk .htm .html .diz .log .xml .json .db .ini` files and subtitles under 10 MiB, `Thumbs.db`,
  `.DS_Store`, `desktop.ini`, images under 1 MiB, and sample or trailer videos under 300 MiB;
- everything else (any other video, archives, programs, documents, photos, files without extension) is
  **kept** and listed once as `[KEPT]` until you delete it. Emptied folders are removed.

To keep a file for good, move it out of its `<run-id>` folder, for example into `_review/keep/`: only
folders named exactly like a run id are ever cleaned. Nothing in the review folder is filed again.

## Skipped downloads

`SKIP` and `PARTIAL` entries stay in Downloads with their reason. The usual fixes:

- rename the file or folder so that it says what it is: `Show.S01E05.mkv`, `Movie (2019)`;
- add the year when several library folders have the same title, or fix a show folder whose year differs
  from the release;
- merge duplicate season folders in the library (`Season 2` and `Season 02`);
- or move the file into the library yourself: mediaorg never needs anything from an entry it skipped.

Check how a name will be read before renaming (no file is touched):

```console
$ mediaorg identify "Some.Show.S02E05.1080p.WEB.h264-GRP.mkv" "Some Name.mkv"
Some.Show.S02E05.1080p.WEB.h264-GRP.mkv
    -> episode: Some Show S02E05  [file]
Some Name.mkv
    -> NOT IDENTIFIED: 'Some Name': no year and no release information (resolution, source, codec) in the name; not filed as a movie
$ mediaorg identify "05 - Pilot.mkv" --folder "Some Show Complete/Season 2"
05 - Pilot.mkv
    -> episode: Some Show S02E05  [folders]
       note: season 2 from folder 'Season 2', show from 'Some Show Complete'
```

`sudo mediaorg library` shows how the library itself is read (shows, seasons, episodes, empty files).
More symptoms and fixes: [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## Undo and recovery

Every run that changes something has a journal. List the runs, look at what an undo would do, then do it:

```console
$ sudo mediaorg runs
20261026T004226Z-a5b4  run   end=ok  {'done': 4}
20261026T004209Z-96c2  run   end=ok  {'done': 8}
$ sudo mediaorg undo 20261026T004209Z-96c2
would undo: rename /srv/media/tv/Some Show (2019)/Season 2/Some.Show.S02E05.1080p.WEB.h264-GRP.mkv -> /srv/media/downloads/Some.Show.S02E05.1080p.WEB.h264-GRP.mkv
would undo: rmdir /srv/media/tv/Some Show (2019)/Season 2
...
DRY-RUN: add --apply to reverse these operations
$ sudo mediaorg undo 20261026T004209Z-96c2 --apply
```

- The dry-run lists the inverse operations read from the journal; it does not look at the disk.
- With `--apply`, undo goes newest first and never overwrites anything. An operation whose file is no
  longer where the run put it (moved or renamed since, by tMM for example), or whose old place is taken
  again, fails and is reported; the other operations are still done.
- It re-creates the empty library files the run replaced.
- A download deleted as an identical copy is not restored (`note: not restored`): the library file named
  in the note holds the same bytes.
- What the review cleaning or tMM's trash cleaning deleted cannot be brought back.

**Interrupted runs** (reboot, power cut, share gone in the middle of a run): the next run first reconciles
the journal with what is on disk (which moves happened, which did not), then sends what remains of a
half-processed download to `_review/leftovers`. Moves are renames, so no half-copied file can exist.
Exit code 5 means the journal contradicts the disk: look at the two paths in the message, fix by hand,
then run `sudo mediaorg recover`.

## Schedule: change, pause, run now

```bash
sudo mediaorg config set schedule.run_every_minutes=15    # the timer is rewritten automatically
sudo mediaorg config set schedule.tmm_sync_time=05:07     # nightly tMM sync ("" = none)
sudo systemctl stop mediaorg.timer       # pause until the next boot (disable --now: until re-enabled)
sudo systemctl start mediaorg.timer      # resume
sudo systemctl start mediaorg.service    # one run now, exactly as the timer runs it
systemctl list-timers 'mediaorg*'        # next and last runs
```

1, 2, 3, 4, 5, 6, 10, 12, 15, 20 or 30 minutes and 1, 2, 3, 4, 6, 8, 12 or 24 hours run at fixed times of
the day (every 5 minutes: `:00`, `:05` ...; every 2 hours: `00:00`, `02:00` ...; every 24 hours: at
midnight). Other values (7, 45 or 90 minutes ...) count from the end of the previous run. A run missed
while the machine was off runs soon after the next boot. The quiet time for files copied by hand is
`completeness.min_quiet_minutes`, not the timer. A run started by hand while the timer's run is active
(or the other way round) stops with exit code 4, which the timer treats as success.

Do not edit the unit files: `service install` rewrites them. A local addition goes into a drop-in
(`sudo systemctl edit mediaorg.service`), which is kept. Set the schedule with `config set`, not in a
drop-in: a drop-in that sets `OnCalendar=` or `OnUnitInactiveSec=` overrides `schedule.*`, and
`sudo mediaorg check` warns about it.

## Logs and reports

```bash
sudo journalctl -u mediaorg.service -n 100 --no-pager    # the latest changes
sudo journalctl -u mediaorg.service --since today -p warning
sudo journalctl -u mediaorg-tmm-sync.service -n 20       # the nightly tMM sync
systemctl --failed                                       # failed runs (and a share that failed to mount)
sudo ls -t /var/lib/mediaorg/runs | head                 # JSON report of each run
```

Warnings and errors carry journal priorities, so `-p warning` shows only them. To also keep a plain log
file, rotated by size: `sudo mediaorg config set logging.file=/var/log/mediaorg/mediaorg.log` (and
`logging.max_size_mb`, `logging.backups`).

## Disk usage stays bounded

| What | Where | Bound |
|---|---|---|
| run reports | `/var/lib/mediaorg/runs` | deleted after `reports.retention_days` (30), at most `reports.max_reports` (2000); quiet runs that change nothing save none |
| journals (undo) | `/var/lib/mediaorg/journal` | journals of finished runs deleted after `reports.journal_retention_days` (365); a journal of an interrupted run is kept until reconciled |
| log file (optional) | `logging.file` | rotated at `logging.max_size_mb` (10), `logging.backups` (5) kept |
| systemd journal | journald | journald's own limits (`SystemMaxUse=`), plus a rate limit in the unit |
| caches and state | `/var/lib/mediaorg/*.json` | rewritten by each run with only the entries still in use |
| review folder | `paths.review` | cleaned by retention as described above; files you keep are yours to delete |

Before a run moves anything, it checks that `paths.state_dir` has `safety.min_free_mb` (100) free, because
every move is journaled there first; with less, it stops with exit code 3 and moves nothing.

## Exit codes

| Code | Meaning | What to do |
|---|---|---|
| 0 | ok (also: nothing to do) | |
| 1 | an operation failed (a move or deletion); the others were done | read the `ERROR` lines; the next run retries |
| 2 | usage or configuration error, or a broken installation | `sudo mediaorg config validate`; run `install.sh` again for a broken installation |
| 3 | precondition: share not mounted or not answering, Freebox or download state unknown, too little free space | `sudo mediaorg check` |
| 4 | another run holds the lock | nothing (the timer treats it as success) |
| 5 | an interrupted run needs a person: its journal contradicts the disk | fix the two paths named, then `sudo mediaorg recover` |
| 6 | the plan has more moves and deletions than `safety.max_moves_per_run` (50) | look at the dry-run, then `sudo mediaorg run --apply --max-moves N` once |
| 130 | interrupted (Ctrl-C, stopped) | nothing: the next run reconciles |

## What to back up

- `/etc/mediaorg/` (configuration, tMM API key);
- `/var/lib/mediaorg/` (the Freebox app token, the journals needed for undo);
- your fstab line and the credentials file of a network share;
- with the Docker helper for tinyMediaManager: `/etc/tmm/` and tMM's data folder ([TMM.md](TMM.md)).
