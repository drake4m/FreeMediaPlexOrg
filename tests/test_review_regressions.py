"""Regression tests for the findings of an independent code review, one class per finding."""

import errno
import glob
import json
import os
import subprocess
import sys
import unicodedata
import unittest
from unittest import mock

from mediaorg import transfer
from mediaorg.library import Library
from mediaorg.planner import Planner
from mediaorg.model import Readiness
from mediaorg.scan import scan_downloads
from mediaorg.transfer import Journal, execute, read_journal, reconcile, run_undo

from .fakes import FakeFreebox, b64
from .helpers import FakeNas, age_tree, cli_env, run_cli, touch
from .test_e2e import CRASH_DRIVER

REMOTE = "/Freebox/Downloads/"
READY = Readiness(True, "test")


def task(tid, name, status, ddir=REMOTE):
    return {"id": tid, "name": name, "status": status, "rx_pct": 10000, "size": 100, "type": "bt",
            "download_dir": b64(ddir), "stop_ratio": 150, "error": "none"}


class Base(unittest.TestCase):
    def setUp(self):
        self.nas = FakeNas()

    def tearDown(self):
        self.nas.cleanup()

    def freebox_run(self, fb, *args):
        tok = os.path.join(self.nas.state, "tok.json")
        os.makedirs(self.nas.state, exist_ok=True)
        with open(tok, "w") as fh:
            json.dump({"app_id": "mediaorg", "app_token": FakeFreebox.APP_TOKEN}, fh)
        os.chmod(tok, 0o600)
        with cli_env(self.nas, completeness={"method": "freebox"},
                     freebox={"remote_downloads_dir": REMOTE, "base_url": fb.url, "allow_insecure_http": True,
                              "token_file": tok}) as p:
            return run_cli("-c", p, "run", *args)

    def plan_and_execute(self, mutate=None):
        cfg = self.nas.config()
        items, _ = scan_downloads(cfg)
        planner = Planner(cfg, Library(cfg.paths.movies, cfg.paths.series, cfg.media.video_extensions), "R")
        plans = [planner.plan_item(i, READY) for i in items]
        if mutate:
            mutate()
        j = Journal(os.path.join(self.nas.state, "journal"), "R")
        try:
            return plans, execute([p for p in plans if p.ops], j)
        finally:
            j.close()


class F1SeedingTaskInSubfolder(Base):
    def test_seeding_task_below_downloads_waits(self):
        touch(f"{self.nas.dl}/Movies/Corvid.2019.2160p.UHD.BluRay.x265-GRP2/Corvid.2019.2160p.UHD.BluRay.x265-GRP2.mkv")
        age_tree(f"{self.nas.dl}/Movies")
        fb = FakeFreebox(tasks=[task(1, "Corvid.2019.2160p.UHD.BluRay.x265-GRP2", "seeding", REMOTE + "Movies/")])
        try:
            before = self.nas.snapshot()
            code, out = self.freebox_run(fb, "--apply")
        finally:
            fb.close()
        self.assertEqual(code, 0, out)
        self.assertIn("[WAIT] Movies", out)
        self.assertEqual(self.nas.snapshot(), before)


class F2NfdPartialFile(Base):
    def test_deselected_file_with_nfd_name_is_not_moved(self):
        nfd = lambda s: unicodedata.normalize("NFD", s)  # noqa: E731
        folder = nfd("Ésker.S01.1080p.NF.WEB")
        e1, e2 = nfd("Ésker.S01E01.1080p.NF.WEB.mkv"), nfd("Ésker.S01E02.1080p.NF.WEB.mkv")
        touch(f"{self.nas.dl}/{folder}/{e1}", b"e1-full")
        touch(f"{self.nas.dl}/{folder}/{e2}", b"e2-PARTIAL")
        age_tree(f"{self.nas.dl}/{folder}")
        fb = FakeFreebox(tasks=[task(2, folder, "done")], files={2: [
            {"filepath": b64(REMOTE + folder + "/" + e1), "size": 7, "rx": 7, "status": "done", "priority": "normal"},
            {"filepath": b64(REMOTE + folder + "/" + e2), "size": 900, "rx": 10, "status": "queued",
             "priority": "no_dl"}]})
        try:
            code, out = self.freebox_run(fb, "--apply")
        finally:
            fb.close()
        self.assertIn("not fully downloaded", out)
        moved = [f for f in glob.glob(f"{self.nas.series}/*/*/*") if "S01E02.1080p.NF" in f]
        self.assertEqual(moved, [])
        self.assertIn("[PARTIAL]", out)
        self.assertTrue(os.path.exists(f"{self.nas.dl}/{folder}/{e2}"))


