"""Release name -> MediaId.  Pure: no filesystem access.

guessit does the parsing; this module decides whether the result is trustworthy
enough to act on.  Anything that does not validate returns media=None with a reason,
which the planner turns into "skip and report".
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import re
from functools import lru_cache

from .model import IdResult, MediaId
from .normalize import make_keys, norm, season_number

# Release-site rule: drop a leading "www.site.tld -  " prefix.
_WEBSITE_PREFIX = re.compile(r"^\s*www\.[^\s-]+\s*-+\s*", re.IGNORECASE)
_SAMPLE_TOKEN = re.compile(r"(?i)(?:^|[\W_])sample(?:[\W_]|$)")
# 'Show.S01E05v2.mkv' (a re-released episode): guessit reads the whole thing as a movie title.
_EPISODE_VERSION = re.compile(r"(?i)((?<![a-z0-9])s\d{1,3}[ ._-]?e\d{1,4})v\d(?![a-z0-9])")
_EPISODE_TOKEN = re.compile(r"(?i)(?<![a-z0-9])s\d{1,3}[ ._-]?e\d{1,4}")
# A season must be written as one ('S02', 'S02E05', '2x05', 'Season 2'): guessit also reads bare
# numbers as season + episode ('tamarind-2048-surround.mp4' -> S20E48), which is never trusted.
_SEASON_MARKER = re.compile(
    r"(?i)(?<![a-z0-9])(?:s\d{1,3}(?![a-z0-9])|s\d{1,3}[ ._-]?e\d|\d{1,2}x\d{2,4}(?!\d)"
    r"|(?:seasons?|saisons?|series|staffel|temporada|stagione)[ ._-]*\d)")
# 'Part 2', 'Pt2', 'Part.1': one part of a split episode or movie (kept apart from the duplicate cleaning).
_PART_TOKEN = re.compile(r"(?i)(?<![a-z0-9])(?:part|pt)[ ._-]?\d{1,2}(?!\d)")
# guessit 'other' values of bonus material ('Cistern.S01.Extras.01' is not S01E01).
_EXTRA_OTHERS = frozenset({"Extras", "Bonus"})
# A file inside a season folder is an episode only if its name starts with the number ('05 - Title',
# 'E05', 'Episode 5') or with the show title; 'Recap 05', 'Deleted Scene 05' are not episodes.
_LEADING_EPISODE = re.compile(r"(?i)^(?:e|ep|episode|épisode)?[ ._-]*\d{1,3}(?!\d)")
# Words that make a folder name a pack, not part of the show title ('The Depot Complete').
_PACK_SUFFIX = re.compile(
    r"(?i)(?:[\s._-]+(?:the[\s._-]+)?(?:complete|int[eé]grale?|collection|coffret)"
    r"(?:[\s._-]+(?:series|collection|edition|seasons?|saisons?))?)+$")
# Evidence that a name is a video release (used to tell a versioned release from software).
_RELEASE_MARKERS = ("screen_size", "source", "video_codec", "audio_codec", "streaming_service",
                    "video_profile", "audio_channels", "color_depth")
_GUESSIT_OPTIONS = {"excludes": ["part"]}  # keep "Part Two" inside the title
MAX_SEASON = 100


def _max_year() -> int:
    return _dt.date.today().year + 1


@lru_cache(maxsize=4096)
def _guess_cached(name: str) -> tuple:
    from guessit import guessit  # imported lazily: keeps --help and config checks fast

    return tuple(guessit(name, dict(_GUESSIT_OPTIONS)).items())


def guess(name: str) -> dict:
    """guessit result as a plain dict, after the website-prefix cleanup."""
    cleaned = _EPISODE_VERSION.sub(r"\1", _WEBSITE_PREFIX.sub("", name))
    try:
        g = dict(_guess_cached(cleaned))
        if g.get("type") == "episode" and g.get("season") is not None and not _SEASON_MARKER.search(cleaned):
            g["_bare_season"] = True
        if _PART_TOKEN.search(cleaned):
            g["_part"] = True
        return g
    except ImportError:
        raise  # broken installation: fail loudly instead of "nothing identified"
    except Exception as exc:  # guessit raises on pathological input; never crash the run
        return {"_error": f"{type(exc).__name__}: {exc}"}


def _as_list(value) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def is_sample(name: str, g: dict | None = None) -> bool:
    g = guess(name) if g is None else g
    return bool(_SAMPLE_TOKEN.search(name)) or "Sample" in _as_list(g.get("other"))


def is_bonus(name: str, g: dict | None = None) -> bool:
    """Bonus material by its name: 'Cistern.S01.Extras.01.mkv', 'Cistern.S01.Bonus.02.mkv' (guessit tags these
    only in this position, never an episode title such as 'Show.S02E03.The.Bonus' or the show 'Extras')."""
    g = guess(name) if g is None else g
    return bool(_EXTRA_OTHERS & set(_as_list(g.get("other"))))


def _first_str(value) -> str | None:
    vals = [v for v in _as_list(value) if isinstance(v, str) and v.strip()]
    return vals[0].strip() if vals else None


def from_guess(g: dict, source: str = "file") -> IdResult:
    """Validate a guessit dict and build a MediaId, or explain why not."""
    if "_error" in g:
        return IdResult(None, f"parser error: {g['_error']}")
    kind = g.get("type")
    title_val = g.get("title")
    if isinstance(title_val, list):
        return IdResult(None, f"several possible titles {title_val!r}")
    title = _first_str(title_val)
    alt = _first_str(g.get("alternative_title"))
    country = g.get("country")
    country = getattr(country, "alpha2", None) or (str(country) if country else None)
    others = _as_list(g.get("other"))
    proper = "Proper" in others or bool(g.get("proper_count"))
    part = bool(g.get("_part"))
    edition = ", ".join(str(e) for e in _as_list(g.get("edition"))) or None
    if _EXTRA_OTHERS & set(others):
        return IdResult(None, "extras or bonus material, not the episode or movie itself")
    year = g.get("year")
    if isinstance(year, list):
        return IdResult(None, f"several years {year!r}")
    if year is not None and not (1900 <= int(year) <= _max_year()):
        return IdResult(None, f"implausible year {year}")

    if kind == "episode":
        if g.get("date") is not None:
            return IdResult(None, "date-based episode naming is not supported")
        if g.get("_bare_season"):
            return IdResult(None, f"season and episode read from a bare number (S{g.get('season')}"
                                  f"E{g.get('episode')}): not trusted")
        season, episode = g.get("season"), g.get("episode")
        if isinstance(season, list):
            return IdResult(None, f"multi-season name (seasons {season})")
        if season is None and episode is not None:
            return IdResult(None, "episode number without season (absolute numbering)")
        if episode is None:
            return IdResult(None, "season pack name without episode number")
        eps = _as_list(episode)
        if not all(isinstance(e, int) and 0 <= e <= 9999 for e in eps):
            return IdResult(None, f"invalid episode numbers {episode!r}")
        if not isinstance(season, int) or not (0 <= season <= MAX_SEASON):
            return IdResult(None, f"invalid season {season!r}")
        if g.get("cd") is not None or g.get("cd_count") is not None:
            return IdResult(None, "multi-part episode file (CD1/CD2) is not supported")
        if not title:
            return IdResult(None, "no show title")
        return IdResult(
            MediaId(
                kind="episode",
                title=title,
                alt_title=alt,
                year=int(year) if year else None,
                season=season,
                episodes=tuple(sorted(set(eps))),
                country=country,
                proper=proper,
                source=source,
                part=part,
                edition=edition,
            )
        )

    if kind == "movie":
        # Since 1.0.1 a movie needs neither a year nor release markers: a name
        # without a year is a "weak" identity (year None) that the folder rules below and the
        # planner (folder size, library match) treat more strictly; tMM completes it when scraping.
        if not title:
            return IdResult(None, "no movie title")
        if g.get("cd") is not None or g.get("cd_count") is not None:
            return IdResult(None, "multi-part movie (CD1/CD2) is not supported")
        if any(_EPISODE_TOKEN.search(t) for t in (title, alt or "")):
            return IdResult(None, f"the name has an episode number (SxxEyy) that could not be read: {title!r}")
        if g.get("bonus") is not None or (g.get("version") is not None
                                          and not any(k in g for k in _RELEASE_MARKERS)):
            return IdResult(None, "looks like software, not a movie (version or x64/x86 in the name)")
        return IdResult(
            MediaId(
                kind="movie",
                title=title,
                alt_title=alt,
                year=int(year) if year else None,
                country=country,
                proper=proper,
                source=source,
                part=part,
                edition=edition,
            )
        )
    return IdResult(None, f"not recognised as movie or episode (type={kind!r})")


def is_weak_movie(media: MediaId | None) -> bool:
    """A movie identity without a year (title only)."""
    return media is not None and media.kind == "movie" and media.year is None


def title_keys(media: MediaId):
    variants = [media.title]
    if media.alt_title:
        variants.append(f"{media.title} {media.alt_title}")
    return make_keys(*variants)


def _guess_title_keys(g: dict):
    title = _first_str(g.get("title"))
    if not title:
        return None
    alt = _first_str(g.get("alternative_title"))
    return make_keys(title, f"{title} {alt}" if alt else title)


_PREFIX = re.compile(r"^([^\W_]+)-(?=[^\W_])")


def _prefix_variants(g: dict, name: str) -> list[dict]:
    """Readings of a name that starts with 'token-'.

    guessit reads 'Ori-Tal.Benno' as group 'Ori' + title 'Tal Benno', and
    'quoll-cistern.s03e01' (scene group prefix) as title 'quoll-cistern'.  A Title-case token is
    taken as part of the title ('Ori-Tal', 'Half-Moon').  A lower-case or UPPER-case
    token (typical of release-group prefixes) is genuinely ambiguous: both readings are
    returned and the caller must break the tie (folder name or library), or skip.
    """
    cleaned = _WEBSITE_PREFIX.sub("", name)
    title, grp = g.get("title"), g.get("release_group")
    m = _PREFIX.match(cleaned)
    if not m or not isinstance(title, str):
        return [g]
    token = m.group(1)
    joined = stripped = None
    tnorm = norm(title)
    if grp == token and norm(cleaned[len(token) + 1:]).startswith(tnorm.split(" ")[0] if tnorm else "\0"):
        joined = dict(g, title=f"{token}-{title}")
        joined.pop("release_group", None)
    elif title.casefold().startswith(token.casefold() + "-"):
        rest = title[len(token) + 1:].strip(" .-_")
        if rest:
            stripped = dict(g, title=rest, release_group=token)
    ambiguous = token.islower() or (token.isupper() and len(token) >= 2)
    if ambiguous:
        return [g] + [v for v in (joined, stripped) if v is not None]
    return [joined] if joined is not None else [g]


def _variants(name: str) -> list[dict]:
    """Interpretations of a name, preferred first."""
    g = guess(name)
    return _prefix_variants(g, name)


def _clean_folder(g: dict) -> dict:
    """A folder guess without pack words in its title: 'The Depot Complete' -> 'The Depot'."""
    title = g.get("title")
    if isinstance(title, str):
        cleaned = _PACK_SUFFIX.sub("", title).strip(" .-_")
        if cleaned and cleaned != title:
            return dict(g, title=cleaned)
    return g


def _folder_variants(name: str) -> list[dict]:
    return [_clean_folder(g) for g in _variants(name)]


def _identify_one(fg: dict, dg: dict | None, single_main_video: bool) -> IdResult:
    fr = from_guess(fg, "file")
    if dg is None:
        if is_weak_movie(fr.media) and not any(k in fg for k in _RELEASE_MARKERS):
            # a loose 'Some Name.mkv': no year, no resolution/source/codec - maybe not a movie at all
            return IdResult(None, f"{fr.media.title!r}: no year and no release information (resolution, source, "
                                  f"codec) in the name; not filed as a movie")
        return fr
    dr = from_guess(dg, "folder")
    dkeys = _guess_title_keys(dg)

    # 1) File name carries a full episode identity.
    if fr.media and fr.media.kind == "episode":
        m = fr.media
        if dkeys is not None and not title_keys(m).intersects(dkeys):
            same_episode = (dr.media is not None and dr.media.kind == "episode"
                            and (dr.media.season, dr.media.episodes) == (m.season, m.episodes))
            if single_main_video and same_episode:
                # e.g. 'quoll-cistern.s03e01.mkv' inside 'Cistern.S03E01.1080p.WEB.H264-QUOLL'
                return IdResult(dr.media, notes=(f"file name title {m.title!r} ignored; folder name used",))
            return IdResult(None, f"show title differs between file ({m.title!r}) and folder "
                                  f"({_first_str(dg.get('title'))!r})")
        # same title: the folder names the show ('Cistern (2022)/Cistern.S01E01.mkv') and may give its year
        if m.year is None and isinstance(dg.get("year"), int) and dkeys is not None:
            m = dataclasses.replace(m, year=dg["year"], source="file+folder")
        return IdResult(m)

    # 2) File has SxxEyy but no title (e.g. 'S01E05.mkv'): borrow the folder's title.
    if (fg.get("type") == "episode" and isinstance(fg.get("season"), int) and fg.get("episode") is not None
            and not fg.get("title") and dg.get("title")):
        merged = dict(fg)
        for k in ("title", "alternative_title", "year", "country"):
            if k in dg and k not in merged:
                merged[k] = dg[k]
        r = from_guess(merged, "file+folder")
        if r.media:
            return IdResult(r.media, notes=("show title taken from folder name",))

    # 3) Single-video folder naming one episode while the file name gives no identity, or only a
    #    title ('Cistern.mkv' inside 'Cistern.S03E01.1080p.WEB.H264-QUOLL').
    if (single_main_video and (fr.media is None or is_weak_movie(fr.media))
            and dr.media and dr.media.kind == "episode"):
        return IdResult(dr.media, notes=("identified from folder name (file name unusable)",))

    # A file that is itself a movie never inherits an episode identity from its folder.
    if fr.media and fr.media.kind == "movie" and dg.get("type") == "episode":
        return IdResult(None, "file looks like a movie but folder looks like a series")

    # 4) Several videos in one folder: each must name its own movie with a year (a collection);
    #    the planner also checks that they are different movies.
    if not single_main_video:
        if fr.media and fr.media.kind == "movie" and fr.media.year is not None:
            return IdResult(fr.media, notes=("several movies in one folder: filed by its own file name",))
        if fr.media and fr.media.kind == "movie":
            return IdResult(None, "several videos in the folder and this file's name has no year")
        if dr.media and dr.media.kind == "movie":
            return IdResult(None, "several main videos in a movie folder (collection or multi-part)")
        return fr  # no identity from the file name (episodes and movies were handled above)

    # 5) One movie in a folder.
    if dr.media and dr.media.kind == "movie":
        dm = dr.media
        if dm.year is not None:  # folder name with a year wins ...
            notes = ()
            if fr.media and fr.media.kind == "movie":
                if fr.media.year is not None and fr.media.year != dm.year:  # ... unless the file names another
                    return IdResult(None, f"folder says {dm.label()!r} but the file says {fr.media.label()!r}")
                if not title_keys(fr.media).intersects(title_keys(dm)):
                    notes = (f"file name suggests {fr.media.label()!r}; folder name used",)
                if fr.media.part and not dm.part:
                    dm = dataclasses.replace(dm, part=True)
            return IdResult(dm, notes=notes)
        # the folder name has no year: it needs the file name's agreement
        if fr.media and fr.media.kind == "movie":
            same = title_keys(fr.media).intersects(title_keys(dm))
            if fr.media.year is not None:
                if same:
                    return IdResult(dataclasses.replace(dm, year=fr.media.year, source="file+folder"),
                                    notes=("year taken from the file name",))
                return IdResult(fr.media, notes=(f"folder name {dm.title!r} has no year; file name used",))
            if same:
                return IdResult(dataclasses.replace(dm, part=dm.part or fr.media.part),
                                notes=("no year in the names: tMM finds it when scraping",))
            return IdResult(None, f"file ({fr.media.title!r}) and folder ({dm.title!r}) name different "
                                  f"movies and neither has a year")
        return IdResult(None, f"folder name {dm.title!r} has no year and the file name "
                              f"({fr.reason or 'not a movie'}) does not confirm it")
    if fr.media and fr.media.kind == "movie":
        if fr.media.year is None:  # e.g. 'tutorial.mp4' in a software folder
            return IdResult(None, f"the file name {fr.media.title!r} has no year and the folder name does not "
                                  f"confirm it is a movie")
        return fr

    reason = fr.reason
    if dr.reason and dr.reason != fr.reason:
        reason = f"file: {fr.reason}; folder: {dr.reason}"
    return IdResult(None, reason)


def _season_of_folder(name: str) -> tuple[int, dict | None] | None:
    """(season, guess of a name that also carries the show title, or None) for a folder naming one
    season: 'Season 2', 'S02', 'Saison 2', 'Specials' (no title); 'The Depot Season 2',
    'Cistern.S01.1080p.WEB' (with title).  None otherwise (also for 'S01-S03')."""
    n = season_number(name)
    if n is not None:
        return n, None
    g = guess(name)
    season = g.get("season")
    if (g.get("type") == "episode" and isinstance(season, int) and 0 <= season <= MAX_SEASON
            and g.get("episode") is None and g.get("date") is None and not g.get("_bare_season")
            and _first_str(g.get("title"))):
        return season, g
    return None


def _from_season_folder(file_name: str, parents: tuple[str, ...]) -> IdResult | None:
    """Episode files without a season in their name, inside a season folder:
    'The Depot (US) Complete/Season 2/05 - Harvest.mkv' -> The Depot S02E05.
    Season from the folder holding the file; show title (and year) from that folder's own name
    ('The Depot Season 2') or from the folder above it; episode number from the file name."""
    if not parents:
        return None
    fg = guess(file_name)
    if fg.get("type") != "episode" or fg.get("date") is not None or fg.get("season") is not None:
        return None
    if _EXTRA_OTHERS & set(_as_list(fg.get("other"))) or fg.get("cd") is not None:
        return None
    eps = _as_list(fg.get("episode"))
    if not eps or not all(isinstance(e, int) and 0 <= e <= 9999 for e in eps):
        return None
    found = _season_of_folder(parents[-1])
    if found is None:
        return None
    season, sg = found
    if sg is not None:
        tg, show_folder = _clean_folder(sg), parents[-1]
    elif len(parents) >= 2:
        tg, show_folder = _clean_folder(guess(parents[-2])), parents[-2]
    else:
        return None
    title = _first_str(tg.get("title"))
    if not title or isinstance(tg.get("title"), list):
        return None
    ftitle = _first_str(fg.get("title"))
    by_title = bool(ftitle) and make_keys(ftitle).intersects(make_keys(title))
    if not (_LEADING_EPISODE.match(_WEBSITE_PREFIX.sub("", file_name)) or by_title):
        return None  # 'Recap 05', 'Deleted Scene 05', 'Interview 02': bonus material, not episode 5
    year = tg.get("year") if isinstance(tg.get("year"), int) and 1900 <= tg["year"] <= _max_year() else None
    country = tg.get("country")
    country = getattr(country, "alpha2", None) or (str(country) if country else None)
    m = MediaId(kind="episode", title=title, alt_title=_first_str(tg.get("alternative_title")), year=year,
                season=season, episodes=tuple(sorted(set(eps))), country=country, source="folders",
                part=bool(fg.get("_part")))
    return IdResult(m, notes=(f"season {season} from folder {parents[-1]!r}, show from {show_folder!r}",))


def _from_inner_folder(fvars: list[dict], parents: tuple[str, ...], single: bool) -> IdResult | None:
    """An episode whose show title differs from the Downloads folder's but matches a folder inside it:
    'TV Pack/The Depot/Season 1/The.Depot.S01E01.mkv'.  Nearest folder first; season folders are
    passed over; only episode identities are taken this way."""
    for name in reversed(parents[1:]):
        if season_number(name) is not None:
            continue
        for dg in _folder_variants(name):
            for fg in fvars:
                r = _identify_one(fg, dg, single)
                if r.media and r.media.kind == "episode":
                    return IdResult(r.media, notes=(*r.notes, f"show title matches the folder {name!r}"))
    return None


def identify_video(file_name: str, folder_name: str | None, single_main_video: bool,
                   parents: tuple[str, ...] = ()) -> IdResult:
    """Identify one main video file.

    Rules:
      * episodes: the file's own name wins; the folder only fills a missing title/year;
        a file without a season in its name ('05 - Title.mkv') takes it from its season folder
        (`parents`: folder names from the Downloads entry down to the file's folder);
      * movies: in a folder holding exactly one main video, the folder name wins if it has a
        year; a name without a year needs the file and folder names to agree;
      * file and folder titles that do not agree are never reconciled by guessing;
      * if a name has two readings and the folder does not decide, the result carries
        `alternatives` and the planner must break the tie against the library or skip.
    """
    fvars = _variants(file_name)
    dvars = _folder_variants(folder_name) if folder_name is not None else [None]
    found: list[IdResult] = []
    failures: list[IdResult] = []
    for dg in dvars:
        for fg in fvars:
            r = _identify_one(fg, dg, single_main_video)
            (found if r.media else failures).append(r)
    if not found:
        return (_from_inner_folder(fvars, parents, single_main_video) or _from_season_folder(file_name, parents)
                or failures[0])
    distinct: dict[tuple, IdResult] = {}
    for r in found:
        m = r.media
        distinct.setdefault((m.kind, norm(m.display_title), m.year, m.season, m.episodes), r)
    first, *others = list(distinct.values())
    if not others:
        return first
    return IdResult(first.media, notes=first.notes, alternatives=tuple(o.media for o in others))
