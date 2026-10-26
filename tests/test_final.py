"""The shapes a download comes in (packs with season folders, a show or movie with or without folder,
names with or without year, files without extension), Downloads left clean, and the fixes of an
independent review."""

import errno
import glob
import json
import os
import unittest
from unittest import mock

from mediaorg import completeness as comp
from mediaorg.freebox import FreeboxClient
from mediaorg.library import Library
from mediaorg.model import Readiness
from mediaorg.planner import Planner
from mediaorg.scan import scan_downloads

from .fakes import FakeFreebox, b64
from .helpers import OLD, FakeNas, age_tree, cli_env, run_cli, touch

READY = Readiness(True, "test")
MKV = b"\x1a\x45\xdf\xa3"  # Matroska magic
MIB = 1 << 20
REMOTE = "/Freebox/Downloads/"


class Base(unittest.TestCase):
    def setUp(self):
        self.nas = FakeNas()

    def tearDown(self):
        self.nas.cleanup()

    def dl(self, rel, data=b"video", mtime=OLD):
        return touch(os.path.join(self.nas.dl, rel), data, mtime)

    def lib(self, rel, data=b"library", mtime=OLD):
        return touch(os.path.join(self.nas.mp, rel), data, mtime)

    def big(self, rel, size, head=b"", root=None):
        """A sparse file of `size` bytes starting with `head` (no disk space used)."""
        p = touch(os.path.join(root or self.nas.dl, rel), head)
        os.truncate(p, size)
        os.utime(p, (OLD, OLD))
        return p

    def age(self):
        for d in glob.glob(os.path.join(self.nas.dl, "*/")):
            age_tree(d.rstrip("/"))

    def read(self, rel):
        try:
            with open(os.path.join(self.nas.mp, rel), "rb") as fh:
                return fh.read()
        except OSError:
            return None

    def run_(self, *args, **cfg):
        with cli_env(self.nas, **cfg) as path:
            return run_cli("-c", path, *args)

    def plans(self, cache=None, **cfg):
        c = self.nas.config(**cfg)
        items, _ = scan_downloads(c)
        planner = Planner(c, Library(c.paths.movies, c.paths.series, c.media.video_extensions), "R", cache)
        return {i.name: planner.plan_item(i, READY) for i in items}

    def downloads(self):
        return sorted(os.listdir(self.nas.dl))


