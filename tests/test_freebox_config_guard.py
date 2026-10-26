"""0.1.1: the configured Freebox download folder must match the Freebox's own setting."""

import json
import os
import unittest

from mediaorg import config as C

from .fakes import FakeFreebox, b64
from .helpers import FakeNas, age_tree, cli_env, run_cli, touch

REMOTE = "/Freebox/Downloads"


class Base(unittest.TestCase):
    def setUp(self):
        self.nas = FakeNas()
        self.fb = None

    def tearDown(self):
        if self.fb:
            self.fb.close()
        self.nas.cleanup()

    def env(self, remote=REMOTE, **fb_kwargs):
        self.fb = FakeFreebox(**fb_kwargs)
        tok = os.path.join(self.nas.state, "tok.json")
        os.makedirs(self.nas.state, exist_ok=True)
        with open(tok, "w") as fh:
            json.dump({"app_id": "mediaorg", "app_token": FakeFreebox.APP_TOKEN}, fh)
        os.chmod(tok, 0o600)
        return cli_env(self.nas, completeness={"method": "freebox", "untracked": "quiescence"},
                       freebox={"remote_downloads_dir": remote, "base_url": self.fb.url,
                                "allow_insecure_http": True, "token_file": tok})


class TestGuard(Base):
    def test_placeholder_value_is_rejected_by_config(self):
        with self.assertRaises(C.ConfigError):
            C.from_dict(self.nas.config_dict(freebox={"remote_downloads_dir": "PASTE_DOWNLOAD_DIR_HERE"}))

    def test_run_refuses_when_folder_differs_from_freebox(self):
        # a seeding torrent that would look "untracked" (and quiet) if the mapping were wrong
        name = "Corvid.2019.2160p.UHD.BluRay.x265-GRP"
        touch(f"{self.nas.dl}/{name}/{name}.mkv")
        age_tree(f"{self.nas.dl}/{name}")
        task = {"id": 1, "name": name, "status": "seeding", "rx_pct": 10000, "size": 1, "type": "bt",
                "download_dir": b64(REMOTE + "/"), "stop_ratio": 10, "error": "none"}
        before = self.nas.snapshot()
        with self.env(remote="/Freebox/Wrong/Downloads", tasks=[task]) as path:
            code, _ = run_cli("-c", path, "run", "--apply")
        self.assertEqual(code, 3)
        self.assertEqual(self.nas.snapshot(), before)

    def test_matching_folder_with_trailing_slash_runs(self):
        with self.env(remote=REMOTE + "/") as path:
            code, out = run_cli("-c", path, "run")
        self.assertEqual(code, 0, out)


class TestCheckAndStatus(Base):
    def test_check_all_ok(self):
        with self.env(permissions={"downloader": True}) as path:
            code, out = run_cli("-c", path, "check")
        self.assertEqual(code, 0, out)
        self.assertIn("[ok] Freebox app permissions: downloader only", out)
        self.assertIn("matches the Freebox download folder", out)
        self.assertIn("[ok] BitTorrent stop ratio 0.1", out)

    def test_check_flags_extra_permissions_mismatch_and_seed_forever(self):
        with self.env(remote="/Freebox/Other", stop_ratio=0,
                      permissions={"downloader": True, "vm": True, "explorer": True}) as path:
            code, out = run_cli("-c", path, "check")
        self.assertEqual(code, 3)
        self.assertIn("[warn] Freebox app permissions: also ['explorer', 'vm']", out)
        self.assertIn("[FAIL] freebox.remote_downloads_dir '/Freebox/Other' matches", out)
        self.assertIn("[FAIL] BitTorrent stop ratio 0 (0 = seed forever", out)

    def test_status_shows_download_config(self):
        with self.env() as path:
            code, out = run_cli("-c", path, "freebox-status")
        self.assertEqual(code, 0, out)
        self.assertIn("download folder (freebox.remote_downloads_dir): '/Freebox/Downloads'", out)
        self.assertIn("BitTorrent stop ratio: 0.1", out)


if __name__ == "__main__":
    unittest.main()