class F3MovieFileInEpisodeFolder(Base):
    def test_not_filed_as_episode(self):
        touch(f"{self.nas.dl}/Cistern.S03E06.1080p.WEB.H264-QUOLL/Undercroft.2008.1080p.BluRay.x264-GRP.mkv")
        age_tree(f"{self.nas.dl}/Cistern.S03E06.1080p.WEB.H264-QUOLL")
        plans, _ = self.plan_and_execute()
        self.assertEqual(plans[0].decisions[0].action, "skip")
        self.assertTrue(os.path.exists(f"{self.nas.dl}/Cistern.S03E06.1080p.WEB.H264-QUOLL"))


class F4PrefixAmbiguity(Base):
    def test_library_breaks_the_tie(self):
        touch(f"{self.nas.dl}/quoll-cistern.s03e05.1080p.mkv")
        plans, res = self.plan_and_execute()
        self.assertEqual(res.failed, 0)
        self.assertTrue(self.nas.exists("Series/Cistern (2022)/Season 3/quoll-cistern.s03e05.1080p.mkv"))
        self.assertFalse(glob.glob(f"{self.nas.series}/QUOLL*"))

    def test_unresolvable_is_skipped(self):
        touch(f"{self.nas.dl}/zzgrp-brand.new.thing.s01e01.1080p.mkv")
        plans, _ = self.plan_and_execute()
        self.assertEqual(plans[0].decisions[0].action, "skip")
        self.assertIn("ambiguous name", plans[0].decisions[0].reason)


class F5ReservationAgainstOtherClients(Base):
    def test_target_appearing_after_planning_is_never_replaced(self):
        touch(f"{self.nas.dl}/Cistern.S03E06.1080p.WEB.h264-GRP.mkv", b"new")
        target = os.path.join(self.nas.series, "Cistern (2022)", "Season 3", "Cistern.S03E06.1080p.WEB.h264-GRP.mkv")

        def other_client():  # e.g. tMM writing the same name between scan and move
            with open(target, "wb") as fh:
                fh.write(b"other-client")
        _, res = self.plan_and_execute(mutate=other_client)
        self.assertEqual(res.failed, 1)
        with open(target, "rb") as fh:
            self.assertEqual(fh.read(), b"other-client")
        self.assertTrue(self.nas.exists("Downloads/Cistern.S03E06.1080p.WEB.h264-GRP.mkv"))

    def test_rename_failure_releases_placeholder(self):
        touch(f"{self.nas.dl}/Cistern.S03E06.1080p.WEB.h264-GRP.mkv", b"new")
        target = os.path.join(self.nas.series, "Cistern (2022)", "Season 3", "Cistern.S03E06.1080p.WEB.h264-GRP.mkv")
        with mock.patch("mediaorg.transfer.os.rename", side_effect=OSError(errno.EACCES, "denied")):
            _, res = self.plan_and_execute()
        self.assertEqual(res.failed, 1)
        self.assertFalse(os.path.exists(target))  # empty placeholder removed

    def test_crash_after_reservation_is_reconciled(self):
        src = touch(f"{self.nas.dl}/x.mkv", b"data")
        dst = os.path.join(self.nas.movies, "x.mkv")
        open(dst, "x").close()  # placeholder left by a crash
        jdir = os.path.join(self.nas.state, "journal")
        os.makedirs(jdir)
        with open(os.path.join(jdir, "c.jsonl"), "w") as fh:
            for r in ({"state": "begin"}, {"seq": 1, "state": "intent", "op": "rename", "src": src, "dst": dst},
                      {"seq": 1, "state": "reserved"}):
                fh.write(json.dumps(r) + "\n")
        self.assertEqual(reconcile(jdir)[1], [])
        self.assertFalse(os.path.exists(dst))
        self.assertTrue(os.path.exists(src))