class TestShowShapes(Base):
    def test_full_show_with_season_folders_and_episode_only_names(self):
        pack = "The.Depot.US.Complete.Series.1080p"
        self.dl(f"{pack}/Season 1/The.Depot.US.S01E01.1080p.mkv", b"e101")
        self.dl(f"{pack}/Season 2/05 - Harvest.mkv", b"e205")
        self.dl(f"{pack}/Season 2/06 - The Fight.mkv", b"e206")
        self.dl(f"{pack}/Season 2/06 - The Fight.en.srt", b"sub206")
        self.dl(f"{pack}/Extras/Bloopers.mkv", b"bonus")
        self.dl(f"{pack}/info.nfo", b"nfo")
        self.age()
        code, out = self.run_("run", "--apply")
        self.assertEqual(code, 0, out)
        show = "Series/The Depot"
        self.assertEqual(self.read(f"{show}/Season 1/The.Depot.US.S01E01.1080p.mkv"), b"e101")
        self.assertEqual(self.read(f"{show}/Season 2/05 - Harvest.mkv"), b"e205")
        self.assertEqual(self.read(f"{show}/Season 2/06 - The Fight.mkv"), b"e206")
        self.assertEqual(self.read(f"{show}/Season 2/06 - The Fight.en.srt"), b"sub206")
        self.assertEqual(self.downloads(), ["_review"])  # the pack left Downloads
        left = glob.glob(os.path.join(self.nas.dl, "_review/leftovers/*", pack))[0]
        found = sorted(os.path.relpath(os.path.join(d, n), left) for d, _, ns in os.walk(left) for n in ns)
        self.assertEqual(found, ["Extras/Bloopers.mkv", "info.nfo"])
        self.assertIn("KEPT until you delete them (videos and anything but release clutter): 'Extras/Bloopers.mkv'",
                      out)

    def test_season_pack_at_the_root_of_its_folder(self):
        pack = "Cistern.S02.1080p.WEB.H264-GRP"
        self.dl(f"{pack}/Cistern.S02E01.1080p.WEB.H264-GRP.mkv", b"e1" * 100)
        self.dl(f"{pack}/Cistern.S02E02.1080p.WEB.H264-GRP.mkv", b"e2" * 100)
        self.dl(f"{pack}/Sample/cistern.s02e01.1080p-sample.mkv", b"s")
        self.dl(f"{pack}/release.nfo", b"nfo")
        self.age()
        code, out = self.run_("run")
        self.assertIn("sample/trailer video(s), deleted with the leftovers: 'Sample/cistern.s02e01.1080p-sample.mkv'", out)
        code, out = self.run_("run", "--apply")
        self.assertEqual(code, 0, out)
        for e in (1, 2):
            self.assertTrue(self.nas.exists(f"Series/Cistern (2022)/Season 2/Cistern.S02E0{e}.1080p.WEB.H264-GRP.mkv"))
        self.assertEqual(self.downloads(), ["_review"])

    def test_pack_subtitles_in_folders_named_after_each_episode(self):
        pack = "Cistern.S02.1080p.WEB.H264-GRP"
        e1, e2 = "Cistern.S02E01.1080p.WEB.H264-GRP", "Cistern.S02E02.1080p.WEB.H264-GRP"
        self.dl(f"{pack}/{e1}.mkv", b"1" * 100)
        self.dl(f"{pack}/{e2}.mkv", b"2" * 100)
        self.dl(f"{pack}/Subs/{e1}/2_English.srt", b"en1")
        self.dl(f"{pack}/Subs/{e2}/2_English.srt", b"en2")
        self.age()
        code, out = self.run_("run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read(f"Series/Cistern (2022)/Season 2/{e1}.2_English.srt"), b"en1")
        self.assertEqual(self.read(f"Series/Cistern (2022)/Season 2/{e2}.2_English.srt"), b"en2")
        self.assertEqual(self.downloads(), [])

    def test_single_episode_without_folder(self):
        self.dl("NightLoom - S01E03 - Reboot.mkv", b"nl3")
        code, out = self.run_("run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read("Series/NightLoom (2025)/Season 1/NightLoom - S01E03 - Reboot.mkv"), b"nl3")
        self.assertEqual(self.downloads(), [])

    def test_episode_named_like_a_sample_is_an_episode(self):
        pack = "Cistern.S02.1080p.WEB.H264-GRP"
        self.dl(f"{pack}/Cistern.S02E01.1080p.WEB.H264-GRP.mkv", b"1" * 1000)
        self.dl(f"{pack}/Cistern.S02E02.Blood.Sample.1080p.WEB.H264-GRP.mkv", b"2" * 1000)
        self.age()
        code, out = self.run_("run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read("Series/Cistern (2022)/Season 2/Cistern.S02E02.Blood.Sample.1080p.WEB.H264-GRP.mkv"),
                         b"2" * 1000)
        self.assertEqual(self.downloads(), [])

    def test_lone_video_named_like_a_sample_stays_in_downloads(self):
        self.dl("Cistern.S02E02.Blood.Sample.1080p.WEB.H264-GRP.mkv", b"2")
        code, out = self.run_("run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertIn("[IGNORED] Cistern.S02E02.Blood.Sample", out)
        self.assertIn("move it by hand if it is the episode or movie", out)
        self.assertTrue(self.nas.exists("Downloads/Cistern.S02E02.Blood.Sample.1080p.WEB.H264-GRP.mkv"))

    def test_video_without_extension_is_recognised_by_its_content(self):
        self.big("NightLoom.S01E04", 60 * MIB, MKV)
        self.dl("NightLoom.S01E04.fr.srt", b"sub")
        self.big("Notes.S01E05", 60 * MIB, b"plain text")  # not a video
        self.dl("Small.S01E06", MKV)                        # too small to be examined
        code, out = self.run_("run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertIn("no video extension in the name: filed as 'NightLoom.S01E04.mkv' (mkv content)", out)
        season = os.path.join(self.nas.series, "NightLoom (2025)", "Season 1")
        self.assertEqual(os.path.getsize(os.path.join(season, "NightLoom.S01E04.mkv")), 60 * MIB)
        self.assertTrue(os.path.exists(os.path.join(season, "NightLoom.S01E04.fr.srt")))
        self.assertEqual(self.downloads(), ["Notes.S01E05", "Small.S01E06"])


class TestMovieShapes(Base):
    def test_movies_with_or_without_year_or_folder(self):
        self.dl("The Ballast 2025 1080p WEB-DL HEVC x265 5.1 LICHEN.mkv", b"ballast")  # loose, with year
        self.dl("Tamarind (2025)/tamarind-2048-surround.mp4", b"tamarind")                # folder with year
        self.dl("Some.Random.Movie.1080p.WEB.mkv", b"random")                       # loose, no year
        self.dl("Some.Random.Video.mkv", b"clip")                                    # no year, no release info
        self.dl("Hollow Reeds/Hollow.Reeds.1080p.WEB.mkv", b"reeds")              # folder, no year
        self.dl("Pale.Hours.Like.Ours.1080p.WEB.mkv", b"phlo")                    # no year, folder exists
        self.age()
        code, out = self.run_("run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read("Movies/The Ballast (2025)/The Ballast 2025 1080p WEB-DL HEVC x265 5.1 LICHEN.mkv"),
                         b"ballast")
        self.assertEqual(self.read("Movies/Tamarind (2025)/tamarind-2048-surround.mp4"), b"tamarind")
        self.assertEqual(self.read("Movies/Some Random Movie/Some.Random.Movie.1080p.WEB.mkv"), b"random")
        self.assertEqual(self.read("Movies/Hollow Reeds/Hollow.Reeds.1080p.WEB.mkv"), b"reeds")
        self.assertEqual(self.read("Movies/Pale Hours Like Ours (2023)/Pale.Hours.Like.Ours.1080p.WEB.mkv"),
                         b"phlo")
        self.assertIn("no year in the name: tMM adds it to the folder name", out)
        self.assertIn("no year and no release information (resolution, source, codec)", out)
        self.assertEqual(self.downloads(), ["Some.Random.Video.mkv"])

    def test_no_year_and_the_library_already_has_a_film_with_that_title(self):
        # Movies/Larkspur (2025) holds Larkspur.(2025).mkv (b"x") in the fixture
        self.dl("Larkspur.1080p.WEB.mkv", b"another film?")
        self.dl("Larkspur.720p.WEB.mkv", b"x")  # the very same bytes: provably the same film
        plans = self.plans()
        d = plans["Larkspur.1080p.WEB.mkv"].decisions[0]
        self.assertEqual(d.action, "skip")  # not a duplicate: never deleted later by the review cleaning
        self.assertIn("no year to tell whether it is the same film", d.reason)
        self.assertEqual(plans["Larkspur.720p.WEB.mkv"].decisions[0].action, "delete")

    def test_no_year_and_several_folders_with_the_title_is_not_guessed(self):
        os.makedirs(os.path.join(self.nas.movies, "Saltmere (1982)"))
        self.dl("Saltmere.1080p.WEB.mkv")
        code, out = self.run_("run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertIn("no year in the name to choose between folders", out)
        self.assertTrue(self.nas.exists("Downloads/Saltmere.1080p.WEB.mkv"))

    def test_collection_folder(self):
        self.dl("Two.Films/Thornbury.2015.1080p.BluRay.mkv", b"thornbury")
        self.dl("Two.Films/Ravelin.2014.1080p.BluRay.mkv", b"ravelin")
        self.dl("Thornbury.Pack/Thornbury.2015.1080p.BluRay.mkv", b"a1")
        self.dl("Thornbury.Pack/Thornbury.2015.2160p.BluRay.mkv", b"a2")
        self.age()
        code, out = self.run_("run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read("Movies/Thornbury (2015)/Thornbury.2015.1080p.BluRay.mkv"), b"thornbury")
        self.assertEqual(self.read("Movies/Ravelin (2014)/Ravelin.2014.1080p.BluRay.mkv"), b"ravelin")
        self.assertIn("several files of the same movie in one folder", out)
        self.assertEqual(sorted(os.listdir(os.path.join(self.nas.dl, "Thornbury.Pack"))),
                         ["Thornbury.2015.1080p.BluRay.mkv", "Thornbury.2015.2160p.BluRay.mkv"])

    def test_folder_that_is_mostly_something_else_is_not_a_movie(self):
        self.dl("Some.App.2024/intro.mp4", b"clip")
        self.big("Some.App.2024/setup.bin", 100 * MIB)
        self.age()
        code, out = self.run_("run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertIn("the folder holds more other data", out)
        self.assertTrue(self.nas.exists("Downloads/Some.App.2024/intro.mp4"))

    def test_extracted_release_keeps_its_archives_beside_the_video(self):
        item = "Film.2019.1080p.BluRay-GRP"
        self.big(f"{item}/film.2019.1080p.bluray-grp.mkv", 70 * MIB)
        self.big(f"{item}/film.2019.1080p.bluray-grp.rar", 50 * MIB)  # archives do not outweigh the video
        self.big(f"{item}/film.2019.1080p.bluray-grp.r00", 50 * MIB)
        self.age()
        plan = self.plans()[item]
        self.assertEqual((plan.status, plan.decisions[0].action), ("ready", "move"))
        self.assertIn("KEPT until you delete them", plan.reason)  # archives are never deleted automatically
        self.assertIn("film.2019.1080p.bluray-grp.rar", plan.reason)


class TestReviewFindings(Base):
    def test_second_copy_after_a_same_name_placeholder_is_a_duplicate(self):
        for n in ("Corvid.(2019).mkv", "movie.nfo"):
            os.remove(os.path.join(self.nas.movies, "Corvid (2019)", n))
        self.lib("Movies/Corvid (2019)/Corvid.2019.1080p.WEB.H264-GRP.mkv", b"")
        self.dl("Corvid.2019.1080p.WEB.H264-GRP.mkv", b"hd")
        self.dl("Corvid.2019.2160p.WEB.H264-GRP.mkv", b"uhd")
        plans = self.plans()
        acts = sorted((p.decisions[0].action, n) for n, p in plans.items())
        self.assertEqual(acts, [("duplicate", "Corvid.2019.2160p.WEB.H264-GRP.mkv"),
                                ("move", "Corvid.2019.1080p.WEB.H264-GRP.mkv")])

    def test_duplicate_takes_its_subtitle_along(self):
        item = "Cistern.S03E01.720p.WEB.H264-GRP"
        self.dl(f"{item}/Cistern.S03E01.720p.WEB.H264-GRP.mkv", b"720")
        self.dl(f"{item}/Cistern.S03E01.720p.WEB.H264-GRP.en.srt", b"sub")
        self.dl(f"{item}/info.nfo", b"nfo")
        self.age()
        code, out = self.run_("run", "--apply")
        self.assertEqual(code, 0, out)
        dup = glob.glob(os.path.join(self.nas.dl, "_review/duplicates/*", item))[0]
        self.assertEqual(sorted(os.listdir(dup)), ["Cistern.S03E01.720p.WEB.H264-GRP.en.srt",
                                                   "Cistern.S03E01.720p.WEB.H264-GRP.mkv"])
        left = glob.glob(os.path.join(self.nas.dl, "_review/leftovers/*", item))[0]
        self.assertEqual(os.listdir(left), ["info.nfo"])
        self.assertIn("(subtitle of the duplicate)", out)

    def test_multi_episode_files(self):
        self.lib("Series/Cistern (2022)/Season 3/Cistern.S03E02E03.1080p.mkv", b"same")
        self.dl("Cistern.S03E02E03.1080p.WEB.H264-GRP.mkv", b"same")             # identical -> delete
        self.dl("Cistern.S03E01E04.1080p.WEB.H264-GRP.mkv", b"e1e4")             # library has E01 only
        self.lib("Series/Brightwater (2025)/Season 1/Brightwater.S01E05.mkv", b"")
        self.lib("Series/Brightwater (2025)/Season 1/Brightwater.S01E06.mkv", b"")
        self.dl("Brightwater.S01E05E06.1080p.WEB.H264-GRP.mkv", b"e5e6")          # replaces both placeholders
        plans = self.plans()
        self.assertEqual(plans["Cistern.S03E02E03.1080p.WEB.H264-GRP.mkv"].decisions[0].action, "delete")
        d = plans["Cistern.S03E01E04.1080p.WEB.H264-GRP.mkv"].decisions[0]
        self.assertEqual(d.action, "skip")
        self.assertIn("has S03E01 but not S03E04", d.reason)
        lan = plans["Brightwater.S01E05E06.1080p.WEB.H264-GRP.mkv"]
        self.assertEqual(lan.decisions[0].action, "move")
        self.assertEqual(sorted(os.path.basename(o.dst) for o in lan.ops if o.role == "empty-placeholder"),
                         ["Brightwater.S01E05.mkv", "Brightwater.S01E06.mkv"])

    def test_identical_comparison_is_cached_between_runs(self):
        self.lib("Series/Brightwater (2025)/Season 1/Brightwater.S01E01.1080p.mkv", b"same")
        self.dl("Brightwater.S01E01.1080p.WEB.H264-GRP.mkv", b"same")
        cache = {}
        with mock.patch("mediaorg.planner.compare_files", return_value=True) as cmp:
            self.plans(cache)
            self.plans(cache)
        self.assertEqual(cmp.call_count, 1)
        with mock.patch("mediaorg.planner.compare_files", return_value=None) as cmp:  # unreadable: not cached
            plans = self.plans({})
            self.plans({})
        self.assertEqual(cmp.call_count, 2)
        self.assertEqual(plans["Brightwater.S01E01.1080p.WEB.H264-GRP.mkv"].decisions[0].action, "duplicate")

    def test_cli_keeps_the_comparison_cache(self):
        self.lib("Series/Brightwater (2025)/Season 1/Brightwater.S01E01.1080p.mkv", b"same")
        self.dl("Brightwater.S01E01.1080p.WEB.H264-GRP.mkv", b"same")
        self.run_("run")
        with open(os.path.join(self.nas.state, "identical_cache.json"), encoding="utf-8") as fh:
            self.assertEqual(list(json.load(fh).values()), [True])

    def test_nas_lost_during_a_deletion(self):
        torch = "Series/Brightwater (2025)/Season 1/Brightwater.S01E01.1080p.mkv"
        self.lib(torch, b"same")
        src = self.dl("Brightwater.S01E01.1080p.WEB.H264-GRP.mkv", b"same")
        real_unlink = os.unlink

        def unlink(path, *a, **k):
            if path == src:
                raise OSError(errno.EHOSTDOWN, "Host is down")
            return real_unlink(path, *a, **k)

        with mock.patch("mediaorg.transfer.os.unlink", side_effect=unlink):
            code, out = self.run_("run", "--apply")
        self.assertEqual(code, 3)
        self.assertTrue(os.path.exists(src))
        code, out = self.run_("run", "--apply")  # reconciles ("not done"), then deletes it
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(src))
        self.assertEqual(self.read(torch), b"same")


class TestFreeboxMapping(unittest.TestCase):
    def setUp(self):
        self.nas = FakeNas()
        self.fb = FakeFreebox()
        self.cfg = self.nas.config(completeness={"method": "freebox"},
                                   freebox={"remote_downloads_dir": REMOTE, "base_url": self.fb.url,
                                            "allow_insecure_http": True})

    def tearDown(self):
        self.fb.close()
        self.nas.cleanup()

    def item(self, rel):
        touch(os.path.join(self.nas.dl, rel))
        name = rel.split("/")[0]
        items, _ = scan_downloads(self.cfg)
        return next(i for i in items if i.name == name)

    def state(self, cache=None):
        client = FreeboxClient(self.fb.url, None)
        client.login("mediaorg", FakeFreebox.APP_TOKEN)
        return comp.FreeboxState(self.cfg, client, cache)

    @staticmethod
    def task(tid, name, status="done", rx=10000):
        return {"id": tid, "name": name, "status": status, "rx_pct": rx, "size": 10, "type": "bt",
                "download_dir": b64(REMOTE), "created_ts": 1700000000 + tid}

    def file(self, rel):
        return {"filepath": b64(REMOTE + rel), "size": 10, "rx": 10, "status": "done", "priority": "normal"}

    def test_a_finished_task_does_not_claim_a_folder_by_its_stem(self):
        folder = self.item("Movie.2020.1080p.WEB.H264-GRP/Movie.2020.1080p.WEB.H264-GRP.mkv")
        self.fb.tasks.append(self.task(1, "Movie.2020.1080p.WEB"))
        self.fb.files[1] = [self.file("Movie.2020.1080p.WEB.mkv")]
        r = self.state().assess(folder)
        self.assertFalse(r.ready)
        self.assertIn("not a Freebox download task", r.reason)  # untracked = wait: not moved

    def test_name_only_mapping_is_limited_to_tasks_whose_files_map_to_no_present_entry(self):
        loose = self.item("Some.Show.S02E01.1080p.WEB.mkv")
        self.item("Other.Entry.mkv")
        self.fb.tasks.append(self.task(2, "Some.Show.S02E01.1080p.WEB", "seeding"))
        self.fb.files[2] = [self.file("Other.Entry.mkv")]  # its files are in another entry: not this one
        self.assertFalse(self.state()._tasks_for(loose.name, loose=True))
        self.assertEqual([t.id for t in self.state()._tasks_for("Other.Entry.mkv", loose=True)], [2])
        self.fb.files[2] = []  # files not known yet: the name may stand for this file
        self.assertEqual([t.id for t in self.state()._tasks_for(loose.name, loose=True)], [2])
        self.fb.files[2] = [self.file("Gone.Entry.mkv")]  # its files are no longer in Downloads: same
        self.assertEqual([t.id for t in self.state()._tasks_for(loose.name, loose=True)], [2])
        self.assertFalse(self.state()._tasks_for(loose.name))  # folders and exact names: never by stem

    def test_task_removed_between_the_two_calls(self):
        self.fb.tasks.append(self.task(3, "Gone.S01E01.mkv"))

        class Gone(FreeboxClient):  # the task disappears after the task list was read
            def _request(self, method, url, body=None, auth=True):
                if url.endswith("/files"):
                    return {"success": False, "error_code": "task_not_found", "msg": "gone"}
                return super()._request(method, url, body, auth)

        client = Gone(self.fb.url, None)
        client.login("mediaorg", FakeFreebox.APP_TOKEN)
        self.assertEqual(client.task_files(3), [])
        st = comp.FreeboxState(self.cfg, client)  # 1.0.1 first pass: FreeboxError, run exit 3
        self.assertEqual([t.id for t in st.by_entry["gone.s01e01.mkv"]], [3])

    def test_file_lists_of_finished_tasks_are_cached(self):
        self.fb.tasks += [self.task(4, "Done.S01E01.mkv"), self.task(5, "Active.S01E02.mkv", "downloading", 5000)]
        self.fb.files[4] = [self.file("Done.S01E01.mkv")]
        self.fb.files[5] = [self.file("Active.S01E02.mkv")]
        path = os.path.join(self.nas.tmp, "freebox_files.json")
        cache = comp.FilesCache(path)
        self.state(cache)
        cache.save()
        self.fb.requests.clear()
        cache = comp.FilesCache(path)
        st = self.state(cache)
        files_calls = [r[1] for r in self.fb.requests if r[1].endswith("/files")]
        self.assertEqual(files_calls, ["/api/v15/downloads/5/files"])  # only the task still changing
        self.assertEqual([t.id for t in st.by_entry["done.s01e01.mkv"]], [4])
        self.fb.tasks[0]["status"] = "seeding"  # any change of the task: asked again
        self.fb.requests.clear()
        self.state(comp.FilesCache(path))
        self.assertIn("/api/v15/downloads/4/files", [r[1] for r in self.fb.requests])


if __name__ == "__main__":
    unittest.main()


class TestSecondReview(Base):
    """Findings of a second independent review."""

    def test_two_files_of_one_download_claiming_one_episode_are_both_skipped(self):
        packs = {
            "Brightwater.S01E05.Parts": ["Brightwater.S01E05.Part1.1080p.mkv", "Brightwater.S01E05.Part2.1080p.mkv"],
            "Brightwater.S01E06.CDs": ["Brightwater.S01E06.CD1.avi", "Brightwater.S01E06.CD2.avi"],
            "Cistern.S02E05.Pack": ["Cistern.S02E05.Inside.The.Episode.1080p.mkv", "Cistern.S02E05.The.Janitors.Boy.1080p.mkv"],
        }
        for pack, files in packs.items():
            for i, name in enumerate(files):
                self.dl(f"{pack}/{name}", bytes([65 + i]) * 1000)
        self.age()
        plans = self.plans()
        for pack, files in packs.items():
            acts = {d.file.name: d.action for d in plans[pack].decisions}
            self.assertEqual(set(acts.values()), {"skip"}, (pack, acts))
        reasons = " ".join(d.reason for p in plans.values() for d in p.decisions)
        self.assertIn("several files of this download are", reasons)
        self.assertIn("CD1/CD2", reasons)

    def test_extras_are_not_episodes(self):
        pack = "Cistern.S01.1080p.BluRay.x264-GRP"
        self.dl(f"{pack}/Cistern.S01E01.1080p.BluRay.x264-GRP.mkv", b"real" * 300)
        self.dl(f"{pack}/Cistern.S01.Extras.01.1080p.BluRay.x264-GRP.mkv", b"extra" * 300)
        self.dl(f"{pack}/Cistern.S01.Bonus.02.1080p.BluRay.x264-GRP.mkv", b"bonus" * 300)
        pack2 = "Cistern.S02.1080p.WEB.H264-GRP"
        self.dl(f"{pack2}/Cistern.S02E01.1080p.WEB.H264-GRP.mkv", b"real2" * 300)
        self.dl(f"{pack2}/Deleted Scene 05.mkv", b"deleted" * 300)
        self.dl(f"{pack2}/Recap 02.mkv", b"recap" * 300)
        self.dl("Cistern.S01.Extras.03.1080p.BluRay.x264-GRP.mkv", b"loose extra")
        self.age()
        plans = self.plans()
        # tagged bonus material is never an episode: it goes with the leftovers, which keep every video
        plan = plans[pack]
        self.assertEqual([(d.file.name, d.action) for d in plan.decisions],
                         [("Cistern.S01E01.1080p.BluRay.x264-GRP.mkv", "move")])
        self.assertEqual(plan.status, "ready")
        self.assertIn("KEPT until you delete them", plan.reason)
        self.assertIn("Cistern.S01.Extras.01.1080p.BluRay.x264-GRP.mkv", plan.reason)
        # untagged bonus names in a season pack: not identified, so the pack stays in Downloads
        acts = {d.file.name: (d.action, d.reason) for d in plans[pack2].decisions}
        self.assertEqual(acts["Cistern.S02E01.1080p.WEB.H264-GRP.mkv"][0], "move")
        self.assertEqual((acts["Deleted Scene 05.mkv"][0], acts["Recap 02.mkv"][0]), ("skip", "skip"))
        self.assertEqual(plans[pack2].status, "partial")
        # a loose bonus file alone: IGNORED, left where it is
        self.assertEqual(plans["Cistern.S01.Extras.03.1080p.BluRay.x264-GRP.mkv"].status, "ignored")
        from mediaorg.identify import identify_video
        self.assertIsNone(identify_video("Cistern.S01.Extras.01.1080p.mkv", None, True).media)
        from mediaorg.library import episodes_in_name
        self.assertEqual(episodes_in_name("Cistern.S01.Extras.01.1080p.mkv", 1), set())

    def test_season_folder_pack_named_complete(self):  # 'The Depot Complete' is the show 'The Depot'
        pack = "The Depot Complete"
        self.dl(f"{pack}/Season 2/05 - Harvest.mkv", b"e5")
        self.dl(f"{pack}/Season 2/Recap 05.mkv", b"recap")
        self.age()
        plan = self.plans()[pack]
        acts = {d.file.name: d for d in plan.decisions}
        self.assertEqual(acts["05 - Harvest.mkv"].target,
                         os.path.join(self.nas.series, "The Depot", "Season 2", "05 - Harvest.mkv"))
        self.assertEqual(acts["Recap 05.mkv"].action, "skip")

    def test_folder_year_and_file_year_disagree(self):
        self.dl("Kestrel (1977)/Kestrel.Basin.2023.1080p.WEB.H264-GRP.mkv", b"basin")
        self.age()
        d = self.plans()["Kestrel (1977)"].decisions[0]
        self.assertEqual(d.action, "skip")
        self.assertIn("folder says 'Kestrel (1977)' but the file says 'Kestrel Basin (2023)'", d.reason)

    def test_clip_in_a_software_folder_is_not_a_movie(self):
        self.dl("SomeTool.v3.2.1-GRP/setup.exe", b"MZ" * 1000)
        self.dl("SomeTool.v3.2.1-GRP/tutorial.mp4", b"clip" * 100)
        self.dl("Holiday/holiday.mp4", b"clip" * 100)
        for i in range(3):
            self.dl(f"Holiday/IMG_000{i}.jpg", b"photo")
        self.age()
        plans = self.plans()
        self.assertEqual(plans["SomeTool.v3.2.1-GRP"].decisions[0].action, "skip")
        # 'Holiday/holiday.mp4': two names agree, so it is filed as a movie without year, but the photos
        # left in review are never deleted by the cleaning (images over 1 MiB are kept; these are tiny)
        self.assertEqual(plans["Holiday"].decisions[0].action, "move")

    def test_small_video_without_extension_is_still_recognised(self):
        self.big("Cistern.S01.Pack/Cistern.S01E01.1080p.mkv", 60 * MIB, MKV)
        self.big("Cistern.S01.Pack/Cistern.S01E02.1080p", 45 * MIB, MKV)   # no extension, 45 MiB
        self.big("Movie.2019.1080p.BluRay.x264.AC3", 60 * MIB, MKV)  # ends in a known token
        self.age()
        plans = self.plans()
        self.assertEqual({d.file.name: d.action for d in plans["Cistern.S01.Pack"].decisions},
                         {"Cistern.S01E01.1080p.mkv": "move", "Cistern.S01E02.1080p": "move"})
        d = plans["Movie.2019.1080p.BluRay.x264.AC3"].decisions[0]
        self.assertEqual((d.action, os.path.basename(d.target)), ("move", "Movie.2019.1080p.BluRay.x264.AC3.mkv"))

    def test_undated_movie_folder_receives_the_dated_release(self):
        os.makedirs(os.path.join(self.nas.movies, "Hollow Reeds"))
        self.dl("Hollow.Reeds.2022.1080p.WEB.H264-GRP.mkv", b"reeds")
        d = self.plans()["Hollow.Reeds.2022.1080p.WEB.H264-GRP.mkv"].decisions[0]
        self.assertEqual(d.target, os.path.join(self.nas.movies, "Hollow Reeds", "Hollow.Reeds.2022.1080p.WEB.H264-GRP.mkv"))

    def test_entry_vanishing_during_the_scan(self):
        from mediaorg import scan
        self.dl("Cistern.S03E06.1080p.WEB.h264-GRP.mkv")
        real = scan.os.scandir

        class Gone:
            name, path = "Gone.S01E01.mkv", os.path.join(self.nas.dl, "Gone.S01E01.mkv")

            def stat(self, follow_symlinks=True):
                raise FileNotFoundError(2, "No such file or directory")

        def scandir(path):
            entries = list(real(path))
            return entries + [Gone()] if path == self.nas.dl else entries

        with mock.patch.object(scan.os, "scandir", side_effect=scandir):
            items, _ = scan_downloads(self.nas.config())
        self.assertEqual([i.name for i in items], ["Cistern.S03E06.1080p.WEB.h264-GRP.mkv"])

    def test_crash_between_reservation_and_its_journal_record(self):
        from mediaorg.transfer import read_journal, reconcile
        src = self.dl("Cistern.S03E06.1080p.WEB.h264-GRP.mkv", b"video")
        dst = self.lib("Series/Cistern (2022)/Season 3/Cistern.S03E06.1080p.WEB.h264-GRP.mkv", b"")  # our placeholder
        jdir = os.path.join(self.nas.state, "journal")
        os.makedirs(jdir)
        with open(os.path.join(jdir, "r.jsonl"), "w", encoding="utf-8") as fh:
            for r in ({"state": "begin"}, {"seq": 1, "state": "intent", "op": "rename", "src": src, "dst": dst,
                                           "role": "media", "size": 5}):
                fh.write(json.dumps(r) + "\n")
        resolved, problems = reconcile(jdir)
        self.assertEqual(problems, [])
        self.assertFalse(os.path.exists(dst))
        self.assertTrue(os.path.exists(src))
        self.assertEqual(read_journal(os.path.join(jdir, "r.jsonl"))[1][1]["state"], "not_done")

    def test_tmm_busy_is_said_in_the_report(self):
        from .fakes import FakeTmm
        tmm = FakeTmm(busy=True)
        key = os.path.join(self.nas.tmp, "tmm.key")
        with open(key, "w") as fh:
            fh.write(FakeTmm.API_KEY)
        os.chmod(key, 0o600)
        try:
            self.dl("Cistern.S03E06.1080p.WEB.h264-GRP.mkv")
            code, out = self.run_("run", "--apply", "--quiet",
                                  tmm={"enabled": True, "base_url": tmm.url, "api_key_file": key})
        finally:
            tmm.close()
        self.assertEqual(code, 0, out)
        self.assertIn("[NOTE] tMM is busy", out)
        self.assertTrue(self.nas.exists("Downloads/Cistern.S03E06.1080p.WEB.h264-GRP.mkv"))

    def test_quiet_state_that_cannot_be_saved_is_a_warning(self):
        self.dl("Cistern.S03E06.1080p.WEB.h264-GRP.mkv")
        with mock.patch("mediaorg.report.QuietState.save", side_effect=OSError(28, "No space left on device")):
            code, out = self.run_("run", "--apply", "--quiet")
        self.assertEqual(code, 0, out)
        self.assertTrue(self.nas.exists("Series/Cistern (2022)/Season 3/Cistern.S03E06.1080p.WEB.h264-GRP.mkv"))

    def test_comparisons_are_saved_as_they_are_made(self):
        self.lib("Series/Brightwater (2025)/Season 1/Brightwater.S01E01.1080p.mkv", b"same")
        self.dl("Brightwater.S01E01.1080p.WEB.H264-GRP.mkv", b"same")
        c = self.nas.config()
        items, _ = scan_downloads(c)
        saved = []
        planner = Planner(c, Library(c.paths.movies, c.paths.series, c.media.video_extensions), "R", {},
                          on_compare=lambda cache: saved.append(dict(cache)))
        planner.plan_item(items[0], READY)
        self.assertEqual([list(x.values()) for x in saved], [[True]])


class TestEmptyFolders(Base):  # an entry with no file at all does not stay forever
    def test_empty_folder_tree_is_removed_once_quiet(self):
        os.makedirs(os.path.join(self.nas.dl, "Old.Show.S01.1080p", "Subs", "en"))
        os.makedirs(os.path.join(self.nas.dl, "Fresh.Folder"))
        self.age()
        os.utime(os.path.join(self.nas.dl, "Fresh.Folder"))  # just created: may still be filled
        code, out = self.run_("run")
        self.assertEqual(code, 0, out)
        self.assertIn("empty folder (no file in it): removed", out)
        self.assertIn("modified 0 min ago", out)
        code, out = self.run_("run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.downloads(), ["Fresh.Folder"])

    def test_a_folder_that_gets_a_file_after_the_scan_is_left_alone(self):
        from mediaorg import transfer
        os.makedirs(os.path.join(self.nas.dl, "Late", "sub"))
        self.age()
        real = transfer.tree_fingerprint

        def late(path):
            touch(os.path.join(self.nas.dl, "Late", "sub", "new.mkv"), b"x")
            return real(path)

        with mock.patch.object(transfer, "tree_fingerprint", side_effect=late):
            code, out = self.run_("run", "--apply")
        self.assertEqual(code, 1, out)  # EXIT_OP_FAILED: the item was not touched
        self.assertTrue(self.nas.exists("Downloads/Late/sub/new.mkv"))


class TestFreeboxApiErrors(TestFreeboxMapping):
    def test_a_refused_file_list_maps_the_task_by_its_name(self):
        self.fb.tasks.append(self.task(7, "Odd.S01E01.mkv", "error", 5000))

        class Refusing(FreeboxClient):
            def _request(self, method, url, body=None, auth=True):
                if url.endswith("/files"):
                    return {"success": False, "error_code": "internal_error", "msg": "?"}
                return super()._request(method, url, body, auth)

        client = Refusing(self.fb.url, None)
        client.login("mediaorg", FakeFreebox.APP_TOKEN)
        with self.assertLogs("mediaorg.completeness", level="WARNING"):
            st = comp.FreeboxState(self.cfg, client)
        self.assertEqual([t.id for t in st.by_entry["odd.s01e01.mkv"]], [7])
