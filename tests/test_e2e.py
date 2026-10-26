"""End-to-end: CLI against a fake NAS tree (dry-run, apply, rerun, undo, failures)."""

import glob
import json
import os
import subprocess
import sys
import textwrap
import time
import unittest
from unittest import mock

from mediaorg.guards import MountEntry, RunLock

from .helpers import FakeNas, age_tree, cli_env, run_cli, touch

THICKET = "The Thicket (2018) Season 1 S01 (1080p AMZN WEB-DL x265 HEVC 10bit DDP 5.1 Breeze)"
THICKET_E1 = "The Thicket (2018) - S01E01 - Episode 1 (1080p AMZN WEB-DL x265 HEVC 10bit DDP 5.1 Breeze)"
THICKET_E2 = "The Thicket (2018) - S01E02 - Episode 2 (1080p AMZN WEB-DL x265 HEVC 10bit DDP 5.1 Breeze)"
BRIGHT = "Brightwater.S01E01.Pilot.REPACK.1080p.AMZN.WEB-DL.DDP5.1.Atmos.H.264-BRISK"
QUILLON = "Quillon (2002) [WEBRip] [1080p] [QRS.XY]"


def populate(nas: FakeNas) -> None:
    dl = nas.dl
    touch(f"{dl}/Switchback.2022.S02E04.1080p.WEB.h264-QUOLL[QRSx.xy].mkv", b"switch4")
    touch(f"{dl}/{BRIGHT}/{BRIGHT}.mkv", b"brightwater-repack")
    touch(f"{dl}/{BRIGHT}/info.nfo")
    touch(f"{dl}/{THICKET}/{THICKET_E1}.mkv", b"thk1")
    touch(f"{dl}/{THICKET}/{THICKET_E2}.mkv", b"thk2")
    touch(f"{dl}/{THICKET}/{THICKET_E1}.en.srt", b"sub1")
    touch(f"{dl}/{THICKET}/Sample/sample-thicket.mkv", b"s")
    touch(f"{dl}/{THICKET}/release.nfo")
    touch(f"{dl}/{QUILLON}/Quillon.2002.1080p.WEBRip.x264-[QRS.XY].mp4", b"quillon")
    touch(f"{dl}/{QUILLON}/Subs/English.srt", b"quillon-sub")
    touch(f"{dl}/{QUILLON}/www.QRS.XY.jpg")
    touch(f"{dl}/Pale.Hours.Like.Ours.2023.1080p.WEB.H264-GRP.mkv", b"phlo")
    touch(f"{dl}/Brimstone.2016.S01E02.1080p.WEB.mkv", b"brimstone")
    touch(f"{dl}/Backup Agent Service 7.2/setup.exe")
    touch(f"{dl}/Brand.New.Show.S01E01.1080p.WEB.h264-GRP.mkv", b"bns1")
    touch(f"{dl}/Brand.New.Show.S01E02.1080p.WEB.h264-GRP.mkv", b"bns2")
    touch(f"{dl}/Cistern.S03E05.720p.WEB.h264-GRP.mkv", b"cistern720")
    touch(f"{dl}/Cistern.S03E05.1080p.WEB.h264-GRP.mkv", b"cistern1080")
    touch(f"{dl}/Corvid.2019.2160p.UHD.BluRay.x265-GRP.mkv", b"corvid")
    touch(f"{dl}/Weird.Show.S01E01.1080p.WEB/Other.Show.S01E02.1080p.WEB.mkv", b"weird")
    touch(f"{dl}/Ésker.S01E01.1080p.NF.WEB.mkv", b"esker")
    for d in glob.glob(f"{dl}/*/"):
        age_tree(d.rstrip("/"))
    touch(f"{dl}/Fresh.Movie.2025.1080p.WEB.H264-GRP/Fresh.Movie.2025.1080p.WEB.H264-GRP.mkv", b"fresh",
          mtime=time.time())  # still being written


class E2EBase(unittest.TestCase):
    def setUp(self):
        self.nas = FakeNas(extra_series=("Esker (2017)",))
        populate(self.nas)

    def tearDown(self):
        self.nas.cleanup()

    def run_cli(self, *args, **cfg):
        with cli_env(self.nas, **cfg) as path:
            return run_cli("-c", path, *args)

    def review_dirs(self, kind):
        return glob.glob(os.path.join(self.nas.dl, "_review", kind, "*"))


class TestDryRun(E2EBase):
    def test_dry_run_changes_nothing_and_reports(self):
        before = self.nas.snapshot()
        code, out = self.run_cli("run")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.nas.snapshot(), before)
        self.assertIn("DRY-RUN", out)
        self.assertIn("[WAIT] Fresh.Movie.2025", out)
        self.assertIn("[SKIP] Brimstone.2016.S01E02", out)
        self.assertIn("year_conflict", out)
        self.assertIn("[IGNORED] Backup Agent Service 7.2  -- no video files", out)
        self.assertIn("show title differs", out)  # Weird.Show folder vs file
        reports = glob.glob(os.path.join(self.nas.state, "runs", "*.json"))
        self.assertEqual(len(reports), 1)
        with open(reports[0], encoding="utf-8") as fh:
            rep = json.load(fh)
        self.assertFalse(rep["meta"]["apply"])


