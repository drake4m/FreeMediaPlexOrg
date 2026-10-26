"""Read-only view of the Movies / Series library, plus the changes planned in this run.

Directory listings are cached for the duration of one run.  Planned folders and
files are layered on top so that two downloads of the same new show (or the same
episode twice) in one run are handled consistently.
"""

from __future__ import annotations

import os
import re
import stat

from .identify import guess
from .matching import LibFolder, lib_folder
from .normalize import same_name, season_number  # noqa: F401  (season_number re-exported)

_SXXEYY = re.compile(r"(?i)(?<![a-z0-9])s(\d{1,3})[ ._-]?e(\d{1,4})((?:[ ._-]?-?[ ._-]?e\d{1,4})*)(?!\d)")
# Library sample/trailer files: "<name>-sample.mkv", "<name>.sample.mkv", "sample-<name>.mkv",
# "sample.mkv", "<name>-trailer.mp4" (Kodi/tMM trailer naming).  Strict on purpose: names that
# END with an episode title ("Show - S02E04 - The Trailer", "... - Blood Sample") are NOT extras.
_LIB_EXTRA = re.compile(r"(?i)-(?:sample|trailer)$|\.sample$|^sample[-.]|^(?:sample|trailer)$")


PLANNED = "(planned in this run)"  # existing_episodes(): an episode only another download of this run brings


class LibraryUnstable(Exception):
    pass


def is_library_extra(filename: str) -> bool:
    """True for sample/trailer files in the library; they never count as the episode or the movie."""
    return bool(_LIB_EXTRA.search(os.path.splitext(filename)[0]))


def episodes_in_name(name: str, season_hint: int | None) -> set[tuple[int, int]]:
    """(season, episode) pairs named by an existing library file."""
    g = guess(name)
    out: set[tuple[int, int]] = set()
    other = g.get("other") or []
    if {"Extras", "Bonus"} & set(other if isinstance(other, list) else [other]):
        return out  # 'Cistern.S01.Extras.01.mkv' is not S01E01
    if g.get("type") == "episode" and g.get("episode") is not None:
        s = g.get("season", season_hint)
        if isinstance(s, int):
            eps = g["episode"] if isinstance(g["episode"], list) else [g["episode"]]
            out.update((s, int(e)) for e in eps if isinstance(e, int))
    for m in _SXXEYY.finditer(name):  # strict fallback, also catches what guessit drops
        s = int(m.group(1))
        out.add((s, int(m.group(2))))
        out.update((s, int(x)) for x in re.findall(r"(?i)e(\d{1,4})", m.group(3) or ""))
    return out


