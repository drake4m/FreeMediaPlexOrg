"""Configuration: TOML file -> validated, frozen dataclasses.

The dataclasses below hold every key with its default; schema.py adds the documentation, value types
and the commented TOML writer used by `mediaorg config`.  Configuration files of 1.x stay valid.
"""

from __future__ import annotations

import dataclasses
import difflib
import ipaddress
import os
import re
import stat
import string
import tomllib
import urllib.parse
from dataclasses import dataclass, field

DEFAULT_CONFIG = "/etc/mediaorg/config.toml"
DEFAULT_STATE_DIR = "/var/lib/mediaorg"
SERVICE_USER = "mediaorg"  # the unprivileged account that owns the state and runs the timers


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Paths:
    downloads: str
    movies: str
    series: str
    review: str = ""  # "" = <downloads>/_review
    state_dir: str = DEFAULT_STATE_DIR


@dataclass(frozen=True)
class MountCheck:
    mountpoint: str
    fstype: str
    source: str


@dataclass(frozen=True)
class Media:
    video_extensions: tuple[str, ...] = (".mkv", ".mp4", ".avi", ".m4v")
    subtitle_extensions: tuple[str, ...] = (".srt", ".ass", ".ssa", ".sub", ".idx", ".sup", ".vtt")
    extras_dir_names: tuple[str, ...] = (
        "sample", "samples", "extras", "extra", "featurettes", "featurette", "bonus",
        "trailers", "trailer", "behind the scenes", "deleted scenes", "interviews", "shorts",
    )
    ignore_names: tuple[str, ...] = ("_review", "@eaDir", "#recycle", ".DS_Store", "Thumbs.db", "desktop.ini")
    ignore_hidden: bool = True


@dataclass(frozen=True)
class Naming:
    movie_folder: str = "{title} ({year})"
    movie_folder_no_year: str = "{title}"  # tMM renames it to 'Title (Year)' once it has scraped the movie
    series_folder: str = "{title} ({year})"
    series_folder_no_year: str = "{title}"
    season_folder: str = "Season {season}"
    # new movie and show folders get " [mediaorg]" until tinyMediaManager renames them (tmm.enabled only), so
    # that tMM's rename never only changes the letter case, which tMM cannot do on Linux on a case-insensitive
    # share (SMB/CIFS)
    tmm_staging: bool = False


@dataclass(frozen=True)
class Completeness:
    method: str = "quiescence"  # quiescence | freebox
    min_quiet_minutes: int = 10
    temp_extensions: tuple[str, ...] = (".part", ".!qb", ".crdownload", ".tmp", ".partial", ".!ut", ".aria2")
    # a finished BitTorrent download still seeding: "wait" until the Freebox says done (its stop ratio), or
    # "stop" it when a run files it (method "freebox" only)
    on_seeding: str = "wait"
    # Items in Downloads that the Freebox does not list as a task (method "freebox" only):
    # "wait" = never move them (safe default); "quiescence" = move after min_quiet_minutes.
    untracked: str = "wait"


@dataclass(frozen=True)
class Freebox:
    base_url: str = "https://mafreebox.freebox.fr"
    ca_file: str = "/etc/mediaorg/freebox-ca.pem"
    app_id: str = "mediaorg"
    app_name: str = "mediaorg"
    device_name: str = ""  # "" = this host's name
    token_file: str = ""  # "" = <state_dir>/freebox_app_token.json
    # Freebox-side path of paths.downloads, e.g. "/Freebox/Downloads" (see `mediaorg freebox status`)
    remote_downloads_dir: str = ""
    timeout_seconds: int = 10
    allow_insecure_http: bool = False


@dataclass(frozen=True)
class Safety:
    max_moves_per_run: int = 50
    min_free_mb: int = 100  # free space needed in state_dir before a run moves or deletes anything


@dataclass(frozen=True)
class Reports:
    retention_days: int = 30  # JSON run reports older than this are deleted; 0 = keep forever
    max_reports: int = 2000  # and never more than this many; 0 = no limit
    journal_retention_days: int = 365  # journals of finished runs (undo) older than this; 0 = keep forever


