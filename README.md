# FreeMediaPlexOrg

[![CI](https://github.com/drake4m/FreeMediaPlexOrg/actions/workflows/ci.yml/badge.svg)](https://github.com/drake4m/FreeMediaPlexOrg/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue.svg)
![Debian / Ubuntu](https://img.shields.io/badge/linux-Debian%20%7C%20Ubuntu-orange.svg)

**Files finished movie and TV downloads into a tidy library, safely.** FreeMediaPlexOrg (command:
`mediaorg`) watches one Downloads folder, recognises each finished download (title, year, season,
episode) and moves it into the layout that Plex, Jellyfin, Kodi and tinyMediaManager expect:

```text
Downloads/Some.Show.S02E05.1080p.WEB.h264-GRP.mkv  ->  TV/Some Show (2019)/Season 2/Some.Show.S02E05.1080p.WEB.h264-GRP.mkv
Downloads/A.Film.2023.1080p.BluRay.x264/           ->  Movies/A Film (2023)/A.Film.2023.1080p.BluRay.x264.mkv
```

An existing show folder is reused (here `Some Show (2019)`); a show the library does not have yet gets a new
folder, `Some Show` when the release name has no year.

It works with a **Freebox** (it asks the Freebox download manager whether a torrent is really finished)
or with **any download client or manual copies** (a download counts as finished once its files stop
changing). After each run it can ask **tinyMediaManager** to update its library, then scrape and rename
only the new items.

## Why it is safe

- **Dry-run by default.** Nothing changes without `--apply`; the report says what would happen and why.
- **Never overwrites.** Moves are instant renames on one filesystem; a target name is reserved atomically
  first, so a file that appears meanwhile is never replaced (proven on your storage by `probe-rename`).
- **Never guesses.** Anything ambiguous (two possible shows, a year that does not match, parts of a file,
  a name with no year and no release markers) stays in Downloads and is reported.
- **Deletes almost nothing, and says so first:** only byte-identical duplicates of a library file, empty
  0-byte placeholders it replaces, empty folders, and, after a retention you choose, release clutter and
  certain duplicates in its own review folder and the files tinyMediaManager put in its `.deletedByTMM`
  trash (7 days by default, also when the tMM calls are off; `-1` keeps them). Everything else is kept
  for you.
- **Journaled and undoable.** Every move is journaled before it happens; an interrupted run is repaired by
  the next one, and the moves of any run can be undone (deletions cannot).
- **Locked down.** Runs as an unprivileged account under a strict systemd sandbox; refuses to touch an
  unmounted share; secrets never on the command line; logs and state are bounded so the disk never fills.

## Requirements

| | Minimum | Notes |
|---|---|---|
| Linux | Debian 12, Ubuntu 22.04 (or a derivative) | other systemd distributions likely work; only Debian and Ubuntu are tested |
| Python | 3.11 | Ubuntu 22.04: `sudo apt install python3.11 python3.11-venv` |
| systemd | 247 | for the timers; manual runs work without it |
| Storage | Downloads and library on **one filesystem** | local disk, NAS share (CIFS/SMB, NFS), USB disk |
| Optional | Freebox with Freebox OS; tinyMediaManager 5.x | tMM can run in Docker with the included helper |

## Quick start

```bash
sudo apt install python3 python3-venv curl
VER=2.0.0
curl -fLO https://github.com/drake4m/FreeMediaPlexOrg/releases/download/v$VER/freemediaplexorg-$VER.tar.gz
curl -fLO https://github.com/drake4m/FreeMediaPlexOrg/releases/download/v$VER/freemediaplexorg-$VER.tar.gz.sha256
sha256sum -c freemediaplexorg-$VER.tar.gz.sha256 && tar -xzf freemediaplexorg-$VER.tar.gz
sudo bash freemediaplexorg-$VER/install.sh

sudo mediaorg config init          # asks for your Downloads, movie and TV folders
sudo mediaorg check                # read-only preflight
sudo mediaorg run                  # dry-run: what would happen
sudo mediaorg run --apply          # once by hand
sudo mediaorg service install --enable    # then every 5 minutes, also after a reboot
```

Everything is configured from the command line (`mediaorg config set KEY=VALUE`), every command has a
detailed `--help`, and `mediaorg status` shows the state at a glance.

## Documentation

| Guide | |
|---|---|
| [Installation](docs/INSTALL.md) | prerequisites, quick and full procedure, storage and permissions, upgrade, uninstall |
| [Configuration](docs/CONFIGURATION.md) | the `config` commands and every key with its default |
| [Daily use](docs/USAGE.md) | reports, review folder, undo, logs, schedule, exit codes |
| [How it works](docs/HOW-IT-WORKS.md) | what is filed where, what is cleaned, safety rules, known limitations |
| [With a Freebox](docs/FREEBOX.md) | share mount, app authorization, download folder, seeding |
| [tinyMediaManager](docs/TMM.md) | API key, the calls sent, trash cleaning, optional Docker deployment |
| [Troubleshooting](docs/TROUBLESHOOTING.md) | symptoms, causes, fixes |
| [Command reference](docs/CLI.md) | the full `--help` of every command |
| [Upgrading](docs/UPGRADING.md) | from 1.x, and between 2.x releases |

## Project

- Changes: [CHANGELOG.md](CHANGELOG.md) · Contributing: [CONTRIBUTING.md](CONTRIBUTING.md) ·
  Security reports: [SECURITY.md](SECURITY.md)
- License: [Apache-2.0](LICENSE). Uses [guessit](https://github.com/guessit-io/guessit) (LGPL-3.0) to parse
  release names; tinyMediaManager and Freebox are trademarks of their owners; this project is not
  affiliated with them.
