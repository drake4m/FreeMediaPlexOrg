"""2.1.0: completeness.on_seeding = "stop" (Freebox) and naming.tmm_staging (folders tMM always renames)."""

import json
import os
import unittest
from unittest import mock

from mediaorg import completeness as comp
from mediaorg import config as C
from mediaorg.freebox import FreeboxClient
from mediaorg.library import Library
from mediaorg.model import FileEntry, Item, Readiness
from mediaorg.normalize import STAGING_TAG, is_staged, split_folder_name, unstaged
from mediaorg.planner import Planner
from mediaorg.scan import scan_downloads

from .fakes import FakeFreebox, b64
from .helpers import OLD, FakeNas, age_tree, cli_env, run_cli, touch

REMOTE = "/Freebox/Downloads/"
MOVIE = "Rookfield.2026.1080p.WEB.H264-GRP"  # a fictional film not in the test library


def task(tid, name, status, rx_pct=10000, ddir=REMOTE):
    return {"id": tid, "name": name, "status": status, "rx_pct": rx_pct, "size": 100, "type": "bt",
            "download_dir": b64(ddir), "stop_ratio": 0, "error": "none"}


class FreeboxBase(unittest.TestCase):
    def setUp(self):
        self.nas = FakeNas()
        self.fb = None

    def tearDown(self):
        if self.fb:
            self.fb.close()
        self.nas.cleanup()

    def env(self, tasks, on_seeding="stop", files=None, safety=None, **fb_kwargs):
        if self.fb:
            self.fb.close()
        self.fb = FakeFreebox(tasks=tasks, files=files, download_dir=REMOTE.rstrip("/"), **fb_kwargs)
        tok = os.path.join(self.nas.state, "tok.json")
        os.makedirs(self.nas.state, exist_ok=True)
        with open(tok, "w") as fh:
            json.dump({"app_id": "mediaorg", "app_token": FakeFreebox.APP_TOKEN}, fh)
        os.chmod(tok, 0o600)
        return cli_env(self.nas, completeness={"method": "freebox", "on_seeding": on_seeding},
                       freebox={"remote_downloads_dir": REMOTE, "base_url": self.fb.url,
                                "allow_insecure_http": True, "token_file": tok}, **({"safety": safety} if safety else {}))

    def movie_download(self, with_subtitle=False):
        touch(f"{self.nas.dl}/{MOVIE}/{MOVIE}.mkv", b"movie")
        if with_subtitle:
            touch(f"{self.nas.dl}/{MOVIE}/{MOVIE}.en.srt", b"sub")
        age_tree(f"{self.nas.dl}/{MOVIE}")
        return {1: [{"filepath": b64(f"{REMOTE}{MOVIE}/{MOVIE}.mkv"), "size": 5, "rx": 5, "status": "done",
                     "priority": "normal"}]}

    def puts(self):
        return [r for r in self.fb.requests if r[0] == "PUT"]

    def filed(self):
        return os.path.exists(f"{self.nas.movies}/Rookfield (2026)/{MOVIE}.mkv")


