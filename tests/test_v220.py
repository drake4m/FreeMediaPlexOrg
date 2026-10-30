"""2.2.0: review.delete_duplicates; Freebox edge cases (stop ratio reached before the stop, paused by hand,
task removed with or without its files, added again, races around the stop request, files deleted during a
run); staged folders without video; the undo note of a deleted duplicate."""

import glob
import json
import os
import time
import unittest
from unittest import mock

from mediaorg import cli
from mediaorg import completeness as comp
from mediaorg import config as C
from mediaorg import transfer
from mediaorg.freebox import FreeboxClient, Task, TaskFile
from mediaorg.model import FileEntry, Item, Op
from mediaorg.normalize import STAGING_TAG

from .fakes import FakeFreebox, b64
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
        self.assertIn("deletes 1 (identical downloads 0, other duplicates 1, empty library files 0)", out)
        code, out = self.run_("run", "--apply", review=DELETE)
        self.assertEqual(code, 0, out)
        self.assertFalse(self.nas.exists("Downloads/Tamsin.2023.1080p.WEB.H264-GRP.mkv"))
        self.assertEqual(self.read(TAMSIN), b"x")  # the library copy is untouched
        self.assertEqual(self.dups(), [])

    def test_json_report_counts_the_deleted_duplicates(self):
        self.dl("Tamsin.2023.1080p.WEB.H264-GRP.mkv", b"y")
        code, out = self.run_("run", "--apply", review=DELETE)
        self.assertEqual(code, 0, out)
        report = sorted(glob.glob(os.path.join(self.nas.state, "runs", "*.json")))[-1]
        with open(report, encoding="utf-8") as fh:
            data = json.load(fh)
        ops = [op for it in data["items"] for op in it.get("ops", [])]
        self.assertEqual([op["role"] for op in ops if op["op"] == "unlink"], ["same-duplicate"])
        self.assertEqual(data["summary"]["dup_deleted"], 1)

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

    def test_dated_release_of_an_undated_show_folder_is_kept(self):
        # an undated show folder may hold another show of that title: a release naming a year is not deleted
        self.lib("Series/Quillon/Season 1/Quillon.S01E01.mkv", b"x")
        self.dl("Quillon.2024.S01E01.1080p.WEB.x264-GRP.mkv", b"y")
        code, out = self.run_("run", "--apply", review=DELETE)
        self.assertEqual(code, 0, out)
        self.assertTrue(self.nas.exists("Series/Quillon/Season 1/Quillon.S01E01.mkv"))
        self.assertFalse(self.nas.exists("Downloads/Quillon.2024.S01E01.1080p.WEB.x264-GRP.mkv"))
        self.assertIn("kept in review until you decide: the release names 2024, the show folder no year", out)
        self.assertEqual([os.path.basename(p) for p in self.dups()], ["Quillon.2024.S01E01.1080p.WEB.x264-GRP.mkv"])

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

    def test_special_edition_is_kept_for_you(self):
        self.dl("Tamsin.2023.Extended.1080p.WEB.H264-GRP.mkv", b"y")
        code, out = self.run_("run", "--apply", review=DELETE)
        self.assertEqual(code, 0, out)
        self.assertIn("kept in review until you decide: it is a special edition (Extended)", out)
        self.assertEqual([os.path.basename(p) for p in self.dups()], ["Tamsin.2023.Extended.1080p.WEB.H264-GRP.mkv"])

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

    def test_every_library_copy_must_be_unchanged(self):
        # the library holds the episode twice: a change to either one (not only the biggest) keeps the download
        other = "Series/Brightwater (2025)/Season 1/Brightwater.S01E02.2160p.mkv"
        self.lib(other, b"xxx")
        src = self.dl("Brightwater.S01E02.720p.WEB.x264-GRP.mkv", b"y")
        plans = self.plan(review=DELETE)
        op = next(o for o in plans[0].ops if o.kind == "unlink")
        self.assertEqual(op.role, "same-duplicate")
        self.assertEqual([p for p, _, _ in op.ref_files],
                         [os.path.join(self.nas.mp, other), os.path.join(self.nas.mp, EP2)])  # biggest first
        self.lib(EP2, b"z", mtime=time.time())  # the smaller copy is replaced meanwhile
        res, _ = self.execute(plans)
        self.assertEqual(res.failed, 1)
        self.assertIn(f"library copy {os.path.join(self.nas.mp, EP2)} changed since the scan", res.errors[0])
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

    def test_cannot_be_combined_with_keeping_identical_copies(self):
        with self.assertRaisesRegex(C.ConfigError, "delete_identical_duplicates = false"):
            self.nas.config(review={"delete_duplicates": True, "delete_identical_duplicates": False})
        self.nas.config(review={"delete_duplicates": False, "delete_identical_duplicates": False})  # still fine


