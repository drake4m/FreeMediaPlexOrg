"""Documentation, value types and the commented TOML writer of the configuration.

Defaults come from the dataclasses in config.py (one source of truth); this module adds what a person
needs: what each key means, how to type a value on the command line (`mediaorg config set`), and how
the configuration file is written back with its documentation.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
import textwrap
import tomllib
from dataclasses import dataclass

from .config import SECTIONS, Config, ConfigError, check_path_text


class _Required:
    def __repr__(self) -> str:
        return "<required>"


REQUIRED = _Required()

BOOL_WORDS = {"true": True, "yes": True, "on": True, "1": True, "false": False, "no": False, "off": False, "0": False}


@dataclass(frozen=True)
class Key:
    section: str
    name: str
    kind: str  # path | str | int | bool | enum | list | pairs | commands | time
    doc: str
    choices: tuple[str, ...] = ()
    example: object = None  # shown for keys without a default
    derived: str = ""  # what an empty default means, e.g. "<downloads>/_review"
    advanced: bool = False  # left out of `config keys` unless --all

    @property
    def dotted(self) -> str:
        return f"{self.section}.{self.name}"

    @property
    def default(self):
        f = {f.name: f for f in dataclasses.fields(SECTIONS[self.section])}[self.name]
        if f.default is not dataclasses.MISSING:
            return f.default
        if f.default_factory is not dataclasses.MISSING:
            return f.default_factory()
        return REQUIRED


SECTION_DOCS = {
    "paths": "Folders. Downloads, the library and the review folder must be on ONE filesystem: files are "
             "moved with instant renames, never copied.",
    "mount": "Optional safety check: nothing is touched unless this mount point is mounted from this source "
             "(a NAS share, a USB disk). Without it, an unmounted share leaves an empty local folder behind "
             "that could be filled by mistake. `mediaorg config init` fills it in when the media folders are "
             "on their own mount. Leave the three keys commented out to disable the check.",
    "completeness": "When is a download finished? \"quiescence\": its files have not changed for "
                    "min_quiet_minutes and none has a temporary extension (any download client, or files "
                    "copied by hand). \"freebox\": the Freebox download manager reports the task as done "
                    "(see `mediaorg freebox authorize`).",
    "freebox": "Freebox download manager; used only with completeness.method = \"freebox\".",
    "tmm": "tinyMediaManager (optional). After a run that filed something, tMM updates its library, then "
           "scrapes and renames the new items. The API key is read from api_key_file "
           "(`sudo mediaorg tmm set-key`), never from this file.",
    "review": "What is cleaned up automatically, and when.",
    "schedule": "Used by `sudo mediaorg service install` to write the systemd timers.",
    "safety": "Limits that stop a run before it acts.",
    "reports": "Run reports and journals in state_dir, bounded so that they never fill the disk.",
    "logging": "Logs always go to standard error (under systemd: the journal, rotated by journald). "
               "Optionally also to a file, rotated by size.",
    "media": "What counts as a video, a subtitle or an extra, and what is ignored in Downloads.",
    "naming": "Names of the folders mediaorg creates; existing folders are always reused. "
              "Placeholders: {title}, {year}, {season}.",
}

KEYS: list[Key] = [
    # [paths]
    Key("paths", "downloads", "path", "Folder where completed downloads appear (the watched folder). Each entry "
        "in it, a file or a folder, is one download.", example="/srv/media/downloads"),
    Key("paths", "movies", "path", "Movie library: <movies>/<Title (Year)>/<files>.", example="/srv/media/movies"),
    Key("paths", "series", "path", "TV library: <series>/<Show (Year)>/Season N/<files>.", example="/srv/media/tv"),
    Key("paths", "review", "path", "Where duplicates and leftovers of handled downloads go for you to review. "
        "Same filesystem as the rest; inside Downloads only as a direct child listed in media.ignore_names. "
        "For the timers it must share a folder other than / with Downloads and the libraries.",
        derived="<downloads>/_review"),
    Key("paths", "state_dir", "path", "Local folder for journals (undo), reports, the run lock, the Freebox token "
        "and caches. Never on the media storage."),
    # [mount]
    Key("mount", "mountpoint", "path", "Mount point that holds all the media folders.", example="/mnt/nas"),
    Key("mount", "fstype", "str", "Expected filesystem type, as `findmnt` shows it (cifs, nfs4, ext4 ...).",
        example="cifs"),
    Key("mount", "source", "str", "Expected source, as `findmnt` shows it (share or device).",
        example="//nas.local/media"),
    # [completeness]
    Key("completeness", "method", "enum", "How finished downloads are recognised (see above).",
        choices=("quiescence", "freebox")),
    Key("completeness", "min_quiet_minutes", "int", "Minutes without any change before a download counts as "
        "finished (method \"quiescence\", and untracked entries with untracked = \"quiescence\"). Minimum 5."),
    Key("completeness", "temp_extensions", "list", "A file with one of these extensions marks its download as "
        "unfinished."),
    Key("completeness", "untracked", "enum", "Method \"freebox\": entries the Freebox does not list as a task "
        "(copied by hand). \"wait\" = never move them; \"quiescence\" = move them after min_quiet_minutes.",
        choices=("wait", "quiescence")),
    Key("completeness", "on_seeding", "enum", "Method \"freebox\": a finished BitTorrent download that is still "
        "seeding. \"wait\" = moved once the Freebox says done (at its stop ratio); \"stop\" = a run that files it "
        "stops the task first (it no longer seeds), then moves it.", choices=("wait", "stop")),
    # [freebox]
    Key("freebox", "remote_downloads_dir", "str", "The Freebox's own path of paths.downloads, exactly as "
        "`mediaorg freebox status` prints it. A run that needs the Freebox's verdict stops (nothing moved) "
        "if the Freebox downloads elsewhere.",
        example="/Freebox/Downloads"),
    Key("freebox", "base_url", "str", "Freebox OS API address. HTTPS; the certificate is checked against ca_file."),
    Key("freebox", "ca_file", "path", "Freebox root certificates (installed by install.sh)."),
    Key("freebox", "token_file", "path", "App token written by `mediaorg freebox authorize` (mode 0600).",
        derived="<state_dir>/freebox_app_token.json"),
    Key("freebox", "device_name", "str", "Device name shown on the Freebox when you authorize the app.",
        derived="this host's name"),
    Key("freebox", "timeout_seconds", "int", "HTTP timeout in seconds."),
    Key("freebox", "app_id", "str", "App identifier on the Freebox; changing it needs a new authorization.",
        advanced=True),
    Key("freebox", "app_name", "str", "App name shown in Freebox OS.", advanced=True),
    Key("freebox", "allow_insecure_http", "bool", "Accept an http:// base_url (not recommended).", advanced=True),
    # [tmm]
    Key("tmm", "enabled", "bool", "Send tMM its calls after runs that filed something (and the nightly sync)."),
    Key("tmm", "base_url", "str", "tMM HTTP API address. Plain http:// only to this machine unless "
        "allow_insecure_http = true."),
    Key("tmm", "api_key_file", "path", "File holding the tMM API key on one line (mode 0640, root:mediaorg); "
        "written by `sudo mediaorg tmm set-key`."),
    Key("tmm", "path_map", "pairs", "Library paths as tMM sees them when it runs in a container: "
        "[[host_path, tmm_path], ...]. Needed only by tMM commands that use \"{paths}\".",
        example=[["/srv/media/movies", "/media/movies"], ["/srv/media/tv", "/media/series"]]),
    Key("tmm", "timeout_seconds", "int", "HTTP timeout in seconds."),
    Key("tmm", "defer_moves_while_busy", "bool", "Move nothing while tMM is busy (needs tMM 5.3.5 or later; "
        "ignored before)."),
    Key("tmm", "allow_insecure_http", "bool", "Accept plain http:// to a tMM on another machine (the key travels "
        "unencrypted).", advanced=True),
    Key("tmm", "movie_endpoint", "str", "API endpoint for movies (checked by `mediaorg check`).", advanced=True),
    Key("tmm", "tvshow_endpoint", "str", "API endpoint for TV shows (checked by `mediaorg check`).", advanced=True),
    Key("tmm", "after_move_movie", "commands", "tMM calls after a run that filed movies. Default: update all, "
        "scrape new, rename new.", advanced=True),
    Key("tmm", "after_move_tvshow", "commands", "tMM calls after a run that filed TV episodes. Default: update "
        "all, scrape new, rename new.", advanced=True),
    Key("tmm", "full_sync_movie", "commands", "Movie calls of the nightly `mediaorg tmm sync`.", advanced=True),
    Key("tmm", "full_sync_tvshow", "commands", "TV-show calls of the nightly `mediaorg tmm sync`.", advanced=True),
    # [review]
    Key("review", "delete_identical_duplicates", "bool", "A download whose bytes are identical to the library "
        "copy is deleted: nothing is lost. false = move it to review instead."),
    Key("review", "delete_duplicates", "bool", "A download the library already has is deleted in the run that "
        "finds it, even when its bytes differ: the same episode, or the same film of the same year, not a "
        "REPACK/PROPER, not one part of a split file, and not larger than the library copy. Other duplicates "
        "(a bigger or repaired copy, a year that cannot be confirmed) go to review/duplicates and are kept "
        "until you decide; certain duplicates that earlier runs moved there are deleted by the next run. "
        "false = duplicates go to review/duplicates (duplicates_retention_days)."),
    Key("review", "duplicates_retention_days", "int", "Days after which the certain duplicates of a run are "
        "deleted from review/duplicates/ (0 = at the end of the run, -1 = never). Other duplicates are kept. "
        "Not used with delete_duplicates = true."),
    Key("review", "leftovers_retention_days", "int", "Days after which release clutter (.nfo, .txt, small images "
        "and subtitles, small samples) is deleted from review/leftovers/ (0 = at the end of the run, -1 = never). "
        "Other files are kept."),
    Key("review", "tmm_trash_retention_days", "int", "Days a file stays in tMM's trash (.deletedByTMM in the "
        "library) before it is deleted (0 = in the first run, -1 = never)."),
    Key("review", "tmm_trash_retention_minutes", "int", "The same in minutes; set this or "
        "tmm_trash_retention_days, not both.", example=30),
    # [schedule]
    Key("schedule", "run_every_minutes", "int", "How often the timer runs mediaorg (1-1440). 1, 2, 3, 4, 5, 6, "
        "10, 12, 15, 20 or 30 minutes and 1, 2, 3, 4, 6, 8, 12 or 24 hours run at fixed times of the day; other "
        "values count from the end of the previous run. A run with nothing to do takes well under a second "
        "and logs nothing."),
    Key("schedule", "tmm_sync_time", "time", "Local time (HH:MM) of the nightly tMM sync: update the library, "
        "scrape and rename new items. \"\" = no nightly sync."),
    # [safety]
    Key("safety", "max_moves_per_run", "int", "A run planning more moves and deletions stops before acting "
        "(exit 6); allow it once with `run --apply --max-moves N`."),
    Key("safety", "min_free_mb", "int", "Free space (MB) needed in state_dir before a run moves anything: every "
        "move is journaled there first (0 = no check)."),
    # [reports]
    Key("reports", "retention_days", "int", "Run reports older than this many days are deleted (0 = never by "
        "age)."),
    Key("reports", "max_reports", "int", "At most this many run reports are kept (0 = no limit)."),
    Key("reports", "journal_retention_days", "int", "Journals of finished runs (used by undo and by the review "
        "cleaning) are deleted after this many days; must be longer than the review retention (0 = keep "
        "forever). The journal of an interrupted run is kept until the next run has reconciled it."),
    # [logging]
    Key("logging", "file", "path", "Also write the log to this file, rotated by size (e.g. "
        "/var/log/mediaorg/mediaorg.log). \"\" = journal only.", example="/var/log/mediaorg/mediaorg.log"),
    Key("logging", "max_size_mb", "int", "Size in MB at which the log file is rotated."),
    Key("logging", "backups", "int", "Number of rotated log files kept."),
    # [media]
    Key("media", "video_extensions", "list", "Files filed as videos."),
    Key("media", "subtitle_extensions", "list", "Subtitles, moved and renamed with their video."),
    Key("media", "extras_dir_names", "list", "Videos below folders with these names are extras, never the "
        "episode or movie."),
    Key("media", "ignore_names", "list", "Entries in Downloads with these names are ignored (the review folder "
        "must be listed when it is inside Downloads)."),
    Key("media", "ignore_hidden", "bool", "Ignore entries in Downloads whose name starts with a dot."),
    # [naming]
    Key("naming", "movie_folder", "str", "New movie folder."),
    Key("naming", "movie_folder_no_year", "str", "New movie folder when the release has no year (tMM adds the "
        "year when it renames the movie)."),
    Key("naming", "series_folder", "str", "New show folder."),
    Key("naming", "series_folder_no_year", "str", "New show folder when the release has no year."),
    Key("naming", "season_folder", "str", "Season folder inside a show folder."),
    Key("naming", "tmm_staging", "bool", "New movie and show folders end with \" [mediaorg]\" until "
        "tinyMediaManager renames them (with tmm.enabled only; tMM ignores text in [...] when it reads the "
        "name). Needed when tMM runs on Linux with the library on an SMB/CIFS share: there tMM cannot rename a "
        "folder whose new name differs only in letter case ('Rise of the ...' -> 'Rise Of The ...')."),
]

BY_NAME = {k.dotted: k for k in KEYS}


def find_key(dotted: str) -> Key:
    key = BY_NAME.get(dotted)
    if key is None:
        import difflib
        near = difflib.get_close_matches(dotted, list(BY_NAME), n=3)
        hint = f"; did you mean {', '.join(near)}?" if near else "; list them with `mediaorg config keys`"
        raise ConfigError(f"unknown key {dotted!r}{hint}")
    return key


# ------------------------------------------------------------------------------- values from the command line
def parse_value(key: Key, text: str):
    """The TOML value for `config set KEY=VALUE`."""
    kind = key.kind
    if kind == "path":
        if text == "" and (key.derived or key.dotted == "logging.file"):
            return ""
        return check_path_text(text, key.dotted)
    if kind == "str":
        return text
    if kind == "int":
        try:
            return int(text, 10)
        except ValueError:
            raise ConfigError(f"{key.dotted} needs an integer, got {text!r}") from None
    if kind == "bool":
        v = BOOL_WORDS.get(text.strip().lower())
        if v is None:
            raise ConfigError(f"{key.dotted} needs true or false, got {text!r}")
        return v
    if kind == "enum":
        if text not in key.choices:
            raise ConfigError(f"{key.dotted} must be one of {', '.join(key.choices)}; got {text!r}")
        return text
    if kind == "time":
        return text.strip()
    if kind == "list":
        if text.lstrip().startswith("["):
            return _toml_fragment(key, text)
        return [x.strip() for x in text.split(",") if x.strip()]
    if kind == "pairs":
        if text.lstrip().startswith("["):
            return _toml_fragment(key, text)
        pairs = []
        for part in filter(None, (p.strip() for p in text.split(","))):
            host, sep, inner = part.partition("=")
            if not sep:
                raise ConfigError(f"{key.dotted}: write HOST_PATH=TMM_PATH[,HOST_PATH=TMM_PATH], got {part!r}")
            pairs.append([host.strip(), inner.strip()])
        return pairs
    if kind == "commands":
        return _toml_fragment(key, text)
    raise ConfigError(f"{key.dotted}: unsupported kind {kind}")


def _toml_fragment(key: Key, text: str):
    try:
        return tomllib.loads(f"v = {text}")["v"]
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{key.dotted}: not a valid TOML value ({exc}): {text}") from None


# ------------------------------------------------------------------------------- TOML writing
_BARE = re.compile(r"^[A-Za-z0-9_-]+$")
_ESC = {"\\": "\\\\", '"': '\\"', "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}


def toml_string(s: str) -> str:
    out = []
    for ch in s:
        if ch in _ESC:
            out.append(_ESC[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def toml_value(v, indent: int = 0, width: int = 100) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return toml_string(v)
    if isinstance(v, dict):
        items = ", ".join(f"{k if _BARE.match(k) else toml_string(k)} = {toml_value(x)}" for k, x in v.items())
        return "{ " + items + " }" if items else "{}"
    if isinstance(v, (list, tuple)):
        parts = [toml_value(x) for x in v]
        inline = "[" + ", ".join(parts) + "]"
        nested = any(isinstance(x, (list, tuple, dict)) for x in v)
        if not nested and len(inline) + indent <= width:
            return inline
        pad = " " * (indent + 2)
        return "[\n" + "".join(f"{pad}{p},\n" for p in parts) + " " * indent + "]"
    raise ConfigError(f"cannot write {v!r} to TOML")


def _plain(v):
    """Tuples -> lists, recursively (what tomllib returns for arrays)."""
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    return v


def _comment(text: str, width: int = 98) -> list[str]:
    return ["# " + line for line in textwrap.wrap(text, width - 2)] or ["#"]


def render_file(raw: dict, version: str, now: dt.datetime | None = None, title: str | None = None) -> str:
    """The whole configuration file: every section and key with its documentation; keys that are not
    set are written as comments showing their default."""
    now = now or dt.datetime.now(dt.UTC)
    out = [
        title or f"# mediaorg configuration - written by mediaorg {version} on {now:%Y-%m-%d %H:%M} UTC.",
        "#",
        "# Change it with commands; they validate the result and keep the previous file as <this file>.bak:",
        "#   sudo mediaorg config set paths.movies=/srv/media/movies",
        "#   sudo mediaorg config unset review.leftovers_retention_days",
        "#   mediaorg config keys --all      (every key with its type, default and meaning)",
        "# You may also edit it by hand, then check it: sudo mediaorg config validate",
        "# Comments are rewritten by `mediaorg config set`; keep notes elsewhere.",
        "",
    ]
    for section in SECTIONS:
        body = raw.get(section) or {}
        out.append("# " + "-" * 96)
        out.append(f"[{section}]")
        out += _comment(SECTION_DOCS[section])
        for key in (k for k in KEYS if k.section == section):
            out.append("")
            label = key.doc
            if key.choices:
                label += f" One of: {', '.join(key.choices)}."
            out += _comment(label)
            if key.name in body:
                out.append(f"{key.name} = {toml_value(_plain(body[key.name]), 0)}")
            else:
                out.append(_unset_line(key))
        out.append("")
    return "\n".join(out)


def _builtin_commands(key: Key):
    from . import config as c
    return {"after_move_movie": c.DEFAULT_AFTER_MOVE_MOVIE, "after_move_tvshow": c.DEFAULT_AFTER_MOVE_TVSHOW,
            "full_sync_movie": c.DEFAULT_FULL_MOVIE, "full_sync_tvshow": c.DEFAULT_FULL_TVSHOW}[key.name]


def _unset_line(key: Key) -> str:
    d = key.default
    if key.kind == "commands":
        d = _builtin_commands(key)
    if d is REQUIRED or (d in ("", None, ()) and key.example is not None):
        shown = key.example if key.example is not None else ""
        tag = "required" if d is REQUIRED and key.section != "mount" else "example"
    elif key.derived and d in ("", None):
        return f"# {key.name} = ...   (default: {key.derived})"
    elif d is None:
        return f"# {key.name} =   (not set)"
    else:
        shown, tag = d, "default"
    value = toml_value(_plain(shown), 2)
    lines = value.split("\n")
    first = f"# {key.name} = {lines[0]}"
    rest = ["# " + ln for ln in lines[1:]]
    if rest:
        return "\n".join([first, *rest]) + f"\n#   ({tag})"
    return f"{first}   ({tag})"


def effective(cfg: Config) -> dict:
    """The configuration in force, defaults included, as a TOML-ready dict (for `config show`)."""
    out = {}
    for section, cls in SECTIONS.items():
        obj = getattr(cfg, section)
        if obj is None:
            continue
        body = {}
        for f in dataclasses.fields(cls):
            v = getattr(obj, f.name)
            if v is None:
                continue
            body[f.name] = _plain(v)
        out[section] = body
    return out


def render_plain(data: dict) -> str:
    lines = []
    for section, body in data.items():
        lines.append(f"[{section}]")
        for k, v in body.items():
            lines.append(f"{k} = {toml_value(v, 0)}")
        lines.append("")
    return "\n".join(lines)


def describe(key: Key) -> str:
    d = _builtin_commands(key) if key.kind == "commands" else key.default
    if key.kind == "commands":
        return f"{key.dotted}  [tMM command list; default: update all, scrape new, rename new]"
    if d is REQUIRED:
        default = "required" if key.section != "mount" else "optional section"
    elif key.derived and d in ("", None):
        default = f"default {key.derived}"
    elif d is None:
        default = "not set"
    else:
        default = "default " + toml_value(_plain(d), 0).replace("\n", " ")
    kind = key.kind + (f" ({'|'.join(key.choices)})" if key.choices else "")
    return f"{key.dotted}  [{kind}; {default}]"