@dataclass(frozen=True)
class Logging:
    file: str = ""  # "" = standard error only (the systemd journal under the timers)
    max_size_mb: int = 10
    backups: int = 5


@dataclass(frozen=True)
class Review:
    # A download whose bytes are identical to the library file of the same episode/movie is deleted
    # instead of being moved to review/duplicates (false = keep it for review like other duplicates).
    delete_identical_duplicates: bool = True
    # review/duplicates/<run-id>/ and review/leftovers/<run-id>/ are deleted this many days after
    # that run; 0 = at the end of the same run; -1 = never (delete them by hand).
    duplicates_retention_days: int = 7
    leftovers_retention_days: int = 7
    # tinyMediaManager's trash (<movies>/.deletedByTMM, <series>/.deletedByTMM): files are deleted once
    # mediaorg has seen them there for this many days; 0 = in the first run; -1 = never (tmmtrash.py).
    tmm_trash_retention_days: int = 7
    # The same in minutes, for a short retention; set one of the two keys, not both.
    tmm_trash_retention_minutes: int | None = None

    @property
    def tmm_trash_retention_seconds(self) -> int:
        """The trash retention in seconds; -1 = never."""
        if self.tmm_trash_retention_minutes is not None:
            m = self.tmm_trash_retention_minutes
            return -1 if m < 0 else m * 60
        d = self.tmm_trash_retention_days
        return -1 if d < 0 else d * 86400


@dataclass(frozen=True)
class Tmm:
    enabled: bool = False
    base_url: str = "http://127.0.0.1:7878"
    api_key_file: str = "/etc/mediaorg/tmm.key"
    timeout_seconds: int = 30
    defer_moves_while_busy: bool = True
    movie_endpoint: str = "movie"
    tvshow_endpoint: str = "tvshow"
    path_map: tuple[tuple[str, str], ...] = ()
    after_move_movie: tuple = ()
    after_move_tvshow: tuple = ()
    full_sync_movie: tuple = ()
    full_sync_tvshow: tuple = ()
    allow_insecure_http: bool = False  # plain http to a tMM that is not on this machine sends the key in clear


@dataclass(frozen=True)
class Schedule:
    run_every_minutes: int = 5
    tmm_sync_time: str = "03:37"  # "HH:MM" local time of the nightly tMM sync; "" = none


@dataclass(frozen=True)
class Config:
    paths: Paths
    mount: MountCheck | None = None
    media: Media = field(default_factory=Media)
    naming: Naming = field(default_factory=Naming)
    completeness: Completeness = field(default_factory=Completeness)
    freebox: Freebox = field(default_factory=Freebox)
    safety: Safety = field(default_factory=Safety)
    tmm: Tmm = field(default_factory=Tmm)
    reports: Reports = field(default_factory=Reports)
    review: Review = field(default_factory=Review)
    logging: Logging = field(default_factory=Logging)
    schedule: Schedule = field(default_factory=Schedule)
    source_file: str = ""

    @property
    def base_dir(self) -> str:
        """Folder that report paths are shown relative to: the mount point, else the deepest folder
        holding Downloads and the library."""
        if self.mount is not None:
            return self.mount.mountpoint
        return os.path.commonpath([self.paths.downloads, self.paths.movies, self.paths.series])


# section name -> dataclass, in the order of the configuration file
SECTIONS: dict[str, type] = {
    "paths": Paths, "mount": MountCheck, "completeness": Completeness, "freebox": Freebox, "tmm": Tmm,
    "review": Review, "schedule": Schedule, "safety": Safety, "reports": Reports, "logging": Logging,
    "media": Media, "naming": Naming,
}


