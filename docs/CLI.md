# Command reference

This page is generated from the program's own help: `mediaorg --help` and `mediaorg COMMAND --help` print
the same text (on Python 3.13 and later; older versions lay out the option lists slightly differently,
for example `-c FILE, --config FILE`).

Conventions:

- **Dry-run by default.** `run`, `undo`, `probe-rename` and `tmm sync` change nothing without `--apply`.
- **Use `sudo`.** Commands that read or move media, or talk to the Freebox or tinyMediaManager, switch to
  the unprivileged `mediaorg` account first. `config init|set|unset`, `tmm set-key` and
  `service install|remove` stay root, because they write root-owned files.
- `-c FILE` (another configuration file) and `-v` (more detail) go before or after the command name.
- The command names of 1.x still work: `freebox-authorize`, `freebox-status`, `tmm-sync`, `tmm-status`.
- `service status|show|remove`, `freebox authorize|status` and `tmm status` take no options of their own;
  they are described under their command group below.
- Exit codes: [USAGE.md](USAGE.md#exit-codes).

<!-- BEGIN GENERATED: tools/gen_docs.py -->

### `mediaorg`

```text
usage: mediaorg [-h] [--version] [-c FILE] [-v] COMMAND ...

mediaorg 2.1.0 - files finished movie and TV downloads into a tidy library, safely.

It watches one Downloads folder. Each finished download is identified (title, year, season,
episode) and moved by an instant rename into
    <movies>/Title (Year)/...      and      <tv>/Show (Year)/Season N/...
Nothing is ever overwritten; whatever is uncertain stays where it is and is reported.
tinyMediaManager can then scrape and rename what was filed. Commands that change files
are dry-runs unless --apply is given.

options:
  -h, --help         show this help message and exit
  --version          show program's version number and exit
  -c, --config FILE  configuration file (default /etc/mediaorg/config.toml or $MEDIAORG_CONFIG)
  -v, --verbose      more detail in the log

commands:
  daily operation
    run              plan the filing of finished downloads; --apply carries it out
    status           version, configuration, timers and the latest run at a glance
    check            read-only preflight: storage, permissions, mount, Freebox, tMM
    runs             recent runs (each one has a journal: it can be undone)
    undo RUN_ID      reverse a run (dry-run unless --apply)
    recover          reconcile the journal of an interrupted run
  
  inspection (read-only)
    identify NAME    how a release name is read: title, year, season, episode
    library          how the library is read: seasons, episodes, anomalies
    probe-rename     prove on the real storage that a move never replaces a file
  
  setup
    config           the configuration: init, set, unset, get, show, keys, validate, path
    service          systemd timers: install [--enable], remove, status, show
    freebox          Freebox download manager: authorize, status
    tmm              tinyMediaManager: set-key, status, sync
  
  `mediaorg COMMAND --help` explains each command, with examples.

  COMMAND

quick start
  sudo mediaorg config init --downloads /srv/media/downloads \
                            --movies /srv/media/movies --series /srv/media/tv
  sudo mediaorg check                       # read-only
  sudo mediaorg run                         # dry-run: what would happen
  sudo mediaorg run --apply                 # once by hand
  sudo mediaorg service install --enable    # then every 5 minutes, also after a reboot

privileges
  Use sudo. Commands that read or move media switch to the unprivileged 'mediaorg'
  account before they do anything. Setup commands (config init/set/unset, tmm set-key,
  service install/remove) write root-owned files. Secrets are never passed as arguments.

files
  /etc/mediaorg/config.toml      configuration (0640 root:mediaorg); -c FILE or $MEDIAORG_CONFIG
  /etc/mediaorg/tmm.key          tinyMediaManager API key (written by `tmm set-key`)
  /var/lib/mediaorg/             journals, reports, lock, Freebox token (paths.state_dir)
  /etc/systemd/system/mediaorg*  units written by `service install`

exit codes
  0 ok, 1 an operation failed, 2 usage or configuration error, 3 precondition (storage
  not mounted or not answering, download state unknown, too little space), 4 another run
  holds the lock, 5 an interrupted run needs a person (then `recover`), 6 plan above
  safety.max_moves_per_run, 130 interrupted (the next run reconciles)

documentation: https://github.com/drake4m/FreeMediaPlexOrg
```

### `mediaorg run`

```text
usage: mediaorg run [-h] [-c FILE] [-v] [--apply] [--only NAME [NAME ...]] [--max-moves N]
                    [--no-tmm] [--quiet]

Scan Downloads, identify each entry, check it is finished and plan where each file goes.
Without --apply nothing changes. With --apply the plan is carried out and journaled,
review folders and tMM's trash are cleaned by their retention, and tMM is told.

options:
  -h, --help              show this help message and exit
  -c, --config FILE       configuration file (default /etc/mediaorg/config.toml or $MEDIAORG_CONFIG)
  -v, --verbose           more detail in the log
  --apply                 carry out the plan (default: dry-run)
  --only NAME [NAME ...]  only these top-level Downloads entries
  --max-moves N           allow up to N moves and deletions in this run (default
                          safety.max_moves_per_run)
  --no-tmm                do not contact tinyMediaManager in this run
  --quiet                 for timers: print only what is acted on or changed since the previous
                          --apply --quiet run; nothing at all when nothing changed

what the report shows
  [READY]    will be (or was) filed; [PARTIAL] some files filed, the rest stays
  [WAIT]     not finished yet (still downloading, seeding, or changed recently)
  [SKIP]     not identified with certainty: left in Downloads, the reason is given
  [IGNORED]  not a video download (software, documents ...): never touched
  [PURGE] [KEPT] [TRASH]  review folders and tMM's trash cleaned (or kept) by retention

examples
  sudo mediaorg run                               # dry-run of everything
  sudo mediaorg run --apply                       # do it
  sudo mediaorg run --apply --only "Some.Show.S01E02.1080p.WEB.mkv"
  sudo mediaorg run --apply --max-moves 200       # after a long pause

A duplicate of something already in the library is never deleted unless it is byte-for-byte
identical; other duplicates go to the review folder.
```

### `mediaorg status`

```text
usage: mediaorg status [-h] [-c FILE] [-v]

Show the version, the main settings, the systemd timers and the latest run (read-only).

options:
  -h, --help         show this help message and exit
  -c, --config FILE  configuration file (default /etc/mediaorg/config.toml or $MEDIAORG_CONFIG)
  -v, --verbose      more detail in the log
```

### `mediaorg check`

```text
usage: mediaorg check [-h] [-c FILE] [-v]

Read-only preflight: configuration and file permissions, guessit, mount, folders (exist, one filesystem,
writable by the service account), free space, systemd units, Freebox, tMM. Exit 0 when everything
required is fine, 3 otherwise; [warn] lines are advice.

options:
  -h, --help         show this help message and exit
  -c, --config FILE  configuration file (default /etc/mediaorg/config.toml or $MEDIAORG_CONFIG)
  -v, --verbose      more detail in the log
```

### `mediaorg runs`

```text
usage: mediaorg runs [-h] [-c FILE] [-v] [--limit N]

List recent runs with their result (newest first).

options:
  -h, --help         show this help message and exit
  -c, --config FILE  configuration file (default /etc/mediaorg/config.toml or $MEDIAORG_CONFIG)
  -v, --verbose      more detail in the log
  --limit N          how many (default 20)
```

### `mediaorg undo`

```text
usage: mediaorg undo [-h] [-c FILE] [-v] [--apply] RUN_ID

Reverse a run: moves are undone newest first and never overwrite anything. A download that was
deleted as a byte-identical copy is not restored (the library file holds its bytes).

positional arguments:
  RUN_ID             as listed by `mediaorg runs`

options:
  -h, --help         show this help message and exit
  -c, --config FILE  configuration file (default /etc/mediaorg/config.toml or $MEDIAORG_CONFIG)
  -v, --verbose      more detail in the log
  --apply            carry it out (default: show what would be undone)

example:
  sudo mediaorg runs
  sudo mediaorg undo 20260115T101500Z-ab12
  sudo mediaorg undo 20260115T101500Z-ab12 --apply
```

### `mediaorg recover`

```text
usage: mediaorg recover [-h] [-c FILE] [-v]

Reconcile the journal of an interrupted run with what is on disk. Every run does this first; use it
after exit code 5, once you fixed the paths it names.

options:
  -h, --help         show this help message and exit
  -c, --config FILE  configuration file (default /etc/mediaorg/config.toml or $MEDIAORG_CONFIG)
  -v, --verbose      more detail in the log
```

### `mediaorg identify`

```text
usage: mediaorg identify [-h] [-c FILE] [-v] [--folder PATH] [--multi] NAME [NAME ...]

Show how release names are read; no file is accessed.

positional arguments:
  NAME               file or folder names

options:
  -h, --help         show this help message and exit
  -c, --config FILE  configuration file (default /etc/mediaorg/config.toml or $MEDIAORG_CONFIG)
  -v, --verbose      more detail in the log
  --folder PATH      Downloads folder holding the file, as context; a path for files in sub-folders
  --multi            the folder holds several main videos

examples:
  mediaorg identify "Some.Show.S02E05.1080p.WEB.h264-GRP.mkv"
  mediaorg identify "05 - Pilot.mkv" --folder "Some Show Complete/Season 2"
```

### `mediaorg library`

```text
usage: mediaorg library [-h] [-c FILE] [-v]

Show how the library is read: shows, seasons, episodes, and anomalies such as empty files (read-only).

options:
  -h, --help         show this help message and exit
  -c, --config FILE  configuration file (default /etc/mediaorg/config.toml or $MEDIAORG_CONFIG)
  -v, --verbose      more detail in the log
```

### `mediaorg probe-rename`

```text
usage: mediaorg probe-rename [-h] [-c FILE] [-v] [--apply]

Prove on the real storage that the move primitive never replaces an existing file: creates
<review>/.probe with three tiny files, tries moves onto them, removes only its own files.

options:
  -h, --help         show this help message and exit
  -c, --config FILE  configuration file (default /etc/mediaorg/config.toml or $MEDIAORG_CONFIG)
  -v, --verbose      more detail in the log
  --apply            run the probe (default: describe it)
```

### `mediaorg config init`

```text
usage: mediaorg config init [-h] [-c FILE] [-v] [--downloads DIR] [--movies DIR] [--series DIR]
                            [--review DIR] [--state-dir DIR] [--mode {watch,freebox}]
                            [--quiet-minutes N] [--freebox-downloads-dir PATH] [--mount {auto,none}]
                            [--tmm] [--tmm-url URL] [--tmm-path-map HOST=TMM] [--every MINUTES]
                            [--force] [--non-interactive]

Write a new configuration from a few answers (interactive in a terminal).

options:
  -h, --help                  show this help message and exit
  -c, --config FILE           configuration file (default /etc/mediaorg/config.toml or
                              $MEDIAORG_CONFIG)
  -v, --verbose               more detail in the log
  --downloads DIR             folder where finished downloads appear
  --movies DIR                movie library
  --series DIR                TV library
  --review DIR                review folder (default <downloads>/_review)
  --state-dir DIR             local state folder (default /var/lib/mediaorg)
  --mode {watch,freebox}      watch: a download is finished when its files stop changing (any
                              client); freebox: when the Freebox says so
  --quiet-minutes N           watch mode: minutes without change (default 10)
  --freebox-downloads-dir PATH
                              freebox mode: the Freebox's own path of the downloads folder (see
                              `freebox status`)
  --mount {auto,none}         auto: add a mount check when the folders are on their own mount
                              (default)
  --tmm                       enable tinyMediaManager calls
  --tmm-url URL               tMM HTTP API (default http://127.0.0.1:7878)
  --tmm-path-map HOST=TMM     a library path and the same path as tMM sees it (repeatable)
  --every MINUTES             how often the timer runs (default 5)
  --force                     replace an existing configuration (kept as .bak)
  --non-interactive           never ask; fail when a value is missing

The folders must exist and be on one filesystem. Missing values are asked for when run in a
terminal. A mount check ([mount]) is added automatically when the media folders are on their
own mount (NAS share, USB disk); --mount none skips it. The file is written to
/etc/mediaorg/config.toml (0640 root:mediaorg); an existing file needs --force and is kept as .bak.

examples
  # any download client, or files copied by hand (watched folder)
  sudo mediaorg config init --downloads /srv/media/downloads \
       --movies /srv/media/movies --series /srv/media/tv

  # a Freebox share mounted on /mnt/freebox, finished downloads reported by the Freebox
  sudo mediaorg config init --mode freebox --downloads /mnt/freebox/Downloads \
       --movies /mnt/freebox/Movies --series /mnt/freebox/Series

  # with tinyMediaManager in a container that sees the library under /media
  sudo mediaorg config init ... --tmm \
       --tmm-path-map /srv/media/movies=/media/movies --tmm-path-map /srv/media/tv=/media/series
```

### `mediaorg config set`

```text
usage: mediaorg config set [-h] [-c FILE] [-v] KEY=VALUE [KEY=VALUE ...]

Change one or more keys: KEY=VALUE [KEY=VALUE ...] (or KEY VALUE).

positional arguments:
  KEY=VALUE

options:
  -h, --help         show this help message and exit
  -c, --config FILE  configuration file (default /etc/mediaorg/config.toml or $MEDIAORG_CONFIG)
  -v, --verbose      more detail in the log

Values are checked, the whole configuration is validated, then the file is rewritten with its
documentation and the previous version kept as .bak. Units that depend on the change (folders,
mount, schedule) are rewritten too when they are installed. List keys: mediaorg config keys --all

examples
  sudo mediaorg config set paths.movies=/srv/media/movies paths.series=/srv/media/tv
  sudo mediaorg config set completeness.method=quiescence completeness.min_quiet_minutes=15
  sudo mediaorg config set schedule.run_every_minutes=10
  sudo mediaorg config set review.tmm_trash_retention_minutes=30
  sudo mediaorg config set mount.mountpoint=/mnt/nas mount.fstype=cifs mount.source=//nas/media
  sudo mediaorg config set media.video_extensions=.mkv,.mp4,.avi,.m4v,.ts
  sudo mediaorg config set 'tmm.path_map=/srv/media/movies=/media/movies,/srv/media/tv=/media/series'
  sudo mediaorg config set 'tmm.after_move_tvshow=[{action="update", scope={name="all"}}]'
  sudo mediaorg config set logging.file=/var/log/mediaorg/mediaorg.log
  sudo mediaorg config set paths.review=            # empty: back to the default
```

### `mediaorg config unset`

```text
usage: mediaorg config unset [-h] [-c FILE] [-v] KEY [KEY ...]

Put keys back to their default.

positional arguments:
  KEY

options:
  -h, --help         show this help message and exit
  -c, --config FILE  configuration file (default /etc/mediaorg/config.toml or $MEDIAORG_CONFIG)
  -v, --verbose      more detail in the log

example:
  sudo mediaorg config unset review.leftovers_retention_days schedule.tmm_sync_time
```

### `mediaorg config get`

```text
usage: mediaorg config get [-h] [-c FILE] [-v] KEY

Print the value in force for one key (defaults included).

positional arguments:
  KEY

options:
  -h, --help         show this help message and exit
  -c, --config FILE  configuration file (default /etc/mediaorg/config.toml or $MEDIAORG_CONFIG)
  -v, --verbose      more detail in the log

example:
  mediaorg config get paths.downloads
```

### `mediaorg config show`

```text
usage: mediaorg config show [-h] [-c FILE] [-v] [--file]

Print the configuration in force, defaults included (or the file itself with --file).

options:
  -h, --help         show this help message and exit
  -c, --config FILE  configuration file (default /etc/mediaorg/config.toml or $MEDIAORG_CONFIG)
  -v, --verbose      more detail in the log
  --file             print the file as written, with its comments
```

### `mediaorg config keys`

```text
usage: mediaorg config keys [-h] [-c FILE] [-v] [--all] [TEXT]

List the keys with their type, default and meaning.

positional arguments:
  TEXT               only keys containing TEXT

options:
  -h, --help         show this help message and exit
  -c, --config FILE  configuration file (default /etc/mediaorg/config.toml or $MEDIAORG_CONFIG)
  -v, --verbose      more detail in the log
  --all              include advanced keys

examples:
  mediaorg config keys
  mediaorg config keys tmm
  mediaorg config keys --all
```

### `mediaorg config validate`

```text
usage: mediaorg config validate [-h] [-c FILE] [-v]

Check the configuration file (exit 0 when valid, 2 otherwise).

options:
  -h, --help         show this help message and exit
  -c, --config FILE  configuration file (default /etc/mediaorg/config.toml or $MEDIAORG_CONFIG)
  -v, --verbose      more detail in the log
```

### `mediaorg config path`

```text
usage: mediaorg config path [-h] [-c FILE] [-v]

Print the path of the configuration file in use.

options:
  -h, --help         show this help message and exit
  -c, --config FILE  configuration file (default /etc/mediaorg/config.toml or $MEDIAORG_CONFIG)
  -v, --verbose      more detail in the log
```

### `mediaorg service`

```text
usage: mediaorg service [-h] [-c FILE] [-v] SUBCOMMAND ...

Install, remove and inspect the systemd timers.

positional arguments:
  SUBCOMMAND

options:
  -h, --help         show this help message and exit
  -c, --config FILE  configuration file (default /etc/mediaorg/config.toml or $MEDIAORG_CONFIG)
  -v, --verbose      more detail in the log

install writes mediaorg.service and mediaorg.timer (and, when tMM is enabled with a sync time,
mediaorg-tmm-sync.service/.timer) from the configuration: folders it may write, mounts it needs,
schedule, and a strict sandbox. It is safe to run again; only changed units are rewritten.

examples
  sudo mediaorg service install --enable    # write the units, enable and start the timers
  sudo mediaorg service status
  sudo journalctl -u mediaorg.service -f    # follow the runs
  sudo mediaorg service remove              # stop and remove the timers (files are untouched)
```

### `mediaorg service install`

```text
usage: mediaorg service install [-h] [-c FILE] [-v] [--enable]

Write the units from the configuration (root).

options:
  -h, --help         show this help message and exit
  -c, --config FILE  configuration file (default /etc/mediaorg/config.toml or $MEDIAORG_CONFIG)
  -v, --verbose      more detail in the log
  --enable           also enable and start the timers
```

### `mediaorg freebox`

```text
usage: mediaorg freebox [-h] [-c FILE] [-v] SUBCOMMAND ...

Freebox download manager (completeness.method = "freebox").

positional arguments:
  SUBCOMMAND

options:
  -h, --help         show this help message and exit
  -c, --config FILE  configuration file (default /etc/mediaorg/config.toml or $MEDIAORG_CONFIG)
  -v, --verbose      more detail in the log

  sudo mediaorg config set completeness.method=freebox
  sudo mediaorg freebox authorize      # then confirm on the Freebox Server display
  sudo mediaorg freebox status         # download folder, stop ratio, tasks
  sudo mediaorg config set freebox.remote_downloads_dir=/Freebox/Downloads
```

### `mediaorg tmm`

```text
usage: mediaorg tmm [-h] [-c FILE] [-v] SUBCOMMAND ...

tinyMediaManager: API key, status, and the nightly sync.

positional arguments:
  SUBCOMMAND

options:
  -h, --help         show this help message and exit
  -c, --config FILE  configuration file (default /etc/mediaorg/config.toml or $MEDIAORG_CONFIG)
  -v, --verbose      more detail in the log

Enable tMM's HTTP API in tinyMediaManager (settings, HTTP API: enable, port, API key), then:
  sudo mediaorg tmm set-key                         # paste the key (hidden input)
  sudo mediaorg tmm set-key < keyfile               # or from standard input
  sudo mediaorg tmm set-key --from-tmm-json /path/to/tmm/data/tmm.json
  sudo mediaorg config set tmm.enabled=true
  sudo mediaorg tmm status
  sudo mediaorg tmm sync --apply                    # what the nightly timer sends
```

### `mediaorg tmm set-key`

```text
usage: mediaorg tmm set-key [-h] [-c FILE] [-v] [--generate | --from-tmm-json FILE]

Store tMM's API key in tmm.api_key_file (0640 root:mediaorg). The key is read from a hidden
prompt or standard input, never from the command line.

options:
  -h, --help            show this help message and exit
  -c, --config FILE     configuration file (default /etc/mediaorg/config.toml or $MEDIAORG_CONFIG)
  -v, --verbose         more detail in the log
  --generate            create a new random key and print it once, to enter in tMM
  --from-tmm-json FILE  read httpApiKey from tMM's settings file tmm.json
```

### `mediaorg tmm sync`

```text
usage: mediaorg tmm sync [-h] [-c FILE] [-v] [--apply]

Send the nightly calls: update the library, scrape and rename new items (TV shows, then movies).

options:
  -h, --help         show this help message and exit
  -c, --config FILE  configuration file (default /etc/mediaorg/config.toml or $MEDIAORG_CONFIG)
  -v, --verbose      more detail in the log
  --apply            send them (default: print them)
```

<!-- END GENERATED -->
