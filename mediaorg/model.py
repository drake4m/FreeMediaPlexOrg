"""Plain data structures shared by the layers."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class MediaId:
    kind: str  # "episode" | "movie"
    title: str
    year: int | None = None
    season: int | None = None
    episodes: tuple[int, ...] = ()
    alt_title: str | None = None
    country: str | None = None
    proper: bool = False  # REPACK / PROPER release
    source: str = "file"  # file | folder | file+folder | folders
    part: bool = False  # the name says "Part 2" / "Pt2": one part of a split episode or movie
    edition: str | None = None  # "Extended", "Director's Cut" ...: maybe not the cut the library holds

    @property
    def display_title(self) -> str:
        return f"{self.title} - {self.alt_title}" if self.alt_title else self.title

    def label(self) -> str:
        y = f" ({self.year})" if self.year else ""
        if self.kind == "episode":
            eps = "".join(f"E{e:02d}" for e in self.episodes)
            return f"{self.display_title}{y} S{self.season:02d}{eps}"
        return f"{self.display_title}{y}"


@dataclass(frozen=True)
class IdResult:
    media: MediaId | None
    reason: str = ""  # why media is None, or an informational note
    notes: tuple[str, ...] = ()
    alternatives: tuple[MediaId, ...] = ()  # other plausible readings; the planner must decide or skip


@dataclass
class FileEntry:
    path: str  # absolute path
    rel: str  # path relative to the item root ('' parts joined by '/'); == name for loose files
    name: str
    ext: str  # lower-case, with dot; for a video found by its content (no extension): the added one
    size: int
    mtime: float
    added_ext: str = ""  # extension appended to the name when filed ('.mkv' for 'Show.S01E04')

    @property
    def dest_name(self) -> str:
        """Name the file gets in the library."""
        return self.name + self.added_ext


@dataclass
class Item:
    """One top-level entry of the Downloads folder (a torrent's file or folder)."""

    name: str
    path: str
    is_dir: bool
    files: list[FileEntry] = field(default_factory=list)
    dirs: list[str] = field(default_factory=list)  # absolute paths of sub-directories
    newest_mtime: float = 0.0
    problems: list[str] = field(default_factory=list)  # symlinks, unreadable entries


@dataclass
class Readiness:
    ready: bool
    reason: str
    blocked_files: frozenset[str] = frozenset()  # absolute paths not fully downloaded
    # completeness.on_seeding = "stop": Freebox tasks still seeding a finished download; the item is
    # ready once a run that files it has stopped them
    stop_tasks: frozenset[int] = frozenset()


@dataclass
class Op:
    kind: str  # mkdir | rename | rmdir | unlink
    dst: str
    src: str | None = None
    # media | sidecar | duplicate | duplicate-sidecar | leftovers | library-dir | review-dir | cleanup |
    # identical-duplicate (unlink of a download byte-identical to `ref`)
    # | same-duplicate (unlink of a download of the media `ref` holds, review.delete_duplicates)
    # | empty-placeholder (unlink of a 0-byte library file)
    # | replaced (unlink of the library video a download replaces, review.replace_duplicates: `ref` is the
    #   new file, `ref_files` the file that must be in place, unchanged, right before: the new library file,
    #   or the download itself when the old file goes first because the new one takes its name)
    role: str = ""
    expect_size: int | None = None
    expect_mtime: float | None = None
    expect_tree: frozenset | None = None  # folder renames: exact file set expected at execution
    ref: str | None = None  # identical-/same-duplicate: the library file holding the same media as dst
    ref_mtime: float | None = None
    # same-duplicate: every library file holding the media, (path, size, mtime) when planned; all must be
    # unchanged right before the deletion
    ref_files: tuple = ()
    # duplicate / duplicate-sidecar: the review cleaning may delete it after the retention (journaled);
    # False = kept in review until the user decides
    purge: bool = False


@dataclass
class FileDecision:
    file: FileEntry
    action: str  # move | replace (review.replace_duplicates) | duplicate | delete (byte-identical, or `same`) | skip
    reason: str
    media: MediaId | None = None
    target: str | None = None
    notes: list[str] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)  # duplicate: library file(s) holding the same media
    purge: bool = False  # duplicate: certain enough to be deleted from review after the retention
    same: bool = False  # delete: a duplicate of refs[0] deleted by review.delete_duplicates (bytes may differ)


@dataclass
class ItemPlan:
    item: Item
    status: str  # ready | wait | ignored | skip | partial
    reason: str = ""
    decisions: list[FileDecision] = field(default_factory=list)
    ops: list[Op] = field(default_factory=list)
    tmm_paths: dict[str, set[str]] = field(default_factory=dict)  # 'movie'|'tvshow' -> library paths
    stop_tasks: frozenset[int] = frozenset()  # Freebox tasks to stop before the ops (Readiness.stop_tasks)
