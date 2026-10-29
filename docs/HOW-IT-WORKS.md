# How it works

- [Overview](#overview)
- [What is filed where](#what-is-filed-where)
- [What stays in Downloads](#what-stays-in-downloads)
- [How Downloads is emptied](#how-downloads-is-emptied)
- [Safety rules](#safety-rules)
- [Known limitations](#known-limitations)

All names below are invented examples.

## Overview

```text
download client ──writes──> Downloads/
                                │  mediaorg.timer (every 5 min) -> mediaorg.service (account mediaorg, sandboxed)
                                ▼
 mediaorg run --apply:  lock -> mount check -> reconcile an interrupted run -> scan Downloads
                        -> finished? (files quiet for N minutes, or the Freebox says so) -> identify (guessit)
                        -> match the library folders -> plan -> journaled, no-overwrite moves
                        -> clean the review folder and tMM's trash by retention
                        -> tinyMediaManager: update the library, scrape and rename the new items (optional)
                                ▼
     Movies/Title (Year)/...     TV/Show (Year)/Season N/...     Downloads/_review/{duplicates,leftovers}/<run-id>/
```

A run is a pure planning pass until `--apply`: the dry-run shows exactly the operations an applied run
would carry out. Each run starts from the disk state, so a download added, renamed or deleted between two
runs is simply seen differently by the next run.

## What is filed where

An episode is identified from its own name; the folders above it only fill in what the name lacks (show
title, year, season). A movie that is the only video of its folder is identified by the folder name when
that has a year (unless the file names another year); otherwise file and folder must agree.

An existing library folder is reused when exactly one fits: a show folder with or without year, a movie
folder with the same year, or the only folder of that title if it has no year yet. Otherwise a new one is
created as `Title (Year)` (`Title` when no year is known; the names come from `[naming]`). With
`naming.tmm_staging` and tMM enabled, a new folder is `Title (Year) [mediaorg]` until tinyMediaManager
renames it ([TMM.md](TMM.md#library-on-an-smbcifs-share-staging-names)).

| What arrives in Downloads | Where it goes |
|---|---|
| one episode, loose: `Night Harbor - S01E03 - Beacons.mkv` | `TV/Night Harbor (2024)/Season 1/` (the existing show folder) |
| one episode in a folder: `Night.Harbor.S01E04.1080p.WEB/...mkv` | the same; the folder leaves Downloads |
| a season pack: `Night.Harbor.S02.1080p/Night.Harbor.S02E01.mkv ...` | each episode by its own `SxxEyy` |
| a whole show with season folders: `Glass Road Complete/Season 1/...`, `Season 2/05 - The Bridge.mkv` | names with `SxxEyy` as usual; names with only a number take the season from their `Season 2` / `S02` folder and the show from the folder above, without pack words (`Glass Road Complete` -> `Glass Road`; also Collection, Intégrale, Coffret). Such a name must start with its number (`05 - The Bridge`, `E05`) or with the show title: `Recap 05.mkv` is not episode 5 |
| a pack of several shows: `TV Pack/Glass Road/Season 1/Glass.Road.S01E01.mkv` | the show title may come from the folder inside the pack |
| a movie, loose or in a folder: `A Quiet Field 2023 1080p WEB.mkv`, `Paper Moons (2021)/paper.moons.mp4` | `Movies/A Quiet Field (2023)/`, `Movies/Paper Moons (2021)/` (a folder name with a year wins over the file name, unless the file names another year: `Paper Moons (2021)/Paper.Moons.Returns.2024.mkv` is skipped) |
| a movie without year: `Some.Movie.1080p.mkv`, `Distant Bells/Distant.Bells.mkv` | only if the name shows it is a release (resolution, source or codec: `1080p`, `WEB`, `x264` ...) or its folder confirms the title; a loose `Some Name.mkv` is skipped. Filed into the one existing folder with that title (if that folder already holds a movie: skipped, it may be another film of the same name, unless the bytes are identical), else into a new `Movies/Some Movie/`; tinyMediaManager adds the year when it renames it |
| several movies in one folder: `Two.Films/First.Film.2016.mkv`, `Second.Film.2015.mkv` | each by its own name (each needs a year); two files of the same movie (parts, qualities) are skipped |
| a video without extension: `Night.Harbor.S01E05`, `A.Film.2019.1080p.x264.AC3` | recognised by its content (Matroska, MP4 or AVI; any file of 1 MiB or more without a video, subtitle or audio extension is examined) and filed with the extension added: `Night.Harbor.S01E05.mkv` |
| subtitles | follow their video (`X.en.srt` next to `X.mkv`; `Subs/English.srt` becomes `X.English.srt`; in packs also `Subs/X/2_English.srt`); next to the library copy when the download was an identical copy; along with a duplicate into review |
| samples, trailers, `Extras/`, `Featurettes/` ..., bonus files named `Show.S01.Extras.01` / `.Bonus.02` | never filed; they leave Downloads with the other leftovers |
| two downloads of the same episode or movie in one run | the first is filed; the other waits for the next run, then is compared with the library copy: identical -> deleted, otherwise -> `review/duplicates` |
| an episode or movie the library already has | byte-identical -> the download is deleted; otherwise -> `review/duplicates`; with `review.delete_duplicates`, deleted too unless it is bigger, a REPACK/PROPER, a special edition, a part or of another year ([USAGE.md](USAGE.md#the-review-folder)) |
| an empty 0-byte library file of the episode or movie being filed | replaced by the download |
| an empty folder (no file left in it) | removed once complete |

`mediaorg identify "<name>" --folder "<folder>/<subfolder>"` shows how a name will be read.

## What stays in Downloads

Left in Downloads and reported, for you to rename or file by hand:

- names that cannot be read safely: a show folder with another year (`Glass Road (2002)` against a 2017
  release), several possible library folders, a file and its folder naming different shows or movies (also a
  dated folder holding another film), a season read from a bare number (`clip-2048-surround.mp4` would be
  "S20E48"), a movie without year among several videos, a loose name with neither year nor release
  information, a multi-episode file whose episodes are only partly in the library;
- several files of one episode in one download (`Part1`/`Part2`, `CD1`/`CD2`, two versions, a featurette
  named like the episode): the whole download waits for your decision;
- bonus files in a season pack that are not tagged as extras (`Recap 05.mkv`, `Deleted Scene 05.mkv`): not
  identified, so the pack stays (`PARTIAL`) with them;
- a folder holding more other data than video (software with a clip, disc images), an entry whose only
  videos are of types not in `media.video_extensions`, or named like a sample, trailer or bonus, and
  downloads that are not finished.

## How Downloads is emptied

- When every main video of an entry is handled (moved, deleted as an identical copy, or sent to review as a
  duplicate), the entry leaves Downloads in that same run: an emptied folder is removed, and whatever else
  it holds (`.nfo`, images, samples, extras, subtitles that could not follow, videos of other types) moves to
  `_review/leftovers/<run-id>/`.
- An entry with any main video that could not be handled stays in Downloads as it is (`SKIP`, or `PARTIAL`
  when some of its videos were filed), so nothing unidentified is ever moved away. An entry holding no file
  at all (empty folders) is removed once complete.
- The review folder and tinyMediaManager's trash clean themselves by retention: [USAGE.md](USAGE.md#the-review-folder)
  and [TMM.md](TMM.md#trash-cleaning).

## Safety rules

Enforced in code and covered by the test suite:

1. **Mount identity.** With `[mount]` set, nothing runs unless the mount point is mounted from the
   configured source and filesystem type. `config init` sets it up when the media folders are on their own
   mount; an empty mount point can additionally be protected with `chattr +i` ([INSTALL.md](INSTALL.md)).
2. **Moves are same-filesystem renames.** Nothing is copied; all folders must be on one filesystem.
3. **No overwrite.** A file's target name is first reserved by an exclusive create (atomic, and
   case-insensitive on SMB servers), then the file is renamed onto that empty placeholder; folders are
   moved with `renameat2(RENAME_NOREPLACE)`. The Linux SMB client always sends renames with "replace if
   exists", which is why the reservation exists. `mediaorg probe-rename --apply` proves it on your storage.
4. **Deletions are explicit and limited**, each listed in the dry-run first:
   - a download whose bytes are identical to the library file of the same episode or movie (compared byte
     by byte; right before the deletion both files must still have the size and time seen then);
   - with `review.delete_duplicates`, a download of an episode or movie the library already has, not bigger
     than the library copy, not a REPACK/PROPER, not a special edition, not a part, of the same year (right
     before the deletion every library copy must still have the size and time seen then);
   - an empty (0-byte) library video of the episode or movie being filed (it must still be 0 bytes);
   - in review run folders after their retention (`--apply` runs only): the duplicates that run journaled
     as certain, and release clutter ([USAGE.md](USAGE.md#the-review-folder)). Any other file is never
     deleted automatically. The cleaning works through folder handles opened without following links:
     a folder replaced by a link, or a run folder replaced since it was listed, is left alone;
   - files in tinyMediaManager's trash (`.deletedByTMM` at the root of each library) once mediaorg has seen
     them there for the retention, each re-checked right before (same size and time, same file, no folder
     on its path turned into a link); links are removed, never followed.

   Everything else that is not filed goes to the review folder. `rmdir` only ever removes empty folders.
5. **No guessing.** Unidentified, ambiguous, year-conflicting or title-conflicting items stay where they
   are. A season is taken only from `SxxEyy`, `NxNN` or a season folder, never from bare digits. A second
   copy of an episode or movie filed in the same run waits for the next run, so a duplicate is always judged
   against a file that is really in the library.
6. **Finished first.** A download is touched only once it is finished: the Freebox reports the task done
   (and no longer seeding; with `completeness.on_seeding = "stop"`, a run that files it stops the seeding
   and moves it only once the Freebox lists the task as stopped), or none of its files changed for
   `completeness.min_quiet_minutes` and none has a temporary extension.
7. **Changed since the scan.** A file that changed between the scan and the move is not moved or deleted,
   and a Downloads folder whose content changed is not touched at all in that run.
8. **Journal.** Every move and deletion is written (and flushed to disk) to the journal before it
   happens. Interrupted runs are reconciled, and runs can be undone.
9. **One run at a time** (a lock), and at most `safety.max_moves_per_run` moves and deletions per run.
10. **Least privilege.** Media commands run as the unprivileged `mediaorg` account (a root caller is
    switched to it first) under a strict systemd sandbox that can write only its state folder and the one
    folder that holds Downloads, the libraries and the review folder.
    Secrets are files readable by that account only, never command-line arguments. A configuration that
    other users could change, or a key or token that they could read or change, is refused each time it is
    read.
11. **Samples.** Library files named `-sample`/`-trailer` never count as the episode or movie. In Downloads,
    a video named like a sample counts as one only when it is under 1 GiB and under a tenth of the entry's
    biggest other video (`Show.S02E02.The.Sample.mkv` next to S02E01 is an episode); a lone one stays in
    Downloads. Bonus files tagged in their name (`Show.S01.Extras.01`, `.Bonus.02`) are extras.
12. **Weak evidence.** A folder holding more other data than video (64 MiB or more of it; archives and
    videos do not count) is not filed as a movie, nor as episodes found by their season folder.
13. **Content check.** A file without a video, subtitle or audio extension, of 1 MiB or more, is filed as a
    video only if its first bytes are those of a Matroska, MP4 or AVI file.

## Known limitations

- **Identification rests on names.** Shows are matched by title: a release without year
  (`Harbor.Lights.S01E01...`) goes into the one show folder with that title, even if it is a remake or
  another country's show (`Harbor Lights (1999)` against a 2013 remake). An episode the library already has
  then goes to review as a duplicate and is kept (no year: not certain); a new one is filed into that show,
  where tinyMediaManager's scrape shows the mismatch. Releases with a year are matched exactly.
- A movie without year is matched by its title only. If the one library folder with that title already
  holds a movie, the download is skipped, unless its bytes are identical; if that folder is empty, the
  download is filed into it even if it is another film of the same name. Add the year to such names.
- The year in a folder name is trusted: `Paper Moons (2021)` is filed as a 2021 movie even if the film is
  older, and tinyMediaManager may then not match it automatically.
- A release named differently from the folder tinyMediaManager created (another title, a subtitle of the
  title) creates a second folder.
- Not handled, so skipped and reported: episodes numbered only by digits outside a season folder
  (`Show - 105.mkv`), date-based and anime absolute numbering, multi-season files, multi-part movies
  (`CD1`/`CD2`) and several files of one movie in a folder, several files of one episode in a download,
  bonus files of a season pack named without season (the pack stays `PARTIAL`), several movies without
  year in one folder.
- Video types other than `media.video_extensions` (`.mkv .mp4 .avi .m4v` by default) are not filed: an
  entry holding only such videos is `IGNORED`; inside an entry that is otherwise handled they go to
  `review/leftovers` and are kept.
- Duplicates are not compared by quality: a duplicate that is not byte-identical goes to review (or, with
  `review.delete_duplicates`, is deleted when it is not bigger than the library copy). A re-encoded or
  re-muxed copy of the same episode is a different file.
- What the review cleaning or the trash cleaning deleted cannot be restored, and `undo` cannot bring it back.
- In watched-folder mode, and for untracked entries with a Freebox, a copy abandoned halfway cannot be told
  apart from a finished one: give unfinished files a temporary extension, or download into another folder
  ([INSTALL.md](INSTALL.md#set-up-your-download-client)).
- Moving files changes the modification time of their folder, so in watched-folder mode such a folder waits
  `completeness.min_quiet_minutes` before it is handled again.
- A 0-byte library file is replaced only when a download of the same episode or movie is filed;
  `mediaorg library` lists the others.
- The review cleaning trusts the system clock: a clock far in the future would clean every review folder at
  once (under the same rules). The trash cleaning relies less on it: each run counts at most 12 hours, so a
  clock jump makes a file due at most 12 hours early.
- With tinyMediaManager older than 5.3.5 (no status endpoint), moves cannot wait while tMM renames: a move
  whose target folder tMM renames at that moment fails, is reported and retried; it cannot overwrite.
- tinyMediaManager specifics (automatic scraping thresholds, a share lost during a tMM update):
  [TMM.md](TMM.md#known-limitations).
