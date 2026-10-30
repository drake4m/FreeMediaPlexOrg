# Configuration

Everything is set from the command line; the commands write the configuration file for you, check every
value, validate the whole file before writing it, and keep the previous file.

- [The file](#the-file)
- [The commands](#the-commands)
- [Values](#values)
- [Common changes](#common-changes)
- [Reference: every key](#reference-every-key)

## The file

- `/etc/mediaorg/config.toml` ([TOML](https://toml.io)), mode 0640, owner root, group `mediaorg`: the
  service account can read it, other users cannot. Another file: `-c FILE` (or `--config FILE`) on any
  command, or the environment variable `MEDIAORG_CONFIG`.
- Written by `config init`, changed by `config set` and `config unset`. Each change rewrites the whole
  file, with the documentation of every key as comments (keys you did not set appear commented out, with
  their default), and keeps the previous version as `config.toml.bak`. Your own comments are not kept.
- Secrets are not in this file: the tinyMediaManager API key is in `tmm.api_key_file`
  (`sudo mediaorg tmm set-key`), the Freebox app token in the state folder (`sudo mediaorg freebox authorize`).
- Editing by hand works too: `sudoedit /etc/mediaorg/config.toml`, then `sudo mediaorg config validate`,
  and `sudo mediaorg service install` when you changed folders, the mount check, the schedule or tMM.
- mediaorg refuses a configuration file that other users could change, and a tMM API key or Freebox token
  file that other users could read or change; the check is made each time the file is read, and the
  error names the `chmod` that fixes it.

## The commands

| Command | What it does |
|---|---|
| `sudo mediaorg config init [options]` | writes a new configuration from a few answers (asks in a terminal; `--non-interactive` for scripts); refuses to replace an existing file without `--force` |
| `sudo mediaorg config set KEY=VALUE ...` | changes one or more keys at once, all checked together; rewrites the systemd units when they depend on the change |
| `sudo mediaorg config unset KEY ...` | puts keys back to their default |
| `mediaorg config get KEY` | the value in force (the default when not set) |
| `mediaorg config show [--file]` | every value in force, defaults included (`--file`: the file as written) |
| `mediaorg config keys [TEXT] [--all]` | the keys with their type, default and meaning (`--all`: advanced keys too) |
| `mediaorg config validate` | checks the file (exit 0 valid, 2 not) |
| `mediaorg config path` | which file is in use |

`get`, `show` and `validate` read the file, so they need `sudo` for the default file. Full help with
examples: [CLI.md](CLI.md#mediaorg-config-init).

## Values

| Type | How to write it | Example |
|---|---|---|
| path | absolute; empty = back to the default (like `config unset`) | `paths.review=/srv/media/_review`, `paths.review=` |
| int, bool | a number; `true` / `false` | `safety.max_moves_per_run=100`, `tmm.enabled=true` |
| enum | one of the values listed in the reference | `completeness.method=quiescence` |
| time | `HH:MM`, local time; empty = none | `schedule.tmm_sync_time=05:07` |
| list | comma-separated, or a TOML array | `media.video_extensions=.mkv,.mp4,.avi,.m4v,.ts` |
| pairs | `HOST=TMM` pairs, comma-separated | `tmm.path_map=/srv/media/movies=/media/movies,/srv/media/tv=/media/series` |
| commands | a TOML array of `{action, scope}` tables | see [TMM.md](TMM.md#changing-the-calls) |

`KEY VALUE` (two words) works as well as `KEY=VALUE`. Quote values with spaces or shell characters:
`sudo mediaorg config set 'paths.movies=/srv/media/My Movies'`. Setting
`review.tmm_trash_retention_minutes` removes `review.tmm_trash_retention_days`, and the other way round.

## Common changes

```bash
# a download counts as finished after 15 minutes without change (watched folder)
sudo mediaorg config set completeness.min_quiet_minutes=15
# run every 10 minutes; nightly tMM sync at 05:07 (the timers are rewritten automatically)
sudo mediaorg config set schedule.run_every_minutes=10 schedule.tmm_sync_time=05:07
# keep review folders 30 days; never delete leftovers automatically
sudo mediaorg config set review.duplicates_retention_days=30 review.leftovers_retention_days=-1
# keep byte-identical copies in review instead of deleting them
sudo mediaorg config set review.delete_identical_duplicates=false
# also log to a file, rotated at 10 MB, 5 files kept
sudo mediaorg config set logging.file=/var/log/mediaorg/mediaorg.log
# add the mount check for a share mounted on /mnt/nas (values as `findmnt /mnt/nas` shows them)
sudo mediaorg config set mount.mountpoint=/mnt/nas mount.fstype=cifs mount.source=//nas.local/media
# file .ts videos too
sudo mediaorg config set media.video_extensions=.mkv,.mp4,.avi,.m4v,.ts
```

## Reference: every key

Generated from the program (`mediaorg config keys --all` prints the same). An example file with every key:
[config/config.example.toml](../config/config.example.toml).

<!-- BEGIN GENERATED: tools/gen_docs.py -->

### `[paths]`

Folders. Downloads, the library and the review folder must be on ONE filesystem: files are moved with instant renames, never copied.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `downloads` | path | required | Folder where completed downloads appear (the watched folder). Each entry in it, a file or a folder, is one download. Example: `"/srv/media/downloads"` |
| `movies` | path | required | Movie library: &lt;movies&gt;/&lt;Title (Year)&gt;/&lt;files&gt;. Example: `"/srv/media/movies"` |
| `series` | path | required | TV library: &lt;series&gt;/&lt;Show (Year)&gt;/Season N/&lt;files&gt;. Example: `"/srv/media/tv"` |
| `review` | path | &lt;downloads&gt;/_review | Where duplicates and leftovers of handled downloads go for you to review. Same filesystem as the rest; inside Downloads only as a direct child listed in media.ignore_names. For the timers it must share a folder other than / with Downloads and the libraries. |
| `state_dir` | path | `"/var/lib/mediaorg"` | Local folder for journals (undo), reports, the run lock, the Freebox token and caches. Never on the media storage. |

### `[mount]`

Optional safety check: nothing is touched unless this mount point is mounted from this source (a NAS share, a USB disk). Without it, an unmounted share leaves an empty local folder behind that could be filled by mistake. `mediaorg config init` fills it in when the media folders are on their own mount. Leave the three keys commented out to disable the check.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `mountpoint` | path | (none: no check) | Mount point that holds all the media folders. Example: `"/mnt/nas"` |
| `fstype` | str | (none: no check) | Expected filesystem type, as `findmnt` shows it (cifs, nfs4, ext4 ...). Example: `"cifs"` |
| `source` | str | (none: no check) | Expected source, as `findmnt` shows it (share or device). Example: `"//nas.local/media"` |

### `[completeness]`

When is a download finished? "quiescence": its files have not changed for min_quiet_minutes and none has a temporary extension (any download client, or files copied by hand). "freebox": the Freebox download manager reports the task as done (see `mediaorg freebox authorize`).

| Key | Type | Default | Meaning |
|---|---|---|---|
| `method` | enum (quiescence / freebox) | `"quiescence"` | How finished downloads are recognised (see above). |
| `min_quiet_minutes` | int | `10` | Minutes without any change before a download counts as finished (method "quiescence", and untracked entries with untracked = "quiescence"). Minimum 5. |
| `temp_extensions` | list | `[".part", ".!qb", ".crdownload", ".tmp", ".partial", ".!ut", ".aria2"]` | A file with one of these extensions marks its download as unfinished. |
| `untracked` | enum (wait / quiescence) | `"wait"` | Method "freebox": entries the Freebox does not list as a task (copied by hand). "wait" = never move them; "quiescence" = move them after min_quiet_minutes. |
| `on_seeding` | enum (wait / stop) | `"wait"` | Method "freebox": a finished BitTorrent download that is still seeding. "wait" = moved once the Freebox says done (at its stop ratio); "stop" = a run that files it stops the task first (it no longer seeds), then moves it. |

### `[freebox]`

Freebox download manager; used only with completeness.method = "freebox".

| Key | Type | Default | Meaning |
|---|---|---|---|
| `remote_downloads_dir` | str | `""` | The Freebox's own path of paths.downloads, exactly as `mediaorg freebox status` prints it. A run that needs the Freebox's verdict stops (nothing moved) if the Freebox downloads elsewhere. Example: `"/Freebox/Downloads"` |
| `base_url` | str | `"https://mafreebox.freebox.fr"` | Freebox OS API address. HTTPS; the certificate is checked against ca_file. |
| `ca_file` | path | `"/etc/mediaorg/freebox-ca.pem"` | Freebox root certificates (installed by install.sh). |
| `token_file` | path | &lt;state_dir&gt;/freebox_app_token.json | App token written by `mediaorg freebox authorize` (mode 0600). |
| `device_name` | str | this host's name | Device name shown on the Freebox when you authorize the app. |
| `timeout_seconds` | int | `10` | HTTP timeout in seconds. |
| `app_id` | str | `"mediaorg"` | App identifier on the Freebox; changing it needs a new authorization. *(advanced)* |
| `app_name` | str | `"mediaorg"` | App name shown in Freebox OS. *(advanced)* |
| `allow_insecure_http` | bool | `false` | Accept an http:// base_url (not recommended). *(advanced)* |

### `[tmm]`

tinyMediaManager (optional). After a run that filed something, tMM updates its library, then scrapes and renames the new items. The API key is read from api_key_file (`sudo mediaorg tmm set-key`), never from this file.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | bool | `false` | Send tMM its calls after runs that filed something (and the nightly sync). |
| `base_url` | str | `"http://127.0.0.1:7878"` | tMM HTTP API address. Plain http:// only to this machine unless allow_insecure_http = true. |
| `api_key_file` | path | `"/etc/mediaorg/tmm.key"` | File holding the tMM API key on one line (mode 0640, root:mediaorg); written by `sudo mediaorg tmm set-key`. |
| `path_map` | pairs | `[]` | Library paths as tMM sees them when it runs in a container: [[host_path, tmm_path], ...]. Needed only by tMM commands that use "{paths}". Example: `[   ["/srv/media/movies", "/media/movies"],   ["/srv/media/tv", "/media/series"], ]` |
| `timeout_seconds` | int | `30` | HTTP timeout in seconds. |
| `defer_moves_while_busy` | bool | `true` | Move nothing while tMM is busy (needs tMM 5.3.5 or later; ignored before). |
| `allow_insecure_http` | bool | `false` | Accept plain http:// to a tMM on another machine (the key travels unencrypted). *(advanced)* |
| `movie_endpoint` | str | `"movie"` | API endpoint for movies (checked by `mediaorg check`). *(advanced)* |
| `tvshow_endpoint` | str | `"tvshow"` | API endpoint for TV shows (checked by `mediaorg check`). *(advanced)* |
| `after_move_movie` | commands | update all, scrape new, rename new | tMM calls after a run that filed movies. Default: update all, scrape new, rename new. *(advanced)* |
| `after_move_tvshow` | commands | update all, scrape new, rename new | tMM calls after a run that filed TV episodes. Default: update all, scrape new, rename new. *(advanced)* |
| `full_sync_movie` | commands | update all, scrape new, rename new | Movie calls of the nightly `mediaorg tmm sync`. *(advanced)* |
| `full_sync_tvshow` | commands | update all, scrape new, rename new | TV-show calls of the nightly `mediaorg tmm sync`. *(advanced)* |

### `[review]`

What is cleaned up automatically, and when.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `delete_identical_duplicates` | bool | `true` | A download whose bytes are identical to the library copy is deleted: nothing is lost. false = move it to review instead. |
| `delete_duplicates` | bool | `false` | A download the library already has is deleted in the run that finds it, even when its bytes differ: the same episode, or the same film of the same year, not a REPACK/PROPER, not a special edition, not one part of a split file, and not larger than the library copy; a release naming a country needs it in the folder's name. Languages are not compared. Other duplicates (a bigger or repaired copy, a year that cannot be confirmed) go to review/duplicates and are kept until you decide; certain duplicates that earlier runs moved there are deleted by the next run. Needs delete_identical_duplicates = true. false = duplicates go to review/duplicates (duplicates_retention_days). |
| `duplicates_retention_days` | int | `7` | Days after which the certain duplicates of a run are deleted from review/duplicates/ (0 = at the end of the run, -1 = never). Other duplicates are kept. Not used with delete_duplicates = true. |
| `leftovers_retention_days` | int | `7` | Days after which release clutter (.nfo, .txt, small images and subtitles, small samples) is deleted from review/leftovers/ (0 = at the end of the run, -1 = never). Other files are kept. |
| `tmm_trash_retention_days` | int | `7` | Days a file stays in tMM's trash (.deletedByTMM in the library) before it is deleted (0 = in the first run, -1 = never). |
| `tmm_trash_retention_minutes` | int | not set | The same in minutes; set this or tmm_trash_retention_days, not both. Example: `30` |

### `[schedule]`

Used by `sudo mediaorg service install` to write the systemd timers.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `run_every_minutes` | int | `5` | How often the timer runs mediaorg (1-1440). 1, 2, 3, 4, 5, 6, 10, 12, 15, 20 or 30 minutes and 1, 2, 3, 4, 6, 8, 12 or 24 hours run at fixed times of the day; other values count from the end of the previous run. A run with nothing to do takes well under a second and logs nothing. |
| `tmm_sync_time` | time | `"03:37"` | Local time (HH:MM) of the nightly tMM sync: update the library, scrape and rename new items. "" = no nightly sync. |

### `[safety]`

Limits that stop a run before it acts.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `max_moves_per_run` | int | `50` | A run planning more moves and deletions stops before acting (exit 6); allow it once with `run --apply --max-moves N`. |
| `min_free_mb` | int | `100` | Free space (MB) needed in state_dir before a run moves anything: every move is journaled there first (0 = no check). |

### `[reports]`

Run reports and journals in state_dir, bounded so that they never fill the disk.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `retention_days` | int | `30` | Run reports older than this many days are deleted (0 = never by age). |
| `max_reports` | int | `2000` | At most this many run reports are kept (0 = no limit). |
| `journal_retention_days` | int | `365` | Journals of finished runs (used by undo and by the review cleaning) are deleted after this many days; must be longer than the review retention (0 = keep forever). The journal of an interrupted run is kept until the next run has reconciled it. |

### `[logging]`

Logs always go to standard error (under systemd: the journal, rotated by journald). Optionally also to a file, rotated by size.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `file` | path | `""` | Also write the log to this file, rotated by size (e.g. /var/log/mediaorg/mediaorg.log). "" = journal only. Example: `"/var/log/mediaorg/mediaorg.log"` |
| `max_size_mb` | int | `10` | Size in MB at which the log file is rotated. |
| `backups` | int | `5` | Number of rotated log files kept. |

### `[media]`

What counts as a video, a subtitle or an extra, and what is ignored in Downloads.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `video_extensions` | list | `[".mkv", ".mp4", ".avi", ".m4v"]` | Files filed as videos. |
| `subtitle_extensions` | list | `[".srt", ".ass", ".ssa", ".sub", ".idx", ".sup", ".vtt"]` | Subtitles, moved and renamed with their video. |
| `extras_dir_names` | list | `["sample", "samples", "extras", "extra", "featurettes", "featurette", "bonus", "trailers", "trailer", "behind the scenes", "deleted scenes", "interviews", "shorts"]` | Videos below folders with these names are extras, never the episode or movie. |
| `ignore_names` | list | `["_review", "@eaDir", "#recycle", ".DS_Store", "Thumbs.db", "desktop.ini"]` | Entries in Downloads with these names are ignored (the review folder must be listed when it is inside Downloads). |
| `ignore_hidden` | bool | `true` | Ignore entries in Downloads whose name starts with a dot. |

### `[naming]`

Names of the folders mediaorg creates; existing folders are always reused. Placeholders: {title}, {year}, {season}.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `movie_folder` | str | `"{title} ({year})"` | New movie folder. |
| `movie_folder_no_year` | str | `"{title}"` | New movie folder when the release has no year (tMM adds the year when it renames the movie). |
| `series_folder` | str | `"{title} ({year})"` | New show folder. |
| `series_folder_no_year` | str | `"{title}"` | New show folder when the release has no year. |
| `season_folder` | str | `"Season {season}"` | Season folder inside a show folder. |
| `tmm_staging` | bool | `false` | New movie and show folders end with " [mediaorg]" until tinyMediaManager renames them (with tmm.enabled only; tMM ignores text in [...] when it reads the name). Needed when tMM runs on Linux with the library on an SMB/CIFS share: there tMM cannot rename a folder whose new name differs only in letter case ('Rise of the ...' -&gt; 'Rise Of The ...'). |

<!-- END GENERATED -->
