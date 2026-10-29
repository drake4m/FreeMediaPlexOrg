"""2.2.0: review.delete_duplicates; Freebox edge cases (stop ratio reached before the stop, paused by hand,
task removed with or without its files, races around the stop request, files deleted during a run);
staged folders without video; the undo note of a deleted duplicate."""

import glob
import json
import os
import time
import unittest
from unittest import mock

from mediaorg import completeness as comp
from mediaorg import transfer
from mediaorg.freebox import FreeboxClient
from mediaorg.model import FileEntry, Item, Op
from mediaorg.normalize import STAGING_TAG

from .fakes import FakeFreebox
from .helpers import OLD, FakeNas, age_tree, cli_env, run_cli, touch
from .test_v101 import Base
from .test_v210 import MOVIE, REMOTE, FreeboxBase, task

DELETE = {"delete_duplicates": True}
TAMSIN = "Movies/Tamsin (2023)/Tamsin.(2023).mkv"  # library file of the fake NAS (1 byte, b"x")
EP2 = "Series/Brightwater (2025)/Season 1/Brightwater.S01E02.1080p.mkv"  # likewise


class TestDeleteDuplicates(Base):
    def dups(self):
        return [p for p in self.review("duplicates") if os.path.isfile(p)]

    def test_same_film_is_deleted_with_the_setting(self):
        self.dl("Tamsin.2023.1080p.WEB.H264-GRP.mkv", b"y")  # same size, other bytes: not identical
        before = self.nas.snapshot()
        code, out = self.run_("run", review=DELETE)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.nas.snapshot(), before)  # a dry-run deletes nothing
        self.assertIn("    delete    Tamsin.2023.1080p.WEB.H264-GRP.mkv", out)
        self.assertIn("not larger than the library copy: deleted from Downloads (review.delete_duplicates)", out)
        self.assertIn("deletes 1 (identical downloads 0, duplicates 1, empty library files 0)", out)
        code, out = self.run_("run", "--apply", review=DELETE)
        self.assertEqual(code, 0, out)
        self.assertFalse(self.nas.exists("Downloads/Tamsin.2023.1080p.WEB.H264-GRP.mkv"))
        self.assertEqual(self.read(TAMSIN), b"x")  # the library copy is untouched
        self.assertEqual(self.dups(), [])

    def test_without_the_setting_the_duplicate_goes_to_review(self):
        self.dl("Tamsin.2023.1080p.WEB.H264-GRP.mkv", b"y")
        code, out = self.run_("run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertIn("deleted from review 7 day(s) after this run", out)
        self.assertEqual([os.path.basename(p) for p in self.dups()], ["Tamsin.2023.1080p.WEB.H264-GRP.mkv"])

    def test_episode_named_without_year(self):
        # the show folder is the one folder of that title: with the setting the duplicate is deleted
        self.dl("Brightwater.S01E02.720p.WEB.x264-GRP.mkv", b"y")
        code, out = self.run_("run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertIn("kept in review until you decide: no year in the release name", out)
        self.assertEqual(len(self.dups()), 1)
        self.dl("Brightwater.S01E02.720p.WEB.x264-OTHER.mkv", b"z")
        code, out = self.run_("run", "--apply", review=DELETE)
        self.assertEqual(code, 0, out)
        self.assertFalse(self.nas.exists("Downloads/Brightwater.S01E02.720p.WEB.x264-OTHER.mkv"))
        self.assertEqual(self.read(EP2), b"x")

    def test_episode_whose_year_is_not_the_show_folders_is_kept(self):
        # Brimstone (1999) in the library; a 2024 show of that title is another show
        self.dl("Brimstone.2024.S01E01.1080p.WEB.x264-GRP.mkv", b"y")
        code, out = self.run_("run", "--apply", review=DELETE)
        self.assertEqual(code, 0, out)
        self.assertIn("[SKIP] Brimstone.2024.S01E01.1080p.WEB.x264-GRP.mkv", out)
        self.assertTrue(self.nas.exists("Downloads/Brimstone.2024.S01E01.1080p.WEB.x264-GRP.mkv"))
        self.assertTrue(self.nas.exists("Series/Brimstone (1999)/Season 1/Brimstone.S01E01.mkv"))

    def test_bigger_or_repaired_copies_are_kept_for_you(self):
        self.dl("Tamsin.2023.2160p.WEB.H265-GRP.mkv", b"yy")  # bigger than the library copy
        self.dl("Brightwater.S01E02.REPACK.1080p.WEB.x264-GRP.mkv", b"y")
        code, out = self.run_("run", "--apply", review=DELETE)
        self.assertEqual(code, 0, out)
        self.assertIn("kept in review until you decide: it is larger than the library copy", out)
        self.assertIn("kept in review until you decide: it is a REPACK/PROPER release", out)
        self.assertEqual(sorted(os.path.basename(p) for p in self.dups()),
                         ["Brightwater.S01E02.REPACK.1080p.WEB.x264-GRP.mkv", "Tamsin.2023.2160p.WEB.H265-GRP.mkv"])
        # kept: not deleted by the next run either
        code, out = self.run_("run", "--apply", review=DELETE)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.dups()), 2)

    def test_identical_copy_is_still_deleted_and_its_subtitle_joins_the_library_copy(self):
        folder = "Tamsin.2023.1080p.WEB.H264-GRP"
        self.dl(f"{folder}/{folder}.mkv", b"x")  # byte-identical to the library copy
        self.dl(f"{folder}/{folder}.en.srt", b"sub")
        age_tree(os.path.join(self.nas.dl, folder))
        code, out = self.run_("run", "--apply", review=DELETE)
        self.assertEqual(code, 0, out)
        self.assertIn("byte-identical copy, deleted from Downloads", out)
        self.assertEqual(self.read("Movies/Tamsin (2023)/Tamsin.(2023).en.srt"), b"sub")

    def test_subtitle_of_a_deleted_duplicate_is_not_put_next_to_the_library_copy(self):
        folder = "Tamsin.2023.1080p.WEB.H264-OTHER"
        self.dl(f"{folder}/{folder}.mkv", b"y")  # another release: its subtitles may not match the library copy
        self.dl(f"{folder}/{folder}.en.srt", b"sub")
        age_tree(os.path.join(self.nas.dl, folder))
        code, out = self.run_("run", "--apply", review=DELETE)
        self.assertEqual(code, 0, out)
        self.assertFalse(self.nas.exists("Downloads", folder))
        self.assertFalse(glob.glob(os.path.join(self.nas.movies, "Tamsin (2023)", "*.srt")))
        left = glob.glob(os.path.join(self.nas.dl, "_review/leftovers/*", folder, "*"))
        self.assertEqual([os.path.basename(p) for p in left], [f"{folder}.en.srt"])

    def test_library_copy_gone_before_the_deletion_keeps_the_download(self):
        src = self.dl("Tamsin.2023.1080p.WEB.H264-GRP.mkv", b"y")
        plans = self.plan(review=DELETE)
        self.assertEqual((plans[0].decisions[0].action, plans[0].decisions[0].same), ("delete", True))
        os.unlink(os.path.join(self.nas.mp, TAMSIN))  # deleted (or moved) meanwhile
        res, _ = self.execute(plans)
        self.assertEqual(res.failed, 1)
        self.assertIn("library copy", res.errors[0])
        self.assertTrue(os.path.exists(src))

    def test_library_copy_changed_before_the_deletion_keeps_the_download(self):
        src = self.dl("Tamsin.2023.1080p.WEB.H264-GRP.mkv", b"y")
        plans = self.plan(review=DELETE)
        self.lib(TAMSIN, b"xx", mtime=time.time())  # replaced by another copy meanwhile
        res, _ = self.execute(plans)
        self.assertEqual(res.failed, 1)
        self.assertTrue(os.path.exists(src))

    def test_earlier_certain_duplicates_are_deleted_by_the_next_run(self):
        self.dl("Tamsin.2023.1080p.WEB.H264-GRP.mkv", b"y")
        self.dl("Tamsin.2023.2160p.WEB.H265-GRP.mkv", b"yy")
        code, out = self.run_("run", "--apply")  # both to review: one certain, one bigger
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.dups()), 2)
        code, out = self.run_("run", "--apply", review=DELETE)
        self.assertEqual(code, 0, out)
        self.assertIn("[PURGE]", out)
        self.assertEqual([os.path.basename(p) for p in self.dups()], ["Tamsin.2023.2160p.WEB.H265-GRP.mkv"])

    def test_undo_says_a_deleted_duplicate_is_not_restored(self):
        self.dl("Tamsin.2023.1080p.WEB.H264-GRP.mkv", b"y")
        code, out = self.run_("run", "--apply", review=DELETE)
        self.assertEqual(code, 0, out)
        jdir = os.path.join(self.nas.state, "journal")
        jpath = glob.glob(os.path.join(jdir, "*.jsonl"))[0]
        inverse, notes = transfer.undo_plan(jpath)
        self.assertEqual(len(notes), 1)
        self.assertIn("was deleted as a duplicate of", notes[0])
        self.assertIn("(review.delete_duplicates)", notes[0])

    def test_status_shows_the_duplicate_rule(self):
        with cli_env(self.nas, review=DELETE) as path:
            _, out = run_cli("-c", path, "status")
        self.assertIn("duplicates: deleted when the library already has them", out)
        with cli_env(self.nas) as path:
            _, out = run_cli("-c", path, "status")
        self.assertIn("duplicates: byte-identical copies deleted; others moved to review, the certain ones deleted "
                      "after 7 day(s)", out)