class F6FolderChangedAfterScan(Base):
    def test_whole_folder_move_rechecks_content(self):
        item = f"{self.nas.dl}/Corvid.2019.2160p.UHD.BluRay.x265-GRP"
        touch(f"{item}/Corvid.2019.2160p.UHD.BluRay.x265-GRP.mkv")
        age_tree(item)
        _, res = self.plan_and_execute(mutate=lambda: touch(f"{item}/late-thornbury.mkv"))
        self.assertEqual(res.failed, 1)
        self.assertIn("changed since the scan", res.errors[0])
        self.assertTrue(os.path.exists(f"{item}/late-thornbury.mkv"))


class F7LooseSubtitles(Base):
    def test_loose_subtitle_travels_with_loose_video(self):
        touch(f"{self.nas.dl}/Quillon.2002.1080p.WEBRip.x264.mkv", b"v")
        touch(f"{self.nas.dl}/Quillon.2002.1080p.WEBRip.x264.en.srt", b"s")
        touch(f"{self.nas.dl}/Quillon.2002.1080p.WEBRip.x264.1.srt.bak", b"other")  # not a subtitle ext
        plans, res = self.plan_and_execute()
        self.assertEqual(res.failed, 0)
        self.assertTrue(self.nas.exists("Movies/Quillon (2002)/Quillon.2002.1080p.WEBRip.x264.en.srt"))