class TestVerifyUnlink(unittest.TestCase):
    def setUp(self):
        self.nas = FakeNas()

    def tearDown(self):
        self.nas.cleanup()

    def test_a_same_duplicate_needs_every_library_copy_unchanged(self):
        dl = touch(os.path.join(self.nas.dl, "a.mkv"), b"y")
        ref = os.path.join(self.nas.mp, TAMSIN)
        st, rst = os.stat(dl), os.stat(ref)
        base = {"dst": dl, "role": "same-duplicate", "expect_size": st.st_size, "expect_mtime": st.st_mtime,
                "ref": ref, "ref_mtime": rst.st_mtime}
        op = Op("unlink", **base, ref_files=((ref, rst.st_size, rst.st_mtime),))
        self.assertIsNone(transfer._verify_unlink(op))
        self.assertIn("no library copy recorded", transfer._verify_unlink(Op("unlink", **base)))
        self.assertIn("no size or time recorded", transfer._verify_unlink(
            Op("unlink", **base, ref_files=((ref, None, rst.st_mtime),))))
        touch(ref, b"xx")
        self.assertIn("library copy", transfer._verify_unlink(op))

    def test_an_identical_duplicate_needs_its_recorded_state(self):
        dl = touch(os.path.join(self.nas.dl, "a.mkv"), b"x")
        ref = os.path.join(self.nas.mp, TAMSIN)
        st, rst = os.stat(dl), os.stat(ref)
        op = Op("unlink", dst=dl, role="identical-duplicate", expect_size=st.st_size, expect_mtime=st.st_mtime,
                ref=ref, ref_mtime=rst.st_mtime)
        self.assertIsNone(transfer._verify_unlink(op))
        op.ref_mtime = None
        self.assertIn("no size or time recorded", transfer._verify_unlink(op))


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

    @staticmethod
    def file(name, size=5, rx=5, priority="normal"):
        return {"filepath": b64(f"{REMOTE}{MOVIE}/{name}"), "size": size, "rx": rx,
                "status": "done" if rx >= size else "downloading", "priority": priority}

    # -- the Freebox finishes the task itself
    def test_stop_ratio_reached_before_the_run(self):
        # the download finished and shared its 1 % before any run saw it seeding: 'done', filed, nothing stopped
        files = self.movie_download()
        code, out = self.run_([task(1, MOVIE, "done")], "--apply", files=files)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.puts(), [])
        self.assertTrue(self.filed(), out)

    def test_stop_refused_because_the_ratio_was_just_reached(self):
        files = self.movie_download()
        code, out = self.run_([task(1, MOVIE, "seeding")], "--apply", files=files, on_stop="finish")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.puts()), 1)
        self.assertIn("Freebox task 1 is now 'done' at 100 %: nothing to stop", out)
        self.assertTrue(self.filed(), out)

    # -- you pause the task
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

    # -- you remove the task in Freebox OS, keeping its files
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
        rec = self.seen()[MOVIE.casefold()]
        self.assertEqual(rec["tasks"]["1"]["pct"], 10000)
        self.assertEqual(rec["tasks"]["1"]["files"], [os.path.join(self.nas.dl, MOVIE, f"{MOVIE}.mkv")])
        code, out = self.run_([], untracked="wait")
        self.assertEqual(code, 0, out)
        self.assertIn(f"[READY] {MOVIE}", out)
        code, out = self.run_([], "--apply", untracked="wait")
        self.assertEqual(code, 0, out)
        self.assertTrue(self.filed(), out)
        self.assertEqual(self.puts(), [])

    def test_done_task_removed_is_filed(self):
        files = self.movie_download()
        code, out = self.run_([task(1, MOVIE, "done", rx_pct=9990)], files=files)  # 'done' is final at any %
        self.assertEqual(code, 0, out)
        code, out = self.run_([], "--apply")
        self.assertEqual(code, 0, out)
        self.assertTrue(self.filed(), out)

    def test_task_removed_before_it_finished_is_never_filed(self):
        files = self.movie_download()
        code, out = self.run_([task(1, MOVIE, "downloading", rx_pct=5000)], "--apply", files=files)
        self.assertEqual(code, 0, out)
        self.assertIn("Freebox task is downloading", out)
        # removed in Freebox OS, its partial files kept: even "untracked = quiescence" does not file them
        for _ in range(2):  # nor any later run
            code, out = self.run_([], "--apply", untracked="quiescence")
            self.assertEqual(code, 0, out)
            self.assertIn("its Freebox task 1 is gone; it was last seen 'downloading' at 50.0 %, so its files may "
                          "be incomplete", out)
        self.assertFalse(self.filed())
        self.assertTrue(self.nas.exists("Downloads", MOVIE, f"{MOVIE}.mkv"))

    def test_task_in_error_then_removed_is_never_filed(self):
        files = self.movie_download()
        code, out = self.run_([task(1, MOVIE, "error")], "--apply", files=files)  # 100 %, yet in error
        self.assertEqual(code, 0, out)
        self.assertIn("Freebox task is in error", out)
        code, out = self.run_([], "--apply", untracked="quiescence")
        self.assertEqual(code, 0, out)
        self.assertIn("its Freebox task 1 is gone; it was last seen 'error' at 100.0 %", out)
        self.assertFalse(self.filed())

    def test_download_added_again_after_an_unfinished_removal_is_filed_once_done(self):
        files = self.movie_download()
        code, out = self.run_([task(1, MOVIE, "downloading", rx_pct=5000)], files=files)
        self.assertEqual(code, 0, out)
        # removed in Freebox OS, then added again into the same folder: a new task checks and completes the files
        code, out = self.run_([task(7, MOVIE, "checking", rx_pct=5000)], "--apply", files={7: files[1]})
        self.assertEqual(code, 0, out)
        self.assertIn("Freebox task is being verified", out)
        self.assertEqual(list(self.seen()[MOVIE.casefold()]["tasks"]), ["7"])  # the removed task is replaced
        code, out = self.run_([task(7, MOVIE, "done")], "--apply", files={7: files[1]})
        self.assertEqual(code, 0, out)
        self.assertTrue(self.filed(), out)

    def test_added_again_and_finished_between_two_runs_is_filed(self):
        files = self.movie_download()
        code, out = self.run_([task(1, MOVIE, "downloading", rx_pct=5000)], files=files)
        self.assertEqual(code, 0, out)
        code, out = self.run_([task(7, MOVIE, "done")], "--apply", files={7: files[1]})
        self.assertEqual(code, 0, out)
        self.assertTrue(self.filed(), out)

    def test_second_task_removed_unfinished_keeps_the_entry_waiting(self):
        files = self.movie_download(with_subtitle=True)
        files[2] = [self.file(f"{MOVIE}.en.srt", size=3, rx=1)]
        tasks = [task(1, MOVIE, "seeding"), task(2, f"{MOVIE}.subs", "downloading", rx_pct=3000)]
        code, out = self.run_(tasks, "--apply", files=files)
        self.assertEqual(code, 0, out)
        self.assertIn("Freebox task is downloading", out)
        for _ in range(2):  # task 2 removed in Freebox OS, its partial file kept
            code, out = self.run_([task(1, MOVIE, "seeding")], "--apply", files={1: files[1]})
            self.assertEqual(code, 0, out)
            self.assertIn("its Freebox task 2 is gone; it was last seen 'downloading' at 30.0 %", out)
        self.assertEqual(self.puts(), [])  # an item that waits: its other task is not stopped
        self.assertFalse(self.filed())

    def test_task_still_listed_but_not_matched_waits(self):
        files = self.movie_download()
        code, out = self.run_([task(1, MOVIE, "seeding")], files=files)
        self.assertEqual(code, 0, out)
        # the task is still listed but now points elsewhere: its record alone does not decide
        moved = task(1, MOVIE, "seeding", ddir="/Freebox/Elsewhere/")
        code, out = self.run_([moved], "--apply", untracked="quiescence")
        self.assertEqual(code, 0, out)
        self.assertIn("Freebox task 1 is still listed, but none of its files could be matched", out)
        self.assertFalse(self.filed())

    def test_record_older_than_the_files_does_not_decide(self):
        files = self.movie_download()
        code, out = self.run_([task(1, MOVIE, "seeding")], files=files)
        self.assertEqual(code, 0, out)
        data = self.seen()
        data[MOVIE.casefold()]["t"] = int(OLD) - 3600  # recorded before the files were last written
        with open(os.path.join(self.nas.state, "freebox_seen.json"), "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        code, out = self.run_([], "--apply", untracked="wait")
        self.assertEqual(code, 0, out)
        self.assertIn("is not a Freebox download task", out)
        self.assertFalse(self.filed())

    def test_subtitle_not_fully_received_is_not_filed(self):
        files = self.movie_download(with_subtitle=True)
        files[1].append(self.file(f"{MOVIE}.en.srt", size=3, rx=0, priority="no_dl"))  # deselected
        code, out = self.run_([task(1, MOVIE, "done")], "--apply", files=files)
        self.assertEqual(code, 0, out)
        self.assertTrue(self.filed(), out)
        self.assertFalse(glob.glob(f"{self.nas.movies}/Rookfield (2026)/*.srt"))
        left = glob.glob(os.path.join(self.nas.dl, "_review/leftovers/*", MOVIE, "*.srt"))
        self.assertEqual([os.path.basename(p) for p in left], [f"{MOVIE}.en.srt"])

    # -- you remove the task and its files
    def test_task_and_files_removed(self):
        files = self.movie_download()
        code, out = self.run_([task(1, MOVIE, "downloading", rx_pct=5000)], "--apply", files=files)
        self.assertEqual(code, 0, out)
        self.assertIn(MOVIE.casefold(), self.seen())
        os.rename(os.path.join(self.nas.dl, MOVIE), os.path.join(self.nas.tmp, "erased"))  # "delete everything"
        code, out = self.run_([], "--apply")  # nothing left in Downloads
        self.assertEqual(code, 0, out)
        self.assertNotIn(MOVIE.casefold(), self.seen())  # forgotten once gone from Downloads
        self.assertFalse(self.filed())
        # downloaded again later under the same name: judged by its new task, not by the old record
        self.movie_download()
        code, out = self.run_([task(9, MOVIE, "done")], "--apply", files={9: files[1]})
        self.assertEqual(code, 0, out)
        self.assertTrue(self.filed(), out)

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

    def test_downloads_gone_during_the_run_stops_it(self):
        # not "every download was deleted": the share went away (unmounted, NAS down)
        files = self.movie_download()
        real = transfer.execute

        def unmount_first(plans, journal, unfinished=None):
            os.rename(self.nas.dl, os.path.join(self.nas.tmp, "unmounted"))
            return real(plans, journal, unfinished)

        with mock.patch("mediaorg.transfer.execute", side_effect=unmount_first):
            code, out = self.run_([task(1, MOVIE, "done")], "--apply", files=files)
        self.assertEqual(code, 3, out)
        self.assertFalse(os.path.exists(f"{self.nas.movies}/Rookfield (2026)"))

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
        self.fb = FakeFreebox(tasks=[task(2, "Seeding.Show.S01E01.mkv", "seeding")], download_dir=REMOTE.rstrip("/"))
        self.client = FreeboxClient(self.fb.url, None)
        self.client.login("mediaorg", FakeFreebox.APP_TOKEN)

    def tearDown(self):
        self.fb.close()

    def stops(self):
        return [r for r in self.fb.requests if r[0] == "PUT"]

    def test_done_when_read_again_needs_no_stop(self):
        self.fb.tasks[0]["status"] = "done"  # the stop ratio was reached between planning and the stop
        ok, text = comp.stop_seeding(self.client, [2], timeout_s=0)
        self.assertTrue(ok, text)
        self.assertIn("is now 'done' at 100 %: nothing to stop", text)
        self.assertEqual(self.stops(), [])

    def test_done_below_100_percent_is_finished_too(self):
        self.fb.tasks[0].update(status="done", rx_pct=9990)  # the same rule as for filing
        ok, text = comp.stop_seeding(self.client, [2], timeout_s=0)
        self.assertTrue(ok, text)
        self.assertEqual(self.stops(), [])

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

    def test_not_found_while_still_listed_is_not_gone(self):
        self.fb.on_stop = "lost"
        ok, text = comp.stop_seeding(self.client, [2], timeout_s=0)
        self.assertFalse(ok)
        self.assertIn("Freebox task 2 could not be stopped", text)
        self.assertIn("it keeps seeding", text)

    def test_other_task_changed_meanwhile_is_not_stopped(self):
        self.fb.tasks.append(task(3, "Seeding.Show.S01E01.en.srt", "seeding"))

        def recheck(fb, t):  # while task 2 reaches its ratio, task 3 is sent back to verification
            fb.tasks[1]["status"] = "checking"

        self.fb.before_stop = recheck
        self.fb.on_stop = "finish"
        ok, text = comp.stop_seeding(self.client, [2, 3], timeout_s=0)
        self.assertFalse(ok)
        self.assertIn("Freebox task 3 is now 'checking'", text)
        self.assertEqual([r[1] for r in self.stops()], ["/api/v15/downloads/2"])


class TestSeenTasks(unittest.TestCase):
    def setUp(self):
        self.nas = FakeNas()
        self.path = os.path.join(self.nas.tmp, "seen.json")

    def tearDown(self):
        self.nas.cleanup()

    @staticmethod
    def state(by_entry, present, files=None):
        st = mock.Mock()
        st.present = present
        st.by_entry = by_entry
        st._tasks_for = lambda key, loose=False: by_entry.get(key, [])
        st._task_files = lambda t: (files or {}).get(t.id, [])
        st.to_local = lambda fp: "/dl/" + fp.split("/", 2)[-1]
        return st

    @staticmethod
    def t(tid, status, pct, name="a"):
        return Task(tid, name, status, pct, 1, "/", "bt", 0, None)

    @staticmethod
    def f(path, rx=1, priority="normal"):
        return TaskFile(f"/Freebox/{path}", 1, rx, "done" if rx else "queued", priority)

    def reopen(self, seen):
        seen.save()
        return comp.SeenTasks(self.path)

    def test_records_every_task_keeps_and_forgets(self):
        t1, t2 = self.t(1, "seeding", 10000), self.t(2, "downloading", 4000)
        seen = comp.SeenTasks(self.path)
        seen.observe(self.state({"a": [t1, t2], "b": [t1]}, {"a", "b", "c"}, {1: [self.f("a/x.mkv")]}))
        seen = self.reopen(seen)
        self.assertEqual(seen.get("a")["tasks"], {
            "1": {"pct": 10000, "status": "seeding", "name": "a", "files": ["/dl/a/x.mkv"]},
            "2": {"pct": 4000, "status": "downloading", "name": "a"}})
        self.assertIsNone(seen.get("c"))  # no task: nothing recorded
        seen.observe(self.state({}, {"a"}))  # "a" lost its tasks: kept as it was; "b" left Downloads: forgotten
        seen = self.reopen(seen)
        self.assertEqual(sorted(seen.get("a")["tasks"]), ["1", "2"])
        self.assertIsNone(seen.get("b"))

    def test_a_task_gone_unfinished_stays_until_its_files_are_written_again(self):
        files = {1: [self.f("a/x.mkv")], 2: [self.f("a/y.srt", rx=0)]}
        seen = comp.SeenTasks(self.path)
        seen.observe(self.state({"a": [self.t(1, "seeding", 10000), self.t(2, "downloading", 4000)]}, {"a"}, files))
        seen = self.reopen(seen)
        seen.observe(self.state({"a": [self.t(1, "seeding", 10000)]}, {"a"}, files))  # task 2 removed
        seen = self.reopen(seen)
        self.assertEqual(sorted(seen.get("a")["tasks"]), ["1", "2"])
        self.assertEqual(seen.get("a")["tasks"]["2"]["blocked"], ["/dl/a/y.srt"])
        files[5] = [self.f("a/y.srt", rx=0)]  # added again: a new task writes its file
        seen.observe(self.state({"a": [self.t(1, "seeding", 10000), self.t(5, "checking", 0)]}, {"a"}, files))
        seen = self.reopen(seen)
        self.assertEqual(sorted(seen.get("a")["tasks"]), ["1", "5"])

    def test_a_finished_task_gone_is_not_kept_beside_new_tasks(self):
        seen = comp.SeenTasks(self.path)
        seen.observe(self.state({"a": [self.t(1, "seeding", 10000)]}, {"a"}))
        seen = self.reopen(seen)
        seen.observe(self.state({"a": [self.t(2, "downloading", 100)]}, {"a"}))
        self.assertEqual(sorted(seen.new["a"]["tasks"]), ["2"])

    def test_unreadable_memory_is_empty(self):
        with open(self.path, "w") as fh:
            fh.write("{not json")
        self.assertIsNone(comp.SeenTasks(self.path).get("a"))
        for bad in ({"task": 1, "pct": 1},  # the format of no release
                    {"t": 1, "tasks": {}}, {"t": 1, "tasks": {"x": {"pct": 1, "status": "done"}}},
                    {"t": 1, "tasks": {"1": {"pct": 1, "status": "done", "files": "/dl/a"}}}):
            with open(self.path, "w") as fh:
                json.dump({"a": bad}, fh)
            self.assertIsNone(comp.SeenTasks(self.path).get("a"), bad)

    def test_downloads_not_listed_forgets_nothing(self):
        seen = comp.SeenTasks(self.path)
        seen.old = {"a": {"t": 0, "tasks": {"1": {"pct": 10000, "status": "done"}}}}
        seen.observe(self.state({}, None))
        self.assertIn("a", seen.new)

    def test_forget_absent_needs_no_freebox(self):
        cfg = self.nas.config(paths={"state_dir": self.nas.state})
        os.makedirs(self.nas.state, exist_ok=True)
        path = os.path.join(self.nas.state, "freebox_seen.json")
        touch(os.path.join(self.nas.dl, "Kept.Show.S01E01.mkv"))
        rec = {"t": 0, "tasks": {"1": {"pct": 10000, "status": "done"}}}
        with open(path, "w") as fh:
            json.dump({"kept.show.s01e01.mkv": rec, "gone.show.s01e01.mkv": rec}, fh)
        comp.forget_absent(cfg)
        with open(path) as fh:
            self.assertEqual(list(json.load(fh)), ["kept.show.s01e01.mkv"])


class TestStagedWithoutVideo(unittest.TestCase):
    def setUp(self):
        self.nas = FakeNas()

    def tearDown(self):
        self.nas.cleanup()

    def check(self):
        key = os.path.join(self.nas.tmp, "tmm.key")  # never the machine's own /etc/mediaorg/tmm.key
        with open(key, "w") as fh:
            fh.write("0123456789abcdef")
        os.chmod(key, 0o600)
        with cli_env(self.nas, tmm={"enabled": True, "base_url": "http://127.0.0.1:9", "api_key_file": key},
                     naming={"tmm_staging": True}) as path:
            _, out = run_cli("-c", path, "check")
        return out

    def test_a_staged_folder_holding_no_video_is_reported_as_such(self):
        touch(f"{self.nas.movies}/Gale Point (2024) {STAGING_TAG}/Gale.Point.2024.1080p-poster.jpg")
        out = self.check()
        self.assertIn(f"[warn] 1 folder(s) ending with ' {STAGING_TAG}' hold no video file, so tMM ignores them", out)
        self.assertIn(f"'Movies/Gale Point (2024) {STAGING_TAG}'", out)
        self.assertNotIn("after a day", out)

    def test_any_video_container_counts(self):
        touch(f"{self.nas.movies}/Gale Point (2024) {STAGING_TAG}/Gale.Point.2024.iso")  # not in video_extensions
        self.assertNotIn("hold no video file", self.check())

    def test_a_folder_that_cannot_be_read_is_not_called_empty(self):
        folder = touch(f"{self.nas.movies}/Gale Point (2024) {STAGING_TAG}/x.jpg")

        def walk(top, onerror=None, **kw):
            onerror(PermissionError(13, "Permission denied", top))
            return iter(())

        with mock.patch("mediaorg.cli.os.walk", side_effect=walk):
            self.assertIsNone(cli._has_video(os.path.dirname(folder), {".mkv"}))
        self.assertFalse(cli._has_video(os.path.dirname(folder), {".mkv"}))
        self.assertTrue(cli._has_video(os.path.dirname(folder), {".jpg"}))


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
                info = {"pct": 10000, "status": "stopped", "name": name, "files": [p]}
                seen.old = {name.casefold(): {"t": int(time.time()), "tasks": {"7": info}}}
                r = comp.FreeboxState(cfg, client, seen=seen).assess(item)
                self.assertFalse(r.ready)
                self.assertIn("temporary download file present", r.reason)
                os.unlink(part)
                item.files.pop()
                info["blocked"] = [p]
                r = comp.FreeboxState(cfg, client, seen=seen).assess(item)
                self.assertTrue(r.ready, r.reason)
                self.assertEqual(r.blocked_files, frozenset({p}))
            finally:
                fb.close()
        finally:
            nas.cleanup()


if __name__ == "__main__":
    unittest.main()
