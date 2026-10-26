"""Freebox completeness and tMM notification, against local fakes of the documented APIs."""

import json
import os
import socket
import time
import unittest

from mediaorg import completeness as comp
from mediaorg.freebox import FreeboxClient, FreeboxError, authorize_interactive, load_token
from mediaorg.model import Item, FileEntry
from mediaorg.tmm import TmmClient, TmmError, TmmUnavailable, render, to_tmm_path

from .fakes import FakeFreebox, FakeTmm, b64
from .helpers import FakeNas, cli_env, run_cli, touch

REMOTE = "/Freebox/Downloads/"


def task(tid, name, status, rx_pct=10000, ddir=REMOTE):
    return {"id": tid, "name": name, "status": status, "rx_pct": rx_pct, "size": 100, "type": "bt",
            "download_dir": b64(ddir), "stop_ratio": 150, "error": "none"}


class TestFreeboxClient(unittest.TestCase):
    def setUp(self):
        self.fb = FakeFreebox(tasks=[task(1, "Show.S01E01.mkv", "done")])

    def tearDown(self):
        self.fb.close()

    def test_login_uses_hmac_and_lists_tasks(self):
        c = FreeboxClient(self.fb.url, None)
        c.login("mediaorg", FakeFreebox.APP_TOKEN)
        tasks = c.tasks()
        self.assertEqual(tasks[0].download_dir, REMOTE)
        self.assertEqual(tasks[0].status, "done")
        c.logout()

    def test_wrong_token_rejected(self):
        with self.assertRaises(FreeboxError):
            FreeboxClient(self.fb.url, None).login("mediaorg", "wrong")

    def test_missing_downloader_permission(self):
        self.fb.permissions = {"settings": True}
        with self.assertRaises(FreeboxError) as ctx:
            FreeboxClient(self.fb.url, None).login("mediaorg", FakeFreebox.APP_TOKEN)
        self.assertIn("downloader", str(ctx.exception))

    def test_authorize_flow_and_token_file_mode(self):
        d = FakeNas()
        try:
            path = os.path.join(d.state, "fbx.json")
            authorize_interactive(FreeboxClient(self.fb.url, None), path, "mediaorg", "mediaorg", "0.1.0",
                                  "host", out=lambda *a: None, sleep=lambda s: None)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(load_token(path)["app_token"], FakeFreebox.APP_TOKEN)
            with self.assertRaises(FreeboxError):  # never overwrites an existing token
                authorize_interactive(FreeboxClient(self.fb.url, None), path, "a", "b", "c", "d",
                                      out=lambda *a: None, sleep=lambda s: None)
        finally:
            d.cleanup()

    def test_unreachable(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with self.assertRaises(FreeboxError):
            FreeboxClient(f"http://127.0.0.1:{port}", None, timeout=2).api_version()


class TestFreeboxCompleteness(unittest.TestCase):
    def setUp(self):
        self.nas = FakeNas()
        old = time.time() - 7200
        self.items = {}
        for name in ("Done.Show.S01E01.mkv", "Seeding.Show.S01E01.mkv", "Downloading.Show.S01E01.mkv",
                     "Stopped.Full.S01E01.mkv", "Stopped.Partial.S01E01.mkv", "Manual.Copy.S01E01.mkv",
                     "Fresh.Manual.S01E01.mkv"):
            p = touch(os.path.join(self.nas.dl, name), mtime=old if name != "Fresh.Manual.S01E01.mkv" else time.time())
            st = os.stat(p)
            self.items[name] = Item(name, p, False, [FileEntry(p, name, name, ".mkv", st.st_size, st.st_mtime)],
                                    newest_mtime=st.st_mtime)
        self.fb = FakeFreebox(tasks=[
            task(1, "Done.Show.S01E01.mkv", "done"),
            task(2, "Seeding.Show.S01E01.mkv", "seeding"),
            task(3, "Downloading.Show.S01E01.mkv", "downloading", 4000),
            task(4, "Stopped.Full.S01E01.mkv", "stopped", 10000),
            task(5, "Stopped.Partial.S01E01.mkv", "stopped", 5000),
            task(6, "Done.Show.S01E01.mkv", "done", ddir="/Freebox/Other/"),  # other folder: ignored
        ], files={1: [{"filepath": b64(REMOTE + "Done.Show.S01E01.mkv"), "size": 10, "rx": 10,
                       "status": "done", "priority": "normal"}]})
        self.cfg = self.nas.config(completeness={"method": "freebox"},
                                   freebox={"remote_downloads_dir": REMOTE, "base_url": self.fb.url,
                                            "allow_insecure_http": True})

    def tearDown(self):
        self.fb.close()
        self.nas.cleanup()

    def test_status_mapping(self):
        client = FreeboxClient(self.fb.url, None)
        client.login("mediaorg", FakeFreebox.APP_TOKEN)
        st = comp.FreeboxState(self.cfg, client)
        r = {n: st.assess(i) for n, i in self.items.items()}
        self.assertTrue(r["Done.Show.S01E01.mkv"].ready)
        self.assertFalse(r["Seeding.Show.S01E01.mkv"].ready)
        self.assertIn("seeding", r["Seeding.Show.S01E01.mkv"].reason)
        self.assertFalse(r["Downloading.Show.S01E01.mkv"].ready)
        self.assertTrue(r["Stopped.Full.S01E01.mkv"].ready)
        self.assertFalse(r["Stopped.Partial.S01E01.mkv"].ready)
        self.assertFalse(r["Manual.Copy.S01E01.mkv"].ready)  # not a task: default untracked = wait
        self.assertIn("not a Freebox download task", r["Manual.Copy.S01E01.mkv"].reason)

    def _state(self, untracked="quiescence"):
        cfg = self.nas.config(completeness={"method": "freebox", "untracked": untracked},
                              freebox={"remote_downloads_dir": REMOTE, "base_url": self.fb.url,
                                       "allow_insecure_http": True})
        client = FreeboxClient(self.fb.url, None)
        client.login("mediaorg", FakeFreebox.APP_TOKEN)
        return comp.FreeboxState(cfg, client)

    def _loose(self, name):
        p = touch(os.path.join(self.nas.dl, name), mtime=time.time() - 7200)
        st = os.stat(p)
        return Item(name, p, False, [FileEntry(p, name, name, ".mkv", st.st_size, st.st_mtime)],
                    newest_mtime=st.st_mtime)

    def test_task_name_without_extension_is_mapped_by_its_files(self):
        # seen with a real Freebox: magnet display name "X" for the single file "X.mkv"
        item = self._loose("NightLoom.S01E04.1080p.x265-ToRuS.mkv")
        self.fb.tasks.append(task(7, "NightLoom.S01E04.1080p.x265-ToRuS", "seeding"))
        self.fb.files[7] = [{"filepath": b64(REMOTE + "NightLoom.S01E04.1080p.x265-ToRuS.mkv"), "size": 10,
                             "rx": 10, "status": "done", "priority": "normal"}]
        r = self._state().assess(item)
        self.assertFalse(r.ready)  # 0.1.x-1.0.0: "not a Freebox task" -> moved after 30 quiet minutes
        self.assertIn("seeding", r.reason)
        self.fb.tasks[-1]["status"] = "done"
        r = self._state().assess(item)
        self.assertTrue(r.ready)
        self.assertIn("Freebox: done", r.reason)

    def test_task_name_without_extension_and_no_file_list_still_maps(self):
        item = self._loose("Some.Show.S02E01.1080p.WEB.mkv")
        self.fb.tasks.append(task(8, "Some.Show.S02E01.1080p.WEB", "seeding"))  # files not known yet
        r = self._state().assess(item)
        self.assertFalse(r.ready)
        self.assertIn("seeding", r.reason)

    def test_task_whose_files_sit_in_a_folder_maps_to_that_folder(self):
        d = os.path.join(self.nas.dl, "Pack.S01.1080p")
        p = touch(os.path.join(d, "Pack.S01E01.1080p.mkv"), mtime=time.time() - 7200)
        st = os.stat(p)
        item = Item("Pack.S01.1080p", d, True, [FileEntry(p, "Pack.S01E01.1080p.mkv", "Pack.S01E01.1080p.mkv",
                                                         ".mkv", st.st_size, st.st_mtime)], newest_mtime=st.st_mtime)
        self.fb.tasks.append(task(9, "Some Display Name", "seeding"))
        self.fb.files[9] = [{"filepath": b64(REMOTE + "Pack.S01.1080p/Pack.S01E01.1080p.mkv"), "size": 10,
                             "rx": 10, "status": "done", "priority": "normal"}]
        r = self._state().assess(item)
        self.assertFalse(r.ready)
        self.assertIn("seeding", r.reason)

    def test_untracked_quiescence_opt_in(self):
        cfg = self.nas.config(completeness={"method": "freebox", "untracked": "quiescence"},
                              freebox={"remote_downloads_dir": REMOTE, "base_url": self.fb.url,
                                       "allow_insecure_http": True})
        client = FreeboxClient(self.fb.url, None)
        client.login("mediaorg", FakeFreebox.APP_TOKEN)
        st = comp.FreeboxState(cfg, client)
        self.assertTrue(st.assess(self.items["Manual.Copy.S01E01.mkv"]).ready)
        self.assertFalse(st.assess(self.items["Fresh.Manual.S01E01.mkv"]).ready)

    def test_partially_received_file_is_blocked(self):
        self.fb.files[1] = [{"filepath": b64(REMOTE + "Done.Show.S01E01.mkv"), "size": 10, "rx": 4,
                             "status": "queued", "priority": "no_dl"}]
        client = FreeboxClient(self.fb.url, None)
        client.login("mediaorg", FakeFreebox.APP_TOKEN)
        r = comp.FreeboxState(self.cfg, client).assess(self.items["Done.Show.S01E01.mkv"])
        self.assertIn(os.path.join(self.nas.dl, "Done.Show.S01E01.mkv"), r.blocked_files)

    def test_run_fails_closed_when_freebox_unreachable(self):
        self.fb.close()
        token = os.path.join(self.nas.state, "tok.json")
        os.makedirs(self.nas.state, exist_ok=True)
        with open(token, "w") as fh:
            json.dump({"app_id": "mediaorg", "app_token": "x"}, fh)
        os.chmod(token, 0o600)
        before = self.nas.snapshot()
        with cli_env(self.nas, completeness={"method": "freebox"},
                     freebox={"remote_downloads_dir": REMOTE, "base_url": self.fb.url, "allow_insecure_http": True,
                              "token_file": token, "timeout_seconds": 2}) as path:
            code, _ = run_cli("-c", path, "run", "--apply")
        self.assertEqual(code, 3)
        self.assertEqual(self.nas.snapshot(), before)
        self.fb = FakeFreebox()  # for tearDown


class TestTmm(unittest.TestCase):
    def setUp(self):
        self.tmm = FakeTmm()
        self.nas = FakeNas()
        self.keyfile = os.path.join(self.nas.tmp, "tmm.key")
        with open(self.keyfile, "w") as fh:
            fh.write(FakeTmm.API_KEY + "\n")
        os.chmod(self.keyfile, 0o600)
        self.tmm_cfg = {"enabled": True, "base_url": self.tmm.url, "api_key_file": self.keyfile,
                        "path_map": [[self.nas.movies, "/media/movies"], [self.nas.series, "/media/series"]],
                        # targeted variant (opt-in; the default updates the whole library)
                        "after_move_tvshow": [{"action": "update", "scope": {"name": "show", "args": "{paths}"}},
                                              {"action": "scrape", "scope": {"name": "unscraped"}}]}

    def tearDown(self):
        self.tmm.close()
        self.nas.cleanup()

    def test_path_mapping_and_render(self):
        pm = ((self.nas.series, "/media/series"),)
        self.assertEqual(to_tmm_path(os.path.join(self.nas.series, "Cistern (2022)"), pm), "/media/series/Cistern (2022)")
        self.assertIsNone(to_tmm_path("/elsewhere/x", pm))
        cmds = render([{"action": "update", "scope": {"name": "show", "args": "{paths}"}}], ["/media/series/A"])
        self.assertEqual(cmds[0]["scope"]["args"], ["/media/series/A"])

    def test_targeted_refresh_after_moves(self):
        touch(os.path.join(self.nas.dl, "Cistern.S03E06.1080p.WEB.h264-GRP.mkv"))
        with cli_env(self.nas, tmm=self.tmm_cfg) as path:
            code, out = run_cli("-c", path, "run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.tmm.posts), 1)
        endpoint, body = self.tmm.posts[0]
        self.assertEqual(endpoint, "/api/tvshow")
        self.assertEqual(body[0], {"action": "update", "scope": {"name": "show", "args": ["/media/series/Cistern (2022)"]}})
        self.assertEqual(body[1]["action"], "scrape")

    def test_busy_tmm_defers_moves(self):
        self.tmm.busy = True
        touch(os.path.join(self.nas.dl, "Cistern.S03E06.1080p.WEB.h264-GRP.mkv"))
        before = self.nas.snapshot()
        with cli_env(self.nas, tmm=self.tmm_cfg) as path:
            code, _ = run_cli("-c", path, "run", "--apply")
        self.assertEqual(code, 0)
        self.assertEqual(self.nas.snapshot(), before)

    def test_tmm_down_keeps_refresh_pending(self):
        self.tmm.close()
        touch(os.path.join(self.nas.dl, "Cistern.S03E06.1080p.WEB.h264-GRP.mkv"))
        with cli_env(self.nas, tmm=self.tmm_cfg) as path:
            code, _ = run_cli("-c", path, "run", "--apply")
        self.assertEqual(code, 0)  # moves are not blocked by tMM being down
        self.assertTrue(self.nas.exists("Series/Cistern (2022)/Season 3/Cistern.S03E06.1080p.WEB.h264-GRP.mkv"))
        with open(os.path.join(self.nas.state, "tmm_pending.json")) as fh:
            self.assertEqual(json.load(fh), {"tvshow": [os.path.join(self.nas.series, "Cistern (2022)")]})
        # tMM back: the next run (nothing new to move) flushes the pending refresh
        self.tmm = FakeTmm()
        self.tmm_cfg["base_url"] = self.tmm.url
        with cli_env(self.nas, tmm=self.tmm_cfg) as path:
            code, _ = run_cli("-c", path, "run", "--apply")
        self.assertEqual(len(self.tmm.posts), 1)
        with open(os.path.join(self.nas.state, "tmm_pending.json")) as fh:
            self.assertEqual(json.load(fh), {})

    def test_status_404_on_old_tmm(self):
        self.tmm.system = False
        from mediaorg.config import from_dict
        cfg = from_dict(self.nas.config_dict(tmm=self.tmm_cfg)).tmm
        self.assertIsNone(TmmClient(cfg, FakeTmm.API_KEY).status())
        # no /api/system before tMM 5.3.5: 404 before any key check; the key is checked on /api/docs
        self.assertIsNone(TmmClient(cfg, "wrong-key").status())
        with self.assertRaisesRegex(TmmError, r'HTTP 403 on docs: .*Invalid API key'):
            TmmClient(cfg, "wrong-key").api_paths()

    def test_wrong_key(self):
        from mediaorg.config import from_dict
        cfg = from_dict(self.nas.config_dict(tmm=self.tmm_cfg)).tmm
        with self.assertRaisesRegex(TmmError, r'HTTP 403 on system: .*Invalid API key'):
            TmmClient(cfg, "wrong-key").status()

    def test_unavailable(self):
        self.tmm.close()
        from mediaorg.config import from_dict
        cfg = from_dict(self.nas.config_dict(tmm=self.tmm_cfg)).tmm
        with self.assertRaises(TmmUnavailable):
            TmmClient(cfg, FakeTmm.API_KEY).status()
        self.tmm = FakeTmm()


if __name__ == "__main__":
    unittest.main()
