"""2.3.0: review.replace_duplicates: a download replaces the library video of the same episode or film
(the newest download wins); only the video is replaced."""

import glob
import json
import os
import unittest

from mediaorg import transfer
from mediaorg.model import Op

from .helpers import FakeNas, age_tree, cli_env, run_cli, touch
from .test_v101 import Base

REPLACE = {"replace_duplicates": True}
SEASON1 = "Series/Brightwater (2025)/Season 1"
EP2 = f"{SEASON1}/Brightwater.S01E02.1080p.mkv"  # library file of the fake NAS (1 byte, b"x")
TAMSIN = "Movies/Tamsin (2023)/Tamsin.(2023).mkv"  # likewise


class TestReplaceDuplicates(Base):
    def files(self, rel):
        return sorted(os.listdir(os.path.join(self.nas.mp, rel)))

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
        self.assertIn(f"    delete    {EP2}  (library video, replaced by the download)", out)
        self.assertIn("replaced library videos 1", out)
        code, out = self.run_("run", "--apply", review=REPLACE)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read(f"{SEASON1}/{name}"), b"yyyy")
        self.assertIsNone(self.read(EP2))
        self.assertEqual(self.read(f"{SEASON1}/Brightwater.S01E02.1080p.nfo"), b"nfo")  # the rest stays
        self.assertEqual(self.read(f"{SEASON1}/Brightwater.S01E02.1080p.en.srt"), b"sub")
        self.assertFalse(self.nas.exists("Downloads", name))
        self.assertEqual([p for p in self.review("duplicates") if os.path.isfile(p)], [])
        report = sorted(glob.glob(os.path.join(self.nas.state, "runs", "*.json")))[-1]
        with open(report, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["summary"]["replaced"], 1)
        self.assertEqual(data["items"][0]["decisions"][0]["action"], "replace")

    def test_the_newest_wins_even_when_smaller(self):
        self.lib(TAMSIN, b"a much bigger library copy")
        name = "Tamsin.2023.720p.WEB.H264-GRP.mkv"
        self.dl(name, b"y")
        code, out = self.run_("run", "--apply", review=REPLACE)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.files("Movies/Tamsin (2023)"), ["Tamsin.2023.720p.WEB.H264-GRP.mkv", "movie.nfo"])

    def test_same_name_old_file_goes_first_while_the_download_is_there(self):
        self.dl("Brightwater.S01E02.1080p.mkv", b"zz")  # the very name of the library file, other bytes
        plans = self.plan(review=REPLACE)
        roles = [(o.kind, o.role) for o in plans[0].ops if o.kind in ("rename", "unlink")]
        self.assertEqual(roles, [("unlink", "replaced"), ("rename", "media")])
        gone = next(o for o in plans[0].ops if o.role == "replaced")
        self.assertEqual([p for p, _, _ in gone.ref_files], [os.path.join(self.nas.dl, "Brightwater.S01E02.1080p.mkv")])
        res, _ = self.execute(plans)
        self.assertEqual(res.failed, 0, res.errors)
        self.assertEqual(self.read(EP2), b"zz")

    def test_the_new_file_goes_in_before_the_old_one_is_deleted(self):
        name = "Brightwater.S01E02.720p.WEB.x264-GRP.mkv"
        self.dl(name, b"y")
        plans = self.plan(review=REPLACE)
        roles = [(o.kind, o.role) for o in plans[0].ops if o.kind in ("rename", "unlink")]
        self.assertEqual(roles, [("rename", "media"), ("unlink", "replaced")])
        gone = next(o for o in plans[0].ops if o.role == "replaced")
        self.assertEqual([p for p, _, _ in gone.ref_files], [os.path.join(self.nas.mp, SEASON1, name)])

    def test_a_library_copy_changed_meanwhile_is_kept(self):
        name = "Brightwater.S01E02.720p.WEB.x264-GRP.mkv"
        self.dl(name, b"y")
        plans = self.plan(review=REPLACE)
        self.lib(EP2, b"changed", mtime=OLD_NOW)  # replaced by hand meanwhile
        res, _ = self.execute(plans)
        self.assertEqual(res.failed, 1)
        self.assertIn("changed since the scan", res.errors[0])
        self.assertEqual(self.read(EP2), b"changed")  # the old one stays; the new one was moved in first
        self.assertEqual(self.read(f"{SEASON1}/{name}"), b"y")

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
        for p in self.plan(review=REPLACE):
            self.assertNotEqual(p.decisions[0].action, "replace")

    def test_a_second_copy_in_the_same_run_waits_for_the_next(self):
        self.dl("Brightwater.S01E02.720p.WEB.x264-GRP.mkv", b"y")
        self.dl("Brightwater.S01E02.480p.WEB.x264-OTHER.mkv", b"z")
        plans = {p.item.name: p for p in self.plan(review=REPLACE)}
        self.assertEqual(plans["Brightwater.S01E02.480p.WEB.x264-OTHER.mkv"].decisions[0].action, "replace")
        later = plans["Brightwater.S01E02.720p.WEB.x264-GRP.mkv"].decisions[0]
        self.assertEqual(later.action, "skip")
        self.assertIn("is filed from another download in this run", later.reason)

    def test_the_downloads_other_files_go_to_leftovers_and_clutter_is_deleted_at_once(self):
        folder = "Tamsin.2023.2160p.WEB.H265-GRP"
        self.dl(f"{folder}/{folder}.mkv", b"yy")
        self.dl(f"{folder}/{folder}.en.srt", b"sub")
        self.dl(f"{folder}/{folder}.nfo", b"nfo")
        age_tree(os.path.join(self.nas.dl, folder))
        code, out = self.run_("run", "--apply", review={**REPLACE, "leftovers_retention_days": 0})
        self.assertEqual(code, 0, out)
        self.assertEqual(self.files("Movies/Tamsin (2023)"), [f"{folder}.mkv", "movie.nfo"])  # no subtitle filed
        self.assertFalse(self.nas.exists("Downloads", folder))
        self.assertEqual([p for p in self.review("leftovers") if os.path.isfile(p)], [])  # deleted, not kept

    def test_undo_says_a_replaced_video_is_not_restored(self):
        self.dl("Brightwater.S01E02.720p.WEB.x264-GRP.mkv", b"y")
        code, out = self.run_("run", "--apply", review=REPLACE)
        self.assertEqual(code, 0, out)
        jpath = glob.glob(os.path.join(self.nas.state, "journal", "*.jsonl"))[0]
        inverse, notes = transfer.undo_plan(jpath)
        self.assertEqual(len(notes), 1)
        self.assertIn(f"{EP2} was replaced by", notes[0])
        self.assertTrue(any(o.kind == "rename" for o in inverse))  # the new file can go back to Downloads

    def test_status_shows_the_rule(self):
        with cli_env(self.nas, review=REPLACE) as path:
            _, out = run_cli("-c", path, "status")
        self.assertIn("duplicates: a download replaces the library video of the same episode or film (the newest "
                      "wins)", out)


OLD_NOW = None  # set in setUpModule: a time after the scan


def setUpModule():
    import time
    global OLD_NOW
    OLD_NOW = time.time()


class TestVerifyReplaced(unittest.TestCase):
    def setUp(self):
        self.nas = FakeNas()

    def tearDown(self):
        self.nas.cleanup()

    def test_the_replacement_must_be_in_place(self):
        old = os.path.join(self.nas.mp, EP2)
        new = touch(os.path.join(self.nas.mp, SEASON1, "new.mkv"), b"yy")
        so, sn = os.stat(old), os.stat(new)
        op = Op("unlink", dst=old, role="replaced", expect_size=so.st_size, expect_mtime=so.st_mtime, ref=new,
                ref_files=((new, sn.st_size, sn.st_mtime),))
        self.assertIsNone(transfer._verify_unlink(op))
        os.unlink(new)  # the move did not happen
        self.assertIn("the replacement", transfer._verify_unlink(op))
        op.ref_files = ()
        self.assertIn("no replacement recorded", transfer._verify_unlink(op))


if __name__ == "__main__":
    unittest.main()
