# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [semantic versioning](https://semver.org/).

## [Unreleased]

## [2.3.0] - 2026-10-30

### Added

- `review.replace_duplicates = true`: a download of an episode or film the library already has takes the place
  of the library video, whatever its size or release: the newest download wins. Only the video is replaced:
  the new one keeps its own name (tMM's next update sees a new file, and scrapes and renames it), the
  library's NFO, artwork and subtitles stay, and the download's other files go to `review/leftovers`
  (`leftovers_retention_days = 0` deletes their release clutter at the end of the run). The old video is
  moved to `review/replaced/<run-id>/` under its library path, only while the new one is intact, and
  deleted after the new `review.replaced_retention_days` (1 by default; 0 = at the end of the run), only if
  its replacement reached the library; `undo` brings it back until then. The identity rules of `delete_duplicates` apply (same episode; same film of the same year; a country
  named by the release must be the folder's). A download named like bonus material (recap, featurette,
  preview ...) or smaller than a tenth of the library video is kept in review instead; a split part, several
  library files for that episode or film, or a library file holding other episodes too keep the download
  from replacing it, and the report says why. `status` shows the rule.

## [2.2.0] - 2026-10-30

One new opt-in setting, the rest are fixes; without `review.delete_duplicates`, duplicates are handled as in
2.1.0. Includes 2.1.0, which was not published as a release of its own.

### Added

- `review.delete_duplicates = true`: a download the library already has is deleted in the run that finds it,
  even when its bytes differ: the same episode, or the same film of the same year, not a REPACK/PROPER, not a
  special edition (Extended, Director's Cut...), not one part of a split file, and not larger than the
  library copy; REPACK/PROPER and editions count in the file name and in any folder of the download. An
  episode needs no year in its name (its show folder is the one folder of that title); a release naming a
  year, or a country (`The.Office.US`), is kept unless the show folder names it too. Languages are not
  compared. Right
  before the deletion every library file of that episode or film must still be there, unchanged. The
  subtitles of such a download are not put next to the library copy (they may not match it): they go to
  `review/leftovers` with the rest. Other duplicates (a bigger or repaired copy, a year that cannot be
  confirmed) go to `review/duplicates` and are kept until you decide; certain duplicates that earlier runs
  moved there are deleted by the next run. `status` shows the rule in force; `undo` lists such deletions as
  not restorable. It cannot be combined with `review.delete_identical_duplicates = false`.
- Freebox: a download whose task disappeared from the Freebox (removed in Freebox OS, its files kept) is filed
  when the task had received everything when a run last saw it, even with `completeness.untracked = "wait"`,
  and never when it had not, or was in error or being verified (its files may be incomplete), even with
  `untracked = "quiescence"`, until the download is added again in Freebox OS and its new task finishes. The
  same holds for one of several tasks writing into a folder, and for a subtitle downloaded by a task of its
  own next to its video. Files a removed task never fully received (deselected ones) stay in Downloads, also
  while other tasks write into the same folder. A task the Freebox still lists, but whose files are not
  matched to its download in a run, makes the download wait unless the task is finished; a file another task
  has since received in full is no longer held. Only tasks whose files were matched to an entry are recorded
  for it. What the Freebox last said about the tasks of each entry of Downloads is kept in
  `<state_dir>/freebox_seen.json`, and forgotten when the entry leaves Downloads; a record older than the
  entry's files does not decide.

### Fixed

- `completeness.on_seeding = "stop"`: a stop request refused because the task reached its stop ratio
  (`done`) or was paused just before no longer leaves the download waiting for the next run: the task is read
  again and the download filed in the same run; a task removed meanwhile is reported as gone, and its
  download filed by the next run.
- `completeness.on_seeding = "stop"`: a task the Freebox says it cannot find while still listing it is
  reported as not stopped (it keeps seeding), not as removed; when one task of a download reaches its ratio
  during the stop, the download's other tasks are checked again before going on (not those the run has
  just stopped); a `done` task counts as finished at any percentage, as it does for filing.
- A REPACK/PROPER or a special edition named only by the folder of a download (or only by its file, when the
  folder gave the identity) no longer counts as a certain duplicate: it is kept in review. This also
  applies without `review.delete_duplicates` (certain duplicates are deleted from review after
  `duplicates_retention_days`).
- A Freebox task listed with an empty name or status no longer stops the run.
- A download deleted from Downloads during a run (for example removed in Freebox OS with its files) is
  skipped with a warning instead of failing the run (exit 1); no library folder is created for it. When the
  Downloads folder itself disappears (share unmounted), the run stops with exit 3 as for any storage failure.
- A subtitle the Freebox did not fully receive (deselected, for example) is no longer filed next to its
  video: it goes to `review/leftovers`.
- `install.sh --rollback` checks that the previous version can read the configuration the units use and
  `/etc/mediaorg/config.toml`, and refuses, changing nothing, when it cannot (the message names the file and
  how to unset the setting). It no longer advises restoring `config.toml.bak`, which after a `config unset`
  holds the newer settings.
- `check`: a folder still ending with `[mediaorg]` that holds no video (of any video type) is reported as
  such (tMM ignores it) instead of as "not renamed after a day"; a folder that cannot be read entirely is
  not.
- `contrib/fix-case-renames.sh`: skips folders that hold no video, and gives each folder it renames today's
  date, so that `check` counts the day from the repair.

### Changed

- The run summary counts the duplicates deleted by `review.delete_duplicates` when there are any:
  `deletes N (identical downloads X, other duplicates Y, empty library files Z)`; the JSON report's summary
  has `dup_deleted`.

## [2.1.0] - 2026-10-29

Two opt-in settings; with neither set, 2.1.0 files exactly as 2.0.0.

### Added

- `completeness.on_seeding = "stop"` (Freebox): a run that files a finished torrent still seeding first
  stops its task through the Freebox OS API (`PUT downloads/{id}`), waits up to 15 seconds for the Freebox
  to list it as stopped, then moves it. Dry-runs, runs that cannot move anything (safety cap, busy tMM, low
  free space, missing mount) and downloads left in Downloads stop nothing; no task of a download is stopped
  unless all of them are finished or seeding at 100 %; a stop that is refused, unanswered or not confirmed
  leaves the download `WAIT`. A task covers its whole torrent (the unfiled files of a `PARTIAL` download stop
  seeding with it).
  `check` accepts a BitTorrent stop ratio of 0 with it. Needed when torrents with few peers never reach the
  stop ratio.
- `naming.tmm_staging = true` (with `tmm.enabled`): new movie and show folders are named
  `Title (Year) [mediaorg]` until tinyMediaManager renames them (tMM ignores text in `[...]` when it reads a
  folder name). On Linux with the
  library on an SMB/CIFS share, tMM cannot rename a folder whose new name differs only in letter case
  (`Rise of the Conqueror` -> `Rise Of The Conqueror`): its rename fails with `NoSuchFileException` and the
  movie keeps its names. With the tag, its rename always changes more than the case. Folders with the tag
  are matched like any other; `check` reports those still tagged after a day, and advises the setting when
  tMM is enabled and the library is on an SMB share.
- `contrib/fix-case-renames.sh`: repairs, once, the library folders tMM could not rename because only the
  letter case differs (dry-run by default; renames them to the tagged name, then sends the tMM calls).

### Changed

- A `WAIT ... Freebox task is seeding` line now names `completeness.on_seeding = "stop"`.
- `status` shows when seeding is stopped and when new folders are staged for tMM; `check` warns when
  `completeness.on_seeding = "stop"` is set without the Freebox, or `naming.tmm_staging` without tMM.
- Freebox API answers cut short or malformed are reported as Freebox errors (the run stops cleanly)
  instead of ending the run with a traceback.

## [2.0.0] - 2026-10-26

First public release. The 1.x releases were private; [UPGRADING.md](docs/UPGRADING.md) covers moving from
them.

### Added

- **Configuration from the command line.** `mediaorg config init` writes a configuration from a few
  answers (interactive, or flags for scripts) and adds a mount check when the media folders are on their
  own mount. `config set`, `unset`, `get`, `show`, `keys`, `validate` and `path` manage it: every value is
  checked, the whole file is validated before it is written, the previous file is kept as `.bak`, and the
  file documents every key.
- **Watched-folder mode** (`completeness.method = "quiescence"`): works with any download client and with
  files copied by hand. The Freebox is now optional.
- **Generated systemd units**: `mediaorg service install [--enable]`, `remove`, `status`, `show`. The units
  carry the folders they may write (the state folder and the one folder that holds Downloads, the
  libraries and the review folder), the mounts they need, the schedule
  (`schedule.run_every_minutes`, `schedule.tmm_sync_time`) and a strict sandbox. They are rewritten when a
  setting they depend on changes; the nightly tinyMediaManager timer is added, and enabled along with
  `mediaorg.timer`, when tMM is enabled. A run missed while the machine was off runs at the next boot.
- `mediaorg status`: version, configuration, timers and the latest run at a glance.
- `mediaorg tmm set-key`: stores tinyMediaManager's API key from a hidden prompt, standard input, tMM's
  `tmm.json`, or a newly generated key; never from the command line.
- **Bounded disk usage**: optional log file rotated by size (`[logging]`), at most `reports.max_reports`
  run reports, journals of finished runs deleted after `reports.journal_retention_days`, a free-space
  check before any move (`safety.min_free_mb`), a log rate limit in the units.
- `install.sh --uninstall [--purge]`, interpreter selection (`PYTHON=`), and validation of an existing
  configuration with the new version before an upgrade changes the program or the units.
  `install.sh --rollback` also restores the previous version's systemd units (the fixed unit files of 1.x
  included).
