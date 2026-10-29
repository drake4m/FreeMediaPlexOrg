"""Bounded state and logs (the disk never fills up), the free-space guard, `status` and `check` advice."""

import json
import logging
import logging.handlers
import os
import shutil
import stat
import tempfile
import time
import unittest
from unittest import mock

from mediaorg import __version__
from mediaorg.guards import MountEntry
from mediaorg.report import prune_reports
from mediaorg.transfer import Journal, prune_journals

from .helpers import FakeNas, cli_env, run_cli, touch

DAY = 86400


class TestPruning(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def report(self, i: int, age_days: float = 0) -> str:
        path = os.path.join(self.dir, f"2026{i // 28 + 1:02d}{i % 28 + 1:02d}T120000Z-{i:04x}.json")
        with open(path, "w") as fh:
            fh.write("{}")
        t = time.time() - age_days * DAY
        os.utime(path, (t, t))
        return path

    def test_reports_capped_by_count_oldest_first(self):
        paths = [self.report(i) for i in range(12)]
        touch(os.path.join(self.dir, "notes.json"), b"mine")  # not a report: never touched
        self.assertEqual(prune_reports(self.dir, 0, max_reports=5), 7)
        left = sorted(os.listdir(self.dir))
        self.assertEqual(left, sorted([os.path.basename(p) for p in paths[-5:]] + ["notes.json"]))

    def test_reports_by_age_then_count(self):
        for i in range(3):
            self.report(i, age_days=40)
        for i in range(3, 10):
            self.report(i)
        self.assertEqual(prune_reports(self.dir, 30, max_reports=4), 6)
        self.assertEqual(len(os.listdir(self.dir)), 4)

    def journal(self, run_id: str, close: str | None, age_days: float) -> str:
        j = Journal(self.dir, run_id)
        j.write({"state": "begin", "kind": "run"})
        if close == "ok":
            j.write({"state": "end", "status": "ok"})
        elif close == "aborted":
            j.write({"state": "end", "status": "aborted", "needs_reconcile": True})
        j.close()
        t = time.time() - age_days * DAY
        os.utime(j.path, (t, t))
        return j.path

    def test_journals_of_finished_runs_only(self):
        old_ok = self.journal("20250101T000000Z-aaaa", "ok", 400)
        old_undo = self.journal("20250101T000001Z-bbbb-undo", "ok", 400)
        old_open = self.journal("20250101T000002Z-cccc", None, 400)  # interrupted: still needs reconciling
        old_aborted = self.journal("20250101T000003Z-dddd", "aborted", 400)
        recent = self.journal("20261001T000000Z-eeee", "ok", 10)
        foreign = touch(os.path.join(self.dir, "keep-me.jsonl"), b"{}")
        os.utime(foreign, (time.time() - 400 * DAY,) * 2)
        self.assertEqual(prune_journals(self.dir, 365), 2)
        for p in (old_open, old_aborted, recent, foreign):
            self.assertTrue(os.path.exists(p), p)
        for p in (old_ok, old_undo):
            self.assertFalse(os.path.exists(p), p)
        self.assertEqual(prune_journals(self.dir, 0), 0)  # 0 = keep forever


class TestRunHousekeeping(unittest.TestCase):
    def setUp(self):
        self.nas = FakeNas()

    def tearDown(self):
        root = logging.getLogger()
        for h in [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]:
            root.removeHandler(h)
            h.close()
        self.nas.cleanup()

    def test_too_little_space_stops_before_moving(self):
        src = touch(os.path.join(self.nas.dl, "Cistern.S03E07.1080p.WEB.h264-GRP.mkv"))
        with cli_env(self.nas, safety={"min_free_mb": 500}) as path, \
                mock.patch("mediaorg.cli.shutil.disk_usage", return_value=mock.Mock(free=100 * 1024 * 1024)):
            code, _ = run_cli("-c", path, "run", "--apply")
        self.assertEqual(code, 3)
        self.assertTrue(os.path.exists(src))
        with cli_env(self.nas, safety={"min_free_mb": 500}) as path, \
                mock.patch("mediaorg.cli.shutil.disk_usage", return_value=mock.Mock(free=100 * 1024 * 1024)):
            code, _ = run_cli("-c", path, "run")  # a dry-run only reads: allowed
        self.assertEqual(code, 0)

    def test_apply_prunes_old_journals(self):
        jdir = os.path.join(self.nas.state, "journal")
        j = Journal(jdir, "20250101T000000Z-aaaa")
        j.write({"state": "begin", "kind": "run"})
        j.write({"state": "end", "status": "ok"})
        j.close()
        os.utime(j.path, (time.time() - 400 * DAY,) * 2)
        with cli_env(self.nas) as path:
            code, _ = run_cli("-c", path, "run", "--apply")
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(j.path))

    def test_log_file_rotates_by_size(self):
        log_dir = os.path.join(self.nas.tmp, "log")
        os.makedirs(log_dir)
        log_file = os.path.join(log_dir, "mediaorg.log")
        with cli_env(self.nas, logging={"file": log_file, "max_size_mb": 1, "backups": 2}) as path:
            code, _ = run_cli("-c", path, "run")
        self.assertEqual(code, 0)
        handlers = [h for h in logging.getLogger().handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
        self.assertEqual(len(handlers), 1)
        self.assertEqual((handlers[0].maxBytes, handlers[0].backupCount), (1024 * 1024, 2))
        self.assertTrue(os.path.exists(log_file))
        for _ in range(3000):  # ~3 MB of log lines: never more than (backups + 1) files of 1 MB
            logging.getLogger("mediaorg").warning("x" * 1000)
        files = sorted(os.listdir(log_dir))
        self.assertEqual(files, ["mediaorg.log", "mediaorg.log.1", "mediaorg.log.2"])
        self.assertTrue(all(os.path.getsize(os.path.join(log_dir, f)) <= 1024 * 1024 + 2000 for f in files))


class TestFolderModes(unittest.TestCase):
    def test_new_folders_copy_the_mode_of_their_parent(self):
        from mediaorg.transfer import make_dir
        tmp = tempfile.mkdtemp()
        try:
            for mode in (0o755, 0o775, 0o2775, 0o700):
                parent = os.path.join(tmp, f"p{mode:o}")
                os.mkdir(parent)
                os.chmod(parent, mode)
                make_dir(os.path.join(parent, "Season 1"))
                got = os.stat(os.path.join(parent, "Season 1")).st_mode & 0o7777
                self.assertEqual(got, mode, f"{mode:o} -> {got:o}")
        finally:
            shutil.rmtree(tmp)

    def test_new_folders_keep_a_shared_mode_under_the_sandbox(self):
        # RestrictSUIDSGID= in the units: chmod/mkdir with a set-id bit fail with EPERM (seccomp)
        from mediaorg import transfer

        real_chmod, real_mkdir = os.chmod, os.mkdir

        def no_setid(fn):
            def call(path, mode, *a, **k):
                if mode & (stat.S_ISUID | stat.S_ISGID):
                    raise PermissionError(1, "Operation not permitted", path)
                return fn(path, mode, *a, **k)
            return call

        tmp = tempfile.mkdtemp()
        try:
            for mode in (0o2775, 0o775, 0o2750):
                parent = os.path.join(tmp, f"p{mode:o}")
                os.mkdir(parent)
                os.chmod(parent, mode)
                with mock.patch("mediaorg.transfer.os.chmod", no_setid(real_chmod)), \
                        mock.patch("mediaorg.transfer.os.mkdir", no_setid(real_mkdir)):
                    transfer.make_dir(os.path.join(parent, "Season 1"))
                got = os.stat(os.path.join(parent, "Season 1")).st_mode & 0o7777
                self.assertEqual(got, mode, f"{mode:o} -> {got:o}")
        finally:
            shutil.rmtree(tmp)


class TestStatusAndCheck(unittest.TestCase):
    def setUp(self):
        self.nas = FakeNas()

    def tearDown(self):
        self.nas.cleanup()

    def test_status(self):
        touch(os.path.join(self.nas.dl, "Cistern.S03E07.1080p.WEB.h264-GRP.mkv"))
        with cli_env(self.nas) as path, mock.patch("mediaorg.service.systemd_running", return_value=False):
            run_cli("-c", path, "run", "--apply")
            code, out = run_cli("-c", path, "status")
        self.assertEqual(code, 0, out)
        self.assertIn(f"mediaorg {__version__}", out)
        self.assertIn(f"downloads  {self.nas.dl}", out)
        self.assertIn("finished downloads: unchanged for 30 min", out)
        self.assertIn("systemd is not running here", out)
        self.assertIn("latest report:", out)
        self.assertIn("(apply)", out)

    def test_check_advises_a_mount_check(self):
        mounts = [MountEntry("/", "xfs", "/dev/root", "rw"), MountEntry(self.nas.mp, "cifs", "//nas.example/media", "")]
        with cli_env(self.nas) as path:
            with open(path, encoding="utf-8") as fh:
                raw = fh.read()
            with open(path, "w", encoding="utf-8") as fh:  # the same configuration without [mount]
                fh.write(raw.split("[mount]")[0] + "[completeness]" + raw.split("[completeness]")[1])
            with mock.patch("mediaorg.guards.read_mountinfo", return_value=mounts):
                code, out = run_cli("-c", path, "check")
        self.assertEqual(code, 0, out)
        self.assertIn(f"[warn] no mount check, but the media folders are on the mount {self.nas.mp}", out)
        self.assertIn(f"sudo mediaorg config set mount.mountpoint={self.nas.mp} mount.fstype=cifs "
                      f"mount.source=//nas.example/media", out)
        self.assertIn("[warn] systemd timers not installed", out)

    def test_check_refuses_a_key_others_can_read(self):
        key = os.path.join(self.nas.tmp, "tmm.key")
        with open(key, "w") as fh:
            fh.write("0123456789abcdef")
        os.chmod(key, 0o644)
        with cli_env(self.nas, tmm={"enabled": True, "base_url": "http://127.0.0.1:9", "api_key_file": key}) as path:
            code, out = run_cli("-c", path, "check")
        self.assertEqual(code, 3)
        self.assertIn("is readable by every user; fix: sudo chmod o-rwx", out)

    def test_report_json_has_no_legacy_fields(self):
        touch(os.path.join(self.nas.dl, "Cistern.S03E07.1080p.WEB.h264-GRP.mkv"))
        with cli_env(self.nas) as path:
            run_cli("-c", path, "run")
        runs = os.path.join(self.nas.state, "runs")
        with open(os.path.join(runs, sorted(os.listdir(runs))[-1]), encoding="utf-8") as fh:
            report = json.load(fh)
        self.assertNotIn("legacy", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
