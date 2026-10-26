# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [semantic versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/drake4m/FreeMediaPlexOrg/compare/v2.0.0...HEAD
[2.0.0]: https://github.com/drake4m/FreeMediaPlexOrg/releases/tag/v2.0.0
