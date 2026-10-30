"""Turn scanned items into an explicit plan of operations.  Performs no writes (it only reads:
byte comparisons of same-size duplicates).

Every main video gets exactly one decision (move | duplicate | delete | skip) with a reason:
  * delete:    the download is byte-identical to the library file of the same episode/movie
               (review.delete_identical_duplicates); it is unlinked, nothing is lost;
  * duplicate: the library has the episode/movie in another file -> review/duplicates (the video
               and its subtitles only);
  * move:      filed into the library; an empty (0-byte) library file of the same episode/movie
               is removed first (it is a broken placeholder, not media).
Items are only cleaned up (leftovers moved to review, empty folders removed) when every main
video in them was handled, so nothing unidentified ever leaves Downloads.  Directory creation is
derived from the renames actually planned, so a skipped decision never leaves an empty folder.
"""

from __future__ import annotations

import os
import unicodedata

from .config import Config
from .identify import country_of, guess, identify_video, is_bonus, is_sample, is_weak_movie, release_markers
from .library import PLANNED, Library, compare_files, is_library_extra
from .matching import LibFolder, Match, match_movie, match_series
from .model import FileDecision, FileEntry, Item, ItemPlan, MediaId, Op, Readiness
from .normalize import STAGING_TAG, UnsafeName, norm, safe_component, same_name, smart_title, unstaged
from .review import disposable, human, video_like
from .scan import is_archive

IDENTICAL = "byte-identical copy, deleted from Downloads"
SAME = "not larger than the library copy: deleted from Downloads (review.delete_duplicates)"
REPLACED = "it replaces the library video (review.replace_duplicates)"
EXTRA_MAX_BYTES = 1 << 30  # a sample or trailer is never 1 GiB or more
SAMPLE_RATIO = 0.10  # a sample/trailer name counts next to bigger videos only below 10 % of the largest
OTHER_BYTES_MIN = 64 << 20  # non-video data that can outweigh a movie's video (software, disc image)


def _short(names, n: int = 3) -> str:
    names = list(names)
    shown = ", ".join(repr(x) for x in names[:n])
    return shown + (f" (+{len(names) - n} more)" if len(names) > n else "")


def _named_extra(f: FileEntry) -> bool:
    """Named like a sample or trailer: 'x-sample.mkv', 'sample.mkv', 'x.Sample.1080p.mkv', 'x.Trailer.mp4'."""
    g = guess(f.name)
    others = g.get("other") or []
    others = others if isinstance(others, list) else [others]
    return is_library_extra(f.name) or is_sample(f.name, g) or "Trailer" in others


