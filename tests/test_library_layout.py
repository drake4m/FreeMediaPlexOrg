"""A tMM-managed library layout, samples/trailers in the library, `mediaorg library`."""

import json
import os
import unittest
from unittest import mock

from mediaorg.library import is_library_extra

from .fakes import FakeTmm
from .helpers import FakeNas, cli_env, run_cli, touch

# Series/Cistern (2022) as tMM's renamer lays out a show (artwork, NFO files, samples)
CISTERN_ROOT = ["banner.jpg", "clearlogo.png", "fanart.jpg", "poster.jpg", "theme.mp3", "tvshow.nfo", "unknown.jpg"]
CISTERN_S1 = [
    "Cistern.S01E01-sample.mkv", "Cistern.S01E01.jpg", "Cistern.S01E01.mkv", "Cistern.S01E01.nfo", "Cistern.S01E01.nfo-orig",
    "Cistern.S01E01.xml", "Cistern.S01E02.1080p-sample.mkv", "Cistern.S01E02.1080p.jpg", "Cistern.S01E02.1080p.mkv",
    "Cistern.S01E02.1080p.nfo", "Cistern.S01E02.1080p.nfo-orig", "Cistern.S01E03.1080p.jpg", "Cistern.S01E03.1080p.mkv",
    "Cistern.S01E03.1080p.nfo", "Cistern.S01E04.1080p.jpg", "Cistern.S01E04.1080p.mkv", "Cistern.S01E04.1080p.nfo",
    "Cistern.S01E05.1080p.jpg", "Cistern.S01E05.1080p.mkv", "Cistern.S01E05.1080p.nfo", "Cistern.S01E06.1080p.jpg",
    "Cistern.S01E06.1080p.mkv", "Cistern.S01E06.1080p.nfo", "Cistern.S01E07.1080p.jpg", "Cistern.S01E07.1080p.mkv",
    "Cistern.S01E07.1080p.nfo", "Cistern.S01E08.1080p.jpg", "Cistern.S01E08.1080p.mkv", "Cistern.S01E08.1080p.nfo",
    "Cistern.S01E09.1080p.jpg", "Cistern.S01E09.1080p.mkv", "Cistern.S01E09.1080p.nfo", "Cistern.S01E10.1080p.jpg",
    "Cistern.S01E10.1080p.mkv", "Cistern.S01E10.1080p.nfo", "season01-poster.jpg",
]
CISTERN = "Series/Cistern (2022)"


class Base(unittest.TestCase):
    def setUp(self):
        self.nas = FakeNas()

    def tearDown(self):
        self.nas.cleanup()

    def lib(self, rel, *names):
        for n in names:
            touch(os.path.join(self.nas.mp, rel, n), data=b"lib")

    def dl(self, name):
        return touch(os.path.join(self.nas.dl, name), data=b"new")

    def run_(self, *args, **cfg):
        with cli_env(self.nas, **cfg) as path:
            return run_cli("-c", path, *args)


class TestIsLibraryExtra(unittest.TestCase):
    def test_extras(self):
        for n in ["Cistern.S01E01-sample.mkv", "Cistern.S01E02.1080p-sample.mkv", "Corvid (2019)-trailer.mp4",
                  "Corvid.2019.2160p-trailer.mkv", "sample-cistern.s01e01.mkv", "Show.S01E01.sample.mkv", "sample.mkv",
                  "SAMPLE-x.mkv"]:
            self.assertTrue(is_library_extra(n), n)

    def test_not_extras(self):
        for n in ["Cistern.S01E01.mkv", "Show.S01E05.The.Sample.1080p.mkv", "Trailer.Yard.Kings.S01E01.mkv",
                  "Samples.Of.Life.2019.mkv", "The.Sampler.2019.mkv", "Show.S01E01.1080p-sampled.mkv",
                  # names that END with an episode/movie title
                  "Show - S02E05 - The Trailer.mkv", "Show - S03E07 - Blood Sample.mkv", "Sample People (1998).mkv",
                  "Show_S01E01_sample.mkv", "Show S01E01 trailer.mkv"]:
            self.assertFalse(is_library_extra(n), n)