class F8RemainsOfInterruptedItem(Base):
    def test_remains_go_to_review_after_crash(self):
        quillon_dir = "Quillon (2002) [WEBRip] [1080p] [QRS.XY]"
        touch(f"{self.nas.dl}/{quillon_dir}/Quillon.2002.1080p.WEBRip.x264-[QRS.XY].mp4", b"quillon")
        touch(f"{self.nas.dl}/{quillon_dir}/Subs/English.srt", b"sub")
        touch(f"{self.nas.dl}/{quillon_dir}/www.QRS.XY.jpg")
        age_tree(f"{self.nas.dl}/{quillon_dir}")
        with cli_env(self.nas) as cfg_path:
            pass
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        p = subprocess.run([sys.executable, "-c", CRASH_DRIVER.format(root=root, mp=self.nas.mp, cfg=cfg_path)],
                           env=dict(os.environ, MEDIAORG_TEST_CRASH_AFTER="1"), capture_output=True, text=True)
        self.assertEqual(p.returncode, 137, p.stderr)
        with cli_env(self.nas) as path:
            code, out = run_cli("-c", path, "run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertIn("remains of an item partly processed", out)
        self.assertFalse(self.nas.exists("Downloads", quillon_dir))
        left = glob.glob(f"{self.nas.dl}/_review/leftovers/*/{glob.escape(quillon_dir)}/www.QRS.XY.jpg")
        self.assertEqual(len(left), 1)


class F9UnknownOutcome(Base):
    def test_unreachable_nas_mid_rename_stops_and_reconciles(self):
        touch(f"{self.nas.dl}/Cistern.S03E06.1080p.WEB.h264-GRP.mkv", b"x")
        real_rename, real_exists = os.rename, transfer._exists
        calls = {"n": 0}

        def rename_then_timeout(a, b):
            real_rename(a, b)
            raise OSError(errno.EHOSTDOWN, "Host is down")

        def flaky_exists(p):
            calls["n"] += 1
            return None if calls["n"] <= 2 else real_exists(p)

        with cli_env(self.nas) as path, \
                mock.patch("mediaorg.transfer.os.rename", side_effect=rename_then_timeout), \
                mock.patch("mediaorg.transfer._exists", side_effect=flaky_exists):
            code, _ = run_cli("-c", path, "run", "--apply")
        self.assertEqual(code, 3)
        jpath = glob.glob(os.path.join(self.nas.state, "journal", "*.jsonl"))[0]
        _, ops, end = read_journal(jpath)
        self.assertTrue(end["needs_reconcile"])
        self.assertIn("unknown", {r["state"] for r in ops.values()})
        with cli_env(self.nas) as path:
            code, _ = run_cli("-c", path, "run", "--apply")
        self.assertEqual(code, 0)
        _, ops, _ = read_journal(jpath)
        renames = [r for r in ops.values() if r.get("op") == "rename"]
        self.assertEqual(renames[0]["state"], "done")  # so undo will see it


class F11LibraryChangingDuringScan(Base):
    def test_unstable_listing_aborts(self):
        touch(f"{self.nas.dl}/Cistern.S03E06.1080p.WEB.h264-GRP.mkv")
        real_scandir = os.scandir

        class Vanishing:
            def __init__(self, it):
                self.it = it

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.it.close()

            def __iter__(self):
                class Gone:
                    def __init__(self, name):
                        self.name = name

                    def stat(self, follow_symlinks=True):
                        raise FileNotFoundError(self.name)
                for e in self.it:
                    yield Gone(e.name)

        def scandir(p="."):
            it = real_scandir(p)
            return Vanishing(it) if str(p).startswith(self.nas.series + "/Cistern") else it

        with cli_env(self.nas) as path, mock.patch("mediaorg.library.os.scandir", side_effect=scandir):
            code, _ = run_cli("-c", path, "run", "--apply")
        self.assertEqual(code, 3)
        self.assertTrue(self.nas.exists("Downloads/Cistern.S03E06.1080p.WEB.h264-GRP.mkv"))

    def test_folder_appearing_since_planning_aborts_item(self):
        touch(f"{self.nas.dl}/Brand.New.Show.S01E01.1080p.WEB.h264-GRP.mkv")
        _, res = self.plan_and_execute(mutate=lambda: os.makedirs(f"{self.nas.series}/Brand New Show"))
        self.assertEqual(res.failed, 1)
        self.assertIn("appeared since planning", res.errors[0])
        self.assertTrue(self.nas.exists("Downloads/Brand.New.Show.S01E01.1080p.WEB.h264-GRP.mkv"))


class F12UndoAndDryRun(Base):
    def test_dry_run_does_not_write_journals(self):
        src = os.path.join(self.nas.dl, "a.mkv")
        dst = touch(os.path.join(self.nas.movies, "a.mkv"))
        jdir = os.path.join(self.nas.state, "journal")
        os.makedirs(jdir)
        jp = os.path.join(jdir, "old.jsonl")
        with open(jp, "w") as fh:
            fh.write(json.dumps({"state": "begin"}) + "\n")
            fh.write(json.dumps({"seq": 1, "state": "intent", "op": "rename", "src": src, "dst": dst}) + "\n")
        with open(jp) as fh:
            before = fh.read()
        with cli_env(self.nas) as path:
            code, _ = run_cli("-c", path, "run")
            self.assertEqual(code, 0)
            with open(jp) as fh:
                self.assertEqual(fh.read(), before)
            code, _ = run_cli("-c", path, "run", "--apply")
        self.assertEqual(read_journal(jp)[1][1]["state"], "done")

    def test_undo_never_creates_missing_parents(self):
        jdir = os.path.join(self.nas.state, "journal")
        os.makedirs(jdir)
        src = os.path.join(self.nas.tmp, "gone", "deep", "x.mkv")
        dst = touch(os.path.join(self.nas.movies, "x.mkv"))
        jp = os.path.join(jdir, "u.jsonl")
        with open(jp, "w") as fh:
            for r in ({"state": "begin"}, {"seq": 1, "state": "intent", "op": "rename", "src": src, "dst": dst},
                      {"seq": 1, "state": "done"}, {"state": "end", "status": "ok"}):
                fh.write(json.dumps(r) + "\n")
        _, errors = run_undo(jp, True, jdir, "undo-test")
        self.assertEqual(len(errors), 1)
        self.assertFalse(os.path.exists(os.path.join(self.nas.tmp, "gone")))
        self.assertTrue(os.path.exists(dst))


if __name__ == "__main__":
    unittest.main()