# What tMM is asked to do after files were filed, and every night: two requests, TV shows then movies,
# each in ONE call, because tMM's "new" means "found by the update action of the same call" (the "new"
# flags of earlier calls are dropped).  Each call updates the library, scrapes what that update found
# (tMM takes the best search result when it reaches its own score threshold, set in tMM; ambiguous
# results stay for the UI), then renames what it found (tMM's own "Automatic rename" may have done it
# already; renaming again changes nothing).  Targeted variants ("path"/"show" scopes with "{paths}")
# can be configured instead.
DEFAULT_FULL_MOVIE = (
    {"action": "update", "scope": {"name": "all"}},
    {"action": "scrape", "scope": {"name": "new"}},
    {"action": "rename", "scope": {"name": "new"}},
)
DEFAULT_FULL_TVSHOW = (
    {"action": "update", "scope": {"name": "all"}},
    {"action": "scrape", "scope": {"name": "new"}},
    {"action": "rename", "scope": {"name": "new"}},
)
DEFAULT_AFTER_MOVE_MOVIE = DEFAULT_FULL_MOVIE
DEFAULT_AFTER_MOVE_TVSHOW = DEFAULT_FULL_TVSHOW

# Media folders are never system folders (a typo must not let the tool rename things in /usr).
_SYSTEM_DIRS = ("/bin", "/boot", "/dev", "/etc", "/lib", "/lib32", "/lib64", "/libx32", "/proc", "/run",
                "/sbin", "/sys", "/usr")
_SYSTEM_EXACT = ("/", "/var", "/home", "/root", "/opt", "/tmp", "/srv", "/mnt", "/media")
_TIME = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


def uses_paths(commands) -> bool:
    """True if a command template contains the "{paths}" placeholder."""
    if commands == "{paths}":
        return True
    if isinstance(commands, dict):
        return any(uses_paths(v) for v in commands.values())
    if isinstance(commands, (list, tuple)):
        return any(uses_paths(v) for v in commands)
    return False


def _suggest(word: str, choices) -> str:
    near = difflib.get_close_matches(word, list(choices), n=1)
    return f" (did you mean {near[0]!r}?)" if near else ""


def _section(data: dict, name: str, cls, required: bool = False, **conv):
    raw = data.get(name)
    if raw is None:
        if required:
            raise ConfigError(f"missing [{name}] section")
        return cls()
    if not isinstance(raw, dict):
        raise ConfigError(f"[{name}] must be a table")
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = set(raw) - known
    if unknown:
        k = sorted(unknown)[0]
        raise ConfigError(f"[{name}] unknown key(s): {', '.join(sorted(unknown))}{_suggest(k, known)}")
    needed = {f.name for f in dataclasses.fields(cls)
              if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING}
    missing = needed - set(raw)
    if missing:
        raise ConfigError(f"[{name}] missing key(s): {', '.join(sorted(missing))}")
    values = {}
    for k, v in raw.items():
        if k in conv:
            v = conv[k](v)
        elif isinstance(v, list):
            v = tuple(v)
        values[k] = v
    types = {f.name: f.type for f in dataclasses.fields(cls)}
    for k, v in values.items():
        _check_type(f"{name}.{k}", types[k], v)
    try:
        return cls(**values)
    except TypeError as exc:
        raise ConfigError(f"[{name}] {exc}") from exc


def _check_type(key: str, typ: str, v) -> None:
    """Reject values of the wrong TOML type with a clear message (types are the annotation strings)."""
    if typ == "str" and not isinstance(v, str):
        raise ConfigError(f"{key} must be a string, got {v!r}")
    if typ == "bool" and not isinstance(v, bool):
        raise ConfigError(f"{key} must be true or false, got {v!r}")
    if typ in ("int", "int | None") and v is not None and not _is_int(v):
        raise ConfigError(f"{key} must be an integer, got {v!r}")
    if typ == "tuple[str, ...]" and not all(isinstance(x, str) for x in v):
        raise ConfigError(f"{key} must be a list of strings")


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def check_path_text(path, key: str) -> str:
    """An absolute, normalized path that a TOML file and a systemd unit can carry safely."""
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ConfigError(f"{key} must be an absolute path, got {path!r}")
    if any(ord(c) < 32 or ord(c) == 127 for c in path):
        raise ConfigError(f"{key} must not contain control characters: {path!r}")
    try:
        path.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ConfigError(f"{key} is not valid UTF-8: {path!r}") from exc
    norm = os.path.normpath(path)
    if norm.startswith("//"):
        norm = "/" + norm.lstrip("/")
    return norm