class Library:
    def __init__(self, movies_root: str, series_root: str, video_exts: tuple[str, ...]):
        self.movies_root = movies_root
        self.series_root = series_root
        self.video_exts = tuple(e.lower() for e in video_exts)
        self._listing: dict[str, tuple[list[str], list[str]]] = {}
        self._stat: dict[str, tuple[int, float]] = {}  # file path -> (size, mtime), from the listings
        self._removed: dict[str, set[str]] = {}  # parent -> names this run deletes (empty placeholders)
        self._planned_dirs: dict[str, set[str]] = {}
        self._planned_files: dict[str, set[str]] = {}
        self._planned_eps: dict[tuple[str, int], set[int]] = {}
        self.series: list[LibFolder] = self._folders(series_root)
        self.movies: list[LibFolder] = self._folders(movies_root)

    # -- raw listings ---------------------------------------------------------------
    def listing(self, path: str) -> tuple[list[str], list[str]]:
        """(subdirs, files) of a directory; ([], []) if it does not exist.

        A listing whose entries vanish while being read (e.g. tMM renaming) is retried and,
        if still unstable, raises LibraryUnstable instead of being cached incomplete.
        """
        if path in self._listing:
            return self._listing[path]
        for _attempt in range(3):
            dirs, files, unstable, stats = [], [], False, {}
            try:
                with os.scandir(path) as it:
                    for e in it:
                        try:
                            st = e.stat(follow_symlinks=False)
                        except FileNotFoundError:
                            unstable = True
                            continue
                        if stat.S_ISDIR(st.st_mode):
                            dirs.append(e.name)
                        elif stat.S_ISREG(st.st_mode):
                            files.append(e.name)
                            stats[os.path.join(path, e.name)] = (st.st_size, st.st_mtime)
            except FileNotFoundError:
                if os.path.lexists(path):
                    unstable = True
                else:
                    dirs, files = [], []
            if not unstable:
                break
        else:
            raise LibraryUnstable(f"{path} changed while being listed (tMM renaming?); try again later")
        self._listing[path] = (sorted(dirs), sorted(files))
        self._stat.update(stats)
        return self._listing[path]

    def stat_of(self, path: str) -> tuple[int, float] | None:
        """(size, mtime) of a library file as listed in this run (None if not listed)."""
        if path not in self._stat:
            self.listing(os.path.dirname(path))
        return self._stat.get(path)

    def is_empty(self, path: str) -> bool:
        st = self.stat_of(path)
        return st is not None and st[0] == 0

    def _folders(self, root: str) -> list[LibFolder]:
        dirs, _ = self.listing(root)
        return [lib_folder(d, os.path.join(root, d)) for d in dirs if not d.startswith(".")]

    def exists_name(self, parent: str, name: str, ignore: tuple[str, ...] = ()) -> bool:
        """Case-insensitive existence check (the SMB server is case-insensitive).
        On-disk names this run deletes, and on-disk names in `ignore`, do not count; names planned
        in this run always count (a planned file may reuse a deleted placeholder's name)."""
        dirs, files = self.listing(parent)
        planned = self._planned_dirs.get(parent, set()) | self._planned_files.get(parent, set())
        gone = {*self._removed.get(parent, set()), *ignore}
        on_disk = [n for n in (*dirs, *files) if n not in gone]
        return any(same_name(n, name) for n in (*on_disk, *planned))

    def find_child_dir(self, parent: str, name: str) -> str | None:
        dirs, _ = self.listing(parent)
        for d in (*dirs, *self._planned_dirs.get(parent, set())):
            if same_name(d, name):
                return os.path.join(parent, d)
        return None

    # -- seasons and episodes ---------------------------------------------------------
    def season_dirs(self, show_path: str, season: int) -> list[str]:
        dirs, _ = self.listing(show_path)
        found = [d for d in (*dirs, *self._planned_dirs.get(show_path, set())) if season_number(d) == season]
        return [os.path.join(show_path, d) for d in sorted(set(found))]

    def existing_episodes(self, show_path: str, season: int, season_dir: str | None,
                          extras: bool = False) -> dict[int, str]:
        """episode number -> file name, from the season folder and the show root.

        Samples/trailers are left out; with extras=True ONLY they are returned (for report notes).
        """
        out: dict[int, str] = {}
        for path in self._episode_files(show_path, season, season_dir, extras=extras):
            f = os.path.basename(path)
            hint = season if os.path.dirname(path) == season_dir else None
            for s, e in episodes_in_name(f, hint):
                if s == season:
                    out.setdefault(e, f)
        if not extras:
            for e in self._planned_eps.get((show_path, season), set()):
                out.setdefault(e, PLANNED)
        return out

    def _episode_files(self, show_path: str, season: int, season_dir: str | None, extras: bool = False,
                       empty: bool = False) -> list[str]:
        """Video files of a show folder and its season folder.  Default: real episodes only
        (no samples/trailers, no 0-byte files); extras=True: only samples/trailers;
        empty=True: only 0-byte non-extra videos."""
        out = []
        places = [show_path] + ([season_dir] if season_dir else [])
        removed = self._removed_paths()
        for place in places:
            _, files = self.listing(place)
            for f in files:
                path = os.path.join(place, f)
                if not self.is_video(f) or is_library_extra(f) != extras or path in removed:
                    continue
                if not extras and self.is_empty(path) != empty:
                    continue
                out.append(path)
        return out

    def _removed_paths(self) -> set[str]:
        return {os.path.join(p, n) for p, names in self._removed.items() for n in names}

    def episode_paths(self, show_path: str, season: int, season_dir: str | None, empty: bool = False
                      ) -> dict[int, list[str]]:
        """episode -> full paths of the (non-empty, or with empty=True only the 0-byte) files naming it."""
        out: dict[int, list[str]] = {}
        for path in self._episode_files(show_path, season, season_dir, empty=empty):
            hint = season if os.path.dirname(path) == season_dir else None
            for s, e in episodes_in_name(os.path.basename(path), hint):
                if s == season:
                    out.setdefault(e, []).append(path)
        return out

    def is_video(self, name: str) -> bool:
        return os.path.splitext(name)[1].lower() in self.video_exts

    def movie_videos(self, folder: str, include_extras: bool = False) -> list[str]:
        """Video files of a movie folder.  Samples/trailers and 0-byte files are left out unless
        include_extras (then: any video file)."""
        _, files = self.listing(folder)
        planned = sorted(self._planned_files.get(folder, set()))
        if include_extras:
            return [f for f in (*files, *planned) if self.is_video(f)]
        gone = self._removed.get(folder, set())
        on_disk = [f for f in files if self.is_video(f) and not is_library_extra(f) and f not in gone
                   and not self.is_empty(os.path.join(folder, f))]
        return on_disk + [f for f in planned if self.is_video(f) and not is_library_extra(f)]

    def empty_movie_videos(self, folder: str) -> list[str]:
        """Full paths of 0-byte (non-extra) video files in a movie folder (not yet planned for removal)."""
        _, files = self.listing(folder)
        gone = self._removed.get(folder, set())
        return [os.path.join(folder, f) for f in files if self.is_video(f) and not is_library_extra(f)
                and f not in gone and self.is_empty(os.path.join(folder, f))]

    # -- planned changes --------------------------------------------------------------
    def plan_dir(self, parent: str, name: str) -> str:
        self._planned_dirs.setdefault(parent, set()).add(name)
        path = os.path.join(parent, name)
        if parent == self.series_root:
            self.series.append(lib_folder(name, path, planned=True))
        elif parent == self.movies_root:
            self.movies.append(lib_folder(name, path, planned=True))
        return path

    def plan_file(self, parent: str, name: str) -> None:
        self._planned_files.setdefault(parent, set()).add(name)

    def plan_remove(self, path: str) -> None:
        self._removed.setdefault(os.path.dirname(path), set()).add(os.path.basename(path))

    def plan_episodes(self, show_path: str, season: int, episodes) -> None:
        self._planned_eps.setdefault((show_path, season), set()).update(episodes)


def compare_files(a: str, b: str, chunk: int = 8 << 20) -> bool | None:
    """True if both are regular files with exactly the same bytes, False if they differ (or are the
    same file seen twice), None if they could not be compared (I/O error: ask again next run).
    Different files usually differ in the first chunk, so a mismatch is found quickly."""
    try:
        sa, sb = os.lstat(a), os.lstat(b)
        if not (stat.S_ISREG(sa.st_mode) and stat.S_ISREG(sb.st_mode)) or sa.st_size != sb.st_size:
            return False
        if os.path.samefile(a, b):
            return False  # the same file seen twice is not a duplicate to delete
        with open(a, "rb") as fa, open(b, "rb") as fb:
            while True:
                x, y = fa.read(chunk), fb.read(chunk)
                if x != y:
                    return False
                if not x:
                    return True
    except OSError:
        return None


def files_identical(a: str, b: str, chunk: int = 8 << 20) -> bool:
    """True only if both are regular files with exactly the same bytes.  Any error -> False."""
    return compare_files(a, b, chunk) is True