class TestVerifyUnlink(unittest.TestCase):
    def test_a_same_duplicate_needs_its_library_copy_unchanged(self):
        nas = FakeNas()
        try:
            dl = touch(os.path.join(nas.dl, "a.mkv"), b"y")
            ref = os.path.join(nas.mp, TAMSIN)
            st, rst = os.stat(dl), os.stat(ref)
            op = Op("unlink", dst=dl, role="same-duplicate", expect_size=st.st_size, expect_mtime=st.st_mtime,
                    ref=ref, ref_size=rst.st_size, ref_mtime=rst.st_mtime)
            self.assertIsNone(transfer._verify_unlink(op))
            self.assertIn("no size recorded", transfer._verify_unlink(
                Op("unlink", dst=dl, role="same-duplicate", expect_size=st.st_size, expect_mtime=st.st_mtime,
                   ref=ref, ref_mtime=rst.st_mtime)))
            touch(ref, b"xx")
            self.assertIn("library copy", transfer._verify_unlink(op))
        finally:
            nas.cleanup()


class TestFreeboxEdgeCases(FreeboxBase):
    """completeness.on_seeding = "stop" around the moment the Freebox, or you, act on the same task."""

    def env(self, tasks, on_seeding="stop", files=None, untracked="wait", **fb_kwargs):
        if self.fb:
            self.fb.close()
        self.fb = FakeFreebox(tasks=tasks, files=files, download_dir=REMOTE.rstrip("/"), **fb_kwargs)
        tok = os.path.join(self.nas.state, "tok.json")
        os.makedirs(self.nas.state, exist_ok=True)
        with open(tok, "w") as fh:
            json.dump({"app_id": "mediaorg", "app_token": FakeFreebox.APP_TOKEN}, fh)
        os.chmod(tok, 0o600)
        return cli_env(self.nas, completeness={"method": "freebox", "on_seeding": on_seeding, "untracked": untracked},
                       freebox={"remote_downloads_dir": REMOTE, "base_url": self.fb.url,
                                "allow_insecure_http": True, "token_file": tok})

    def run_(self, tasks, *args, **kw):
        with self.env(tasks, **kw) as path:
            return run_cli("-c", path, "run", *args)

    def seen(self):
        try:
            with open(os.path.join(self.nas.state, "freebox_seen.json"), encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return {}

    def test_stop_ratio_reached_before_the_run(self):
        # the download finished and shared its 1 % before any run saw it seeding: 'done', filed, nothing stopped
        files = self.movie_download()
        code, out = self.run_([task(1, MOVIE, "done")], "--apply", files=files)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.puts(), [])
        self.assertTrue(self.filed(), out)

    def test_paused_by_hand(self):
        files = self.movie_download()
        code, out = self.run_([task(1, MOVIE, "stopped")], "--apply", files=files)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.puts(), [])
        self.assertTrue(self.filed(), out)

    def test_paused_by_hand_before_it_finished_is_not_filed(self):
        files = self.movie_download()
        files[1][0]["rx"] = 2
        code, out = self.run_([task(1, MOVIE, "stopped", rx_pct=4000)], "--apply", files=files)
        self.assertEqual(code, 0, out)
        self.assertIn("[WAIT]", out)
        self.assertFalse(self.filed())

    def test_stop_refused_because_the_ratio_was_just_reached(self):
        files = self.movie_download()
        code, out = self.run_([task(1, MOVIE, "seeding")], "--apply", files=files, on_stop="finish")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.puts()), 1)
        self.assertIn("Freebox task 1 is now 'done' at 100 %: nothing to stop", out)
        self.assertTrue(self.filed(), out)

    def test_task_removed_while_being_stopped_is_filed_by_the_next_run(self):
        files = self.movie_download()
        before = self.nas.snapshot()
        code, out = self.run_([task(1, MOVIE, "seeding")], "--apply", files=files, on_stop="vanish")
        self.assertEqual(code, 0, out)
        self.assertIn(f"[WAIT] {MOVIE}  -- Freebox task 1 is gone", out)
        self.assertEqual(self.nas.snapshot(), before)
        code, out = self.run_([], "--apply")  # removed in Freebox OS, the files kept
        self.assertEqual(code, 0, out)
        self.assertTrue(self.filed(), out)
        self.assertEqual(self.puts(), [])

    def test_task_removed_after_it_finished_is_filed_even_with_untracked_wait(self):
        files = self.movie_download()
        code, out = self.run_([task(1, MOVIE, "seeding")], files=files)  # a dry-run sees it seeding at 100 %
        self.assertEqual(code, 0, out)
        self.assertEqual(self.seen()[MOVIE.casefold()]["pct"], 10000)
        code, out = self.run_([], "--apply", untracked="wait")
        self.assertEqual(code, 0, out)
        self.assertTrue(self.filed(), out)
        self.assertEqual(self.puts(), [])

    def test_task_removed_before_it_finished_is_never_filed(self):
        files = self.movie_download()
        code, out = self.run_([task(1, MOVIE, "downloading", rx_pct=5000)], "--apply", files=files)
        self.assertEqual(code, 0, out)
        self.assertIn("Freebox task is downloading", out)
        # removed in Freebox OS, its partial files kept: even "untracked = quiescence" does not file them
        code, out = self.run_([], "--apply", untracked="quiescence")
        self.assertEqual(code, 0, out)
        self.assertIn("its Freebox task 1 is gone; it was last seen 'downloading' at 50.0 %, so the files are "
                      "incomplete", out)
        self.assertFalse(self.filed())
        self.assertTrue(self.nas.exists("Downloads", MOVIE, f"{MOVIE}.mkv"))

    def test_task_and_files_removed(self):
        files = self.movie_download()
        code, out = self.run_([task(1, MOVIE, "downloading", rx_pct=5000)], "--apply", files=files)
        self.assertEqual(code, 0, out)
        self.assertIn(MOVIE.casefold(), self.seen())
        os.rename(os.path.join(self.nas.dl, MOVIE), os.path.join(self.nas.tmp, "erased"))  # "delete everything"
        other = "Larkspur.2025.1080p.WEB.H264-GRP"  # another download keeps the runs asking the Freebox
        touch(f"{self.nas.dl}/{other}.mkv", b"y")
        code, out = self.run_([task(2, other, "downloading", rx_pct=100)], "--apply")
        self.assertEqual(code, 0, out)
        self.assertNotIn(MOVIE.casefold(), self.seen())  # forgotten once gone from Downloads
        self.assertFalse(self.filed())

    def test_files_deleted_during_the_run_is_not_an_error(self):
        files = self.movie_download()
        real = transfer.execute

        def delete_first(plans, journal, unfinished=None):
            os.rename(os.path.join(self.nas.dl, MOVIE), os.path.join(self.nas.tmp, "erased"))
            return real(plans, journal, unfinished)

        with mock.patch("mediaorg.transfer.execute", side_effect=delete_first), \
                self.assertLogs("mediaorg", "WARNING") as logs:
            code, out = self.run_([task(1, MOVIE, "done")], "--apply", files=files)
        self.assertEqual(code, 0, out)
        self.assertTrue(any("gone from Downloads since the scan" in m for m in logs.output), logs.output)
        self.assertFalse(os.path.exists(f"{self.nas.movies}/Rookfield (2026)"))  # no folder created for it

    def test_resumed_task_writing_the_film_again_is_not_kept(self):
        # a task resumed after its film was filed writes it again: with delete_duplicates the copy is deleted
        # once the Freebox lists it finished (stopped first when it seeds), never kept
        lib = touch(f"{self.nas.movies}/Rookfield (2026)/Rookfield.2026.1080p.mkv", b"MOVIE")  # other bytes
        files = self.movie_download()
        with self.env([task(1, MOVIE, "seeding")], files=files) as path:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("\n[review]\ndelete_duplicates = true\n")
            code, out = run_cli("-c", path, "run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.puts()), 1)
        self.assertIn("Freebox task 1 was seeding: stopped", out)
        self.assertIn("deleted from Downloads (review.delete_duplicates)", out)
        self.assertFalse(os.path.exists(f"{self.nas.dl}/{MOVIE}/{MOVIE}.mkv"))
        self.assertTrue(os.path.exists(lib))
        self.assertFalse(glob.glob(os.path.join(self.nas.dl, "_review/duplicates/*/*")))


class TestStopSeedingRaces(unittest.TestCase):
    def setUp(self):
        self.nas = FakeNas()
        self.fb = FakeFreebox(tasks=[task(2, "Seeding.Show.S01E01.mkv", "seeding")], download_dir=REMOTE.rstrip("/"))
        self.client = FreeboxClient(self.fb.url, None)
        self.client.login("mediaorg", FakeFreebox.APP_TOKEN)

    def tearDown(self):
        self.fb.close()
        self.nas.cleanup()

    def test_done_when_read_again_needs_no_stop(self):
        self.fb.tasks[0]["status"] = "done"  # the stop ratio was reached between planning and the stop
        ok, text = comp.stop_seeding(self.client, [2], timeout_s=0)
        self.assertTrue(ok, text)
        self.assertIn("is now 'done' at 100 %: nothing to stop", text)
        self.assertEqual([r for r in self.fb.requests if r[0] == "PUT"], [])

    def test_refusal_then_done(self):
        self.fb.on_stop = "finish"
        ok, text = comp.stop_seeding(self.client, [2], timeout_s=0)
        self.assertTrue(ok, text)

    def test_refusal_then_gone(self):
        self.fb.on_stop = "vanish"
        ok, text = comp.stop_seeding(self.client, [2], timeout_s=0)
        self.assertFalse(ok)
        self.assertIn("Freebox task 2 is gone", text)

    def test_refusal_while_still_seeding(self):
        self.fb.on_stop = "refuse"
        ok, text = comp.stop_seeding(self.client, [2], timeout_s=0)
        self.assertFalse(ok)
        self.assertIn("could not be stopped", text)


class TestSeenTasks(unittest.TestCase):
    def setUp(self):
        self.nas = FakeNas()
        self.path = os.path.join(self.nas.tmp, "seen.json")

    def tearDown(self):
        self.nas.cleanup()

    def state(self, by_entry, present, files=None):
        st = mock.Mock()
        st.present = present
        st.by_entry = by_entry
        st._tasks_for = lambda key, loose=False: by_entry.get(key, [])
        st._task_files = lambda t: (files or {}).get(t.id, [])
        st.to_local = lambda fp: "/dl/" + fp.rsplit("/", 1)[-1]
        return st

    def test_remembers_the_least_advanced_task_keeps_and_forgets(self):
        from mediaorg.freebox import Task
        t1 = Task(1, "a", "seeding", 10000, 1, "/", "bt", 0, None)
        t2 = Task(2, "a", "downloading", 4000, 1, "/", "bt", 0, None)
        seen = comp.SeenTasks(self.path)
        seen.observe(self.state({"a": [t1, t2], "b": [t1]}, {"a", "b", "c"}))
        seen.save()
        seen = comp.SeenTasks(self.path)
        self.assertEqual((seen.get("a")["task"], seen.get("a")["pct"]), (2, 4000))
        seen.observe(self.state({}, {"a"}))  # "a" lost its tasks: kept; "b" left Downloads: forgotten
        seen.save()
        seen = comp.SeenTasks(self.path)
        self.assertIsNotNone(seen.get("a"))
        self.assertIsNone(seen.get("b"))

    def test_files_never_fully_received_stay_blocked_once_the_task_is_gone(self):
        from mediaorg.freebox import Task, TaskFile
        t = Task(3, "pack", "done", 10000, 2, "/", "bt", 0, None)
        files = {3: [TaskFile("/Freebox/pack/a.mkv", 1, 1, "done", "normal"),
                     TaskFile("/Freebox/pack/b.mkv", 1, 0, "queued", "no_dl")]}  # deselected
        seen = comp.SeenTasks(self.path)
        seen.observe(self.state({"pack": [t]}, {"pack"}, files))
        self.assertEqual(seen.new["pack"]["blocked"], ["/dl/b.mkv"])

    def test_unreadable_memory_is_empty(self):
        with open(self.path, "w") as fh:
            fh.write("{not json")
        self.assertIsNone(comp.SeenTasks(self.path).get("a"))
        with open(self.path, "w") as fh:
            json.dump({"a": {"task": "x", "pct": 1}}, fh)
        self.assertIsNone(comp.SeenTasks(self.path).get("a"))

    def test_downloads_not_listed_forgets_nothing(self):
        seen = comp.SeenTasks(self.path)
        seen.old = {"a": {"task": 1, "pct": 10000, "status": "done", "t": 0}}
        seen.observe(self.state({}, None))
        self.assertIn("a", seen.new)


class TestStagedWithoutVideo(unittest.TestCase):
    def setUp(self):
        self.nas = FakeNas()

    def tearDown(self):
        self.nas.cleanup()

    def test_a_staged_folder_holding_no_video_is_reported_as_such(self):
        os.makedirs(f"{self.nas.movies}/Gale Point (2024) {STAGING_TAG}")
        touch(f"{self.nas.movies}/Gale Point (2024) {STAGING_TAG}/Gale.Point.2024.1080p-poster.jpg")
        with cli_env(self.nas, tmm={"enabled": True, "base_url": "http://127.0.0.1:9"},
                     naming={"tmm_staging": True}) as path:
            _, out = run_cli("-c", path, "check")
        self.assertIn(f"[warn] 1 folder(s) ending with ' {STAGING_TAG}' hold no video file, so tMM ignores them", out)
        self.assertIn(f"'Movies/Gale Point (2024) {STAGING_TAG}'", out)
        self.assertNotIn("after a day", out)


class TestItemOfReadiness(unittest.TestCase):
    """A loose item whose task is gone, seen finished: temporary files and files it never fully received
    still block it."""

    def test_temporary_file_blocks_a_removed_finished_task(self):
        nas = FakeNas()
        try:
            name = "Gone.Show.S01E01.mkv"
            p = touch(os.path.join(nas.dl, name))
            part = touch(os.path.join(nas.dl, name + ".part"))
            st = os.stat(p)
            item = Item(name, p, False, [FileEntry(p, name, name, ".mkv", st.st_size, st.st_mtime),
                                         FileEntry(part, name + ".part", name + ".part", ".part", 1, OLD)],
                        newest_mtime=st.st_mtime)
            fb = FakeFreebox(tasks=[], download_dir=REMOTE.rstrip("/"))
            try:
                client = FreeboxClient(fb.url, None)
                client.login("mediaorg", FakeFreebox.APP_TOKEN)
                cfg = nas.config(completeness={"method": "freebox"},
                                 freebox={"remote_downloads_dir": REMOTE, "base_url": fb.url,
                                          "allow_insecure_http": True})
                seen = comp.SeenTasks(None)
                seen.old = {name.casefold(): {"task": 7, "pct": 10000, "status": "stopped", "t": 0}}
                r = comp.FreeboxState(cfg, client, seen=seen).assess(item)
                self.assertFalse(r.ready)
                self.assertIn("temporary download file present", r.reason)
                os.unlink(part)
                item.files.pop()
                seen.old[name.casefold()]["blocked"] = [p]
                r = comp.FreeboxState(cfg, client, seen=seen).assess(item)
                self.assertTrue(r.ready)
                self.assertEqual(r.blocked_files, frozenset({p}))
            finally:
                fb.close()
        finally:
            nas.cleanup()


if __name__ == "__main__":
    unittest.main()
