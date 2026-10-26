"""Test fixtures: a fake media tree with a fictional library whose folder names cover the naming
edge cases met in real libraries (years, sequels, digits, punctuation, accents, camel case)."""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import tempfile
import time
from unittest import mock

from mediaorg import config as config_mod
from mediaorg.guards import MountEntry

# Fictional library folders (Movies, Series), in the layout tinyMediaManager creates.
MOVIES = [
    "2048 Copper Sound (2017)", "KWO - Kestrel Vs. Warden (2002)", "Kestrel (1977)", "Kestrel - Quarry (2016)",
    "Kestrel - Basin (2023)", "Kestrel Revival (1994)", "Kestrel3 (1988)", "Kestrels (1983)",
    "Kestrels Vs Warden - Coda (2005)", "Tamsin (2023)", "Lowlight (2025)",
    "Ironbark V Gullwing - Dusk Of Rule (2015)", "By Low Tide (2025)", "Paper Orchard (2010)",
    "Ember Day (2025)", "Saltmere (2020)", "Saltmere - Part Two (2023)", "Driftwood (2021)", "Undertow (2020)",
    "Curious Engines - The Riddles Of Lomax (2021)", "Walk me to the shore (2023)",
    "Tempest - A Long River Tale (2023)", "Sentinel II (2023)", "Fernland 2 - Crossing (2024)", "Lucent (2022)",
    "In The Reeds (2025)", "In The Quiet Hills (2024)", "Ned Ash - Chapter 4 (2022)", "Halvard (2022)",
    "Keepers Of The Amber Field (2022)", "Osric The Finder (2023)", "Ferry Calling (2024)", "Tobin 17 (2024)",
    "Courier - Unbound - Low Ledger Part One (2022)", "Courier - Unbound - The Final Ledger (2024)",
    "Tidewater (2021)", "Gaunt (2021)", "Brackish (2022)", "Find Us At Dawn - Were Long Gone (2024)",
    "Two Roads Past Midnight (2024)", "Warden (1986)", "Warden - Saltmarsh (2024)", "Warden 2 (1987)",
    "Wardens (2008)", "Lodestar (2013)", "Project Glass Wren (2025)", "Halcyon (2010)",
    "Beacon Nine (2025)", "Pale Hours Like Ours (2023)", "Wavecrest (2025)", "Corvid (2019)", "Larkspur (2025)",
    "The Surveyor2 (2023)", "The Lamplighter (2024)", "The Cartographer (2024)", "The Orchardist (2022)",
    "The Glassblower (2011)", "The Tinsmith (2025)", "The Salt Merchant (2023)", "The Drifter And Fennel (2025)",
    "The Tidecallers (2022)", "The Weir (1995)", "The Weir Rebuilt (2001)", "The Weir Rekindled (2020)",
    "The Weir Returns (2001)", "The Guild of Unlikely Gardeners (2023)", "The Long Pier (2022)",
    "The Outer Shoal (2025)", "The Warden (2017)", "The Weaver - One Last Thread (2025)",
    "The Quiet Trio (2024)", "Ada Corwin's Garden Club (2019)",
]
SERIES = [
    "Odd Heron (2023)", "Keeper Of Tides (2024)", "Pendulum (2023)", "Slow Current (2023)", "Saltmere Chronicle (2023)",
    "Calloway Farm (2025)", "Uncommon (2022)", "For Every Shore (2018)", "Scaffold (2020)", "Gleam (2021)",
    "Switchback (2022)", "Our Quiet Engines (2018)", "House Of The Otter (2021)", "Overspill (2020)", "Ropemaker (2023)",
    "Brightwater (2025)", "Marigold (2022)", "TideMark (2024)", "Clockwork (2024)", "Ori-Tal Benno (2021)",
    "Taught By Owls (2019)", "Yonder (2018)", "Partition (2021)", "Cistern (2022)", "Late Swallows (2021)",
    "Saltworks (2019)", "Severin House Of Adler (2024)", "Glass Town (2025)",
    "Far Signal Brave Old Coasts (2021)", "Rookery (2017)", "Brimstone (1999)", "Lumen The Silent Choir (2024)",
    "The Lodge (2023)", "The Lodgekeeper From The Tales Of Ned Ash (2022)", "The Hour Of The Moth (2023)",
    "The Near Border (2024)", "The Edge Of Here (2022)", "The Drifter (2018)", "The Open Register (2021)",
    "The Wandering Choir Corin Vale (2022)", "Prairie Duke (2021)", "NightLoom (2025)", "Tallgrass (2015)",
]
EPISODES = {
    "Far Signal Brave Old Coasts (2021)/Season 3": ["Far.Signal.Brave.Old.Coasts.S03E07.1080p.mkv"],
    "Switchback (2022)/Season 2": ["Switchback.S02E03.1080p.mkv", "Switchback.S02E03.1080p.nfo"],
    "Brightwater (2025)/Season 1": ["Brightwater.S01E01.1080p.mkv", "Brightwater.S01E02.1080p.mkv"],
    "Brimstone (1999)/Season 1": ["Brimstone.S01E01.mkv"],
    "Cistern (2022)/Season 3": ["Cistern.S03E01.1080p.mkv"],
    "The Edge Of Here (2022)/Season 1": ["The.Edge.Of.Here.S01E01.1080p.mkv"],
    "The Wandering Choir Corin Vale (2022)/Season 2": ["The.Wandering.Choir.Corin.Vale.S02E01.1080p.mkv"],
}
EMPTY_MOVIES = {"Pale Hours Like Ours (2023)"}

