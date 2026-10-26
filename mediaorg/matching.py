"""Match a MediaId against existing library folders.  Pure: no filesystem access.

A folder is reused only when exactly one candidate fits.  Several
candidates, a year conflict or a near-year movie are reported instead of guessed.
There is no substring matching.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .identify import title_keys
from .model import MediaId
from .normalize import Keys, make_keys, norm, split_folder_name


@dataclass(frozen=True)
class LibFolder:
    name: str
    path: str
    title: str
    year: int | None
    keys: Keys
    planned: bool = False  # created by the current plan, not on disk yet


def lib_folder(name: str, path: str, planned: bool = False) -> LibFolder:
    title, year = split_folder_name(name)
    return LibFolder(name=name, path=path, title=title, year=year, keys=make_keys(title), planned=planned)


@dataclass
class Match:
    status: str  # match | new | ambiguous | year_conflict | near_year
    folder: LibFolder | None = None
    candidates: list[LibFolder] = field(default_factory=list)
    note: str = ""

    def describe(self) -> str:
        names = ", ".join(repr(c.name) for c in self.candidates)
        return f"{self.status}: {self.note} [{names}]" if names else f"{self.status}: {self.note}"


def _candidates(media: MediaId, folders: list[LibFolder]) -> list[LibFolder]:
    keys = title_keys(media)
    if keys.empty:
        return []
    cands = [f for f in folders if keys.intersects(f.keys)]
    if media.country:
        # 'Brazen US' -> prefer a 'Brazen (US)' folder over 'Brazen'
        ckeys = make_keys(f"{media.title} {media.country}")
        specific = [f for f in folders if ckeys.intersects(f.keys)]
        if specific:
            return specific
    return cands


def match_series(media: MediaId, folders: list[LibFolder]) -> Match:
    cands = _candidates(media, folders)
    if not cands:
        return Match("new", note="no existing show folder")
    if media.year:
        same = [c for c in cands if c.year == media.year]
        if len(same) == 1:
            return Match("match", same[0], same)
        if len(same) > 1:
            return Match("ambiguous", None, same, f"several folders for {media.title} ({media.year})")
        dated = [c for c in cands if c.year is not None]
        undated = [c for c in cands if c.year is None]
        if dated:
            return Match(
                "year_conflict", None, cands, f"release says {media.year}, library folder(s) differ"
            )
        if len(undated) == 1:
            return Match("match", undated[0], undated, "folder has no year yet")
        return Match("ambiguous", None, undated, "several undated folders")
    if len(cands) == 1:
        return Match("match", cands[0], cands)
    return Match("ambiguous", None, cands, "release has no year to choose between folders")


def match_movie(media: MediaId, folders: list[LibFolder]) -> Match:
    cands = _candidates(media, folders)
    if not cands:
        return Match("new", note="no existing movie folder")
    if media.year is None:  # title only: reuse the one folder with this title, never choose between several
        if len(cands) == 1:
            return Match("match", cands[0], cands, "no year in the name; the only folder with this title")
        return Match("ambiguous", None, cands, "no year in the name to choose between folders")
    same = [c for c in cands if c.year == media.year]
    if len(same) == 1:
        return Match("match", same[0], same)
    if len(same) > 1:
        return Match("ambiguous", None, same, "several folders with the same title and year")
    near = [c for c in cands if c.year is not None and abs(c.year - media.year) == 1]
    if near:
        return Match("near_year", None, near, f"release year {media.year} is one year off")
    undated = [c for c in cands if c.year is None]
    if len(undated) == 1 and all(c.year is None for c in cands):
        # e.g. 'Movies/Hollow Reeds' filed from a release without year, not renamed by tMM yet
        return Match("match", undated[0], undated, "folder has no year yet")
    if undated:
        return Match("ambiguous", None, undated, "folder with same title but no year")
    return Match("new", note=f"same title exists with other year(s): {[c.year for c in cands]}")


def titles_equal(a: str, b: str) -> bool:
    return norm(a) == norm(b)
