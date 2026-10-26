"""Regression tests for the findings of the 2.0 pre-release review: the review cleaning never reaches
outside the review folder through a link, and a move or an undo whose source vanished leaves no empty
reservation behind and is never counted as done."""

import errno
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from mediaorg import review
from mediaorg.model import Item, ItemPlan, Op
from mediaorg.transfer import Journal, execute, read_journal, run_undo

from .helpers import FakeNas, touch


def rid(seconds_ago: float) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() - seconds_ago)) + "-abcd"


class TestReviewPurgeNeverFollowsLinks(unittest.TestCase):
    def setUp(self):
        self.nas = FakeNas()
        self.cfg = self.nas.config(review={"leftovers_retention_days": 1})
        self.run = os.path.join(self.nas.dl, "_review", "leftovers", rid(3 * 86400))
        self.nfo = touch(os.path.join(self.run, "Some.Release", "info.nfo"), b"release info")
        # a library folder holding a file with the same name and size
        self.victim_dir = os.path.join(self.nas.movies, "Victim (2019)")
        self.victim = touch(os.path.join(self.victim_dir, "info.nfo"), b"release info")

    def tearDown(self):
        self.nas.cleanup()

    def entry(self):
        (e,) = review.due(self.cfg)
        self.assertEqual(e.delete, [("Some.Release/info.nfo", 12)])
        return e

    def test_a_folder_swapped_for_a_link_is_not_followed(self):
        e = self.entry()
        # after the listing, the release folder is replaced by a link into the library
        shutil.rmtree(os.path.join(self.run, "Some.Release"))
        os.symlink(self.victim_dir, os.path.join(self.run, "Some.Release"))
        done, _, errors, deleted = review.purge(e)
        self.assertEqual((done, deleted), (0, []))
        self.assertTrue(os.path.exists(self.victim), "a library file was deleted through a link")
        self.assertTrue(any("is now a link" in err for err in errors), errors)
        self.assertTrue(os.path.islink(os.path.join(self.run, "Some.Release")))  # the link itself stays

    def test_a_run_folder_replaced_since_the_listing_is_left_alone(self):
        e = self.entry()
        os.rename(self.run, self.run + "-moved")
        touch(os.path.join(self.run, "Some.Release", "info.nfo"), b"release info")  # same names, other folder
        done, _, errors, _ = review.purge(e)
        self.assertEqual((done, errors), (0, []))
        self.assertTrue(os.path.exists(os.path.join(self.run, "Some.Release", "info.nfo")))

    def test_a_run_folder_swapped_for_a_link_is_left_alone(self):
        e = self.entry()
        shutil.rmtree(self.run)
        os.symlink(self.victim_dir, self.run)
        done, _, errors, _ = review.purge(e)
        self.assertEqual(done, 0)
        self.assertTrue(os.path.exists(self.victim))
        self.assertTrue(errors and "now a link" in errors[0], errors)

    def test_normal_cleaning_still_removes_files_and_emptied_folders(self):
        e = self.entry()
        done, size, errors, deleted = review.purge(e)
        self.assertEqual((done, size, errors, deleted), (1, 12, [], ["Some.Release/info.nfo"]))
        self.assertFalse(os.path.exists(self.run))  # emptied folders removed, the run folder included
        self.assertTrue(os.path.exists(self.victim))