def _media_path(path, key: str) -> str:
    p = check_path_text(path, key)
    if p in _SYSTEM_EXACT or any(_under(p, s) for s in _SYSTEM_DIRS):
        raise ConfigError(f"{key} = {p!r} is a system folder; use a dedicated media folder")
    return p


def _lower_exts(v) -> tuple[str, ...]:
    if not isinstance(v, (list, tuple)):
        raise ConfigError(f"extension lists must be arrays, got {v!r}")
    out = []
    for e in v:
        e = str(e).lower().strip()
        if not e or "/" in e or any(c.isspace() for c in e):
            raise ConfigError(f"invalid file extension {e!r}")
        out.append(e if e.startswith(".") else "." + e)
    return tuple(out)


def _path_map(v) -> tuple[tuple[str, str], ...]:
    if not isinstance(v, (list, tuple)):
        raise ConfigError("tmm.path_map must be an array of [host_path, tmm_path] pairs")
    pairs = []
    for p in v:
        if not (isinstance(p, (list, tuple)) and len(p) == 2 and all(isinstance(x, str) for x in p)):
            raise ConfigError("tmm.path_map entries must be [host_path, tmm_path]")
        host = check_path_text(p[0], "tmm.path_map host path")
        if not p[1].startswith("/"):
            raise ConfigError(f"tmm.path_map: {p[1]!r} must be an absolute path as tMM sees it")
        pairs.append((host, p[1].rstrip("/") or "/"))
    return tuple(sorted(pairs, key=lambda x: -len(x[0])))


def _commands(v) -> tuple:
    if not isinstance(v, (list, tuple)) or not all(isinstance(c, dict) and "action" in c for c in v):
        raise ConfigError("tmm command lists must be arrays of tables with an 'action' key")
    return tuple(v)


_TEMPLATE_SAMPLE = {"title": "Some Title", "year": 2000, "season": 1}  # the types the planner passes


def _template(value: str, key: str, allowed: set[str], required: set[str]) -> None:
    try:
        fields = [(f, spec) for _, f, spec, _ in string.Formatter().parse(value) if f is not None]
    except ValueError as exc:
        raise ConfigError(f"{key}: {exc}") from exc
    names = {f for f, _ in fields}
    bad = names - allowed
    if bad:
        raise ConfigError(f"{key}: unknown placeholder(s) {sorted(bad)}; allowed: {sorted(allowed)}")
    if not required <= names:
        raise ConfigError(f"{key} must contain {', '.join('{' + r + '}' for r in sorted(required))}")
    if "/" in value or value.strip() in ("", ".", ".."):
        raise ConfigError(f"{key} must be a single folder name, got {value!r}")
    if any(int(n) > 255 for _, spec in fields for n in re.findall(r"\d+", spec or "")):
        raise ConfigError(f"{key}: a width above 255 in {value!r}")
    try:  # a format that cannot apply to the value ('{title:d}') would otherwise stop every run
        sample = value.format(**_TEMPLATE_SAMPLE)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
        raise ConfigError(f"{key}: {value!r} cannot be applied: {exc}") from None
    if "/" in sample or len(sample.encode("utf-8")) > 255:
        raise ConfigError(f"{key}: {value!r} does not give a single folder name")