class TestApply(E2EBase):
    def test_apply_rerun_and_undo(self):
        before = self.nas.snapshot()
        code, out = self.run_cli("run", "--apply")
        self.assertEqual(code, 0, out)
        n = self.nas
        # moves
        self.assertTrue(n.exists("Series/Switchback (2022)/Season 2/Switchback.2022.S02E04.1080p.WEB.h264-QUOLL[QRSx.xy].mkv"))
        self.assertTrue(n.exists(f"Series/The Thicket (2018)/Season 1/{THICKET_E1}.mkv"))
        self.assertTrue(n.exists(f"Series/The Thicket (2018)/Season 1/{THICKET_E2}.mkv"))
        self.assertTrue(n.exists(f"Series/The Thicket (2018)/Season 1/{THICKET_E1}.en.srt"))
        self.assertTrue(n.exists("Movies/Quillon (2002)/Quillon.2002.1080p.WEBRip.x264-[QRS.XY].mp4"))
        self.assertTrue(n.exists("Movies/Quillon (2002)/Quillon.2002.1080p.WEBRip.x264-[QRS.XY].English.srt"))
        self.assertTrue(n.exists("Movies/Pale Hours Like Ours (2023)/Pale.Hours.Like.Ours.2023.1080p.WEB.H264-GRP.mkv"))
        self.assertTrue(n.exists("Series/Brand New Show/Season 1/Brand.New.Show.S01E01.1080p.WEB.h264-GRP.mkv"))
        self.assertTrue(n.exists("Series/Brand New Show/Season 1/Brand.New.Show.S01E02.1080p.WEB.h264-GRP.mkv"))
        self.assertTrue(n.exists("Series/Cistern (2022)/Season 3/Cistern.S03E05.1080p.WEB.h264-GRP.mkv"))
        self.assertTrue(n.exists("Series/Esker (2017)/Season 1/Ésker.S01E01.1080p.NF.WEB.mkv"))
        # duplicates kept for review, never deleted
        dup = self.review_dirs("duplicates")
        self.assertEqual(len(dup), 1)
        self.assertTrue(os.path.exists(os.path.join(dup[0], BRIGHT, BRIGHT + ".mkv")))
        self.assertTrue(os.path.exists(os.path.join(dup[0], "Corvid.2019.2160p.UHD.BluRay.x265-GRP.mkv")))
        # the second quality of an episode filed in the same run waits for the next run
        self.assertTrue(n.exists("Downloads/Cistern.S03E05.720p.WEB.h264-GRP.mkv"))
        self.assertIn("S03E05 is filed from another download in this run", out)
        # leftovers kept for review
        left = self.review_dirs("leftovers")
        self.assertTrue(os.path.exists(os.path.join(left[0], THICKET, "Sample", "sample-thicket.mkv")))
        self.assertTrue(os.path.exists(os.path.join(left[0], QUILLON, "www.QRS.XY.jpg")))
        # untouched
        for name in ("Brimstone.2016.S01E02.1080p.WEB.mkv", "Backup Agent Service 7.2",
                     "Fresh.Movie.2025.1080p.WEB.H264-GRP", "Weird.Show.S01E01.1080p.WEB"):
            self.assertTrue(n.exists("Downloads", name), name)
        # existing library content unchanged
        with open(os.path.join(n.series, "Brightwater (2025)/Season 1/Brightwater.S01E01.1080p.mkv"), "rb") as fh:
            self.assertEqual(fh.read(), b"x")
        # nothing lost: every original byte string still exists somewhere
        after_blobs = sorted(v for k, v in n.snapshot().items() if not k.endswith("/"))
        before_blobs = sorted(v for k, v in before.items() if not k.endswith("/"))
        self.assertEqual(after_blobs, before_blobs)

        # the next run handles it: the library now has S03E05, so it is a duplicate
        first = set(os.listdir(os.path.join(n.state, "journal")))
        code, out = self.run_cli("run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertIn("renames 1 (media 0, subtitles 0, duplicates 1", out)
        self.assertEqual(len(glob.glob(os.path.join(self.nas.dl, "_review/duplicates/*/Cistern.S03E05.720p*"))), 1)
        second = set(os.listdir(os.path.join(n.state, "journal"))) - first

        # then a rerun is a no-op for everything already handled
        code, out = self.run_cli("run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertIn("renames 0", out)

        # undo (newest run first) restores the exact original tree (apart from the review root folder)
        for run_id in (sorted(second)[0][:-6], sorted(first)[0][:-6]):
            code, out = self.run_cli("undo", run_id, "--apply")
            self.assertEqual(code, 0, out)
        restored = {k: v for k, v in n.snapshot().items() if "_review" not in k}
        self.assertEqual(restored, before)

    def test_safety_cap(self):
        before = self.nas.snapshot()
        code, _ = self.run_cli("run", "--apply", "--max-moves", "2")
        self.assertEqual(code, 6)
        self.assertEqual(self.nas.snapshot(), before)

    def test_only_restricts_scope(self):
        code, out = self.run_cli("run", "--apply", "--only", "Switchback.2022.S02E04.1080p.WEB.h264-QUOLL[QRSx.xy].mkv")
        self.assertEqual(code, 0, out)
        self.assertTrue(self.nas.exists("Series/Switchback (2022)/Season 2/Switchback.2022.S02E04.1080p.WEB.h264-QUOLL[QRSx.xy].mkv"))
        self.assertTrue(self.nas.exists("Downloads/Corvid.2019.2160p.UHD.BluRay.x265-GRP.mkv"))
        code, _ = self.run_cli("run", "--only", "does-not-exist")
        self.assertEqual(code, 2)


class TestPreconditions(E2EBase):
    def test_nas_not_mounted(self):
        before = self.nas.snapshot()
        with cli_env(self.nas) as path, \
                mock.patch("mediaorg.guards.read_mountinfo", return_value=[MountEntry("/", "xfs", "/dev/x", "")]):
            code, _ = run_cli("-c", path, "run", "--apply")
        self.assertEqual(code, 3)
        self.assertEqual(self.nas.snapshot(), before)

    def test_wrong_share_mounted(self):
        wrong = [MountEntry(self.nas.mp, "cifs", "//nas.example/other", "")]
        with cli_env(self.nas) as path, mock.patch("mediaorg.guards.read_mountinfo", return_value=wrong):
            code, _ = run_cli("-c", path, "run", "--apply")
        self.assertEqual(code, 3)

    def test_lock_busy(self):
        before = self.nas.snapshot()
        with RunLock(os.path.join(self.nas.state, "lock")):
            code, _ = self.run_cli("run", "--apply")
        self.assertEqual(code, 4)
        self.assertEqual(self.nas.snapshot(), before)

    def test_root_switches_to_the_service_account(self):
        # 2.0: started with sudo, a media command becomes the unprivileged service account first
        with cli_env(self.nas) as path, mock.patch("os.geteuid", return_value=0), \
                mock.patch("mediaorg.system.drop_to_service_user") as drop:
            code, _ = run_cli("-c", path, "run")
        self.assertEqual(code, 0)
        drop.assert_called_once()


CRASH_DRIVER = textwrap.dedent("""
    import sys
    from unittest import mock
    sys.path.insert(0, {root!r})
    from mediaorg.guards import MountEntry
    entries = [MountEntry({mp!r}, "cifs", "//nas.example/media", "")]
    with mock.patch("mediaorg.guards.read_mountinfo", return_value=entries), \\
         mock.patch("os.geteuid", return_value=1000):
        from mediaorg.cli import main
        sys.exit(main(["-c", {cfg!r}, "run", "--apply"]))
""")


class TestInterruption(E2EBase):
    def test_crash_mid_run_then_recover(self):
        before = self.nas.snapshot()
        with cli_env(self.nas) as cfg_path:
            pass  # writes the config file
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script = CRASH_DRIVER.format(root=root, mp=self.nas.mp, cfg=cfg_path)
        env = dict(os.environ, MEDIAORG_TEST_CRASH_AFTER="3")
        p = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 137, p.stderr)
        journals = glob.glob(os.path.join(self.nas.state, "journal", "*.jsonl"))
        self.assertEqual(len(journals), 1)
        with open(journals[0], encoding="utf-8") as fh:
            states = [json.loads(line).get("state") for line in fh]
        self.assertIn("intent", states)
        self.assertNotIn("end", states)

        # next run reconciles the journal and finishes the remaining work
        code, out = self.run_cli("run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertTrue(self.nas.exists(f"Series/The Thicket (2018)/Season 1/{THICKET_E2}.mkv"))
        with open(journals[0], encoding="utf-8") as fh:
            recs = [json.loads(line) for line in fh]
        self.assertEqual(recs[-1]["state"], "end")
        self.assertEqual(recs[-1]["status"], "interrupted")
        after_blobs = sorted(v for k, v in self.nas.snapshot().items() if not k.endswith("/"))
        self.assertEqual(after_blobs, sorted(v for k, v in before.items() if not k.endswith("/")))

    def test_conflicting_journal_blocks_runs(self):
        jdir = os.path.join(self.nas.state, "journal")
        os.makedirs(jdir)
        src = os.path.join(self.nas.dl, "Corvid.2019.2160p.UHD.BluRay.x265-GRP.mkv")
        with open(os.path.join(jdir, "20260101T000000Z-dead.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"state": "begin", "kind": "run"}) + "\n")
            fh.write(json.dumps({"seq": 1, "state": "intent", "op": "rename", "src": src,
                                 "dst": os.path.join(self.nas.movies, "Corvid (2019)", "Corvid.2019.mkv")}) + "\n")
            fh.write(json.dumps({"seq": 2, "state": "intent", "op": "rename", "src": "/nonexistent/a",
                                 "dst": "/nonexistent/b"}) + "\n")
        before = self.nas.snapshot()
        code, _ = self.run_cli("run", "--apply")
        self.assertEqual(code, 5)
        self.assertEqual(self.nas.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
