"""Name normalisation, matching keys and safe path components (pure functions)."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_APOSTROPHES = re.compile(r"['’‘`´]")
_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)
_WS = re.compile(r"\s+")
_YEAR_SUFFIX = re.compile(r"^(?P<title>.*?)\s*\((?P<year>\d{4})\)\s*$")
# naming.tmm_staging: new library folders are named 'Title (Year) [mediaorg]' until tinyMediaManager renames
# them (tMM ignores text in [...] when it reads a folder name)
STAGING_TAG = "[mediaorg]"
_STAGING_SUFFIX = re.compile(r"\s*\[mediaorg\]\s*$", re.IGNORECASE)
_RESERVED = re.compile(r'[\\/*?"<>|\x00-\x1f]')
_ARTICLE = re.compile(r"^the ")
_SEASON_DIR = re.compile(
    r"^(?:season|saison|series|staffel|temporada|stagione)[\s._-]*0*(\d{1,3})$|^s0*(\d{1,3})$",
    re.IGNORECASE,
)
_SPECIALS_DIR = re.compile(r"^specials?$", re.IGNORECASE)


def season_number(dirname: str) -> int | None:
    """'Season 2', 'Saison 02', 'S2' -> 2; 'Specials' -> 0; anything else -> None."""
    if _SPECIALS_DIR.match(dirname):
        return 0
    m = _SEASON_DIR.match(dirname.strip())
    if m:
        return int(m.group(1) or m.group(2))
    return None


def norm(text: str) -> str:
    """Comparison key: accents folded, case folded, punctuation -> space.

    'Ésker' -> 'esker', "Ada Corwin's" -> 'ada corwins', 'Kestrel - Quarry' -> 'kestrel quarry'.
    Non-Latin letters are kept (\\w is Unicode-aware), so CJK/Cyrillic titles do not collapse to ''.
    """
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.casefold().replace("&", " and ")
    t = _APOSTROPHES.sub("", t)
    t = _NON_WORD.sub(" ", t)
    return _WS.sub(" ", t).strip()


@dataclass(frozen=True)
class Keys:
    """Three tiers of keys; two names 'match' if any tier intersects."""

    exact: frozenset[str]
    noart: frozenset[str]
    compact: frozenset[str]

    def intersects(self, other: Keys) -> bool:
        return bool(
            self.exact & other.exact or self.noart & other.noart or self.compact & other.compact
        )

    def tier_with(self, other: Keys) -> str | None:
        if self.exact & other.exact:
            return "exact"
        if self.noart & other.noart:
            return "noart"
        if self.compact & other.compact:
            return "compact"
        return None

    @property
    def empty(self) -> bool:
        return not any(self.exact)


def make_keys(*variants: str) -> Keys:
    exact = {norm(v) for v in variants if v and norm(v)}
    noart = {_ARTICLE.sub("", k) for k in exact}
    compact = {k.replace(" ", "") for k in noart}
    return Keys(frozenset(exact), frozenset(noart), frozenset(compact))


def unstaged(name: str) -> str:
    """The folder name without mediaorg's staging tag: 'Brimstone (1999) [mediaorg]' -> 'Brimstone (1999)'."""
    return _STAGING_SUFFIX.sub("", name)


def is_staged(name: str) -> bool:
    return bool(_STAGING_SUFFIX.search(name))


def split_folder_name(name: str) -> tuple[str, int | None]:
    """'Brimstone (1999)' -> ('Brimstone', 1999); 'Glass Town' -> ('Glass Town', None); a staging tag
    ('Brimstone (1999) [mediaorg]') is not part of the title."""
    name = unstaged(name)
    m = _YEAR_SUFFIX.match(name)
    if m:
        return m.group("title").strip(), int(m.group("year"))
    return name.strip(), None


def smart_title(text: str) -> str:
    """Title-case only an all-lowercase title; keeps apostrophes intact."""
    if text != text.lower():
        return text
    return re.sub(r"(^|[\s\-(\[])([^\W\d_])", lambda m: m.group(1) + m.group(2).upper(), text)


class UnsafeName(ValueError):
    pass


def safe_component(name: str) -> str:
    """Make a single path component that is valid on the SMB share (Windows rules)."""
    n = unicodedata.normalize("NFC", name)
    n = re.sub(r"\s*:\s*", " - ", n)
    n = _RESERVED.sub("", n)
    n = _WS.sub(" ", n).strip().rstrip(". ")
    if not n or n in {".", ".."}:
        raise UnsafeName(f"empty name after sanitising {name!r}")
    if len(n.encode("utf-8")) > 255:
        raise UnsafeName(f"name longer than 255 bytes: {n[:60]!r}...")
    return n


def same_name(a: str, b: str) -> bool:
    """Filename equality as the SMB server sees it (case-insensitive, NFC)."""
    return unicodedata.normalize("NFC", a).casefold() == unicodedata.normalize("NFC", b).casefold()