def _http_url(url, key: str, allow_insecure: bool, insecure_key: str) -> None:
    if not isinstance(url, str):
        raise ConfigError(f"{key} must be a string")
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ConfigError(f"{key} must be an http:// or https:// URL, got {url!r}")
    if u.scheme == "http" and not allow_insecure and not _is_loopback(u.hostname):
        raise ConfigError(f"{key} uses plain http:// to another machine ({u.hostname}); use https://, "
                          f"or set {insecure_key} = true to accept that the key travels unencrypted")


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_file_permissions(path: str, what: str, secret: bool = False) -> None:
    """Refuse files that other users could change (or read, for secrets).  Fixes are printed."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return
    if st.st_mode & stat.S_IWOTH:
        raise ConfigError(f"{what} {path} is writable by every user; fix: sudo chmod o-w {path}")
    if secret and st.st_mode & stat.S_IROTH:
        raise ConfigError(f"{what} {path} is readable by every user; fix: sudo chmod o-rwx {path}")
    parent = os.path.dirname(path) or "."
    try:
        pst = os.stat(parent)
    except OSError:
        return
    if pst.st_mode & stat.S_IWOTH and not pst.st_mode & stat.S_ISVTX:
        raise ConfigError(f"{what} is in {parent}, which every user can write to; use a protected folder")


def read_raw(path: str | None = None) -> tuple[str, dict]:
    """(path, parsed TOML) of the configuration file, without validation."""
    path = os.path.abspath(path or os.environ.get("MEDIAORG_CONFIG") or DEFAULT_CONFIG)
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path} (create it: sudo mediaorg config init --help)") from exc
    except PermissionError as exc:
        raise ConfigError(f"cannot read {path}: permission denied (use sudo)") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    return path, data


def load(path: str | None = None) -> Config:
    path, data = read_raw(path)
    check_file_permissions(path, "the configuration file")
    return from_dict(data, source_file=path)


def from_dict(data: dict, source_file: str = "<dict>") -> Config:
    if not isinstance(data, dict):
        raise ConfigError("the configuration must be a TOML document")
    unknown = set(data) - set(SECTIONS)
    if unknown:
        s = sorted(unknown)[0]
        raise ConfigError(f"unknown section(s): {', '.join(sorted(unknown))}{_suggest(s, SECTIONS)}")
    raw_paths = _section(data, "paths", Paths, required=True)
    downloads = _media_path(raw_paths.downloads, "paths.downloads")
    review = raw_paths.review or os.path.join(downloads, "_review")
    paths = Paths(
        downloads=downloads,
        movies=_media_path(raw_paths.movies, "paths.movies"),
        series=_media_path(raw_paths.series, "paths.series"),
        review=_media_path(review, "paths.review"),
        state_dir=check_path_text(raw_paths.state_dir, "paths.state_dir"),
    )
    mount = None
    raw_mount = data.get("mount")
    if raw_mount:  # an absent or empty [mount] section: no mount check
        m = _section(data, "mount", MountCheck)
        if not m.fstype or not m.source:
            raise ConfigError("[mount] needs mountpoint, fstype and source (see `mediaorg config init --help`)")
        mount = MountCheck(check_path_text(m.mountpoint, "mount.mountpoint"), m.fstype, m.source.rstrip("/"))
    media = _section(data, "media", Media, video_extensions=_lower_exts, subtitle_extensions=_lower_exts)
    if not media.video_extensions:
        raise ConfigError("media.video_extensions must not be empty")
    naming = _section(data, "naming", Naming)
    _template(naming.movie_folder, "naming.movie_folder", {"title", "year"}, {"title", "year"})
    _template(naming.movie_folder_no_year, "naming.movie_folder_no_year", {"title"}, {"title"})
    _template(naming.series_folder, "naming.series_folder", {"title", "year"}, {"title", "year"})
    _template(naming.series_folder_no_year, "naming.series_folder_no_year", {"title"}, {"title"})
    _template(naming.season_folder, "naming.season_folder", {"season"}, {"season"})

    comp = _section(data, "completeness", Completeness, temp_extensions=_lower_exts)
    fbx_raw = data.get("freebox")
    if ("method" not in (data.get("completeness") or {}) and isinstance(fbx_raw, dict)
            and fbx_raw.get("remote_downloads_dir")):
        raise ConfigError("completeness.method is not set but [freebox] is configured: the default changed "
                          "to 'quiescence' in 2.0; set completeness.method = \"freebox\" (or \"quiescence\")")
    if comp.method not in ("freebox", "quiescence"):
        raise ConfigError(f"completeness.method must be 'freebox' or 'quiescence', got {comp.method!r}")
    if comp.untracked not in ("wait", "quiescence"):
        raise ConfigError("completeness.untracked must be 'wait' or 'quiescence'")
    if comp.on_seeding not in ("wait", "stop"):
        raise ConfigError(f"completeness.on_seeding must be 'wait' or 'stop', got {comp.on_seeding!r}")
    if comp.min_quiet_minutes < 5:
        raise ConfigError("completeness.min_quiet_minutes must be an integer >= 5")

    fbx = _section(data, "freebox", Freebox)
    if fbx.remote_downloads_dir and not fbx.remote_downloads_dir.startswith("/"):
        raise ConfigError(f"freebox.remote_downloads_dir must be an absolute Freebox path, got "
                          f"{fbx.remote_downloads_dir!r} (see `mediaorg freebox status`)")
    _http_url(fbx.base_url, "freebox.base_url", fbx.allow_insecure_http, "freebox.allow_insecure_http")
    if fbx.base_url.startswith("http://") and not fbx.allow_insecure_http:
        raise ConfigError("freebox.base_url uses http://; set allow_insecure_http = true to accept that")
    if fbx.timeout_seconds < 1:
        raise ConfigError("freebox.timeout_seconds must be >= 1")
    fbx = dataclasses.replace(
        fbx,
        ca_file=check_path_text(fbx.ca_file, "freebox.ca_file"),
        token_file=check_path_text(fbx.token_file or os.path.join(paths.state_dir, "freebox_app_token.json"),
                                   "freebox.token_file"),
    )

    safety = _section(data, "safety", Safety)
    if safety.max_moves_per_run < 1:
        raise ConfigError("safety.max_moves_per_run must be >= 1")
    if safety.min_free_mb < 0:
        raise ConfigError("safety.min_free_mb must be >= 0")

    reports = _section(data, "reports", Reports)
    for key in ("retention_days", "max_reports", "journal_retention_days"):
        if getattr(reports, key) < 0:
            raise ConfigError(f"reports.{key} must be an integer >= 0 (0 = no limit)")

    review_cfg = _section(data, "review", Review)
    for key in ("duplicates_retention_days", "leftovers_retention_days", "tmm_trash_retention_days"):
        if getattr(review_cfg, key) < -1:
            raise ConfigError(f"review.{key} must be an integer: days, 0 = at the end of the run, -1 = never")
    if review_cfg.tmm_trash_retention_minutes is not None:
        if review_cfg.tmm_trash_retention_minutes < -1:
            raise ConfigError("review.tmm_trash_retention_minutes must be an integer: minutes, 0 = in the first "
                              "run, -1 = never")
        if "tmm_trash_retention_days" in (data.get("review") or {}):
            raise ConfigError("[review] sets both tmm_trash_retention_days and tmm_trash_retention_minutes: "
                              "keep one of them")
    longest = max(review_cfg.duplicates_retention_days, review_cfg.leftovers_retention_days)
    if reports.journal_retention_days and longest >= 0 and reports.journal_retention_days <= longest:
        raise ConfigError(f"reports.journal_retention_days ({reports.journal_retention_days}) must be longer than "
                          f"the review retention ({longest} days): the review cleaning reads the run journals")

    logging_cfg = _section(data, "logging", Logging)
    if logging_cfg.file:
        logging_cfg = dataclasses.replace(logging_cfg, file=check_path_text(logging_cfg.file, "logging.file"))
    if logging_cfg.max_size_mb < 1 or logging_cfg.backups < 1:
        raise ConfigError("logging.max_size_mb and logging.backups must be >= 1")

    schedule = _section(data, "schedule", Schedule)
    if not 1 <= schedule.run_every_minutes <= 1440:
        raise ConfigError("schedule.run_every_minutes must be between 1 and 1440")
    if schedule.tmm_sync_time and not _TIME.match(schedule.tmm_sync_time):
        raise ConfigError(f"schedule.tmm_sync_time must be HH:MM (24 h) or \"\", got {schedule.tmm_sync_time!r}")

    tmm = _section(
        data, "tmm", Tmm,
        path_map=_path_map,
        after_move_movie=_commands, after_move_tvshow=_commands,
        full_sync_movie=_commands, full_sync_tvshow=_commands,
    )
    tmm = dataclasses.replace(
        tmm,
        api_key_file=check_path_text(tmm.api_key_file, "tmm.api_key_file"),
        after_move_movie=tmm.after_move_movie or DEFAULT_AFTER_MOVE_MOVIE,
        after_move_tvshow=tmm.after_move_tvshow or DEFAULT_AFTER_MOVE_TVSHOW,
        full_sync_movie=tmm.full_sync_movie or DEFAULT_FULL_MOVIE,
        full_sync_tvshow=tmm.full_sync_tvshow or DEFAULT_FULL_TVSHOW,
    )
    _http_url(tmm.base_url, "tmm.base_url", tmm.allow_insecure_http, "tmm.allow_insecure_http")
    if tmm.timeout_seconds < 1:
        raise ConfigError("tmm.timeout_seconds must be >= 1")
    for key in ("movie_endpoint", "tvshow_endpoint"):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", getattr(tmm, key)):
            raise ConfigError(f"tmm.{key} must be a plain endpoint name, got {getattr(tmm, key)!r}")
    if tmm.enabled and not tmm.path_map and uses_paths((tmm.after_move_movie, tmm.after_move_tvshow)):
        raise ConfigError("tmm.path_map is required when a tmm command uses \"{paths}\"")

    cfg = Config(
        paths=paths, mount=mount, media=media, naming=naming, completeness=comp, freebox=fbx,
        safety=safety, tmm=tmm, reports=reports, review=review_cfg, logging=logging_cfg, schedule=schedule,
        source_file=source_file,
    )
    _check_layout(cfg)
    return cfg


def _under(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent.rstrip("/") + "/")


def _check_layout(cfg: Config) -> None:
    p = cfg.paths
    if cfg.mount is not None:
        mp = cfg.mount.mountpoint
        for key in ("downloads", "movies", "series", "review"):
            if not _under(getattr(p, key), mp):
                raise ConfigError(f"paths.{key} must be inside mount.mountpoint ({mp})")
    roots = {"downloads": p.downloads, "movies": p.movies, "series": p.series}
    for a, pa in roots.items():
        for b, pb in roots.items():
            if a != b and _under(pa, pb):
                raise ConfigError(f"paths.{a} must not be inside paths.{b}")
    if _under(p.review, p.movies) or _under(p.review, p.series):
        raise ConfigError("paths.review must not be inside the library")
    if _under(p.downloads, p.review) or _under(p.movies, p.review) or _under(p.series, p.review):
        raise ConfigError("paths.review must not contain Downloads or the library")
    if _under(p.review, p.downloads):
        ignored = {n.casefold() for n in cfg.media.ignore_names}
        if os.path.dirname(p.review) != p.downloads or os.path.basename(p.review).casefold() not in ignored:
            raise ConfigError("paths.review inside Downloads must be a direct child whose name is listed in "
                              "media.ignore_names (default: <downloads>/_review)")
    media_roots = (p.downloads, p.movies, p.series, p.review)
    if cfg.mount is not None and _under(p.state_dir, cfg.mount.mountpoint):
        raise ConfigError("paths.state_dir must be on local disk, not on the media storage")
    if any(_under(p.state_dir, r) or _under(r, p.state_dir) for r in media_roots):
        raise ConfigError("paths.state_dir must be separate from Downloads, the library and the review folder")
    if cfg.logging.file and any(_under(cfg.logging.file, r) for r in (*media_roots, p.state_dir)):
        raise ConfigError("logging.file must be outside the media folders and state_dir (e.g. /var/log/mediaorg/)")
