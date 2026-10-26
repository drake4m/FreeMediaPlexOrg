#!/usr/bin/env python3
"""Turn the settings of an existing tinyMediaManager (Windows, macOS or Linux) into settings for the
container started by setup-tmm.sh.

Reads   <backup>/tmm.json, movies.json, tvShows.json and scraper_*.conf (tMM 5.x format), i.e. the
        files of the old tMM's data folder.
Writes  <out>/ (the container's /data/data/) - refuses to overwrite anything.

Changes (everything else is kept as configured before):
  tmm.json     httpApiKey -> a NEW random key (also written to --api-key-file, root:<key-group> 0640)
               enableHttpServer -> true, httpServerPort -> 7878, ignoreSSLProblems -> false,
               mediaPlayer -> "" (a path on the old machine)
  movies.json  movieDataSource -> [<movies path in the container>]
  tvShows.json tvShowDataSource -> [<series path in the container>]
               renamerTvShowFoldername -> --tv-folder-pattern (default "${showTitle} (${showYear})",
               the folder name mediaorg creates for new shows)
  without --pro (tMM FREE): TV scraper tvdb -> tmdb; PRO-only artwork scrapers (tvdb, fanarttv,
               ffmpeg) removed (["tmdb"] if none is left)
  scraper_*.conf copied unchanged (they may hold scraper keys or logins, encrypted by tMM).
  Settings still holding a Windows path (C:\\...) are reported, not changed.

Secrets are never printed. Standard library only; run as root by setup-tmm.sh.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import sys
import time
import uuid

TV_FOLDER = "${showTitle} (${showYear})"
FREE_SCRAPER = "tmdb"
PRO_ONLY_ARTWORK = {"tvdb", "fanarttv", "ffmpeg"}  # TVDB, fanart.tv, FFmpeg integration: PRO in tMM 5


class MigrationError(Exception):
    pass


def _load(path: str, expect_name: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError as exc:
        raise MigrationError(f"missing {path}") from exc
    except ValueError as exc:
        raise MigrationError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise MigrationError(f"{path}: unexpected content")
    name = data.get("configFilename")
    if name not in (None, expect_name):
        raise MigrationError(f"{path}: configFilename is {name!r}, expected {expect_name!r}")
    return data


def _set(changes: list[str], label: str, data: dict, key: str, value, secret: bool = False) -> None:
    old = data.get(key, "<absent>")
    data[key] = value
    if old != value:
        changes.append(f"{label}: {key}: " + ("<replaced>" if secret else f"{old!r} -> {value!r}"))


def windows_paths(data, where: str = "") -> list[str]:
    """Settings that still hold a Windows path (e.g. C:\\... or Z:\\...): reported, not changed."""
    out = []
    if isinstance(data, dict):
        for k, v in data.items():
            out += windows_paths(v, f"{where}.{k}" if where else k)
    elif isinstance(data, list):
        for i, v in enumerate(data):
            out += windows_paths(v, f"{where}[{i}]")
    elif isinstance(data, str) and re.match(r"^[A-Za-z]:\\", data):
        out.append(f"{where} = {data!r}")
    return out


def migrate(tmm: dict, movies: dict, tv: dict, *, movies_path: str, series_path: str, api_key: str,
            pro: bool, tv_folder: str = TV_FOLDER) -> list[str]:
    """Apply the changes in place; return a human-readable change list (no secret values)."""
    ch: list[str] = []
    _set(ch, "tmm.json", tmm, "httpApiKey", api_key, secret=True)
    _set(ch, "tmm.json", tmm, "enableHttpServer", True)
    _set(ch, "tmm.json", tmm, "httpServerPort", 7878)
    _set(ch, "tmm.json", tmm, "ignoreSSLProblems", False)
    _set(ch, "tmm.json", tmm, "mediaPlayer", "")
    _set(ch, "movies.json", movies, "movieDataSource", [movies_path])
    _set(ch, "tvShows.json", tv, "tvShowDataSource", [series_path])
    _set(ch, "tvShows.json", tv, "renamerTvShowFoldername", tv_folder)
    if not pro:
        if tv.get("scraper") != FREE_SCRAPER:
            _set(ch, "tvShows.json", tv, "scraper", FREE_SCRAPER)
        for label, data in (("movies.json", movies), ("tvShows.json", tv)):
            arts = data.get("artworkScrapers")
            if isinstance(arts, list):
                kept = [a for a in arts if a not in PRO_ONLY_ARTWORK] or [FREE_SCRAPER]
                if kept != arts:
                    _set(ch, label, data, "artworkScrapers", kept)
    return ch


def _write_new(path: str, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.fchmod(fd, 0o600)  # independent of the caller's umask
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def write_api_key(path: str, key: str, group: str | None) -> str | None:
    """Write the key (0640, root:<group>); an existing file is kept as <path>.bak-<time>."""
    backup = None
    if os.path.exists(path):
        backup = f"{path}.bak-{time.strftime('%Y%m%d%H%M%S')}"
        os.replace(path, backup)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    os.fchmod(fd, 0o640)  # readable by the service group whatever the caller's umask
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(key + "\n")
    if group:
        shutil.chown(path, user="root", group=group)
    return backup


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backup", required=True, help="folder with tmm.json, movies.json, tvShows.json, scraper_*.conf")
    ap.add_argument("--out", required=True, help="target settings folder (container /data/data), empty or absent")
    ap.add_argument("--api-key-file", required=True, help="where mediaorg reads the tMM API key")
    ap.add_argument("--key-group", default="mediaorg", help="group owning the API key file (default mediaorg)")
    ap.add_argument("--movies-path", default="/media/movies")
    ap.add_argument("--series-path", default="/media/series")
    ap.add_argument("--pro", action="store_true", help="tMM PRO licence: keep TVDB / fanart.tv")
    ap.add_argument("--tv-folder-pattern", default=TV_FOLDER,
                    help=f"tMM's renamer pattern for show folders (default {TV_FOLDER!r})")
    a = ap.parse_args(argv)
    try:
        tmm = _load(os.path.join(a.backup, "tmm.json"), "tmm.json")
        movies = _load(os.path.join(a.backup, "movies.json"), "movies.json")
        tv = _load(os.path.join(a.backup, "tvShows.json"), "tvShows.json")
        if os.path.isdir(a.out) and os.listdir(a.out):
            raise MigrationError(f"{a.out} is not empty: refusing to overwrite existing tMM settings")
        key = str(uuid.uuid4())
        changes = migrate(tmm, movies, tv, movies_path=a.movies_path, series_path=a.series_path,
                          api_key=key, pro=a.pro, tv_folder=a.tv_folder_pattern)
        os.makedirs(a.out, mode=0o700, exist_ok=True)
        for name, data in (("tmm.json", tmm), ("movies.json", movies), ("tvShows.json", tv)):
            _write_new(os.path.join(a.out, name),
                       (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
        confs = sorted(glob.glob(os.path.join(a.backup, "scraper_*.conf")))
        for src in confs:  # byte-for-byte (tMM-encrypted values, Windows line endings kept)
            with open(src, "rb") as fh:
                _write_new(os.path.join(a.out, os.path.basename(src)), fh.read())
        backup = write_api_key(a.api_key_file, key, a.key_group)
    except (MigrationError, OSError, LookupError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"tMM settings written to {a.out} (version in backup: {tmm.get('version', '?')})")
    for c in changes:
        print(f"  changed  {c}")
    for src in confs:
        print(f"  copied   {os.path.basename(src)}")
    for name, data in (("tmm.json", tmm), ("movies.json", movies), ("tvShows.json", tv)):
        for w in windows_paths(data):
            print(f"  WARNING  {name}: {w} is a Windows path - set it in the tMM UI if the feature is used")
    print(f"  API key  new key written to {a.api_key_file}" + (f" (previous kept as {backup})" if backup else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