- `mediaorg check` warns when a drop-in overrides the timer schedule (as 1.x documented), and lists other
  drop-ins.
- A generic Docker deployment of tinyMediaManager (`contrib/tmm-docker/`), configured in `/etc/tmm/tmm.conf`.
- Documentation: installation (quick and full), configuration, daily use, how it works, Freebox,
  tinyMediaManager, troubleshooting, upgrading; the command and configuration references are generated
  from the program.
- Continuous integration on Python 3.11 to 3.14 (tests, ruff, shellcheck, generated documentation).

### Changed

- The default of `completeness.method` is now `"quiescence"`; a configuration that sets
  `freebox.remote_downloads_dir` must set it, or it is refused.
- Commands run with `sudo` switch to the `mediaorg` account by themselves.
- Command names: `freebox authorize`, `freebox status`, `tmm sync`, `tmm status` (the 1.x names still work).
- The nightly tMM sync runs at `schedule.tmm_sync_time` in the machine's local time.
- New library folders get the permissions of the folder they are created in, so a library shared with a
  media server stays readable.
- `config set` and `config unset` leave the file (and its `.bak`) alone when nothing changes.

### Removed

- Site-specific paths, addresses and settings; every folder and address is now configuration.
- The static unit files (replaced by `service install`).

### Security

- Configuration, key and token files that other users could change (or read, for secrets) are refused
  each time they are read.
- The review-folder cleaning deletes through folder handles opened without following links: a folder
  swapped for a link while it runs, or a run folder replaced since it was listed, is left alone.
- The configuration's `.bak` copy is written as a new file, never through an existing link.
- The tinyMediaManager Docker helper hands its data folder to tMM's account only when it is a folder of its
  own: system folders, `/tmp`, mediaorg's folders, the libraries and folders holding other files are
  refused, and an existing parent folder keeps its owner and mode.
- Plain `http://` to a tinyMediaManager on another machine is refused unless explicitly allowed.
- The units run without capabilities, with a read-only system, private `/tmp` and devices, a system-call
  filter, and write access only to the state folder and the folder that holds the media folders.

[Unreleased]: https://github.com/drake4m/FreeMediaPlexOrg/compare/v2.1.0...HEAD
[2.1.0]: https://github.com/drake4m/FreeMediaPlexOrg/compare/v2.0.0...v2.1.0
[2.0.0]: https://github.com/drake4m/FreeMediaPlexOrg/releases/tag/v2.0.0