class TestVanishedSource(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.src = touch(os.path.join(self.tmp, "dl", "Show.S01E01.mkv"), b"video")
        os.makedirs(os.path.join(self.tmp, "lib", "Season 1"))
        self.dst = os.path.join(self.tmp, "lib", "Season 1", "Show.S01E01.mkv")
        self.jdir = os.path.join(self.tmp, "journal")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def execute(self):
        plan = ItemPlan(item=Item(name="Show.S01E01.mkv", path=self.src, is_dir=False), status="ready",
                        ops=[Op("rename", src=self.src, dst=self.dst, role="media", expect_size=5)])
        j = Journal(self.jdir, "r1")
        res = execute([plan], j)
        j.close()
        _, ops, _ = read_journal(j.path)
        return res, ops[max(ops)]["state"]

    def test_source_gone_before_the_move(self):
        def checked_then_gone(op):  # passes the check (returns None), then the file is gone
            os.unlink(self.src)  # removed by the download client right after the last check
        with mock.patch("mediaorg.transfer._verify", side_effect=checked_then_gone):
            res, state = self.execute()
        self.assertEqual((res.done, res.failed, state), (0, 1, "failed"))
        self.assertFalse(os.path.lexists(self.dst), "an empty reservation was left in the library")

    def test_source_gone_during_the_rename(self):
        def gone(src, dst):
            os.unlink(src)
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", src)
        with mock.patch("mediaorg.transfer.os.rename", side_effect=gone):
            res, state = self.execute()
        self.assertEqual((res.done, res.failed, state), (0, 1, "failed"))
        self.assertFalse(os.path.lexists(self.dst), "an empty reservation was left in the library")

    def test_a_rename_done_by_the_server_despite_an_error_still_counts(self):
        real_rename = os.rename

        def done_but_error(src, dst):
            real_rename(src, dst)
            raise OSError(errno.EIO, "Input/output error", src)
        with mock.patch("mediaorg.transfer.os.rename", side_effect=done_but_error):
            res, state = self.execute()
        self.assertEqual((res.done, res.failed, state), (1, 0, "done"))
        with open(self.dst, "rb") as fh:
            self.assertEqual(fh.read(), b"video")

    def test_undo_after_the_library_file_was_renamed(self):
        res, _ = self.execute()
        self.assertEqual(res.done, 1)
        os.rename(self.dst, os.path.join(os.path.dirname(self.dst), "Show - S01E01.mkv"))  # renamed by tMM
        _, errors = run_undo(os.path.join(self.jdir, "r1.jsonl"), True, self.jdir, "r1-undo")
        self.assertTrue(errors)
        self.assertFalse(os.path.lexists(self.src), "an empty file was left in Downloads")


class TestBackupNeverWritesThroughALink(unittest.TestCase):
    def test_a_planted_backup_link_is_replaced_not_followed(self):
        from mediaorg.system import atomic_write

        tmp = tempfile.mkdtemp()
        try:
            cfg = touch(os.path.join(tmp, "conf", "config.toml"), b"old settings")
            victim = touch(os.path.join(tmp, "elsewhere", "precious"), b"precious data")
            os.chmod(victim, 0o600)
            os.symlink(victim, cfg + ".bak")
            self.assertEqual(atomic_write(cfg, b"new settings", 0o640, backup=cfg + ".bak"), cfg + ".bak")
            with open(victim, "rb") as fh:
                self.assertEqual(fh.read(), b"precious data")
            self.assertEqual(os.stat(victim).st_mode & 0o777, 0o600)
            self.assertFalse(os.path.islink(cfg + ".bak"))
            with open(cfg + ".bak", "rb") as fh:
                self.assertEqual(fh.read(), b"old settings")
            with open(cfg, "rb") as fh:
                self.assertEqual(fh.read(), b"new settings")
        finally:
            shutil.rmtree(tmp)


class TestSecretsAreCheckedWhenRead(unittest.TestCase):
    def test_key_and_token_that_others_can_read_are_refused(self):
        from mediaorg.freebox import FreeboxError, load_token
        from mediaorg.tmm import TmmError, read_api_key

        tmp = tempfile.mkdtemp()
        try:
            key = touch(os.path.join(tmp, "tmm.key"), b"0123456789abcdef")
            tok = touch(os.path.join(tmp, "token.json"), b'{"app_id": "mediaorg", "app_token": "x"}')
            for path in (key, tok):
                os.chmod(path, 0o644)
            with self.assertRaisesRegex(TmmError, "readable by every user"):
                read_api_key(key)
            with self.assertRaisesRegex(FreeboxError, "readable by every user"):
                load_token(tok)
            for path in (key, tok):
                os.chmod(path, 0o600)
            self.assertEqual(read_api_key(key), "0123456789abcdef")
            self.assertEqual(load_token(tok)["app_id"], "mediaorg")
        finally:
            shutil.rmtree(tmp)


class TestConfigurationEdges(unittest.TestCase):
    def setUp(self):
        self.nas = FakeNas()

    def tearDown(self):
        self.nas.cleanup()

    def test_naming_formats_are_tried_before_they_are_accepted(self):
        from mediaorg.config import ConfigError

        self.assertEqual(self.nas.config(naming={"season_folder": "Season {season:02d}"}).naming.season_folder,
                         "Season {season:02d}")
        for key, value in (("movie_folder", "{title} ({year}) {title:d}"), ("season_folder", "S{season:>999}"),
                           ("season_folder", "Season {season!z}")):
            with self.assertRaises(ConfigError, msg=value):
                self.nas.config(naming={key: value})

    def test_relative_config_path_survives_the_switch_of_account(self):
        from .helpers import cli_env, run_cli

        with cli_env(self.nas) as path:
            cwd = os.getcwd()
            os.chdir(os.path.dirname(path))
            try:
                with mock.patch("mediaorg.system.is_root", return_value=True), \
                        mock.patch("mediaorg.system.drop_to_service_user", side_effect=lambda: os.chdir("/")):
                    code, out = run_cli("-c", os.path.basename(path), "run")
            finally:
                os.chdir(cwd)
        self.assertEqual(code, 0, out)

    def test_an_idle_run_is_not_stopped_by_low_space_and_status_survives_a_damaged_report(self):
        from .helpers import cli_env, run_cli

        with cli_env(self.nas, safety={"min_free_mb": 500}) as path, \
                mock.patch("mediaorg.cli.shutil.disk_usage", return_value=mock.Mock(free=1024 * 1024)):
            code, _ = run_cli("-c", path, "run", "--apply")
        self.assertEqual(code, 0)  # nothing to move: nothing is journaled, nothing to protect
        runs = os.path.join(self.nas.state, "runs")
        os.makedirs(runs, exist_ok=True)
        open(os.path.join(runs, "29991231T235959Z-ffff.json"), "w").close()  # newest report, empty
        with cli_env(self.nas) as path, mock.patch("mediaorg.service.systemd_running", return_value=False):
            code, out = run_cli("-c", path, "status")
        self.assertEqual(code, 0, out)
        self.assertIn("latest report: 29991231T235959Z-ffff", out)

    def test_an_empty_path_value_means_the_default(self):
        from .test_config_v2 import _Tree

        t = _Tree()
        try:
            t.init()
            code, out = t.cli("config", "set", "paths.state_dir=")
            self.assertEqual(code, 0, out)
            self.assertNotIn("state_dir", t.raw()["paths"])
        finally:
            t.cleanup()

    def test_a_change_the_installed_units_cannot_serve_is_refused_before_writing(self):
        from mediaorg import service
        from mediaorg.config import load

        from .test_config_v2 import _Tree

        t = _Tree()
        try:
            t.init()
            units = os.path.join(t.tmp, "units")
            os.makedirs(units)
            for name, text in service.render_units(load(t.cfg), t.cfg).items():
                touch(os.path.join(units, name), text.encode())
            with open(t.cfg, "rb") as fh:
                before = fh.read()
            with self.assertLogs("mediaorg", "ERROR") as logs:
                code, out = t.cli("config", "set", "paths.review=/var/tmp")  # moves would leave the writable folder
            self.assertEqual(code, 2, out)
            self.assertIn("no common folder other than /", "\n".join(logs.output))
            with open(t.cfg, "rb") as fh:
                self.assertEqual(fh.read(), before)
            with mock.patch("mediaorg.service.UNIT_DIR", units), mock.patch("os.geteuid", return_value=1000):
                from .helpers import run_cli
                code, out = run_cli("-c", t.cfg, "check")
            self.assertIn("systemd units match the configuration", out)
        finally:
            t.cleanup()

    def test_a_schedule_set_in_a_drop_in_is_reported(self):
        from mediaorg import service

        units = tempfile.mkdtemp()
        try:
            touch(os.path.join(units, "mediaorg.timer.d", "override.conf"), b"[Timer]\nOnCalendar=\nOnCalendar=*:0/10\n")
            touch(os.path.join(units, "mediaorg.service.d", "nice.conf"), b"[Service]\nNice=15\n")
            schedule, other = service.dropins(units)
            self.assertEqual(schedule, [os.path.join(units, "mediaorg.timer.d", "override.conf")])
            self.assertEqual(other, [os.path.join(units, "mediaorg.service.d", "nice.conf")])
        finally:
            shutil.rmtree(units)


if __name__ == "__main__":
    unittest.main()