class TestBunkerLayout(Base):
    def test_existing_episode_is_duplicate_and_named_by_real_file(self):
        self.lib(f"{CISTERN}/Season 1", *CISTERN_S1)
        self.lib(CISTERN, *CISTERN_ROOT)
        self.dl("Cistern.S01E01.2160p.WEB.h265-GRP.mkv")
        code, out = self.run_("run")
        self.assertEqual(code, 0, out)
        self.assertIn("already in library: S01E01 as 'Cistern.S01E01.mkv'", out)
        self.assertNotIn("as 'Cistern.S01E01-sample.mkv'", out)

    def test_sample_alone_does_not_block_the_real_episode(self):
        # library has only the sample of S01E02 (the real file is missing)
        files = [f for f in CISTERN_S1 if f != "Cistern.S01E02.1080p.mkv"]
        self.lib(f"{CISTERN}/Season 1", *files)
        self.dl("Cistern.S01E02.1080p.WEB.h264-GRP.mkv")
        code, out = self.run_("run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertTrue(self.nas.exists(CISTERN, "Season 1", "Cistern.S01E02.1080p.WEB.h264-GRP.mkv"))
        self.assertTrue(self.nas.exists(CISTERN, "Season 1", "Cistern.S01E02.1080p-sample.mkv"))  # untouched
        self.assertIn("library had only 'Cistern.S01E02.1080p-sample.mkv' for S01E02", out)

    def test_episode_named_after_its_title_still_counts(self):
        # "... - The Trailer" is a real episode, not an extra -> download is a duplicate
        self.lib("Series/Switchback (2022)/Season 2", "Switchback - S02E04 - The Trailer.mkv")
        self.dl("Switchback.S02E04.1080p.WEB.h264-GRP.mkv")
        code, out = self.run_("run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertFalse(self.nas.exists("Series/Switchback (2022)/Season 2/Switchback.S02E04.1080p.WEB.h264-GRP.mkv"))
        self.assertIn("already in library: S02E04 as 'Switchback - S02E04 - The Trailer.mkv'", out)

    def test_new_episode_goes_into_existing_season_folder(self):
        self.lib(f"{CISTERN}/Season 1", *CISTERN_S1)
        self.lib(CISTERN, *CISTERN_ROOT)
        self.dl("Cistern.S01E11.1080p.WEB.h264-GRP.mkv")  # hypothetical extra episode
        code, out = self.run_("run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertTrue(self.nas.exists(CISTERN, "Season 1", "Cistern.S01E11.1080p.WEB.h264-GRP.mkv"))
        self.assertEqual(sorted(os.listdir(os.path.join(self.nas.mp, CISTERN))),
                         sorted(CISTERN_ROOT + ["Season 1", "Season 3"]))  # no new folder created


class TestMovieExtras(Base):
    def test_trailer_only_folder_gets_the_movie(self):
        self.lib("Movies/Larkspur (2025)", "Larkspur.2025.1080p-trailer.mp4")
        os.remove(os.path.join(self.nas.movies, "Larkspur (2025)", "Larkspur.(2025).mkv"))
        self.dl("Larkspur.2025.1080p.WEB.h264-GRP.mkv")
        code, out = self.run_("run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertTrue(self.nas.exists("Movies/Larkspur (2025)/Larkspur.2025.1080p.WEB.h264-GRP.mkv"))
        self.assertIn("movie folder had only 'Larkspur.2025.1080p-trailer.mp4'", out)

    def test_movie_plus_trailer_is_duplicate_named_by_movie(self):
        self.lib("Movies/Corvid (2019)", "Corvid (2019)-trailer.mp4")
        self.dl("Corvid.2019.1080p.BluRay.x264-GRP.mkv")
        code, out = self.run_("run")
        self.assertEqual(code, 0, out)
        self.assertIn("movie folder already has 'Corvid.(2019).mkv'", out)


class TestLibraryCommand(Base):
    def build_anomalies(self):
        self.lib(f"{CISTERN}/Season 1", *CISTERN_S1)
        self.lib(CISTERN, *CISTERN_ROOT)
        os.makedirs(os.path.join(self.nas.series, "Cistern (2022)", "extrafanart"))
        self.lib("Series/Gleam (2021)/Season 2", "Gleam.S02E01.1080p.mkv")
        self.lib("Series/Gleam (2021)/Season 02", "Gleam.S02E02.1080p.mkv")  # two folders for season 2
        self.lib("Series/Partition (2021)/Season 1", "Partition.S02E01.1080p.mkv")  # misplaced
        self.lib("Series/Partition (2021)/Season 2", "Partition.Episode.Three.mkv")  # unreadable
        self.lib("Series/Scaffold (2020)", "Scaffold.S01E01.1080p.mkv")  # in show root
        self.lib("Series/Scaffold (2020)/Featurettes", "making-of.mkv")
        self.lib("Series", "Stray.S01E01.mkv")  # outside any show folder
        self.lib("Movies/Corvid (2019)", "Corvid (2019)-trailer.mp4")
        self.lib("Movies/Kestrel (1977)", "Kestrel.1977.Directors.Cut.mkv")  # second video

    def test_report_and_read_only(self):
        self.build_anomalies()
        before = self.nas.snapshot()
        code, out = self.run_("library")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.nas.snapshot(), before)
        cistern = next(line for line in out.splitlines() if line.strip().startswith("Cistern (2022)"))
        self.assertIn("S1:10", cistern)
        self.assertIn("S3:1", cistern)
        self.assertIn("[note] Series/Cistern (2022): 2 sample/trailer file(s) ignored", out)
        self.assertNotIn("extrafanart", out)
        self.assertIn("[warn] Series/Gleam (2021): several folders for season 2", out)
        self.assertIn("[warn] Series/Partition (2021)/Season 1: 'Partition.S02E01.1080p.mkv' is S02E01", out)
        self.assertIn("[warn] Series/Partition (2021)/Season 2: 1 video(s) with no episode number", out)
        self.assertIn("[note] Series/Scaffold (2020): 1 episode file(s) directly in the show folder", out)
        self.assertIn("[note] Series/Scaffold (2020): folder(s) not treated as seasons", out)
        self.assertIn("[warn] Series: 1 video file(s) outside any show folder", out)
        self.assertIn("[note] Movies/Pale Hours Like Ours (2023): no movie file", out)
        self.assertIn("[note] Movies/Kestrel (1977): 2 video files", out)
        self.assertIn("SERIES ", out)
        self.assertIn("MOVIES ", out)
        self.assertRegex(out, r"library: warnings 4, notes \d+, sample/trailer files ignored 3")

    def test_unreadable_folder_is_a_finding_not_an_abort(self):
        from mediaorg.library import Library
        self.build_anomalies()
        blocked = os.path.join(self.nas.series, "Gleam (2021)", "Season 2")
        real = Library.listing

        def listing(lib, path):
            if path == blocked:
                raise PermissionError(13, "Permission denied", path)
            return real(lib, path)

        with mock.patch.object(Library, "listing", listing):
            code, out = self.run_("library")
        self.assertEqual(code, 0, out)
        self.assertIn("[warn] Series/Gleam (2021)/Season 2: cannot be read (Permission denied)", out)
        self.assertIn("Cistern (2022)", out)  # the rest is still reported

    def test_state_untouched(self):
        self.build_anomalies()
        code, _ = self.run_("library")
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.nas.state))  # no lock, no report, no journal

    def test_refuses_when_nas_not_mounted(self):
        with cli_env(self.nas) as path, \
                mock.patch("mediaorg.guards.read_mountinfo", return_value=self.nas.mount_entries()[:1]):
            code, _ = run_cli("-c", path, "library")
        self.assertEqual(code, 3)


class TestBrokenInstall(Base):
    def test_missing_guessit_fails_loudly_and_moves_nothing(self):
        from mediaorg import identify
        identify._guess_cached.cache_clear()
        self.dl("Cistern.S03E06.1080p.WEB.h264-GRP.mkv")
        before = self.nas.snapshot()
        with mock.patch.object(identify, "_guess_cached", side_effect=ImportError("No module named 'guessit'")):
            code, _ = self.run_("run", "--apply")
        self.assertEqual(code, 2)
        self.assertEqual(self.nas.snapshot(), before)
        self.assertFalse(os.listdir(os.path.join(self.nas.state, "journal"))
                         if os.path.isdir(os.path.join(self.nas.state, "journal")) else [])
        self.assertFalse(os.path.isdir(os.path.join(self.nas.state, "runs")))  # no report written
        from mediaorg.guards import RunLock
        with RunLock(os.path.join(self.nas.state, "lock")):  # lock was released
            pass


class TestInvalidNames(Base):
    def test_non_utf8_names_are_left_untouched_and_the_run_continues(self):
        bad = os.fsencode(self.nas.dl) + b"/Bad\xff.Show.S01E01.1080p.WEB.mkv"
        with open(bad, "wb") as fh:
            fh.write(b"x")
        os.utime(bad, (1, 1))
        d = os.path.join(self.nas.dl, "Good.Show.S01E01.1080p.WEB")
        os.makedirs(d)
        inner = os.fsencode(d) + b"/Good.Show.S01E01.1080p.WEB\xe9.mkv"
        with open(inner, "wb") as fh:
            fh.write(b"x")
        os.utime(inner, (1, 1))
        os.utime(d, (1, 1))
        self.dl("Cistern.S03E06.1080p.WEB.h264-GRP.mkv")
        code, out = self.run_("run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertTrue(self.nas.exists(CISTERN, "Season 3", "Cistern.S03E06.1080p.WEB.h264-GRP.mkv"))
        self.assertTrue(os.path.lexists(bad) and os.path.lexists(inner))
        self.assertIn("name is not valid UTF-8 (left untouched)", out)
        self.assertIn("[SKIP] Good.Show.S01E01.1080p.WEB  -- item left untouched: name is not valid UTF-8", out)


class TestTmmCallsMatchOldScript(Base):
    def setUp(self):
        super().setUp()
        self.tmm = FakeTmm(system=False)  # tMM 5.3.4: no /api/system yet
        key = os.path.join(self.nas.tmp, "tmm.key")
        with open(key, "w") as fh:
            fh.write(FakeTmm.API_KEY)
        os.chmod(key, 0o600)
        self.cfg = {"enabled": True, "base_url": self.tmm.url, "api_key_file": key}

    def tearDown(self):
        self.tmm.close()
        super().tearDown()

    def test_default_after_move_is_two_calls_tvshows_then_movies(self):
        self.dl("Cistern.S03E06.1080p.WEB.h264-GRP.mkv")
        self.dl("Meander.2011.1080p.BluRay.x264.mkv")
        code, out = self.run_("run", "--apply", tmm=self.cfg)  # no path_map needed
        self.assertEqual(code, 0, out)
        self.assertTrue(self.nas.exists(CISTERN, "Season 3", "Cistern.S03E06.1080p.WEB.h264-GRP.mkv"))
        # update the library, scrape and rename only what that update found
        calls = [{"action": "update", "scope": {"name": "all"}}, {"action": "scrape", "scope": {"name": "new"}},
                 {"action": "rename", "scope": {"name": "new"}}]
        self.assertEqual(self.tmm.posts, [("/api/tvshow", calls), ("/api/movie", calls)])
        with open(os.path.join(self.nas.state, "tmm_pending.json")) as fh:
            self.assertEqual(json.load(fh), {})

    def test_targeted_template_is_opt_in(self):
        cfg = dict(self.cfg, path_map=[[self.nas.series, "/media/series"]],
                   after_move_tvshow=[{"action": "update", "scope": {"name": "show", "args": "{paths}"}},
                                      {"action": "scrape", "scope": {"name": "unscraped"}}])
        self.dl("Cistern.S03E06.1080p.WEB.h264-GRP.mkv")
        code, out = self.run_("run", "--apply", tmm=cfg)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.tmm.posts[0][1][0],
                         {"action": "update", "scope": {"name": "show", "args": ["/media/series/Cistern (2022)"]}})


if __name__ == "__main__":
    unittest.main()