class Planner:
    def __init__(self, cfg: Config, library: Library, run_id: str, compare_cache: dict | None = None,
                 on_compare=None):
        self.cfg = cfg
        self.lib = library
        self.run_id = run_id
        self.review_dup = os.path.join(cfg.paths.review, "duplicates", run_id)
        self.review_left = os.path.join(cfg.paths.review, "leftovers", run_id)
        self._stop_dirs = {
            cfg.paths.downloads, cfg.paths.movies, cfg.paths.series, cfg.base_dir, "/",
        }
        self._review_targets: set[str] = set()  # casefolded review destinations planned in this run
        # results of byte comparisons, kept between runs by the caller, so that an item that cannot
        # be acted on yet (cap, tMM busy) is not read again every run; keys used now: used_compare_keys
        self.compare_cache = compare_cache if compare_cache is not None else {}
        self.used_compare_keys: set[str] = set()
        self.on_compare = on_compare  # called after each new comparison: a long run killed later keeps them
        # naming.tmm_staging: new folders carry the staging tag until tMM renames them (only tMM removes it)
        self.staging = cfg.naming.tmm_staging and cfg.tmm.enabled

    # ------------------------------------------------------------------ classification
    def _is_extra(self, f: FileEntry, largest_other: int) -> bool:
        """Sample/trailer/bonus video, never filed as the episode or movie.

        A video below an extras folder ('Sample/', 'Featurettes/') or tagged as bonus material in its
        name ('Cistern.S01.Extras.01.mkv') is an extra.  A video NAMED like
        a sample or trailer is one only if it is under 1 GiB and either the item's only video (the
        item is then left in Downloads as IGNORED, never deleted) or under a tenth of the item's
        largest other video: 'Cistern.S02E02.Blood.Sample.mkv' next to S02E01 is an episode."""
        parts = f.rel.split("/")[:-1]
        extras = {n.casefold() for n in self.cfg.media.extras_dir_names}
        if any(p.casefold() in extras for p in parts):
            return True
        if is_bonus(f.name):  # 'Show.S01.Extras.01.mkv': kept with the leftovers, never filed
            return True
        if f.size >= EXTRA_MAX_BYTES or not _named_extra(f):
            return False
        return not largest_other or f.size < SAMPLE_RATIO * largest_other

    def classify(self, item: Item) -> tuple[list[FileEntry], list[FileEntry], list[FileEntry]]:
        """-> (main videos, extra/sample videos, subtitles)"""
        videos = [f for f in item.files if f.ext in self.cfg.media.video_extensions]
        mains, extras, subs = [], [], []
        for f in item.files:
            if f.ext in self.cfg.media.video_extensions:
                largest_other = max((v.size for v in videos if v is not f), default=0)
                (extras if self._is_extra(f, largest_other) else mains).append(f)
            elif f.ext in self.cfg.media.subtitle_extensions:
                subs.append(f)
        return mains, extras, subs

    # ------------------------------------------------------------------ op helpers
    def _role_for(self, path: str) -> str:
        lib_roots = (self.cfg.paths.movies, self.cfg.paths.series)
        return "library-dir" if any(path.startswith(r + "/") for r in lib_roots) else "review-dir"

    def _with_parents(self, ops: list[Op], op: Op, emitted: set[str]) -> None:
        missing, p = [], os.path.dirname(op.dst)
        while p not in self._stop_dirs and p not in emitted and not os.path.isdir(p):
            missing.append(p)
            p = os.path.dirname(p)
        for d in reversed(missing):
            ops.append(Op("mkdir", dst=d, role=self._role_for(d)))
            emitted.add(d)
        ops.append(op)

    @staticmethod
    def _rename(f: FileEntry, dst: str, role: str) -> Op:
        return Op("rename", src=f.path, dst=dst, role=role, expect_size=f.size, expect_mtime=f.mtime)

    def _show(self, path: str) -> str:
        """Path as shown in reports: relative to the media base folder (cfg.base_dir)."""
        mp = self.cfg.base_dir
        return os.path.relpath(path, mp) if path.startswith(mp + "/") else path

    def _review_target(self, path: str) -> bool:
        """Reserve a review destination for this run; False if the name is already taken (case-insensitive)."""
        key = unicodedata.normalize("NFC", path).casefold()
        if key in self._review_targets:
            return False
        self._review_targets.add(key)
        return True

    def _new_folder(self, root: str, tmpl: str, m: MediaId, what: str) -> tuple[str | None, str]:
        """-> (name of a new show/movie folder, '') or (None, why it cannot be created).  With naming.tmm_staging
        the name ends with the staging tag; a folder with the name it will get from tMM must not exist either."""
        try:
            base = safe_component(tmpl.format(title=smart_title(m.display_title), year=m.year))
            name = safe_component(f"{base} {STAGING_TAG}") if self.staging else base
        except UnsafeName as exc:
            return None, f"cannot build {what} folder name: {exc}"
        for n in dict.fromkeys((base, name)):
            if self.lib.exists_name(root, n):
                return None, f"a folder named {n!r} exists but did not match by title"
        return name, ""

    @staticmethod
    def _empty_tree(item: Item) -> bool:
        """A Downloads folder holding no file at all, only (empty) folders."""
        return item.is_dir and not item.files

    def needs_readiness(self, item: Item) -> bool:
        """plan_item consults the readiness only for items with a main video (or no file at all) and no problem."""
        return not item.problems and (self._empty_tree(item) or bool(self.classify(item)[0]))

    # ------------------------------------------------------------------ duplicates and placeholders
    def _identical(self, f: FileEntry, refs: list[str]) -> str | None:
        """The first library file whose bytes equal the download's (same size first), or None."""
        if not self.cfg.review.delete_identical_duplicates or f.size <= 0:
            return None
        for ref in refs:
            st = self.lib.stat_of(ref)
            if not st or st[0] != f.size:
                continue
            key = f"{f.path}|{f.size}|{int(f.mtime)}|{ref}|{st[0]}|{int(st[1])}"
            self.used_compare_keys.add(key)
            if key not in self.compare_cache:
                same = compare_files(f.path, ref)
                if same is None:  # could not read: not identical this run, compared again next run
                    continue
                self.compare_cache[key] = same
                if self.on_compare:
                    self.on_compare(self.compare_cache)
            if self.compare_cache[key]:
                return ref
        return None

    def _delete_op(self, d: FileDecision) -> Op:
        ref = d.refs[0]
        st = self.lib.stat_of(ref)
        if d.same:
            files = tuple((r, *self.lib.stat_of(r)) for r in d.refs if self.lib.stat_of(r))
            return Op("unlink", dst=d.file.path, role="same-duplicate", expect_size=d.file.size,
                      expect_mtime=d.file.mtime, ref=ref, ref_mtime=st[1] if st else None,
                      ref_files=files if len(files) == len(d.refs) else ())
        return Op("unlink", dst=d.file.path, role="identical-duplicate", expect_size=d.file.size,
                  expect_mtime=d.file.mtime, ref=ref, ref_mtime=st[1] if st else None)

    def _certain(self, f: FileEntry, m: MediaId, folder: LibFolder, refs: list[str], item_name: str) -> str:
        """'' when a duplicate is certain enough to be deleted (after the review retention, or at once with
        review.delete_duplicates), else why it is kept for the user to decide: the release and the library
        folder must agree on the year (a title alone also fits a remake or another country's show; with
        delete_duplicates an episode needs only no conflicting year, its show folder being the one folder
        of that title), and on the country a release names; the download must not be a REPACK/PROPER, a
        special edition (named by the file or by any folder above it) or one part of a split file, and it must
        not be larger than the library copy (a repaired or better download)."""
        what = "show" if m.kind == "episode" else "film"
        folder_year = folder.year
        proper, edition = release_markers((item_name, *f.rel.split("/")))
        if m.proper or proper:
            return "it is a REPACK/PROPER release"
        if m.part:
            return "it is one part of a split file"
        edition = m.edition or edition
        if edition:
            return f"it is a special edition ({edition})"
        if m.country and country_of(unstaged(folder.name)) != m.country:
            return f"the release names a country ({m.country}) that the folder {folder.name!r} does not"
        if m.kind == "episode" and self.cfg.review.delete_duplicates:
            # the show folder is the one folder of that title; a year in the release must be that folder's
            # own (an undated folder may hold another show of the same title)
            if m.year is not None and m.year != folder_year:
                return (f"the release names {m.year}, the show folder "
                        + ("no year" if folder_year is None else str(folder_year)))
        elif m.year is None or folder_year is None or m.year != folder_year:
            return f"no year in the release name to confirm it is the same {what}"
        sizes = [st[0] for st in (self.lib.stat_of(r) for r in refs) if st]
        if not sizes:
            return "the library copy could not be read"
        if f.size > max(sizes):
            return "it is larger than the library copy (a repaired or better copy?)"
        return ""

    def _replace(self, renames: list[Op], plan: ItemPlan, f: FileEntry, m: MediaId, folder: LibFolder,
                 refs: list[str], reason: str, note: str, ref_episodes: dict[str, set[int]] | None = None
                 ) -> FileDecision | str:
        """review.replace_duplicates: the download takes the place of the library video of the same episode or
        film, whatever its size or release (the newest download wins); the other library files (NFO, artwork,
        subtitles) stay.  The download keeps its own name, so tMM's next update sees a new video and scrapes
        and renames it.  -> the decision (its operations added to `renames`), or why it cannot replace it."""
        if m.part:
            return "it is one part of a split file"
        if m.country and country_of(unstaged(folder.name)) != m.country:
            return f"the release names a country ({m.country}) that the folder {folder.name!r} does not"
        if m.kind == "movie" and (m.year is None or folder.year is None or m.year != folder.year):
            return "no year in the release name to confirm it is the same film"
        if m.kind == "episode" and m.year is not None and m.year != folder.year:
            return f"the release names {m.year}, the show folder " + ("no year" if folder.year is None
                                                                        else str(folder.year))
        if len(refs) != 1:
            return f"the library has {len(refs)} files for it"
        ref = refs[0]
        st = self.lib.stat_of(ref)
        if not st or st[0] <= 0:
            return "the library copy could not be read"
        extra = sorted((ref_episodes or {}).get(ref, set()) - set(m.episodes)) if m.kind == "episode" else []
        if extra:
            return "the library file also holds " + ", ".join(f"S{m.season:02d}E{e:02d}" for e in extra)
        parent, old = os.path.split(ref)
        in_place = same_name(old, f.dest_name)
        if not in_place and self.lib.exists_name(parent, f.dest_name, ignore=(old,)):
            return f"a file named {f.dest_name!r} already exists in {self._show(parent)!r}"
        dst = os.path.join(parent, f.dest_name)
        new = self._rename(f, dst, "media")
        gone = Op("unlink", dst=ref, role="replaced", expect_size=st[0], expect_mtime=st[1], ref=dst)
        if in_place:  # the same name: the old file goes first, and only while the download is still there
            gone.ref_files = ((f.path, f.size, f.mtime),)
            renames.extend([gone, new])
        else:  # the new file first: the library always holds a copy
            gone.ref_files = ((dst, f.size, f.mtime),)
            renames.extend([new, gone])
        self.lib.plan_remove(ref)
        self.lib.plan_file(parent, f.dest_name)
        if m.kind == "episode":
            self.lib.plan_episodes(folder.path, m.season, m.episodes)
        plan.tmm_paths.setdefault("tvshow" if m.kind == "episode" else "movie", set()).add(folder.path)
        return FileDecision(f, "replace", f"{reason}; {REPLACED}", m, target=dst, refs=[ref],
                            notes=[note, f"replaces {self._show(ref)!r}, which is deleted"])

    def _by_size(self, refs: list[str]) -> list[str] | None:
        """All library files of `refs`, biggest first, or None when one of them cannot be read or is empty."""
        stats = [(r, self.lib.stat_of(r)) for r in refs]
        if not stats or any(not st or st[0] <= 0 for _, st in stats):
            return None
        return [r for r, _ in sorted(stats, key=lambda x: (-x[1][0], x[0]))]

    def _duplicate(self, f: FileEntry, m: MediaId, reason: str, note: str, refs: list[str],
                   folder: LibFolder, item_name: str) -> FileDecision:
        keep = self._certain(f, m, folder, refs, item_name)
        if not keep and self.cfg.review.delete_duplicates:
            ordered = self._by_size(refs)
            if ordered:
                return FileDecision(f, "delete", f"{reason}; {SAME}", m, notes=[note], refs=ordered, same=True)
        days = self.cfg.review.duplicates_retention
        if keep or days < 0:
            extra = f"kept in review until you decide: {keep}" if keep else "kept in review (retention -1)"
        else:
            extra = ("deleted from review at the end of this run" if days == 0
                     else f"deleted from review {days} day(s) after this run")
        return FileDecision(f, "duplicate", reason, m, notes=[note, extra], refs=refs, purge=not keep)

    def _placeholder_ops(self, renames: list[Op], placeholders: list[str], notes: list[str]) -> None:
        """Remove empty library files of the media being filed, before the media is moved in
        (a placeholder may carry the very name the download needs)."""
        for p in placeholders:
            st = self.lib.stat_of(p)
            self.lib.plan_remove(p)
            renames.append(Op("unlink", dst=p, role="empty-placeholder", expect_size=0,
                              expect_mtime=st[1] if st else None))
            notes.append(f"replaces the empty (0-byte) library file {self._show(p)!r}")

    # ------------------------------------------------------------------ episodes
    def _plan_episode(self, renames: list[Op], plan: ItemPlan, f: FileEntry, m: MediaId) -> FileDecision:
        root = self.cfg.paths.series
        match = match_series(m, self.lib.series)
        if match.status in ("ambiguous", "year_conflict"):
            return FileDecision(f, "skip", f"show folder {match.describe()}", m)
        if match.status == "new":
            tmpl = self.cfg.naming.series_folder if m.year else self.cfg.naming.series_folder_no_year
            name, why = self._new_folder(root, tmpl, m, "show")
            if name is None:
                return FileDecision(f, "skip", why, m)
            show = os.path.join(root, name)
            note = f"new show folder {name!r}"
            new_show = name
        else:
            show = match.folder.path
            note = f"show folder {match.folder.name!r}"
            new_show = None

        seasons = self.lib.season_dirs(show, m.season)
        if len(seasons) > 1:
            names = [os.path.basename(s) for s in seasons]
            return FileDecision(f, "skip", f"several folders for season {m.season}: {names}", m)
        try:
            season_dir = seasons[0] if seasons else os.path.join(
                show, safe_component(self.cfg.naming.season_folder.format(season=m.season)))
        except UnsafeName as exc:
            return FileDecision(f, "skip", f"cannot build season folder name: {exc}", m)

        listed_season = season_dir if seasons else None
        existing = self.lib.existing_episodes(show, m.season, listed_season)
        wanted = set(m.episodes)
        overlap = sorted(wanted & set(existing))
        if overlap and len(overlap) < len(wanted):
            # e.g. a S01E01E02 file while the library has only S01E01: neither a duplicate nor new
            have = ", ".join(f"S{m.season:02d}E{e:02d}" for e in overlap)
            lack = ", ".join(f"S{m.season:02d}E{e:02d}" for e in sorted(wanted - set(existing)))
            return FileDecision(f, "skip", f"the library has {have} but not {lack}: a multi-episode file "
                                           f"needs your decision", m)
        if overlap:
            planned = [f"S{m.season:02d}E{e:02d}" for e in overlap if existing[e] == PLANNED]
            if planned:  # never a duplicate of a file that is not in the library yet
                return FileDecision(f, "skip", f"{', '.join(planned)} is filed from another download in this run; "
                                               f"this one is handled in the next run", m)
            what = ", ".join(f"S{m.season:02d}E{e:02d} as {existing[e]!r}" for e in overlap)
            extra = " (this release is a REPACK/PROPER)" if m.proper else ""
            paths = self.lib.episode_paths(show, m.season, listed_season)
            refs = sorted({p for e in overlap for p in paths.get(e, [])})
            same = self._identical(f, refs)
            if same:
                return FileDecision(f, "delete", f"already in library: {what}; {IDENTICAL}", m, notes=[note],
                                    refs=[same])
            not_replaced = ""
            if self.cfg.review.replace_duplicates:
                held = {p: {e for e, ps in paths.items() if p in ps} for p in refs}
                r = self._replace(renames, plan, f, m, match.folder, refs, f"already in library: {what}{extra}",
                                  note, held)
                if isinstance(r, FileDecision):
                    return r
                not_replaced = f"not replaced: {r}"
            d = self._duplicate(f, m, f"already in library: {what}{extra}", note, refs, match.folder,
                                plan.item.name)
            if not_replaced:
                d.notes.append(not_replaced)
            return d
        empties = self.lib.episode_paths(show, m.season, listed_season, empty=True)
        placeholders = sorted({p for e in m.episodes for p in empties.get(e, [])})
        ignore = tuple(os.path.basename(p) for p in placeholders if os.path.dirname(p) == season_dir)
        if self.lib.exists_name(season_dir, f.dest_name, ignore=ignore):
            return FileDecision(f, "skip", f"destination file name already exists in {season_dir}", m)
        notes = [note]
        if seasons:
            only_extras = self.lib.existing_episodes(show, m.season, season_dir, extras=True)
            for e in sorted(set(m.episodes) & set(only_extras)):
                notes.append(f"library had only {only_extras[e]!r} for S{m.season:02d}E{e:02d} "
                             f"(sample/trailer, not counted)")

        if new_show:
            self.lib.plan_dir(root, new_show)
        if not seasons:
            self.lib.plan_dir(show, os.path.basename(season_dir))
        self._placeholder_ops(renames, placeholders, notes)
        dst = os.path.join(season_dir, f.dest_name)
        renames.append(self._rename(f, dst, "media"))
        self.lib.plan_file(season_dir, f.dest_name)
        self.lib.plan_episodes(show, m.season, m.episodes)
        plan.tmm_paths.setdefault("tvshow", set()).add(show)
        return FileDecision(f, "move", note, m, target=dst, notes=notes[1:])

    # ------------------------------------------------------------------ movies
    def _plan_movie(self, renames: list[Op], plan: ItemPlan, f: FileEntry, m: MediaId) -> FileDecision:
        root = self.cfg.paths.movies
        movie_notes: list[str] = []
        placeholders: list[str] = []
        match = match_movie(m, self.lib.movies)
        if match.status in ("ambiguous", "near_year"):
            return FileDecision(f, "skip", f"movie folder {match.describe()}", m)
        if match.status == "new":
            tmpl = self.cfg.naming.movie_folder if m.year else self.cfg.naming.movie_folder_no_year
            name, why = self._new_folder(root, tmpl, m, "movie")
            if name is None:
                return FileDecision(f, "skip", why, m)
            folder = os.path.join(root, name)
            note, new_folder = f"new movie folder {name!r}", name
            if m.year is None:
                movie_notes.append("no year in the name: tMM adds it to the folder name when it recognises the "
                                   "movie (scrape + rename)")
        else:
            folder = match.folder.path
            note, new_folder = f"movie folder {match.folder.name!r}", None
            videos = self.lib.movie_videos(folder)
            if videos:
                extra = " (this release is a REPACK/PROPER)" if m.proper else ""
                refs = [p for p in (os.path.join(folder, v) for v in videos) if self.lib.stat_of(p)]
                if not refs:  # never a duplicate of a file that is not in the library yet
                    return FileDecision(f, "skip", f"movie folder {match.folder.name!r} receives another download "
                                                   f"in this run; this one is handled in the next run", m)
                same = self._identical(f, refs)
                if same:
                    return FileDecision(f, "delete", f"movie folder already has {os.path.basename(same)!r}; "
                                                     f"{IDENTICAL}", m, notes=[note], refs=[same])
                if m.year is None:  # a title alone does not prove it is the same film (remakes)
                    return FileDecision(f, "skip", f"movie folder {match.folder.name!r} already has {videos[0]!r} "
                                                   f"and the name has no year to tell whether it is the same film", m)
                not_replaced = ""
                if self.cfg.review.replace_duplicates:
                    r = self._replace(renames, plan, f, m, match.folder, refs,
                                      f"movie folder already has {videos[0]!r}{extra}", note)
                    if isinstance(r, FileDecision):
                        return r
                    not_replaced = f"not replaced: {r}"
                d = self._duplicate(f, m, f"movie folder already has {videos[0]!r}{extra}", note, refs,
                                    match.folder, plan.item.name)
                if not_replaced:
                    d.notes.append(not_replaced)
                return d
            extras = [v for v in self.lib.movie_videos(folder, include_extras=True) if is_library_extra(v)]
            if extras:
                movie_notes.append(f"movie folder had only {extras[0]!r} (sample/trailer, not counted)")
            placeholders = self.lib.empty_movie_videos(folder)
        if self.lib.exists_name(folder, f.dest_name, ignore=tuple(os.path.basename(p) for p in placeholders)):
            return FileDecision(f, "skip", f"destination file name already exists in {folder}", m)
        if new_folder:
            self.lib.plan_dir(root, new_folder)
        self._placeholder_ops(renames, placeholders, movie_notes)
        dst = os.path.join(folder, f.dest_name)
        renames.append(self._rename(f, dst, "media"))
        self.lib.plan_file(folder, f.dest_name)
        plan.tmm_paths.setdefault("movie", set()).add(folder)
        return FileDecision(f, "move", note, m, target=dst, notes=movie_notes)

    # ------------------------------------------------------------------ sidecars
    @staticmethod
    def sidecar_name(video_name: str, sub_name: str, source_video: str | None = None) -> str:
        """Subtitle name next to `video_name`: 'Video.en.srt' when the subtitle is named after the video
        (or after `source_video`, the download's own name, when the video is a library copy), else
        'Video.<subtitle name>.srt' ('Subs/English.srt' -> 'Video.English.srt')."""
        vstem = os.path.splitext(video_name)[0]
        sstem, sext = os.path.splitext(sub_name)
        stems = {vstem, os.path.splitext(source_video)[0] if source_video else ""} - {""}
        for stem in sorted(stems, key=len, reverse=True):  # the longest name the subtitle starts with
            if sstem.casefold() == stem.casefold() or sstem.casefold().startswith(stem.casefold() + "."):
                return vstem + sub_name[len(stem):]
        return f"{vstem}.{safe_component(sstem)}{sext.lower()}"

    def _dup_target(self, item: Item, f: FileEntry, name: str | None = None) -> str:
        """Where a duplicate video (or its subtitle) goes: review/duplicates/<run>/<item>/<same path>."""
        name = name or f.name
        if not item.is_dir:
            return os.path.join(self.review_dup, name)
        sub = os.path.dirname(f.rel)
        return os.path.join(self.review_dup, item.name, *([sub] if sub else []), name)

    def _plan_sidecars(self, item: Item, renames: list[Op], mains, subs, decisions, moved: set[str],
                       blocked: frozenset = frozenset()) -> None:
        """Subtitles follow their video: next to the moved file, next to the library copy when the
        download was deleted as identical to it, or along with a duplicate into review/duplicates.
        A subtitle the download client did not fully receive (`blocked`) stays with the leftovers."""
        by_file = {d.file.path: d for d in decisions}
        blocked = frozenset(unicodedata.normalize("NFC", p) for p in blocked)
        for sub in sorted(subs, key=lambda s: s.rel):
            owner = None
            if len(mains) == 1:
                owner = by_file[mains[0].path]
            else:  # 'X.en.srt' next to 'X.mkv', or 'Subs/X/2_English.srt' (a folder named after the video)
                parent = os.path.basename(os.path.dirname(sub.rel)).casefold()
                for v in mains:
                    vstem = os.path.splitext(v.dest_name)[0].casefold()
                    if sub.name.casefold().startswith(vstem + ".") or parent == vstem:
                        owner = by_file[v.path]
                        break
            if owner is None:
                continue
            if unicodedata.normalize("NFC", sub.path) in blocked:
                owner.notes.append(f"subtitle {sub.rel!r} not filed (not fully downloaded): left with the leftovers")
                continue
            if owner.action == "duplicate":
                dst = self._dup_target(item, sub)
                if self._review_target(dst):
                    op = self._rename(sub, dst, "duplicate-sidecar")
                    op.purge = owner.purge
                    renames.append(op)
                    moved.add(sub.path)
                continue
            if owner.action == "move":
                video = owner.target
            elif owner.action == "delete" and owner.refs and not owner.same:  # byte-identical: same release
                video = owner.refs[0]
            else:
                continue
            target_dir = os.path.dirname(video)
            try:
                name = self.sidecar_name(os.path.basename(video), sub.name, owner.file.dest_name)
            except UnsafeName:
                owner.notes.append(f"subtitle {sub.rel!r} not filed (unusable name)")
                continue
            if self.lib.exists_name(target_dir, name):
                owner.notes.append(f"subtitle {sub.rel!r} not filed (name exists at destination)")
                continue
            renames.append(self._rename(sub, os.path.join(target_dir, name), "sidecar"))
            self.lib.plan_file(target_dir, name)
            moved.add(sub.path)
            owner.notes.append(f"subtitle {sub.rel!r} -> {name!r}")

    # ------------------------------------------------------------------ ambiguity
    def _match(self, m: MediaId) -> Match:
        return match_series(m, self.lib.series) if m.kind == "episode" else match_movie(m, self.lib.movies)

    def _resolve(self, candidates: list[MediaId]) -> tuple[MediaId | None, str]:
        """Two readings of one name: keep the single one that fits an existing library folder."""
        hits = [c for c in candidates if self._match(c).status == "match"]
        labels = " / ".join(repr(c.label()) for c in candidates)
        if len(hits) == 1:
            return hits[0], f"name read as {hits[0].label()!r} (the only reading matching the library; also {labels})"
        what = "none" if not hits else "several"
        return None, f"ambiguous name: {labels}; {what} of these match an existing library folder"

    @staticmethod
    def _tree(files) -> frozenset:
        return frozenset((f.rel, f.size, int(f.mtime)) for f in files)

    # ------------------------------------------------------------------ remains
    def plan_remains(self, item: Item, origin_run: str) -> ItemPlan:
        """What is left of an item a previous run started (crash or failed op) -> review."""
        plan = ItemPlan(item=item, status="ready",
                        reason=f"remains of an item partly processed by run {origin_run} -> review/leftovers")
        if item.problems:
            plan.status, plan.reason = "skip", "item left untouched: " + "; ".join(item.problems[:3])
            return plan
        emitted: set[str] = set()
        if item.files or not item.is_dir:
            op = Op("rename", src=item.path, dst=os.path.join(self.review_left, item.name), role="leftovers")
            if item.is_dir:
                op.expect_tree = self._tree(item.files)
            else:
                op.expect_size, op.expect_mtime = item.files[0].size, item.files[0].mtime
            self._with_parents(plan.ops, op, emitted)
        else:
            for d in sorted(item.dirs, key=lambda p: -p.count("/")):
                plan.ops.append(Op("rmdir", dst=d, role="cleanup"))
            plan.ops.append(Op("rmdir", dst=item.path, role="cleanup"))
        return plan

    # ------------------------------------------------------------------ item
    def _identify_all(self, item: Item, mains: list[FileEntry], readiness: Readiness
                      ) -> list[tuple[FileEntry, MediaId | None, list[str], str]]:
        """(file, media or None, notes, reason when None) for every main video of the item."""
        single = len(mains) == 1
        blocked = {unicodedata.normalize("NFC", p) for p in readiness.blocked_files}
        out = []
        for f in sorted(mains, key=lambda x: x.rel):
            if unicodedata.normalize("NFC", f.path) in blocked:
                out.append((f, None, [], "file not fully downloaded (download client)"))
                continue
            parents = (item.name, *f.rel.split("/")[:-1]) if item.is_dir else ()
            idr = identify_video(f.name, item.name if item.is_dir else None, single, parents)
            if idr.media is None:
                out.append((f, None, [], f"not identified: {idr.reason}"))
                continue
            media, notes = idr.media, list(idr.notes)
            if idr.alternatives:
                media, why = self._resolve([idr.media, *idr.alternatives])
                if media is None:
                    out.append((f, None, [], why))
                    continue
                notes.append(why)
            if f.added_ext:
                notes.append(f"no video extension in the name: filed as {f.dest_name!r} "
                             f"({f.added_ext[1:]} content)")
            out.append((f, media, notes, ""))
        return out

    def _item_checks(self, item: Item, mains: list[FileEntry], ids) -> list:
        """Item-level rules for the identities that rest on weaker evidence."""
        if not item.is_dir:
            return ids
        # A folder holding more non-video data than video (at least 64 MiB of it) is not a movie or
        # season folder: software with a promo clip, a disc image.  Videos of any kind (extras too)
        # and archives do not count: an extracted release keeps its .rar volumes next to the video.
        video_exts = self.cfg.media.video_extensions
        other = sum(f.size for f in item.files
                    if f.ext not in video_exts and not is_archive(f.name)
                    and not (video_like(f.name, self.cfg) and os.path.splitext(f.name)[1].lower() != ".iso"))
        main_bytes = sum(f.size for f in mains)
        outweighed = other >= OTHER_BYTES_MIN and main_bytes < other
        movies = [m for _, m, _, _ in ids if m is not None and m.kind == "movie"]
        same_movie = len({(norm(m.title), m.year) for m in movies}) < len(movies)
        claims: dict[tuple, list[str]] = {}  # (show, season, episode) -> files of this download naming it
        for f, m, _, _ in ids:
            if m is not None and m.kind == "episode":
                for e in m.episodes:
                    claims.setdefault((norm(m.title), m.season, e), []).append(f.path)
        clash = {p for paths in claims.values() if len(paths) > 1 for p in paths}
        out = []
        for f, m, notes, why in ids:
            if m is not None and m.kind == "episode" and f.path in clash:  # never pick one of them
                out.append((f, None, notes, f"several files of this download are {m.label()}: parts, versions "
                                            f"or bonus material? needs your decision"))
                continue
            if m is not None and (m.kind == "movie" or m.source == "folders") and outweighed:
                what = "a movie" if m.kind == "movie" else "episodes"
                m, why = None, (f"the folder holds more other data ({human(other)}) than video ({human(main_bytes)}): "
                                f"not filed as {what} (software or disc image?)")
            elif m is not None and m.kind == "movie" and same_movie:
                m, why = None, ("several files of the same movie in one folder (parts, versions or qualities): "
                                "needs your decision")
            elif m is not None and is_weak_movie(m) and len(movies) > 1:
                m, why = None, "several movies in one folder and this one has no year"
            out.append((f, m, notes, why))
        return out

    def _leftover_note(self, item: Item, remaining: list[FileEntry]) -> str:
        """What happens to the leftovers once their retention is over (review.py rules, applied to the
        path the file gets inside the review run folder)."""
        gone, kept = [], []
        for f in remaining:
            rel = f"{item.name}/{f.rel}" if item.is_dir else f.name
            is_video = video_like(f.name, self.cfg) or f.ext in self.cfg.media.video_extensions
            if disposable("leftovers", rel, f.size, self.cfg, {}, f.path):
                if is_video:
                    gone.append(f.rel)
            else:
                kept.append(f.rel)
        parts = []
        if gone:
            parts.append(f"sample/trailer video(s), deleted with the leftovers: {_short(gone)}")
        if kept:
            parts.append(f"KEPT until you delete them (videos and anything but release clutter): {_short(kept)}")
        return "; ".join(parts)

    def plan_item(self, item: Item, readiness: Readiness) -> ItemPlan:
        plan = ItemPlan(item=item, status="skip")
        mains, extras, subs = self.classify(item)
        if item.problems:  # symlink, unreadable entry, invalid name: even "no video" may be wrong
            plan.reason = "item left untouched: " + "; ".join(item.problems[:3])
            return plan
        plan.stop_tasks = readiness.stop_tasks if readiness.ready else frozenset()
        if self._empty_tree(item):  # nothing in it, but removed only once the download client is done
            if not readiness.ready:
                plan.status, plan.reason = "wait", readiness.reason
                return plan
            plan.status, plan.reason = "ready", "empty folder (no file in it): removed"
            for d in sorted(item.dirs, key=lambda p: -p.count("/")):
                plan.ops.append(Op("rmdir", dst=d, role="cleanup"))
            plan.ops.append(Op("rmdir", dst=item.path, role="cleanup"))
            return plan
        if not mains:
            plan.status = "ignored"
            plan.reason = (f"only video(s) named or filed like samples/trailers/extras: {_short(f.rel for f in extras)}"
                           f" (not filed automatically; move it by hand if it is the episode or movie)"
                           if extras else "no video files")
            return plan
        if not readiness.ready:
            plan.status, plan.reason = "wait", readiness.reason
            return plan

        renames: list[Op] = []
        decisions: list[FileDecision] = []
        for f, media, notes, why in self._item_checks(item, mains, self._identify_all(item, mains, readiness)):
            if media is None:
                decisions.append(FileDecision(f, "skip", why))
                continue
            plan_fn = self._plan_episode if media.kind == "episode" else self._plan_movie
            d = plan_fn(renames, plan, f, media)
            d.notes.extend(notes)
            decisions.append(d)
        plan.decisions = decisions

        for d in decisions:  # duplicates: only the video (and its subtitles) go to review/duplicates
            if d.action != "duplicate":
                continue
            dst = self._dup_target(item, d.file, d.file.dest_name)
            if not self._review_target(dst):
                d.action, d.reason = "skip", f"{d.reason}; another duplicate of this run has the same review name"
                continue
            d.target = dst
        moves = [d for d in decisions if d.action in ("move", "replace")]
        dups = [d for d in decisions if d.action == "duplicate"]
        dels = [d for d in decisions if d.action == "delete"]
        skips = [d for d in decisions if d.action == "skip"]
        moved: set[str] = {d.file.path for d in moves}  # item files that leave the item (moved or deleted)
        emitted: set[str] = set()

        for d in dups:
            op = self._rename(d.file, d.target, "duplicate")
            op.purge = d.purge
            renames.append(op)
            moved.add(d.file.path)
        self._plan_sidecars(item, renames, mains, subs, decisions, moved, readiness.blocked_files)
        # deletions last: if any move of this item fails, the item stops before anything is deleted
        for d in dels:
            renames.append(self._delete_op(d))
            moved.add(d.file.path)
        for op in renames:
            self._with_parents(plan.ops, op, emitted)

        if skips:
            plan.status = "partial" if (moves or dups or dels) else "skip"
            plan.reason = f"{len(skips)} of {len(decisions)} main video(s) not handled; item stays in Downloads"
            return plan

        plan.status = "ready"
        remaining = [f for f in item.files if f.path not in moved]
        if remaining:
            note = self._leftover_note(item, remaining)
            plan.reason = f"{len(remaining)} leftover file(s) -> review/leftovers" + (f"; {note}" if note else "")
        if item.is_dir:
            if remaining:
                op = Op("rename", src=item.path, dst=os.path.join(self.review_left, item.name), role="leftovers",
                        expect_tree=self._tree(remaining))
                self._with_parents(plan.ops, op, emitted)
            else:
                for d in sorted(item.dirs, key=lambda p: -p.count("/")):
                    plan.ops.append(Op("rmdir", dst=d, role="cleanup"))
                plan.ops.append(Op("rmdir", dst=item.path, role="cleanup"))
        else:  # loose video handled; subtitles attached to it that did not travel with it
            for f in remaining:
                self._with_parents(plan.ops, self._rename(f, os.path.join(self.review_left, f.name), "leftovers"),
                                   emitted)
        return plan
