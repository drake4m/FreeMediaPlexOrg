"""Read-only inventory of the library as the planner sees it (`mediaorg library`).

Per show: the season folders and the episode numbers the tool recognises (the same rules the
planner uses to decide "already in library").  Findings list whatever would make the tool
decide differently from what a person expects; nothing is ever changed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .library import Library, episodes_in_name, is_library_extra, season_number

# artwork folders written by tMM/Kodi inside show folders: expected, not reported
_ARTWORK_DIRS = {"extrafanart", "extrathumbs"}


@dataclass
class Finding:
    level: str  # "warn": can change a decision | "note": informational
    where: str  # e.g. "Series/Cistern (2022)/Season 1"
    text: str


@dataclass
class ShowSummary:
    name: str
    seasons: dict[int, int]  # season -> distinct episode numbers recognised
    extras: int  # sample/trailer files ignored


@dataclass
class Inventory:
    shows: list[ShowSummary] = field(default_factory=list)
    movie_folders: int = 0
    movies_with_video: int = 0
    movies_without_video: int = 0
    movie_extras: int = 0
    findings: list[Finding] = field(default_factory=list)

    def add(self, level: str, where: str, text: str) -> None:
        self.findings.append(Finding(level, where, text))


def _short(names, n: int = 3) -> str:
    names = sorted(names)
    shown = ", ".join(repr(x) for x in names[:n])
    return shown + (f" (+{len(names) - n} more)" if len(names) > n else "")


def _label(root: str) -> str:
    return os.path.basename(root.rstrip("/")) or root


def _stray_videos(lib: Library, inv: Inventory, root: str, what: str) -> None:
    _, files = lib.listing(root)
    stray = [f for f in files if lib.is_video(f)]
    if stray:
        inv.add("warn", _label(root), f"{len(stray)} video file(s) outside any {what} folder, not seen by the "
                                      f"tool: {_short(stray)}")


def _empty_videos(lib: Library, inv: Inventory, folder: str, names, where: str) -> None:
    """0-byte video files are broken placeholders, not media: never counted, replaced by a download."""
    empty = [f for f in names if lib.is_video(f) and not is_library_extra(f) and lib.is_empty(os.path.join(folder, f))]
    if empty:
        inv.add("warn", where, f"{len(empty)} empty (0-byte) video file(s), not counted as present: {_short(empty)}; "
                               f"a download of the same episode/movie replaces them")


def _series(lib: Library, inv: Inventory) -> None:
    top = _label(lib.series_root)
    _stray_videos(lib, inv, lib.series_root, "show")
    for show in sorted((f for f in lib.series if not f.planned), key=lambda f: f.name.casefold()):
        where = f"{top}/{show.name}"
        try:
            dirs, files = lib.listing(show.path)
        except OSError as exc:
            inv.add("warn", where, f"cannot be read ({exc.strerror or exc}); the tool cannot use it either")
            inv.shows.append(ShowSummary(show.name, {}, 0))
            continue
        by_season: dict[int, list[str]] = {}
        other: list[str] = []
        for d in dirs:
            if d.startswith(".") or d.casefold() in _ARTWORK_DIRS:
                continue
            n = season_number(d)
            if n is None:
                other.append(d)
            else:
                by_season.setdefault(n, []).append(d)

        eps: dict[int, dict[int, list[str]]] = {}
        extras = 0
        # episode files directly in the show folder count for every season they name (as in the planner)
        root_eps, root_unread = 0, []
        _empty_videos(lib, inv, show.path, files, where)
        for f in files:
            if not lib.is_video(f) or lib.is_empty(os.path.join(show.path, f)):
                continue
            if is_library_extra(f):
                extras += 1
                continue
            found = episodes_in_name(f, None)
            if not found:
                root_unread.append(f)
            root_eps += bool(found)
            for s, e in found:
                eps.setdefault(s, {}).setdefault(e, []).append(f)
        if root_eps:
            inv.add("note", where, f"{root_eps} episode file(s) directly in the show folder (counted as present; "
                                   f"new episodes still go into season folders)")
        if root_unread:
            inv.add("warn", where, f"video(s) in the show folder with no SxxEyy the tool can read: "
                                   f"{_short(root_unread)}")
        if other:
            inv.add("note", where, f"folder(s) not treated as seasons, contents ignored: {_short(other)}")

        for n, ds in sorted(by_season.items()):
            if len(ds) > 1:
                inv.add("warn", where, f"several folders for season {n}: {sorted(ds)} - downloads for season {n} "
                                       f"will be skipped until only one remains")
            eps.setdefault(n, {})
            for d in sorted(ds):
                sw = f"{where}/{d}"
                try:
                    sdirs, sfiles = lib.listing(os.path.join(show.path, d))
                except OSError as exc:
                    inv.add("warn", sw, f"cannot be read ({exc.strerror or exc}); episodes in it are not seen")
                    continue
                unread, misplaced = [], []
                sdir = os.path.join(show.path, d)
                _empty_videos(lib, inv, sdir, sfiles, sw)
                for f in sfiles:
                    if not lib.is_video(f) or lib.is_empty(os.path.join(sdir, f)):
                        continue
                    if is_library_extra(f):
                        extras += 1
                        continue
                    found = episodes_in_name(f, n)
                    if not found:
                        unread.append(f)
                        continue
                    mine = {(s, e) for s, e in found if s == n}
                    if not mine:
                        misplaced.append((f, sorted(found)))
                    for _s, e in mine:
                        eps[n].setdefault(e, []).append(f)
                if unread:
                    inv.add("warn", sw, f"{len(unread)} video(s) with no episode number the tool can read (a "
                                        f"download of that episode would be moved in next to it): {_short(unread)}")
                for f, found in misplaced:
                    s, e = found[0]
                    inv.add("warn", sw, f"{f!r} is S{s:02d}E{e:02d} but sits in {d!r}: not seen as present for "
                                        f"season {s}, so a download of it would be moved into season {s} too")
                sub = [x for x in sdirs if not x.startswith(".")]
                if sub:
                    inv.add("note", sw, f"subfolder(s) ignored: {_short(sub)}")

        for s, by_ep in sorted(eps.items()):
            multi = {e: fs for e, fs in by_ep.items() if len(fs) > 1}
            if multi:
                shown = "; ".join(f"S{s:02d}E{e:02d}: {_short(fs, 2)}" for e, fs in sorted(multi.items())[:3])
                more = f" (+{len(multi) - 3} more)" if len(multi) > 3 else ""
                inv.add("note", where, f"{len(multi)} episode(s) present in several files: {shown}{more}")
        if not by_season and not root_eps:
            inv.add("note", where, "no season folder and no episode file")
        if extras:
            inv.add("note", where, f"{extras} sample/trailer file(s) ignored (not counted as episodes)")
        inv.shows.append(ShowSummary(show.name, {s: len(e) for s, e in eps.items()}, extras))


def _movies(lib: Library, inv: Inventory) -> None:
    top = _label(lib.movies_root)
    _stray_videos(lib, inv, lib.movies_root, "movie")
    for mv in sorted((f for f in lib.movies if not f.planned), key=lambda f: f.name.casefold()):
        where = f"{top}/{mv.name}"
        inv.movie_folders += 1
        try:
            videos = lib.movie_videos(mv.path, include_extras=True)
        except OSError as exc:
            inv.add("warn", where, f"cannot be read ({exc.strerror or exc}); the tool cannot use it either")
            continue
        _empty_videos(lib, inv, mv.path, videos, where)
        main = [f for f in videos if not is_library_extra(f) and not lib.is_empty(os.path.join(mv.path, f))]
        extras = [f for f in videos if is_library_extra(f)]
        inv.movie_extras += len(extras)
        if not main:
            inv.movies_without_video += 1
            only = f" (only {_short(extras)}, ignored)" if extras else ""
            inv.add("note", where, f"no movie file{only}: a matching download will be moved in")
        else:
            inv.movies_with_video += 1
            if len(main) > 1:
                inv.add("note", where, f"{len(main)} video files ({_short(main)}): a matching download goes to "
                                       f"review/duplicates")


def build(lib: Library) -> Inventory:
    inv = Inventory()
    _series(lib, inv)
    _movies(lib, inv)
    return inv


def render(inv: Inventory, series_root: str, movies_root: str) -> str:
    out = []
    n_seasons = sum(len(s.seasons) for s in inv.shows)
    n_eps = sum(sum(s.seasons.values()) for s in inv.shows)
    out.append(f"SERIES {series_root}: {len(inv.shows)} shows, {n_seasons} seasons, {n_eps} episodes recognised")
    width = min(max((len(s.name) for s in inv.shows), default=0), 52)
    for s in inv.shows:
        seasons = " ".join(f"S{n}:{c}" for n, c in sorted(s.seasons.items())) or "-"
        out.append(f"  {s.name:<{width}}  {seasons}")
    out.append(f"MOVIES {movies_root}: {inv.movie_folders} folders, {inv.movies_with_video} with a movie file, "
               f"{inv.movies_without_video} without")
    warns = [f for f in inv.findings if f.level == "warn"]
    notes = [f for f in inv.findings if f.level != "warn"]
    out.append("")
    if inv.findings:
        out.append("FINDINGS (how the tool will treat these; nothing was changed):")
        out.extend(f"  [{f.level}] {f.where}: {f.text}" for f in (*warns, *notes))
        out.append("")
    extras = sum(s.extras for s in inv.shows) + inv.movie_extras
    out.append(f"library: warnings {len(warns)}, notes {len(notes)}, sample/trailer files ignored {extras}")
    return "\n".join(out)