OLD = time.time() - 3 * 3600  # files "finished" three hours ago


def touch(path: str, data: bytes = b"x", mtime: float = OLD) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    os.utime(path, (mtime, mtime))
    return path


def age_tree(root: str, mtime: float = OLD) -> None:
    for d, dirs, files in os.walk(root):
        for n in files + dirs:
            os.utime(os.path.join(d, n), (mtime, mtime), follow_symlinks=False)
    os.utime(root, (mtime, mtime))


class FakeNas:
    """A temporary directory laid out like a mounted media share: Downloads, Movies, Series."""

    def __init__(self, extra_series=(), extra_movies=()):
        self.tmp = tempfile.mkdtemp(prefix="mediaorg-test-")
        self.mp = os.path.join(self.tmp, "nas")
        self.dl = os.path.join(self.mp, "Downloads")
        self.movies = os.path.join(self.mp, "Movies")
        self.series = os.path.join(self.mp, "Series")
        self.state = os.path.join(self.tmp, "state")
        for d in (self.dl, self.movies, self.series):
            os.makedirs(d)
        for m in [*MOVIES, *extra_movies]:
            os.makedirs(os.path.join(self.movies, m))
            if m not in EMPTY_MOVIES:
                touch(os.path.join(self.movies, m, m.replace(" ", ".") + ".mkv"))
                touch(os.path.join(self.movies, m, "movie.nfo"))
        for s in [*SERIES, *extra_series]:
            os.makedirs(os.path.join(self.series, s), exist_ok=True)
        for rel, files in EPISODES.items():
            for f in files:
                touch(os.path.join(self.series, rel, f))

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def config_dict(self, **overrides) -> dict:
        d = {
            "paths": {"downloads": self.dl, "movies": self.movies, "series": self.series,
                      "review": os.path.join(self.dl, "_review"), "state_dir": self.state},
            "mount": {"mountpoint": self.mp, "fstype": "cifs", "source": "//nas.example/media"},
            "completeness": {"method": "quiescence", "min_quiet_minutes": 30},
        }
        for k, v in overrides.items():
            d.setdefault(k, {}).update(v)
        return d

    def config(self, **overrides):
        return config_mod.from_dict(self.config_dict(**overrides), source_file="<test>")

    def mount_entries(self):
        return [MountEntry("/", "xfs", "/dev/root", "rw"),
                MountEntry(self.mp, "cifs", "//nas.example/media", "rw")]

    def snapshot(self) -> dict[str, bytes]:
        out = {}
        for d, dirs, files in os.walk(self.mp):
            rel = os.path.relpath(d, self.mp)
            out[rel + "/"] = b""
            for f in files:
                with open(os.path.join(d, f), "rb") as fh:
                    out[os.path.join(rel, f)] = fh.read()
        return out

    def exists(self, *rel) -> bool:
        return os.path.lexists(os.path.join(self.mp, *rel))


@contextlib.contextmanager
def cli_env(nas: FakeNas, **cfg_overrides):
    """Run mediaorg.cli.main against the fake NAS: config file, mountinfo and uid patched."""
    import tomllib  # noqa: F401  (ensures 3.11+)

    path = os.path.join(nas.tmp, "config.toml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(to_toml(nas.config_dict(**cfg_overrides)))
    with mock.patch("mediaorg.guards.read_mountinfo", return_value=nas.mount_entries()), \
            mock.patch("mediaorg.service.UNIT_DIR", os.path.join(nas.tmp, "systemd")), \
            mock.patch("os.geteuid", return_value=1000):
        yield path


def run_cli(*argv) -> tuple[int, str]:
    from mediaorg.cli import main

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = main(list(argv))
    return code, buf.getvalue()


def to_toml(d: dict) -> str:
    def val(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        if isinstance(v, str):
            return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
        if isinstance(v, (list, tuple)):
            return "[" + ", ".join(val(x) for x in v) + "]"
        if isinstance(v, dict):
            return "{ " + ", ".join(f"{k} = {val(x)}" for k, x in v.items()) + " }"
        raise TypeError(v)
    out = []
    for section, body in d.items():
        out.append(f"[{section}]")
        out.extend(f"{k} = {val(v)}" for k, v in body.items())
        out.append("")
    return "\n".join(out)
