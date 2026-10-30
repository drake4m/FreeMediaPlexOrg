"""2.3.0: review.replace_duplicates: a download replaces the library video of the same episode or film
(the newest download wins); only the video is replaced, and the old one is kept in review/replaced for
review.replaced_retention_days."""

import glob
import json
import os
import time
import unittest

from mediaorg import transfer
from mediaorg.model import Op

from .helpers import OLD, FakeNas, age_tree, cli_env, run_cli, touch
from .test_v101 import Base, rid

REPLACE = {"replace_duplicates": True}
SEASON1 = "Series/Brightwater (2025)/Season 1"
EP2 = f"{SEASON1}/Brightwater.S01E02.1080p.mkv"  # library file of the fake NAS (1 byte, b"x")
TAMSIN = "Movies/Tamsin (2023)/Tamsin.(2023).mkv"  # likewise


class TestReplaceDuplicates(Base):
    def files(self, rel):
        return sorted(os.listdir(os.path.join(self.nas.mp, rel)))

    def replaced(self):
        return sorted(os.path.relpath(p, self.nas.dl) for p in self.review("replaced") if os.path.isfile(p))

    def roles(self, plan):
        return [(o.kind, o.role) for o in plan.ops if o.kind in ("rename", "unlink")]

    def test_a_download_replaces_the_episode_video_only(self):
        self.lib(f"{SEASON1}/Brightwater.S01E02.1080p.nfo", b"nfo")
        self.lib(f"{SEASON1}/Brightwater.S01E02.1080p.en.srt", b"sub")
        name = "Brightwater.S01E02.REPACK.2160p.WEB.x265-GRP.mkv"
        self.dl(name, b"yyyy")  # bigger, and a REPACK: replaces anyway
        before = self.nas.snapshot()
        code, out = self.run_("run", review=REPLACE)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.nas.snapshot(), before)  # a dry-run changes nothing
        self.assertIn(f"    replace   {name} -> {SEASON1}/{name}", out)
        self.assertIn("it replaces the library video (review.replace_duplicates)", out)
        self.assertIn("moved to review: deleted from review 1 day(s) after this run", out)
        self.assertIn(f"    move      {EP2} -> Downloads/_review/replaced/", out)
        self.assertIn("(library video, replaced by the download)", out)
        self.assertIn("replaced library videos 1", out)
        code, out = self.run_("run", "--apply", review=REPLACE)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read(f"{SEASON1}/{name}"), b"yyyy")
        self.assertIsNone(self.read(EP2))
        self.assertEqual(self.read(f"{SEASON1}/Brightwater.S01E02.1080p.nfo"), b"nfo")  # the rest stays
        self.assertEqual(self.read(f"{SEASON1}/Brightwater.S01E02.1080p.en.srt"), b"sub")
        self.assertFalse(self.nas.exists("Downloads", name))
        [kept] = self.replaced()  # the old video, kept for a day under its library path
        self.assertTrue(kept.endswith(f"/{EP2}"), kept)
        with open(os.path.join(self.nas.dl, kept), "rb") as fh:
            self.assertEqual(fh.read(), b"x")
        report = sorted(glob.glob(os.path.join(self.nas.state, "runs", "*.json")))[-1]
        with open(report, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["summary"]["replaced"], 1)
        self.assertEqual(data["items"][0]["decisions"][0]["action"], "replace")

    def test_the_newest_wins_even_when_smaller(self):
        self.lib(TAMSIN, b"a bigger library copy")
        name = "Tamsin.2023.720p.WEB.H264-GRP.mkv"
        self.dl(name, b"yyyyy")
        code, out = self.run_("run", "--apply", review=REPLACE)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.files("Movies/Tamsin (2023)"), [name, "movie.nfo"])

    def test_same_name_old_file_goes_first(self):
        self.dl("Brightwater.S01E02.1080p.mkv", b"zz")  # the very name of the library file, other bytes
        plans = self.plan(review=REPLACE)
        self.assertEqual(self.roles(plans[0]), [("rename", "replaced"), ("rename", "media")])
        res, _ = self.execute(plans)
        self.assertEqual(res.failed, 0, res.errors)
        self.assertEqual(self.read(EP2), b"zz")
        self.assertEqual(len(self.replaced()), 1)

    def test_the_new_file_goes_in_before_the_old_one_leaves(self):
        self.dl("Brightwater.S01E02.720p.WEB.x264-GRP.mkv", b"y")
        self.assertEqual(self.roles(self.plan(review=REPLACE)[0]), [("rename", "media"), ("rename", "replaced")])

    def test_a_library_copy_changed_meanwhile_is_kept(self):
        name = "Brightwater.S01E02.720p.WEB.x264-GRP.mkv"
        self.dl(name, b"y")
        plans = self.plan(review=REPLACE)
        self.lib(EP2, b"changed", mtime=time.time())  # replaced by hand meanwhile
        res, _ = self.execute(plans)
        self.assertEqual(res.failed, 1)
        self.assertIn("changed since the scan", res.errors[0])
        self.assertEqual(self.read(EP2), b"changed")  # it stays; the new one was moved in first
        self.assertEqual(self.read(f"{SEASON1}/{name}"), b"y")
        self.assertEqual(self.replaced(), [])

    def test_bonus_material_or_a_tiny_file_never_replaces_the_episode(self):
        self.lib(EP2, b"x" * 100)
        recap, tiny = "Brightwater.S01E02.Recap.1080p.WEB.x264-GRP.mkv", "Brightwater.S01E02.720p.WEB.x264-GRP.mkv"
        self.dl(recap, b"y" * 50)
        self.dl(tiny, b"y")
        code, out = self.run_("run", "--apply", review={**REPLACE, "delete_duplicates": True})
        self.assertEqual(code, 0, out)
        self.assertIn("kept in review until you decide: not replaced: its name says 'Recap': bonus material, not the "
                      "episode", out)
        self.assertIn("kept in review until you decide: not replaced: it is 1 B, less than a tenth of the library video "
                      "(100 B): a clip or a sample?", out)
        self.assertEqual(self.read(EP2), b"x" * 100)  # untouched
        dups = sorted(os.path.basename(p) for p in self.review("duplicates") if os.path.isfile(p))
        self.assertEqual(dups, sorted([recap, tiny]))  # kept for you, not deleted by delete_duplicates either
        self.assertEqual(self.replaced(), [])

    def test_cases_that_cannot_be_replaced_are_handled_as_before(self):
        self.lib(f"{SEASON1}/Brightwater.S01E01.2160p.mkv", b"xx")  # two library files for S01E01
        self.lib("Series/Brightwater (2025)/Season 2/Brightwater.S02E01E02.1080p.mkv", b"x")  # one file, two episodes
        self.dl("Brightwater.S01E01.720p.WEB.x264-GRP.mkv", b"y")
        self.dl("Brightwater.S02E01.720p.WEB.x264-GRP.mkv", b"y")
        self.dl("Brightwater.UK.S01E02.720p.WEB.x264-GRP.mkv", b"y")  # another country's show
        notes = {p.item.name: p.decisions[0].notes for p in self.plan(review=REPLACE)}
        self.assertIn("not replaced: the library has 2 files for it", notes["Brightwater.S01E01.720p.WEB.x264-GRP.mkv"])
        self.assertIn("not replaced: the library file also holds S02E02", notes["Brightwater.S02E01.720p.WEB.x264-GRP.mkv"])
        self.assertIn("not replaced: the release names a country (GB) that the folder 'Brightwater (2025)' does "
                      "not", notes["Brightwater.UK.S01E02.720p.WEB.x264-GRP.mkv"])
        before = self.nas.snapshot()
        code, out = self.run_("run", "--apply", review=REPLACE)
        self.assertEqual(code, 0, out)
        after = self.nas.snapshot()
        for rel in (f"{SEASON1}/Brightwater.S01E01.1080p.mkv", f"{SEASON1}/Brightwater.S01E01.2160p.mkv",
                    "Series/Brightwater (2025)/Season 2/Brightwater.S02E01E02.1080p.mkv", EP2):
            self.assertEqual(after[rel], before[rel], rel)  # every library video stays
        self.assertEqual(self.replaced(), [])

    def test_a_second_copy_in_the_same_run_waits_for_the_next(self):
        self.dl("Brightwater.S01E02.720p.WEB.x264-GRP.mkv", b"y")
        self.dl("Brightwater.S01E02.480p.WEB.x264-OTHER.mkv", b"z")
        plans = {p.item.name: p for p in self.plan(review=REPLACE)}
        self.assertEqual(plans["Brightwater.S01E02.480p.WEB.x264-OTHER.mkv"].decisions[0].action, "replace")
        later = plans["Brightwater.S01E02.720p.WEB.x264-GRP.mkv"].decisions[0]
        self.assertEqual(later.action, "skip")
        self.assertIn("is filed from another download in this run", later.reason)

    def test_a_second_copy_of_a_film_in_the_same_run_waits_for_the_next(self):
        self.dl("Tamsin.(2023).mkv", b"zz")  # takes the library file's very name
        self.dl("Tamsin.2023.1080p.WEB.H264-GRP.mkv", b"yy")
        plans = {p.item.name: p for p in self.plan(review=REPLACE)}
        self.assertEqual(plans["Tamsin.(2023).mkv"].decisions[0].action, "replace")
        later = plans["Tamsin.2023.1080p.WEB.H264-GRP.mkv"].decisions[0]
        self.assertEqual(later.action, "skip")
        self.assertIn("receives another download in this run", later.reason)
        res, _ = self.execute(list(plans.values()))
        self.assertEqual(res.failed, 0, res.errors)
        self.assertEqual(self.files("Movies/Tamsin (2023)"), ["Tamsin.(2023).mkv", "movie.nfo"])

    def test_the_downloads_other_files_go_to_leftovers_and_retention_zero_deletes_at_once(self):
        folder = "Tamsin.2023.2160p.WEB.H265-GRP"
        self.dl(f"{folder}/{folder}.mkv", b"yy")
        self.dl(f"{folder}/{folder}.en.srt", b"sub")
        self.dl(f"{folder}/{folder}.nfo", b"nfo")
        age_tree(os.path.join(self.nas.dl, folder))
        review = {**REPLACE, "leftovers_retention_days": 0, "replaced_retention_days": 0}
        code, out = self.run_("run", "--apply", review=review)
        self.assertEqual(code, 0, out)
        self.assertIn("review.replaced_retention_days = 0: the library videos this run replaces are deleted", out)
        self.assertEqual(self.files("Movies/Tamsin (2023)"), [f"{folder}.mkv", "movie.nfo"])  # no subtitle filed
        self.assertFalse(self.nas.exists("Downloads", folder))
        self.assertEqual([p for p in self.review("leftovers") if os.path.isfile(p)], [])  # clutter deleted
        self.assertEqual(self.replaced(), [])  # the old video too, at the end of the run

    def test_only_what_a_run_replaced_is_deleted_from_review(self):
        stray = touch(os.path.join(self.nas.dl, "_review", "replaced", rid(3 * 86400), "mine.mkv"), b"keep me")
        code, out = self.run_("run", "--apply", review=REPLACE)
        self.assertEqual(code, 0, out)
        self.assertIn("[KEPT]", out)
        self.assertTrue(os.path.exists(stray))

    def test_undo_brings_the_old_video_back(self):
        name = "Brightwater.S01E02.720p.WEB.x264-GRP.mkv"
        self.dl(name, b"y")
        code, out = self.run_("run", "--apply", review=REPLACE)
        self.assertEqual(code, 0, out)
        jpath = glob.glob(os.path.join(self.nas.state, "journal", "*.jsonl"))[0]
        inverse, notes = transfer.undo_plan(jpath)
        self.assertEqual(notes, [])
        moves = {(o.src, o.dst) for o in inverse if o.kind == "rename"}
        lib_ep2 = os.path.join(self.nas.mp, EP2)
        self.assertTrue(any(dst == lib_ep2 and "/_review/replaced/" in src for src, dst in moves), moves)
        self.assertIn((os.path.join(self.nas.mp, SEASON1, name), os.path.join(self.nas.dl, name)), moves)

    def test_status_shows_the_rule(self):
        with cli_env(self.nas, review=REPLACE) as path:
            _, out = run_cli("-c", path, "status")
        self.assertIn("duplicates: a download replaces the library video of the same episode or film (the newest "
                      "wins); the old video is kept in review 1 day(s)", out)


class TestNoDeletionOfReplacedVideos(unittest.TestCase):
    def test_an_unlink_of_a_library_video_is_never_planned_for_a_replacement(self):
        nas = FakeNas()
        try:
            old = os.path.join(nas.mp, EP2)
            st = os.stat(old)
            op = Op("unlink", dst=old, role="replaced", expect_size=st.st_size, expect_mtime=st.st_mtime)
            self.assertIn("refusing an unplanned kind of deletion", transfer._verify_unlink(op))
            self.assertLess(OLD, time.time())
        finally:
            nas.cleanup()


if __name__ == "__main__":
    unittest.main()
