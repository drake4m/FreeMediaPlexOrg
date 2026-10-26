"""1.0.2-1.0.3: cleaning of tinyMediaManager's trash folders (<movies>/.deletedByTMM, <series>/.deletedByTMM)."""

import glob
import json
import os
import time
import unittest
from unittest import mock

from mediaorg import tmmtrash
from mediaorg.config import ConfigError

from .helpers import FakeNas, age_tree, cli_env, run_cli, touch

DAY = 86400


class Base(unittest.TestCase):
    def setUp(self):
        self.nas = FakeNas()
        self.trash_m = os.path.join(self.nas.movies, ".deletedByTMM")
        self.trash_s = os.path.join(self.nas.series, ".deletedByTMM")

    def tearDown(self):
        self.nas.cleanup()

    def put(self, path, data=b"x"):
        return touch(path, data)

    def run_(self, *args, **cfg):
        with cli_env(self.nas, **cfg) as path:
            return run_cli("-c", path, *args)

    def state(self):
        with open(os.path.join(self.nas.state, tmmtrash.STATE_FILE), encoding="utf-8") as fh:
            return json.load(fh)["files"]

    def backdate(self, days):
        """Pretend every recorded file has been seen in the trash for `days` more."""
        self.edit_state(lambda v: v.__setitem__(2, v[2] + days * DAY))

    def edit_state(self, change):
        path = os.path.join(self.nas.state, tmmtrash.STATE_FILE)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        for v in data["files"].values():
            change(v)  # [size, mtime, seconds seen, clock of the last count]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def report(self):
        newest = max(glob.glob(os.path.join(self.nas.state, "runs", "*.json")), key=os.path.getmtime)
        with open(newest, encoding="utf-8") as fh:
            return json.load(fh)


class TestTrash(Base):
    def setUp(self):
        super().setUp()
        self.poster = self.put(os.path.join(self.trash_m, "The Ballast (2025)", "poster.jpg"), b"old poster")
        self.nfo = self.put(os.path.join(self.trash_s, "Cistern (2022)", "Season 1", "Cistern.S01E01.nfo"), b"old nfo")
        self.outside = self.put(os.path.join(self.nas.movies, "The Ballast (2025)", "poster.jpg"), b"new poster")
        age_tree(self.trash_m)
        age_tree(self.trash_s)

    def test_files_are_deleted_7_days_after_they_are_first_seen(self):
        code, out = self.run_("run")  # dry-run: nothing due yet, nothing recorded
        self.assertEqual(code, 0, out)
        self.assertNotIn("[TRASH]", out)
        self.assertFalse(os.path.exists(os.path.join(self.nas.state, tmmtrash.STATE_FILE)))
        code, out = self.run_("run", "--apply")  # records first seen, deletes nothing
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.exists(self.poster) and os.path.exists(self.nfo))
        self.assertEqual(sorted(self.state()), sorted([self.poster, self.nfo]))
        self.backdate(6)
        code, out = self.run_("run", "--apply")
        self.assertTrue(os.path.exists(self.poster), out)
        self.backdate(1)  # 7 days now
        before = self.nas.snapshot()
        code, out = self.run_("run")  # the dry-run names them and changes nothing
        self.assertEqual(code, 0, out)
        self.assertEqual(self.nas.snapshot(), before)
        self.assertIn("[TRASH] Movies/.deletedByTMM  -- deleting 1 file(s), 10 B: 'The Ballast (2025)/poster.jpg' "
                      "(tMM's trash is cleaned once seen there for 7 day(s))", out)
        self.assertIn("tMM trash folders to clean 2", out)
        code, out = self.run_("run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(self.poster) or os.path.exists(self.nfo))
        self.assertTrue(os.path.exists(self.outside))  # the library itself is never touched
        # emptied folders below the trash go, the trash folders themselves stay
        self.assertEqual(os.listdir(self.trash_m), [])
        self.assertEqual(os.listdir(self.trash_s), [])
        done = self.report()["tmm_trash_done"]
        self.assertEqual(sorted(x for d in done for x in d["deleted"]),
                         ["Cistern (2022)/Season 1/Cistern.S01E01.nfo", "The Ballast (2025)/poster.jpg"])
        self.assertEqual(self.state(), {})

    def test_retention_0_deletes_in_the_first_run(self):
        code, out = self.run_("run", review={"tmm_trash_retention_days": 0})
        self.assertIn("(tMM's trash is cleaned in the run that first sees them)", out)
        self.assertTrue(os.path.exists(self.poster))
        code, out = self.run_("run", "--apply", review={"tmm_trash_retention_days": 0})
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(self.poster) or os.path.exists(self.nfo))

    def test_retention_minus_1_never_deletes(self):
        for _ in range(2):
            code, out = self.run_("run", "--apply", review={"tmm_trash_retention_days": -1})
            self.assertEqual(code, 0, out)
        self.assertTrue(os.path.exists(self.poster) and os.path.exists(self.nfo))
        self.assertFalse(os.path.exists(os.path.join(self.nas.state, tmmtrash.STATE_FILE)))

    def test_a_changed_file_counts_as_new(self):
        self.run_("run", "--apply")
        self.backdate(8)
        with open(self.poster, "wb") as fh:  # tMM moved another poster.jpg there
            fh.write(b"another old poster")
        code, out = self.run_("run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.exists(self.poster))
        self.assertFalse(os.path.exists(self.nfo))
        self.assertLess(self.state()[self.poster][2], 60)  # counted again from 0

    def test_only_runs_do_not_clean_the_trash(self):
        self.run_("run", "--apply")
        self.backdate(8)
        self.put(os.path.join(self.nas.dl, "Cistern.S03E06.1080p.WEB.h264-GRP.mkv"))
        code, out = self.run_("run", "--apply", "--only", "Cistern.S03E06.1080p.WEB.h264-GRP.mkv")
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.exists(self.poster))

    def test_quiet_runs_report_only_what_they_delete(self):
        code, out = self.run_("run", "--apply", "--quiet")
        self.assertEqual(out.strip(), "")
        self.backdate(8)
        code, out = self.run_("run", "--apply", "--quiet")
        self.assertIn("[TRASH] Movies/.deletedByTMM", out)
        code, out = self.run_("run", "--apply", "--quiet")
        self.assertEqual(out.strip(), "")