class TestStopSeeding(FreeboxBase):
    def test_apply_stops_the_seeding_task_then_files_it(self):
        files = self.movie_download()
        with self.env([task(1, MOVIE, "seeding")], files=files) as path:
            code, out = run_cli("-c", path, "run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.puts(), [("PUT", "/api/v15/downloads/1", {"status": "stopped"})])
        self.assertIn("Freebox task 1 was seeding: stopped", out)
        self.assertTrue(self.filed(), out)
        self.assertFalse(os.path.exists(f"{self.nas.dl}/{MOVIE}"))

    def test_dry_run_stops_nothing_and_says_what_apply_does(self):
        files = self.movie_download()
        before = self.nas.snapshot()
        with self.env([task(1, MOVIE, "seeding")], files=files) as path:
            code, out = run_cli("-c", path, "run")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.puts(), [])
        self.assertIn(f"[READY] {MOVIE}  -- Freebox task 1 is seeding: an --apply run stops it, then files", out)
        self.assertEqual(self.nas.snapshot(), before)

    def test_wait_mode_never_stops(self):
        files = self.movie_download()
        before = self.nas.snapshot()
        with self.env([task(1, MOVIE, "seeding")], on_seeding="wait", files=files) as path:
            code, out = run_cli("-c", path, "run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.puts(), [])
        self.assertIn(f"[WAIT] {MOVIE}  -- Freebox task is seeding", out)
        self.assertEqual(self.nas.snapshot(), before)

    def test_task_not_confirmed_stopped_is_not_moved(self):
        files = self.movie_download()
        before = self.nas.snapshot()
        with self.env([task(1, MOVIE, "seeding")], files=files, on_stop="stopping") as path, \
                mock.patch("mediaorg.completeness.STOP_CONFIRM_SECONDS", 0):
            code, out = run_cli("-c", path, "run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.puts()), 1)
        self.assertIn(f"[WAIT] {MOVIE}  -- Freebox task 1 is still 'stopping'", out)
        self.assertEqual(self.nas.snapshot(), before)
        # next run: the Freebox lists it as stopped at 100 %: filed without another stop request
        with self.env([task(1, MOVIE, "stopped")], files=files) as path:
            code, out = run_cli("-c", path, "run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.puts(), [])
        self.assertTrue(self.filed(), out)

    def test_refused_stop_leaves_everything(self):
        files = self.movie_download()
        before = self.nas.snapshot()
        with self.env([task(1, MOVIE, "seeding")], files=files, on_stop="refuse") as path:
            code, out = run_cli("-c", path, "run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertIn(f"[WAIT] {MOVIE}  -- Freebox task 1 could not be stopped", out)
        self.assertNotIn("move ", out)  # a WAIT item shows no planned moves (2.0 never did either)
        self.assertNotIn("new movie folder", out)
        self.assertEqual(self.nas.snapshot(), before)
        runs = os.path.join(self.nas.state, "runs")
        with open(os.path.join(runs, sorted(os.listdir(runs))[-1]), encoding="utf-8") as fh:
            item = json.load(fh)["items"][0]
        self.assertEqual((item["status"], item["decisions"], item["ops"]), ("wait", [], []))

    def test_answer_cut_short_is_a_freebox_error_not_a_crash(self):
        files = self.movie_download()
        before = self.nas.snapshot()
        with self.env([task(1, MOVIE, "seeding")], files=files, on_stop="truncate") as path:
            code, out = run_cli("-c", path, "run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertIn(f"[WAIT] {MOVIE}  -- the seeding Freebox task could not be stopped: cannot reach Freebox API", out)
        self.assertEqual(self.nas.snapshot(), before)

    def test_low_free_space_stops_nothing(self):
        files = self.movie_download()
        before = self.nas.snapshot()
        with self.env([task(1, MOVIE, "seeding")], files=files, safety={"min_free_mb": 999999999}) as path:
            code, out = run_cli("-c", path, "run", "--apply")
        self.assertEqual(code, 3, out)  # EXIT_PRECONDITION, as without the Freebox
        self.assertEqual(self.puts(), [])
        self.assertIn("is seeding: not stopped, this run cannot file it (only", out)
        self.assertEqual(self.nas.snapshot(), before)

    def test_mount_gone_before_the_stop_stops_nothing(self):
        from mediaorg.guards import PreconditionError

        files = self.movie_download()
        before = self.nas.snapshot()
        calls = []

        def check_mount(cfg):  # mounted when the run starts, gone afterwards
            calls.append(1)
            if len(calls) > 1:
                raise PreconditionError("the share is not mounted")

        with self.env([task(1, MOVIE, "seeding")], files=files) as path, \
                mock.patch("mediaorg.guards.check_mount", side_effect=check_mount):
            code, out = run_cli("-c", path, "run", "--apply")
        self.assertEqual(code, 3, out)
        self.assertEqual(self.puts(), [])
        self.assertIn("not stopped, this run cannot file it (the share is not mounted)", out)
        self.assertEqual(self.nas.snapshot(), before)

    def test_mixed_run_files_only_what_stopped(self):
        other = "Lanternfall.2026.1080p.WEB.H264-GRP"
        files = self.movie_download()
        touch(f"{self.nas.dl}/{other}/{other}.mkv", b"other")
        age_tree(f"{self.nas.dl}/{other}")
        files[2] = [{"filepath": b64(f"{REMOTE}{other}/{other}.mkv"), "size": 5, "rx": 5, "status": "done",
                     "priority": "normal"}]
        with self.env([task(1, MOVIE, "seeding"), task(2, other, "seeding")], files=files) as path:
            self.fb.refuse_ids = {2}
            code, out = run_cli("-c", path, "run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertTrue(self.filed(), out)
        self.assertIn(f"[WAIT] {other}  -- Freebox task 2 could not be stopped", out)
        self.assertTrue(os.path.exists(f"{self.nas.dl}/{other}/{other}.mkv"))
        self.assertFalse(os.path.exists(f"{self.nas.movies}/Lanternfall (2026)"))

    def test_unfinished_download_is_never_stopped(self):
        files = self.movie_download()
        with self.env([task(1, MOVIE, "downloading", rx_pct=4000)], files=files) as path:
            code, out = run_cli("-c", path, "run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.puts(), [])
        self.assertIn("Freebox task is downloading", out)

    def test_run_that_cannot_act_stops_nothing(self):
        files = self.movie_download(with_subtitle=True)  # video + subtitle: 2 moves, above a cap of 1
        before = self.nas.snapshot()
        with self.env([task(1, MOVIE, "seeding")], files=files) as path:
            code, out = run_cli("-c", path, "run", "--apply", "--max-moves", "1")
        self.assertEqual(code, 6, out)  # EXIT_CAP
        self.assertEqual(self.puts(), [])
        self.assertIn("is seeding: stopped when the download can be filed", out)
        self.assertEqual(self.nas.snapshot(), before)

    def test_skipped_download_keeps_seeding(self):
        name = "Corvid.2018.1080p.WEB.H264-GRP"  # the library has 'Corvid (2019)': one year off, left for you
        touch(f"{self.nas.dl}/{name}/{name}.mkv", b"x")
        age_tree(f"{self.nas.dl}/{name}")
        files = {1: [{"filepath": b64(f"{REMOTE}{name}/{name}.mkv"), "size": 1, "rx": 1, "status": "done",
                      "priority": "normal"}]}
        with self.env([task(1, name, "seeding")], files=files) as path:
            code, out = run_cli("-c", path, "run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.puts(), [], out)  # only what is filed is stopped
        self.assertTrue(os.path.exists(f"{self.nas.dl}/{name}/{name}.mkv"))

    def test_check_accepts_ratio_zero_with_stop(self):
        with self.env([], stop_ratio=0) as path:
            code, out = run_cli("-c", path, "check")
        self.assertNotIn("[FAIL] BitTorrent stop ratio", out)
        self.assertIn("[ok] completeness.on_seeding = stop", out)
        with self.env([], on_seeding="wait", stop_ratio=0) as path:
            code, out = run_cli("-c", path, "check")
        self.assertIn("[FAIL] BitTorrent stop ratio 0 (0 = seed forever", out)


class TestStopSeedingUnit(unittest.TestCase):
    def setUp(self):
        self.nas = FakeNas()
        name = "Seeding.Show.S01E01.mkv"
        p = touch(os.path.join(self.nas.dl, name))
        st = os.stat(p)
        self.item = Item(name, p, False, [FileEntry(p, name, name, ".mkv", st.st_size, st.st_mtime)],
                         newest_mtime=st.st_mtime)
        self.fb = FakeFreebox(tasks=[task(2, name, "seeding")], download_dir=REMOTE.rstrip("/"))
        self.client = FreeboxClient(self.fb.url, None)
        self.client.login("mediaorg", FakeFreebox.APP_TOKEN)

    def tearDown(self):
        self.fb.close()
        self.nas.cleanup()

    def state(self, on_seeding):
        cfg = self.nas.config(completeness={"method": "freebox", "on_seeding": on_seeding},
                              freebox={"remote_downloads_dir": REMOTE, "base_url": self.fb.url,
                                       "allow_insecure_http": True})
        return comp.FreeboxState(cfg, self.client)

    def test_readiness_names_the_tasks_to_stop(self):
        r = self.state("stop").assess(self.item)
        self.assertTrue(r.ready)
        self.assertEqual(r.stop_tasks, frozenset({2}))
        r = self.state("wait").assess(self.item)
        self.assertFalse(r.ready)
        self.assertEqual(r.stop_tasks, frozenset())

    def test_partial_seeding_task_is_not_stoppable(self):
        self.fb.tasks[0]["rx_pct"] = 9000  # never seen for a seeding task, but never stopped either
        self.assertFalse(self.state("stop").assess(self.item).ready)

    def test_stop_seeding_gone_task(self):
        ok, why = comp.stop_seeding(self.client, [99], timeout_s=0)
        self.assertFalse(ok)
        self.assertIn("Freebox task 99 is gone", why)

    def test_no_task_is_stopped_unless_all_can_be(self):
        self.fb.tasks.append(task(3, "Other.Task", "checking"))
        ok, why = comp.stop_seeding(self.client, [2, 3], timeout_s=0)
        self.assertFalse(ok)
        self.assertIn("Freebox task 3 is now 'checking'", why)
        self.assertEqual([r for r in self.fb.requests if r[0] == "PUT"], [])
        self.assertEqual(self.fb.tasks[0]["status"], "seeding")

    def test_a_refusal_after_a_stop_is_reported(self):
        self.fb.tasks.append(task(3, "Other.Task", "seeding"))
        self.fb.refuse_ids = {3}
        with self.assertLogs("mediaorg.completeness", "WARNING") as logs:
            ok, why = comp.stop_seeding(self.client, [2, 3], timeout_s=0)
        self.assertFalse(ok)
        self.assertIn("Freebox task 3 could not be stopped", why)
        self.assertIn("Freebox task(s) 2 stopped, but not task 3", logs.output[0])

    def test_stop_seeding_rereads_the_state_first(self):
        self.fb.tasks[0]["status"] = "checking"  # changed since the run read it: never stopped
        ok, why = comp.stop_seeding(self.client, [2], timeout_s=0)
        self.assertFalse(ok)
        self.assertIn("Freebox task 2 is now 'checking'", why)
        self.fb.tasks[0]["status"] = "stopped"  # stopped meanwhile (by hand, or for another item of this run)
        ok, _ = comp.stop_seeding(self.client, [2], timeout_s=0)
        self.assertTrue(ok)
        self.assertEqual([r for r in self.fb.requests if r[0] == "PUT"], [])

    def test_stop_seeding_waits_for_the_stopped_state(self):
        self.fb.on_stop = "stopping"
        calls = []

        def sleep(s):
            calls.append(s)
            self.fb.tasks[0]["status"] = "stopped"  # the Freebox finishes stopping meanwhile

        ok, text = comp.stop_seeding(self.client, [2], timeout_s=30, sleep=sleep)
        self.assertTrue(ok, text)
        self.assertEqual(calls, [1])


class TestOnSeedingConfig(unittest.TestCase):
    def test_values(self):
        nas = FakeNas()
        try:
            self.assertEqual(nas.config(completeness={"on_seeding": "stop"}).completeness.on_seeding, "stop")
            with self.assertRaises(C.ConfigError):
                nas.config(completeness={"on_seeding": "pause"})
        finally:
            nas.cleanup()

    def test_check_warns_without_the_freebox(self):
        nas = FakeNas()
        try:
            with cli_env(nas, completeness={"on_seeding": "stop"}) as path:
                _, out = run_cli("-c", path, "check")
            self.assertIn("[warn] completeness.on_seeding = stop has no effect: it needs completeness.method", out)
        finally:
            nas.cleanup()


class TestStagingNames(unittest.TestCase):
    def setUp(self):
        self.nas = FakeNas()
        key = os.path.join(self.nas.tmp, "tmm.key")
        with open(key, "w") as fh:
            fh.write("0123456789abcdef")
        os.chmod(key, 0o600)
        self.tmm = {"enabled": True, "base_url": "http://127.0.0.1:9", "api_key_file": key}

    def tearDown(self):
        self.nas.cleanup()

    def plan(self, staging=True, tmm=True):
        cfg = self.nas.config(naming={"tmm_staging": staging}, **({"tmm": self.tmm} if tmm else {}))
        items, _ = scan_downloads(cfg)
        planner = Planner(cfg, Library(cfg.paths.movies, cfg.paths.series, cfg.media.video_extensions), "R")
        return {p.item.name: p for p in (planner.plan_item(i, Readiness(True, "test")) for i in items)}

    def test_folder_name_helpers(self):
        self.assertEqual(split_folder_name(f"Rise of the Conqueror (2026) {STAGING_TAG}"), ("Rise of the Conqueror", 2026))
        self.assertEqual(split_folder_name(f"Signal One {STAGING_TAG}"), ("Signal One", None))
        self.assertEqual(unstaged("Corvid (2019) [MediaOrg]"), "Corvid (2019)")
        self.assertFalse(is_staged("Corvid [mediaorg] (2019)"))  # only a trailing tag is mediaorg's

    def test_new_movie_and_show_folders_are_staged(self):
        touch(f"{self.nas.dl}/{MOVIE}.mkv")
        touch(f"{self.nas.dl}/Vellum.Gate.S01E01.1080p.WEB.h264-GRP.mkv")
        plans = self.plan()
        movie = plans[f"{MOVIE}.mkv"].decisions[0]
        self.assertEqual(movie.target, f"{self.nas.movies}/Rookfield (2026) {STAGING_TAG}/{MOVIE}.mkv")
        show = plans["Vellum.Gate.S01E01.1080p.WEB.h264-GRP.mkv"].decisions[0]
        self.assertEqual(show.target, f"{self.nas.series}/Vellum Gate {STAGING_TAG}/Season 1/"
                                      "Vellum.Gate.S01E01.1080p.WEB.h264-GRP.mkv")

    def test_no_staging_without_tmm_or_when_off(self):
        touch(f"{self.nas.dl}/{MOVIE}.mkv")
        for kwargs in ({"staging": False}, {"tmm": False}):
            d = self.plan(**kwargs)[f"{MOVIE}.mkv"].decisions[0]
            self.assertEqual(d.target, f"{self.nas.movies}/Rookfield (2026)/{MOVIE}.mkv", kwargs)

    def test_existing_movie_and_show_folders_are_used_as_they_are(self):
        touch(f"{self.nas.dl}/Corvid.2019.1080p.BluRay.x264-OTHER.mkv", b"other bytes")
        touch(f"{self.nas.dl}/Cistern.S03E02.1080p.WEB.H264-GRP.mkv")
        plans = self.plan()
        self.assertEqual(plans["Corvid.2019.1080p.BluRay.x264-OTHER.mkv"].decisions[0].action, "duplicate")
        self.assertEqual(plans["Cistern.S03E02.1080p.WEB.H264-GRP.mkv"].decisions[0].target,
                         f"{self.nas.series}/Cistern (2022)/Season 3/Cistern.S03E02.1080p.WEB.H264-GRP.mkv")

    def test_staged_folders_are_matched_until_tmm_renames_them(self):
        staged = f"{self.nas.movies}/Rookfield (2026) {STAGING_TAG}"
        touch(f"{staged}/{MOVIE}.mkv", b"first copy")
        touch(f"{self.nas.series}/Vellum Gate {STAGING_TAG}/Season 1/Vellum.Gate.S01E01.1080p.WEB.h264-GRP.mkv")
        touch(f"{self.nas.dl}/Rookfield.2026.2160p.WEB.H265-OTHER.mkv", b"second copy")
        touch(f"{self.nas.dl}/Vellum.Gate.S01E02.1080p.WEB.h264-GRP.mkv")
        plans = self.plan()
        dup = plans["Rookfield.2026.2160p.WEB.H265-OTHER.mkv"].decisions[0]
        self.assertEqual(dup.action, "duplicate", dup.reason)
        ep = plans["Vellum.Gate.S01E02.1080p.WEB.h264-GRP.mkv"].decisions[0]
        self.assertEqual(ep.target, f"{self.nas.series}/Vellum Gate {STAGING_TAG}/Season 1/"
                                    "Vellum.Gate.S01E02.1080p.WEB.h264-GRP.mkv")

    def test_episodes_of_a_new_show_in_one_run_share_its_staged_folder(self):
        for e in (1, 2, 3):
            touch(f"{self.nas.dl}/Vellum.Gate.2025.S01E0{e}.1080p.WEB.h264-GRP.mkv")
        plans = self.plan()
        targets = {os.path.dirname(p.decisions[0].target) for p in plans.values()}
        self.assertEqual(targets, {f"{self.nas.series}/Vellum Gate (2025) {STAGING_TAG}/Season 1"})

    def test_name_too_long_with_the_tag_is_skipped(self):
        title = "Very." * 47 + "Long"  # 'Very Very ... Long (2026)' fits 255 bytes, not with the tag
        touch(f"{self.nas.dl}/{title}.2026.mkv")
        d = next(iter(self.plan().values())).decisions[0]
        self.assertEqual(d.action, "skip")
        self.assertIn("cannot build movie folder name: name longer than 255 bytes", d.reason)
        d = next(iter(self.plan(staging=False).values())).decisions[0]
        self.assertEqual(d.action, "move", d.reason)

    def test_apply_creates_the_staged_folder(self):
        touch(f"{self.nas.dl}/{MOVIE}.mkv")
        with cli_env(self.nas, naming={"tmm_staging": True}, tmm=self.tmm) as path, \
                mock.patch("mediaorg.cli._tmm_send"):
            code, out = run_cli("-c", path, "run", "--apply", "--no-tmm")
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.exists(f"{self.nas.movies}/Rookfield (2026) {STAGING_TAG}/{MOVIE}.mkv"), out)
        self.assertIn(f"new movie folder 'Rookfield (2026) {STAGING_TAG}'", out)


class TestStagingCheck(unittest.TestCase):
    def setUp(self):
        self.nas = FakeNas()
        key = os.path.join(self.nas.tmp, "tmm.key")
        with open(key, "w") as fh:
            fh.write("0123456789abcdef")
        os.chmod(key, 0o600)
        self.tmm = {"enabled": True, "base_url": "http://127.0.0.1:9", "api_key_file": key}

    def tearDown(self):
        self.nas.cleanup()

    def check(self, **overrides):
        with cli_env(self.nas, **overrides) as path:
            return run_cli("-c", path, "check")

    def test_smb_library_with_tmm_advises_staging(self):
        _, out = self.check(tmm=self.tmm)
        self.assertIn("[warn] the library is on an SMB share (cifs): tMM on Linux cannot rename a folder whose new "
                      "name differs only in letter case; to avoid it: sudo mediaorg config set "
                      "naming.tmm_staging=true", out)
        _, out = self.check(tmm=self.tmm, naming={"tmm_staging": True})
        self.assertIn(f"[ok] naming.tmm_staging: new movie and show folders end with ' {STAGING_TAG}'", out)
        self.assertNotIn("SMB share", out)

    def test_staging_without_tmm_warns(self):
        _, out = self.check(naming={"tmm_staging": True})
        self.assertIn("[warn] naming.tmm_staging is set but tMM is disabled", out)

    def test_folders_tmm_left_staged_are_reported(self):
        old = f"{self.nas.movies}/Rookfield (2026) {STAGING_TAG}"
        new = f"{self.nas.series}/Vellum Gate {STAGING_TAG}"
        touch(f"{old}/Rookfield.2026.1080p.mkv")
        touch(f"{new}/Season 1/Vellum.Gate.S01E01.mkv")
        os.utime(old, (OLD - 86400, OLD - 86400))  # unchanged for more than a day
        _, out = self.check(tmm=self.tmm, naming={"tmm_staging": True})
        self.assertIn(f"[warn] 1 folder(s) still end with ' {STAGING_TAG}' after a day", out)
        self.assertIn(f"'Movies/Rookfield (2026) {STAGING_TAG}'", out)
        self.assertNotIn("Vellum Gate", out)

    def test_status_mentions_staging(self):
        with cli_env(self.nas, tmm=self.tmm, naming={"tmm_staging": True}) as path:
            _, out = run_cli("-c", path, "status")
        self.assertIn("new folders end with ' [mediaorg]' until tMM renames them", out)


if __name__ == "__main__":
    unittest.main()