class TestTrashSafety(Base):
    def test_links_are_removed_never_followed(self):
        target_dir = os.path.join(self.nas.tmp, "elsewhere")
        target = self.put(os.path.join(target_dir, "precious.mkv"), b"precious")
        os.makedirs(self.trash_m)
        os.symlink(target, os.path.join(self.trash_m, "link-to-file"))
        os.symlink(target_dir, os.path.join(self.trash_m, "link-to-dir"))
        code, out = self.run_("run", "--apply", review={"tmm_trash_retention_days": 0})
        self.assertEqual(code, 0, out)
        self.assertEqual(os.listdir(self.trash_m), [])
        self.assertTrue(os.path.exists(target))

    def test_a_trash_name_that_is_not_a_folder_is_ignored(self):
        os.makedirs(self.nas.movies, exist_ok=True)
        self.put(self.trash_m, b"a file named like the trash")
        os.makedirs(self.nas.series, exist_ok=True)
        os.symlink(self.nas.movies, self.trash_s)
        code, out = self.run_("run", "--apply", review={"tmm_trash_retention_days": 0})
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.isfile(self.trash_m) and os.path.islink(self.trash_s))

    def test_a_recent_empty_folder_is_left_for_tmm(self):
        fresh = os.path.join(self.trash_m, "Just Created")
        os.makedirs(fresh)
        old = os.path.join(self.trash_m, "Old Empty")
        os.makedirs(old)
        os.utime(old, (time.time() - 3600,) * 2)
        self.put(os.path.join(self.trash_m, "x.nfo"))
        code, out = self.run_("run", "--apply", review={"tmm_trash_retention_days": 0})
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.isdir(fresh))
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.isdir(self.trash_m))

    def test_no_trash_folder_is_not_an_error(self):
        code, out = self.run_("run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.state(), {})

    def test_config_validation(self):
        with self.assertRaises(ConfigError):
            self.nas.config(review={"tmm_trash_retention_days": -2})
        with self.assertRaises(ConfigError):
            self.nas.config(review={"tmm_trash_retention_days": "7"})
        self.assertEqual(self.nas.config().review.tmm_trash_retention_days, 7)

    def test_retention_in_minutes(self):
        # a retention in minutes (here 30)
        p = self.put(os.path.join(self.trash_m, "H (2025)", "poster.jpg"))
        cfg = {"review": {"tmm_trash_retention_minutes": 30}}
        self.run_("run", "--apply", **cfg)
        self.edit_state(lambda v: v.__setitem__(2, 29 * 60))
        code, out = self.run_("run", "--apply", **cfg)
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.exists(p))  # 29 minutes and a few seconds
        self.edit_state(lambda v: v.__setitem__(2, 31 * 60))
        code, out = self.run_("run", **cfg)
        self.assertIn("[TRASH] Movies/.deletedByTMM  -- deleting 1 file(s), 1 B: 'H (2025)/poster.jpg' "
                      "(tMM's trash is cleaned once seen there for 30 minute(s))", out)
        code, out = self.run_("run", "--apply", **cfg)
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(p))

    def test_retention_keys(self):
        secs = lambda **r: self.nas.config(review=r).review.tmm_trash_retention_seconds  # noqa: E731
        self.assertEqual(secs(), 7 * 86400)
        self.assertEqual(secs(tmm_trash_retention_minutes=30), 1800)
        self.assertEqual(secs(tmm_trash_retention_minutes=0), 0)
        self.assertEqual(secs(tmm_trash_retention_minutes=-1), -1)
        self.assertEqual(secs(tmm_trash_retention_days=-1), -1)
        for bad in ({"tmm_trash_retention_minutes": -2}, {"tmm_trash_retention_minutes": "30"},
                    {"tmm_trash_retention_minutes": 1.5},
                    {"tmm_trash_retention_minutes": 30, "tmm_trash_retention_days": 7}):
            with self.assertRaises(ConfigError, msg=bad):
                self.nas.config(review=bad)
        self.assertEqual(tmmtrash.retention_text(1800), "30 minute(s)")
        self.assertEqual(tmmtrash.retention_text(7 * 86400), "7 day(s)")

    def test_due_files_left_behind_are_logged(self):
        # a due file that cannot be opened any more (most likely a deletion pending on the NAS) must not be
        # skipped without a word: every due file not deleted is named with the reason
        p = self.put(os.path.join(self.trash_m, "I (2025)", "I.2025.mkv"), b"video")
        real_scan = tmmtrash.scan

        def scan(*a, **k):
            out = real_scan(*a, **k)
            for sc in out:
                for f in sc.files:
                    f.size += 1  # as if it had changed between the scan and the deletion
                sc.files.append(tmmtrash.TrashFile(os.path.join(sc.root, "wisp.mkv"), 5, 0, 1e9, False, (0, 0)))
            return out

        with mock.patch.object(tmmtrash, "scan", scan), self.assertLogs("mediaorg", "INFO") as logs:
            code, out = self.run_("run", "--apply", "--quiet", review={"tmm_trash_retention_days": 0})
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.exists(p))
        text = "\n".join(logs.output)
        self.assertIn("'I (2025)/I.2025.mkv' left for the next run: changed since the scan (size 6 -> 5", text)
        self.assertIn("'wisp.mkv' left for the next run: no longer readable: deleted elsewhere, or still open in "
                      "another program", text)
        self.assertEqual(len(self.report()["tmm_trash_done"][0]["left"]), 2)

    def test_a_corrected_clock_or_a_restored_snapshot_ages_the_trash_by_one_step_at_most(self):
        # a clock 10 days late (a VM restored from a snapshot together with tmm_trash.json, before
        # NTP), then corrected, made a movie deleted in tMM's UI just before look 10 days old
        late = time.time() - 10 * DAY
        poster = self.put(os.path.join(self.trash_m, "A (2020)", "poster.jpg"))
        with mock.patch("time.time", return_value=late):
            self.run_("run", "--apply")
        movie = self.put(os.path.join(self.trash_m, "B (2020)", "B.2020.1080p.mkv"), b"a movie")
        with mock.patch("time.time", return_value=late + 300):
            self.run_("run", "--apply")
        code, out = self.run_("run", "--apply", review={"tmm_trash_retention_days": 1})  # clock corrected
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.exists(poster) and os.path.exists(movie))
        ages = {p: v[2] for p, v in self.state().items()}
        self.assertAlmostEqual(ages[poster], 300 + tmmtrash.AGE_STEP_CAP, delta=5)
        self.assertAlmostEqual(ages[movie], tmmtrash.AGE_STEP_CAP, delta=5)

    def test_a_clock_set_back_counts_nothing_and_does_not_stall(self):
        # a clock that ran 3 days ahead, then corrected: nothing counted for the step back, no stall either
        p = self.put(os.path.join(self.trash_m, "C (2021)", "poster.jpg"))
        with mock.patch("time.time", return_value=time.time() + 3 * DAY):
            self.run_("run", "--apply")
        with self.assertNoLogs("mediaorg", "WARNING"):
            self.run_("run", "--apply", "--quiet")
        self.assertLess(self.state()[p][2], 60)
        self.backdate(7)
        self.run_("run", "--apply")
        self.assertFalse(os.path.exists(p))

    def test_time_between_runs_adds_up_12_hours_at_most_per_run(self):
        self.assertEqual(tmmtrash.AGE_STEP_CAP, 12 * 3600)  # above a night without runs (8 h)
        p = self.put(os.path.join(self.trash_s, "x.nfo"))
        self.run_("run", "--apply")
        self.edit_state(lambda v: v.__setitem__(3, v[3] - 8 * 3600))  # a timer stopped for the night
        self.run_("run", "--apply")
        self.assertAlmostEqual(self.state()[p][2], 8 * 3600, delta=60)
        self.edit_state(lambda v: v.__setitem__(3, v[3] - 30 * 3600))  # the server was off for 30 hours
        self.run_("run", "--apply")
        self.assertAlmostEqual(self.state()[p][2], 8 * 3600 + 12 * 3600, delta=60)

    def test_state_file(self):
        self.put(os.path.join(self.trash_s, "x.nfo"))
        self.run_("run", "--apply")
        with open(os.path.join(self.nas.state, tmmtrash.STATE_FILE), encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["version"], tmmtrash.STATE_VERSION)
        self.assertAlmostEqual(data["saved_at"], time.time(), delta=60)
        self.assertEqual((data["unreadable"], data["folder_errors"]), ([], []))
        # a damaged state, or one of another version, counts as empty: every file starts again from 0
        for bad in ({"version": 2, "files": ["not", "a", "dict"], "saved_at": "x", "unreadable": 3},
                    {"version": 1, "files": {"/x": [1, 2, 3.0]}},
                    {"version": 2, "files": {"/x": [1, 2, -5.0, 4.0], "/y": [1, 2, "3", 4]}},
                    {"version": 2, "files": {"/x": [1, 2, float("inf"), 4.0], "/y": [1, 2, float("nan"), 4.0]}}):
            with open(os.path.join(self.nas.state, tmmtrash.STATE_FILE), "w", encoding="utf-8") as fh:
                json.dump(bad, fh)
            st = tmmtrash.load_state(self.nas.state)
            self.assertEqual((st.files, st.unreadable, st.folder_errors), ({}, [], []), bad)

    def test_old_empty_folders_go_even_when_no_file_is_due(self):
        # empty folders were removed only in a run that deleted a file in that trash
        old = os.path.join(self.trash_m, "Old Empty", "Season 1")
        os.makedirs(old)
        age_tree(os.path.join(self.trash_m, "Old Empty"), time.time() - 3600)
        fresh = os.path.join(self.trash_m, "Just Created")
        os.makedirs(fresh)
        waiting = self.put(os.path.join(self.trash_m, "C (2021)", "poster.jpg"))  # not due for 7 days
        code, out = self.run_("run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(os.path.join(self.trash_m, "Old Empty")))
        self.assertTrue(os.path.isdir(fresh) and os.path.exists(waiting))
        done = self.report()["tmm_trash_done"]
        self.assertEqual(done[0]["deleted"], [])
        self.assertEqual(sorted(done[0]["removed_folders"]), ["Old Empty", "Old Empty/Season 1"])

    def test_tmm_is_called_after_the_trash_is_cleaned(self):
        # tMM's renames move replaced files into its trash, so the cleaning runs before the calls;
        # the filed folders are recorded before the cleaning, so an interrupted run keeps them (review M-f)
        from .fakes import FakeTmm

        tmm = FakeTmm(system=False)
        self.addCleanup(tmm.close)
        key = self.put(os.path.join(self.nas.tmp, "tmm.key"), FakeTmm.API_KEY.encode())
        os.chmod(key, 0o600)
        self.put(os.path.join(self.trash_m, "x.nfo"))
        self.put(os.path.join(self.nas.dl, "Cistern.S03E06.1080p.WEB.h264-GRP.mkv"))
        pending = os.path.join(self.nas.state, "tmm_pending.json")
        order, real_purge = [], tmmtrash.purge

        def purge(*a, **k):
            with open(pending, encoding="utf-8") as fh:
                order.append(("cleaning; recorded for tMM", sorted(json.load(fh)), len(tmm.posts)))
            return real_purge(*a, **k)

        with mock.patch.object(tmmtrash, "purge", purge):
            code, out = self.run_("run", "--apply", review={"tmm_trash_retention_days": 0},
                                  tmm={"enabled": True, "base_url": tmm.url, "api_key_file": key})
        self.assertEqual(code, 0, out)
        self.assertEqual(order, [("cleaning; recorded for tMM", ["tvshow"], 0)])
        self.assertEqual([p for p, _ in tmm.posts], ["/api/tvshow"])  # then sent
        with open(pending, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {})
        self.assertTrue(self.nas.exists("Series", "Cistern (2022)", "Season 3", "Cistern.S03E06.1080p.WEB.h264-GRP.mkv"))

    def test_an_empty_folder_that_cannot_be_removed_is_reported_when_that_changes(self):
        # review M-c: a warning and a JSON report at every timer run
        import errno

        stuck = os.path.join(self.trash_m, "Stuck (2019)")
        os.makedirs(stuck)
        os.utime(stuck, (time.time() - 3600,) * 2)
        real_rmdir = os.rmdir

        def rmdir(path, *a, **k):
            if path == stuck:
                raise PermissionError(errno.EACCES, "Permission denied", path)
            return real_rmdir(path, *a, **k)

        reports = lambda: len(glob.glob(os.path.join(self.nas.state, "runs", "*.json")))  # noqa: E731
        with mock.patch("os.rmdir", rmdir):
            with self.assertLogs("mediaorg", "WARNING") as logs:
                code, out = self.run_("run", "--apply", "--quiet")
            self.assertEqual(code, 0, out)  # not worth a failed run
            self.assertIn("could not remove the empty folder", "\n".join(logs.output))
            before = reports()
            with self.assertNoLogs("mediaorg", "WARNING"):
                code, out = self.run_("run", "--apply", "--quiet")
            self.assertEqual((code, out.strip(), reports()), (0, "", before))  # nothing new: silent
            with self.assertLogs("mediaorg", "WARNING"):
                self.run_("run", "--apply")  # a manual run says it again
        self.run_("run", "--apply", "--quiet")  # fixed: removed
        self.assertFalse(os.path.exists(stuck))
        self.assertEqual(tmmtrash.load_state(self.nas.state).folder_errors, [])


class TestTrashRaces(Base):
    """purge() re-checks each entry right before deleting it."""

    def scan(self):
        sc = [s for s in tmmtrash.scan(self.nas.config(), tmmtrash.TrashState(), time.time())
              if s.root == self.trash_m]
        self.assertEqual(len(sc), 1)
        return sc[0]

    def test_a_file_replaced_since_the_scan_is_kept(self):
        p = self.put(os.path.join(self.trash_m, "D (2022)", "poster.jpg"), b"old")
        sc = self.scan()
        os.replace(self.put(p + ".new", b"new"), p)  # same name, size and time: another file
        res = tmmtrash.purge(sc, 0, time.time())
        self.assertEqual((res.deleted, res.errors), ([], []))
        self.assertTrue(os.path.exists(p))

    def test_a_folder_turned_into_a_link_is_not_followed(self):
        inner = self.put(os.path.join(self.trash_m, "E (2024)", "E.2024.mkv"), b"trash copy")
        library = self.put(os.path.join(self.nas.movies, "E (2024)", "E.2024.mkv"), b"trash copy")
        sc = self.scan()
        os.rename(os.path.dirname(inner), os.path.join(self.nas.tmp, "moved away"))
        os.symlink(os.path.dirname(library), os.path.join(self.trash_m, "E (2024)"))
        res = tmmtrash.purge(sc, 0, time.time())
        self.assertEqual((res.deleted, res.removed), ([], []))
        self.assertIn("a folder on its path is now a link", res.errors[0])
        self.assertTrue(os.path.exists(library))

    def test_a_trash_root_swapped_since_the_scan_is_left_alone(self):
        # also what a renewed inode number of the root looks like on a noserverino mount (review M-d):
        # a warning and the next run, not a failed run
        self.put(os.path.join(self.trash_m, "F (2024)", "poster.jpg"))
        sc = self.scan()
        os.rename(self.trash_m, os.path.join(self.nas.tmp, "old trash"))
        os.symlink(self.nas.movies, self.trash_m)
        res = tmmtrash.purge(sc, 0, time.time())
        self.assertEqual((res.deleted, res.errors), ([], []))
        self.assertIn("not the folder seen at the scan any more", res.skipped)

    def test_a_skipped_trash_folder_does_not_fail_the_run(self):
        self.put(os.path.join(self.trash_m, "G (2025)", "poster.jpg"))
        real_scan = tmmtrash.scan

        def scan(*a, **k):  # the root's identity as the mount reported it at the scan, renewed since
            out = real_scan(*a, **k)
            for sc in out:
                sc.root_ident = (sc.root_ident[0], sc.root_ident[1] + 1)
            return out

        with mock.patch.object(tmmtrash, "scan", scan), self.assertLogs("mediaorg", "WARNING") as logs:
            code, out = self.run_("run", "--apply", review={"tmm_trash_retention_days": 0})
        self.assertEqual(code, 0, out)
        self.assertIn("left alone in this run", "\n".join(logs.output))
        self.assertTrue(os.path.exists(os.path.join(self.trash_m, "G (2025)", "poster.jpg")))
        self.run_("run", "--apply", review={"tmm_trash_retention_days": 0})  # the next run cleans it
        self.assertFalse(os.path.exists(os.path.join(self.trash_m, "G (2025)", "poster.jpg")))

    def test_unreadable_entries_are_logged_once_in_scheduled_runs(self):
        # they were only in the JSON report
        os.makedirs(self.trash_m)
        os.mkfifo(os.path.join(self.trash_m, "a pipe"))
        with self.assertLogs("mediaorg", "WARNING") as logs:
            self.run_("run")  # a manual run always says it
        self.assertIn("1 entry(ies) cannot be read or are not files; left alone: 'Movies/.deletedByTMM/a pipe'",
                      "\n".join(logs.output))
        with self.assertLogs("mediaorg", "WARNING") as logs:
            self.run_("run", "--apply", "--quiet")  # the first scheduled run too
        self.assertIn("cannot be read", "\n".join(logs.output))
        with self.assertNoLogs("mediaorg", "WARNING"):
            self.run_("run", "--apply", "--quiet")  # then only when it changes
        self.assertTrue(os.path.exists(os.path.join(self.trash_m, "a pipe")))


if __name__ == "__main__":
    unittest.main()
