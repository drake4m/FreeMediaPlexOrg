"""1.0.1: byte-identical downloads deleted, empty (0-byte) library files replaced, review folders
purged after their retention, Freebox contacted only when needed, item-level change check."""

import glob
import json
import os
import time
import unittest
from unittest import mock

from mediaorg import config as C
from mediaorg import review
from mediaorg.library import Library
from mediaorg.model import Readiness
from mediaorg.planner import Planner
from mediaorg.scan import scan_downloads
from mediaorg.transfer import Journal, execute, read_journal, reconcile, undo_plan

from .helpers import OLD, FakeNas, age_tree, cli_env, run_cli, touch

READY = Readiness(True, "test")
TORCH = "Series/Brightwater (2025)/Season 1/Brightwater.S01E01.1080p.mkv"  # library file of the fake NAS


def rid(seconds_ago: float) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() - seconds_ago)) + "-abcd"


class Base(unittest.TestCase):
    def setUp(self):
        self.nas = FakeNas()

    def tearDown(self):
        self.nas.cleanup()

    def lib(self, rel, data=b"library", mtime=OLD):
        return touch(os.path.join(self.nas.mp, rel), data, mtime)

    def dl(self, rel, data=b"download", mtime=OLD):
        return touch(os.path.join(self.nas.dl, rel), data, mtime)

    def read(self, rel):
        try:
            with open(os.path.join(self.nas.mp, rel), "rb") as fh:
                return fh.read()
        except OSError:
            return None

    def run_(self, *args, **cfg):
        with cli_env(self.nas, **cfg) as path:
            return run_cli("-c", path, *args)

    def plan(self, **cfg):
        c = self.nas.config(**cfg)
        items, _ = scan_downloads(c)
        planner = Planner(c, Library(c.paths.movies, c.paths.series, c.media.video_extensions), "R")
        return [planner.plan_item(i, READY) for i in items]

    def execute(self, plans):
        j = Journal(os.path.join(self.nas.state, "journal"), "R")
        try:
            return execute([p for p in plans if p.ops], j), j.path
        finally:
            j.close()

    def review(self, kind):
        return glob.glob(os.path.join(self.nas.dl, "_review", kind, "*", "**"), recursive=True)


class TestIdenticalDuplicates(Base):
    def test_identical_episode_is_deleted_not_reviewed(self):
        data = os.urandom(4096)
        self.lib(TORCH, data)
        self.dl("Brightwater.S01E01.1080p.WEB.H264-GRP.mkv", data)
        before = self.nas.snapshot()
        code, out = self.run_("run")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.nas.snapshot(), before)  # dry-run
        self.assertIn("    delete    Brightwater.S01E01.1080p.WEB.H264-GRP.mkv", out)
        self.assertIn("byte-identical copy, deleted from Downloads", out)
        self.assertIn("deletes 1 (identical downloads 1, empty library files 0)", out)
        code, out = self.run_("run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertFalse(self.nas.exists("Downloads/Brightwater.S01E01.1080p.WEB.H264-GRP.mkv"))
        self.assertEqual(self.read(TORCH), data)
        self.assertEqual([p for p in self.review("duplicates") if os.path.isfile(p)], [])

    def test_identical_movie_in_folder_deleted_subtitle_joins_the_library_copy(self):
        data = os.urandom(5000)
        self.lib("Movies/Corvid (2019)/Corvid.(2019).mkv", data)
        folder = "Corvid.2019.2160p.UHD.BluRay.x265-GRP"
        self.dl(f"{folder}/{folder}.mkv", data)
        self.dl(f"{folder}/info.nfo", b"nfo")
        self.dl(f"{folder}/{folder}.en.srt", b"sub")
        age_tree(os.path.join(self.nas.dl, folder))
        code, out = self.run_("run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertFalse(self.nas.exists("Downloads", folder))
        self.assertEqual(self.read("Movies/Corvid (2019)/Corvid.(2019).mkv"), data)
        self.assertEqual(self.read("Movies/Corvid (2019)/Corvid.(2019).en.srt"), b"sub")
        left = glob.glob(os.path.join(self.nas.dl, "_review/leftovers/*", folder, "*"))
        self.assertEqual(sorted(os.path.basename(p) for p in left), ["info.nfo"])
        self.assertFalse(glob.glob(os.path.join(self.nas.dl, "_review/**", f"{folder}.mkv"), recursive=True))

    def test_same_size_different_bytes_goes_to_review(self):
        self.lib(TORCH, b"A" * 1000)
        self.dl("Brightwater.S01E01.1080p.WEB.H264-GRP.mkv", b"B" * 1000)
        code, out = self.run_("run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(glob.glob(os.path.join(self.nas.dl, "_review/duplicates/*/Brightwater.S01E01*"))), 1)
        self.assertEqual(self.read(TORCH), b"A" * 1000)

    def test_option_off_keeps_identical_copy_for_review(self):
        self.lib(TORCH, b"same")
        self.dl("Brightwater.S01E01.1080p.WEB.H264-GRP.mkv", b"same")
        code, out = self.run_("run", "--apply", review={"delete_identical_duplicates": False})
        self.assertEqual(code, 0, out)
        self.assertEqual(len(glob.glob(os.path.join(self.nas.dl, "_review/duplicates/*/Brightwater.S01E01*"))), 1)

    def test_samples_and_empty_files_are_never_the_reference(self):
        self.lib("Series/Cistern (2022)/Season 1/Cistern.S01E02.1080p-sample.mkv", b"same")
        self.lib("Series/Cistern (2022)/Season 1/Cistern.S01E02.mkv", b"")
        self.dl("Cistern.S01E02.1080p.WEB.h264-GRP.mkv", b"same")
        plans = self.plan()
        self.assertEqual(plans[0].decisions[0].action, "move")

    def test_library_copy_changed_after_planning_blocks_the_deletion(self):
        self.lib(TORCH, b"same")
        src = self.dl("Brightwater.S01E01.1080p.WEB.H264-GRP.mkv", b"same")
        plans = self.plan()
        self.assertEqual(plans[0].decisions[0].action, "delete")
        self.lib(TORCH, b"same", mtime=time.time())  # e.g. rewritten by another program meanwhile
        res, _ = self.execute(plans)
        self.assertEqual(res.failed, 1)
        self.assertIn("library copy", res.errors[0])
        self.assertTrue(os.path.exists(src))

    def test_download_changed_after_planning_blocks_the_deletion(self):
        self.lib(TORCH, b"same")
        src = self.dl("Brightwater.S01E01.1080p.WEB.H264-GRP.mkv", b"same")
        plans = self.plan()
        touch(src, b"same plus more")
        res, _ = self.execute(plans)
        self.assertEqual(res.failed, 1)
        self.assertTrue(os.path.exists(src))

    def test_unplanned_deletion_kinds_are_refused(self):
        from mediaorg.model import Op
        from mediaorg.transfer import _verify_unlink
        p = self.dl("x.mkv", b"x")
        st = os.stat(p)
        self.assertIn("refusing", _verify_unlink(Op("unlink", dst=p, role="leftovers", expect_size=1,
                                                    expect_mtime=st.st_mtime)))
        self.assertIn("no library copy", _verify_unlink(Op("unlink", dst=p, role="identical-duplicate",
                                                           expect_size=1, expect_mtime=st.st_mtime)))
        self.assertIn("refusing", _verify_unlink(Op("unlink", dst=p, role="empty-placeholder", expect_size=1,
                                                    expect_mtime=st.st_mtime)))

    def test_loose_identical_with_attached_subtitle(self):
        self.lib(TORCH, b"same")
        self.dl("Brightwater.S01E01.1080p.WEB.H264-GRP.mkv", b"same")
        self.dl("Brightwater.S01E01.1080p.WEB.H264-GRP.en.srt", b"sub")
        code, out = self.run_("run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertEqual(os.listdir(self.nas.dl), [])  # nothing left, not even a review folder
        self.assertEqual(self.read("Series/Brightwater (2025)/Season 1/Brightwater.S01E01.1080p.en.srt"), b"sub")

    def test_partial_item_deletes_only_the_identical_file(self):
        self.lib(TORCH, b"same")
        pack = "Brightwater.S01.1080p.WEB.H264-GRP"
        self.dl(f"{pack}/Brightwater.S01E01.1080p.WEB.H264-GRP.mkv", b"same")
        self.dl(f"{pack}/Brightwater.Untold.Episode.1080p.WEB.H264-GRP.mkv", b"what is this")
        age_tree(os.path.join(self.nas.dl, pack))
        code, out = self.run_("run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertIn("[PARTIAL]", out)
        self.assertEqual(os.listdir(os.path.join(self.nas.dl, pack)), ["Brightwater.Untold.Episode.1080p.WEB.H264-GRP.mkv"])

    def test_undo_reports_what_cannot_be_restored(self):
        self.lib(TORCH, b"same")
        self.dl("Brightwater.S01E01.1080p.WEB.H264-GRP.mkv", b"same")
        self.dl("Cistern.S03E06.1080p.WEB.h264-GRP.mkv", b"cistern")
        code, out = self.run_("run", "--apply")
        self.assertEqual(code, 0, out)
        run = sorted(os.listdir(os.path.join(self.nas.state, "journal")))[0][:-6]
        code, out = self.run_("undo", run, "--apply")
        self.assertEqual(code, 0, out)
        self.assertIn("note: not restored:", out)
        self.assertIn("byte-identical copy of", out)
        self.assertTrue(self.nas.exists("Downloads/Cistern.S03E06.1080p.WEB.h264-GRP.mkv"))
        self.assertFalse(self.nas.exists("Downloads/Brightwater.S01E01.1080p.WEB.H264-GRP.mkv"))



class TestEmptyPlaceholders(Base):
    def test_episode_with_other_name_moves_in_and_placeholder_goes(self):
        self.lib("Series/Cistern (2022)/Season 2/Cistern.S02E09.1080p.mkv", b"e9")
        self.lib("Series/Cistern (2022)/Season 2/Cistern.S02E10.mkv", b"")
        self.dl("Cistern.S02E10.1080p.WEB.H264-GRP.mkv", b"real")
        code, out = self.run_("run")
        self.assertIn("(empty 0-byte library file, replaced)", out)
        self.assertIn("deletes 1 (identical downloads 0, empty library files 1)", out)
        code, out = self.run_("run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read("Series/Cistern (2022)/Season 2/Cistern.S02E10.1080p.WEB.H264-GRP.mkv"), b"real")
        self.assertFalse(self.nas.exists("Series/Cistern (2022)/Season 2/Cistern.S02E10.mkv"))
        self.assertEqual(self.read("Series/Cistern (2022)/Season 2/Cistern.S02E09.1080p.mkv"), b"e9")

    def test_same_name_and_other_case_is_replaced(self):
        self.lib("Series/Prairie Duke (2021)/Season 2/prairie.duke.s02e08.1080p.web.h264-grp.mkv", b"")
        self.dl("Prairie.Duke.S02E08.1080p.WEB.H264-GRP.mkv", b"real")
        code, out = self.run_("run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertEqual(os.listdir(os.path.join(self.nas.series, "Prairie Duke (2021)", "Season 2")),
                         ["Prairie.Duke.S02E08.1080p.WEB.H264-GRP.mkv"])
        self.assertEqual(self.read("Series/Prairie Duke (2021)/Season 2/Prairie.Duke.S02E08.1080p.WEB.H264-GRP.mkv"),
                         b"real")

    def test_movie_placeholder_is_replaced(self):
        os.remove(os.path.join(self.nas.movies, "Sentinel II (2023)", "Sentinel.II.(2023).mkv"))  # fixture
        os.remove(os.path.join(self.nas.movies, "Sentinel II (2023)", "movie.nfo"))
        self.lib("Movies/Sentinel II (2023)/Sentinel.II.2023.1080p.BluRay.mkv", b"")
        self.dl("Sentinel.II.2023.1080p.WEB.H264-GRP.mkv", b"movie")
        code, out = self.run_("run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertEqual(os.listdir(os.path.join(self.nas.movies, "Sentinel II (2023)")),
                         ["Sentinel.II.2023.1080p.WEB.H264-GRP.mkv"])

    def test_placeholder_filled_meanwhile_is_kept_and_nothing_moves(self):
        ph = self.lib("Series/Cistern (2022)/Season 2/Cistern.S02E10.mkv", b"")
        src = self.dl("Cistern.S02E10.1080p.WEB.H264-GRP.mkv", b"real")
        plans = self.plan()
        with open(ph, "wb") as fh:
            fh.write(b"someone wrote this")
        res, _ = self.execute(plans)
        self.assertEqual(res.failed, 1)
        self.assertIn("empty library file changed", res.errors[0])
        self.assertTrue(os.path.exists(src))
        with open(ph, "rb") as fh:
            self.assertEqual(fh.read(), b"someone wrote this")

    def test_undo_restores_the_placeholder_and_the_download(self):
        ph = self.lib("Series/Prairie Duke (2021)/Season 2/Prairie.Duke.S02E08.1080p.WEB.H264-GRP.mkv", b"",
                      mtime=OLD - 1000)
        self.dl("Prairie.Duke.S02E08.1080p.WEB.H264-GRP.mkv", b"real")
        before = self.nas.snapshot()
        code, out = self.run_("run", "--apply")
        self.assertEqual(code, 0, out)
        run = sorted(os.listdir(os.path.join(self.nas.state, "journal")))[0][:-6]
        code, out = self.run_("undo", run, "--apply")
        self.assertEqual(code, 0, out)
        self.assertIn("create (empty file)", out)
        self.assertEqual({k: v for k, v in self.nas.snapshot().items() if "_review" not in k}, before)
        self.assertAlmostEqual(os.stat(ph).st_mtime, OLD - 1000, delta=1)

    def test_journal_reconcile_of_interrupted_deletions(self):
        jdir = os.path.join(self.nas.state, "journal")
        os.makedirs(jdir)
        gone = os.path.join(self.nas.dl, "gone.mkv")
        here = self.dl("here.mkv")
        with open(os.path.join(jdir, "x.jsonl"), "w") as fh:
            for r in ({"state": "begin"}, {"seq": 1, "state": "intent", "op": "unlink", "dst": gone},
                      {"seq": 2, "state": "intent", "op": "unlink", "dst": here}):
                fh.write(json.dumps(r) + "\n")
        self.assertEqual(reconcile(jdir)[1], [])
        _, ops, _ = read_journal(os.path.join(jdir, "x.jsonl"))
        self.assertEqual((ops[1]["state"], ops[2]["state"]), ("done", "not_done"))

    def test_library_report_flags_empty_videos(self):
        os.remove(os.path.join(self.nas.movies, "Sentinel II (2023)", "Sentinel.II.(2023).mkv"))  # fixture
        self.lib("Series/Cistern (2022)/Season 1/Cistern.S01E02.mkv", b"")
        self.lib("Movies/Sentinel II (2023)/Sentinel.II.2023.1080p.BluRay.mkv", b"")
        code, out = self.run_("library")
        self.assertEqual(code, 0, out)
        self.assertIn("[warn] Series/Cistern (2022)/Season 1: 1 empty (0-byte) video file(s)", out)
        self.assertIn("'Cistern.S01E02.mkv'", out)
        self.assertIn("[warn] Movies/Sentinel II (2023): 1 empty (0-byte) video file(s)", out)
        self.assertIn("[note] Movies/Sentinel II (2023): no movie file", out)
        cistern = next(line for line in out.splitlines() if line.strip().startswith("Cistern (2022)"))
        self.assertIn("S1:0", cistern)  # the empty file is not counted as episode 2


def journal_dup(nas, run_id: str, dst: str, size: int, purge: bool = True) -> None:
    """The journal record a run writes when it moves a duplicate to review/duplicates ("purge": true
    for a certain duplicate; 1.0.0 and uncertain duplicates have no such flag)."""
    jdir = os.path.join(nas.state, "journal")
    os.makedirs(jdir, exist_ok=True)
    rec = {"seq": 1, "state": "intent", "op": "rename", "src": "/x", "dst": dst, "role": "duplicate", "size": size}
    if purge:
        rec["purge"] = True
    with open(os.path.join(jdir, f"{run_id}.jsonl"), "a", encoding="utf-8") as fh:
        for r in ({"state": "begin", "kind": "run"}, rec, {"seq": 1, "state": "done"},
                  {"state": "end", "status": "ok"}):
            fh.write(json.dumps(r) + "\n")


class TestReviewPurge(Base):
    def setUp(self):
        super().setUp()
        old, new = rid(8 * 86400), rid(86400)
        self.old_dup = self.dl(f"_review/duplicates/{old}/Old.Show.S01E01.mkv")
        journal_dup(self.nas, old, self.old_dup, len(b"download"))
        self.new_dup = self.dl(f"_review/duplicates/{new}/New.Show.S01E01.mkv")
        journal_dup(self.nas, new, self.new_dup, len(b"download"))
        # a duplicates folder without journal record (1.0.0 moved whole items): its videos are kept
        self.legacy_dup = self.dl(f"_review/duplicates/{rid(10 * 86400)}/Legacy.S01E01/Legacy.S01E01.mkv")
        # a duplicate journaled without "purge" (1.0.0, or not certain): kept too
        unsure = rid(11 * 86400)
        self.unsure_dup = self.dl(f"_review/duplicates/{unsure}/Unsure.S01E01.mkv")
        journal_dup(self.nas, unsure, self.unsure_dup, len(b"download"), purge=False)
        self.old_left = self.dl(f"_review/leftovers/{rid(30 * 86400)}/Old.Movie/info.nfo")
        self.notes = self.dl("_review/my notes.txt")
        self.foreign = self.dl("_review/duplicates/keep-me/file.mkv")
        self.file_named_like_run = self.dl(f"_review/duplicates/{rid(9 * 86400)[:-4]}beef")

    def test_due_and_dry_run(self):
        due = review.due(self.nas.config())
        self.assertEqual(sorted((d.kind, d.action) for d in due),
                         [("duplicates", "kept"), ("duplicates", "kept"), ("duplicates", "purge"), ("leftovers", "purge")])
        before = self.nas.snapshot()
        code, out = self.run_("run")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.nas.snapshot(), before)
        self.assertEqual(out.count("[PURGE] "), 2)
        self.assertEqual(out.count("[KEPT] "), 2)
        self.assertIn("'Legacy.S01E01/Legacy.S01E01.mkv'", out)
        self.assertIn("deleting 1 file(s), 8 B: 'Old.Show.S01E01.mkv'", out)  # names what it deletes
        self.assertIn("review folders to purge 2", out)

    def test_apply_purges_only_expired_run_folders(self):
        code, out = self.run_("run", "--apply")
        self.assertEqual(code, 0, out)
        for p in (self.old_dup, self.old_left):
            self.assertFalse(os.path.exists(os.path.dirname(p)), p)
        self.assertFalse(os.path.exists(os.path.dirname(os.path.dirname(self.old_left))))
        for p in (self.new_dup, self.legacy_dup, self.unsure_dup, self.notes, self.foreign, self.file_named_like_run):
            self.assertTrue(os.path.exists(p), p)
        report = max(glob.glob(os.path.join(self.nas.state, "runs", "*.json")), key=os.path.getmtime)
        with open(report, encoding="utf-8") as fh:
            done = json.load(fh)["review_purge_done"]
        self.assertEqual(sorted(d["deleted_files"] for d in done), [1, 1])
        self.assertEqual(sorted(x for d in done for x in d["deleted"]), ["Old.Movie/info.nfo", "Old.Show.S01E01.mkv"])

    def test_leftover_rules_keep_videos_and_big_files(self):
        run = rid(8 * 86400)
        base = f"_review/leftovers/{run}/Show.S01.Pack"
        keep_video = self.dl(f"{base}/Featurettes/Making of.mkv")
        keep_big = self.dl(f"{base}/disc.iso")
        os.truncate(keep_big, 300 << 20)  # sparse
        keep_ts = self.dl(f"{base}/Show.S01E01.ts")
        gone = [self.dl(f"{base}/Sample/show.s01e01-sample.mkv"), self.dl(f"{base}/info.nfo"),
                self.dl(f"{base}/cover.jpg"), self.dl(f"{base}/Show.S01E01.Sample.mkv"),
                self.dl(f"{base}/Subs/French.srt"), self.dl(f"{base}/www.site.url")]
        big_sample = self.dl(f"{base}/Sample/huge-sample.mkv")
        os.truncate(big_sample, 400 << 20)
        yours = [self.dl(f"{base}/setup.exe"), self.dl(f"{base}/manual.pdf"), self.dl(f"{base}/extras.zip"),
                 self.dl(f"{base}/Show.S01E02")]  # never deleted: programs, documents, ...
        photo = self.dl(f"{base}/IMG_0001.jpg")
        os.truncate(photo, 3 << 20)  # a photo, not release artwork
        named_sample = self.dl(f"_review/leftovers/{run}/Sample/clip.ts")  # a download named Sample
        code, out = self.run_("run", "--apply")
        self.assertEqual(code, 0, out)
        for p in (keep_video, keep_big, keep_ts, big_sample, *yours, photo, named_sample):
            self.assertTrue(os.path.exists(p), p)
        for p in gone:
            self.assertFalse(os.path.exists(p), p)
        code, out = self.run_("run")
        self.assertIn(f"[KEPT] Downloads/_review/leftovers/{run}  -- 8.0 day(s) old; 10 file(s) kept for you", out)
        self.assertIn("'Sample/clip.ts'", out)

    def test_zero_purges_this_runs_duplicates_and_minus_one_keeps_all(self):
        self.lib(TORCH, b"A" * 10)
        self.dl("Brightwater.2025.S01E01.1080p.WEB.H264-GRP.mkv", b"B" * 10)  # year = the folder's: certain
        code, out = self.run_("run", "--apply", review={"duplicates_retention_days": -1,
                                                        "leftovers_retention_days": -1})
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.exists(self.old_dup) and os.path.exists(self.old_left))
        dups = glob.glob(os.path.join(self.nas.dl, "_review/duplicates/*/Brightwater*"))
        self.assertEqual(len(dups), 1)
        code, out = self.run_("run", "--apply", review={"duplicates_retention_days": 0})
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(dups[0]))
        self.assertFalse(os.path.exists(self.new_dup))
        self.assertTrue(os.path.exists(self.legacy_dup))
        self.assertTrue(os.path.exists(self.foreign))
        # 0 = deleted at the end of the very run that moved the duplicate in
        self.dl("Brightwater.2025.S01E01.720p.WEB.H264-GRP.mkv", b"C" * 10)
        code, out = self.run_("run", "--apply", review={"duplicates_retention_days": 0})
        self.assertEqual(code, 0, out)
        self.assertIn("[NOTE] review.duplicates_retention_days = 0", out)  # announced before acting
        # the newest report (run ids of runs started in the same second do not sort by time)
        report = max(glob.glob(os.path.join(self.nas.state, "runs", "*.json")), key=os.path.getmtime)
        with open(report, encoding="utf-8") as fh:
            done = json.load(fh)["review_purge_done"]
        self.assertTrue(any(d["deleted_files"] == 1 and d["kind"] == "duplicates" for d in done), done)
        self.assertFalse(self.nas.exists("Downloads/Brightwater.2025.S01E01.720p.WEB.H264-GRP.mkv"))
        self.assertEqual(glob.glob(os.path.join(self.nas.dl, "_review/duplicates/*/Brightwater*")), [])
        self.assertEqual(self.read(TORCH), b"A" * 10)

    def test_uncertain_duplicates_are_kept_even_with_retention_0(self):
        self.lib(TORCH, b"A" * 10)
        cases = {"Brightwater.S01E01.720p.WEB.H264-GRP.mkv": b"B" * 10,           # no year: maybe another show
                 "Brightwater.2025.S01E01.REPACK.1080p.WEB.H264-GRP.mkv": b"C" * 10,  # a fix of the release
                 "Brightwater.2025.S01E01.2160p.WEB.H264-GRP.mkv": b"D" * 50}      # larger: repaired / better?
        for name, data in cases.items():
            self.dl(name, data)
        code, out = self.run_("run", "--apply", "--max-moves", "10", review={"duplicates_retention_days": 0})
        self.assertEqual(code, 0, out)
        self.assertIn("no year in the release name to confirm it is the same show", out)
        self.assertIn("it is a REPACK/PROPER release", out)
        self.assertIn("it is larger than the library copy", out)
        for name in cases:
            self.assertEqual(len(glob.glob(os.path.join(self.nas.dl, "_review/duplicates/*", name))), 1, name)

    def test_only_runs_do_not_purge(self):
        self.dl("Cistern.S03E06.1080p.WEB.h264-GRP.mkv")
        code, out = self.run_("run", "--apply", "--only", "Cistern.S03E06.1080p.WEB.h264-GRP.mkv")
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.exists(self.old_dup))

    def test_symlinked_run_folder_is_not_followed(self):
        target = os.path.join(self.nas.tmp, "elsewhere")
        touch(os.path.join(target, "precious.mkv"))
        os.symlink(target, os.path.join(self.nas.dl, "_review", "duplicates", rid(20 * 86400)))
        code, out = self.run_("run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.exists(os.path.join(target, "precious.mkv")))

    def test_purge_failure_is_reported(self):
        with mock.patch("mediaorg.review._remove", side_effect=PermissionError(13, "Permission denied")):
            code, out = self.run_("run", "--apply")
        self.assertEqual(code, 1)
        self.assertTrue(os.path.exists(self.old_dup))

    def test_nas_lost_during_purge_stops_it(self):
        import errno
        with mock.patch("mediaorg.review._remove", side_effect=OSError(errno.EHOSTDOWN, "Host is down")):
            code, out = self.run_("run", "--apply")
        self.assertEqual(code, 1)
        self.assertTrue(os.path.exists(self.old_dup) and os.path.exists(self.old_left))

    def test_config_validation(self):
        for bad in ({"duplicates_retention_days": -2}, {"leftovers_retention_days": "7"},
                    {"duplicates_retention_days": True}, {"delete_identical_duplicates": "yes"}):
            with self.assertRaises(C.ConfigError, msg=str(bad)):
                C.from_dict(self.nas.config_dict(review=bad))
        cfg = C.from_dict(self.nas.config_dict())
        self.assertEqual((cfg.review.delete_identical_duplicates, cfg.review.duplicates_retention_days,
                          cfg.review.leftovers_retention_days), (True, 7, 7))
        self.assertEqual(C.Completeness().min_quiet_minutes, 10)


class TestFreeboxOnlyWhenNeeded(Base):
    def freebox_cfg(self):
        tok = os.path.join(self.nas.state, "tok.json")
        os.makedirs(self.nas.state, exist_ok=True)
        with open(tok, "w") as fh:
            json.dump({"app_id": "mediaorg", "app_token": "x"}, fh)
        os.chmod(tok, 0o600)
        return {"completeness": {"method": "freebox"},
                "freebox": {"remote_downloads_dir": "/Freebox/Downloads", "allow_insecure_http": True,
                            "base_url": "http://127.0.0.1:9", "token_file": tok, "timeout_seconds": 2}}

    def test_no_video_no_freebox_call(self):
        self.dl("Some Software 1.0/setup.exe")
        with mock.patch("mediaorg.completeness.make_assessor", side_effect=AssertionError("called")):
            code, out = self.run_("run", "--apply", **self.freebox_cfg())
        self.assertEqual(code, 0, out)
        self.assertIn("[IGNORED] Some Software 1.0", out)

    def test_video_needs_the_freebox(self):
        self.dl("Cistern.S03E06.1080p.WEB.h264-GRP.mkv")
        code, out = self.run_("run", "--apply", **self.freebox_cfg())
        self.assertEqual(code, 3)
        self.assertTrue(self.nas.exists("Downloads/Cistern.S03E06.1080p.WEB.h264-GRP.mkv"))


class TestItemChangedSinceScan(Base):
    def test_a_late_file_in_a_folder_stops_the_whole_item(self):
        pack = "Cistern.S03.1080p.WEB.h264-GRP"
        self.dl(f"{pack}/Cistern.S03E06.1080p.WEB.h264-GRP.mkv", b"e6")
        self.dl(f"{pack}/Cistern.S03E07.1080p.WEB.h264-GRP.mkv", b"e7")
        age_tree(os.path.join(self.nas.dl, pack))
        plans = self.plan()
        self.dl(f"{pack}/Cistern.S03E08.1080p.WEB.h264-GRP.mkv", b"e8")  # still arriving
        res, _ = self.execute(plans)
        self.assertEqual(res.failed, 1)
        self.assertIn("folder content changed since the scan", res.errors[0])
        self.assertEqual(sorted(os.listdir(os.path.join(self.nas.dl, pack))),
                         ["Cistern.S03E06.1080p.WEB.h264-GRP.mkv", "Cistern.S03E07.1080p.WEB.h264-GRP.mkv",
                          "Cistern.S03E08.1080p.WEB.h264-GRP.mkv"])


class TestLooseLeftovers(Base):
    def test_subtitle_that_cannot_follow_its_video_goes_to_review(self):
        self.lib("Movies/Quillon (2002)/Quillon.2002.1080p.WEBRip.x264.en.srt", b"existing sub")
        os.makedirs(os.path.join(self.nas.movies, "Quillon (2002)"), exist_ok=True)
        self.dl("Quillon.2002.1080p.WEBRip.x264.mkv", b"v")
        self.dl("Quillon.2002.1080p.WEBRip.x264.en.srt", b"new sub")
        code, out = self.run_("run", "--apply")
        self.assertEqual(code, 0, out)
        self.assertTrue(self.nas.exists("Movies/Quillon (2002)/Quillon.2002.1080p.WEBRip.x264.mkv"))
        self.assertEqual(self.read("Movies/Quillon (2002)/Quillon.2002.1080p.WEBRip.x264.en.srt"), b"existing sub")
        left = glob.glob(os.path.join(self.nas.dl, "_review/leftovers/*/Quillon.2002.1080p.WEBRip.x264.en.srt"))
        self.assertEqual(len(left), 1)


class TestUndoPlanUnit(unittest.TestCase):
    def test_inverse_of_deletions(self):
        import tempfile
        d = tempfile.mkdtemp()
        p = os.path.join(d, "j.jsonl")
        with open(p, "w") as fh:
            for r in ({"state": "begin"},
                      {"seq": 1, "state": "intent", "op": "unlink", "dst": "/l/e.mkv", "role": "empty-placeholder",
                       "size": 0, "mtime": 123.0}, {"seq": 1, "state": "done"},
                      {"seq": 2, "state": "intent", "op": "rename", "src": "/d/x.mkv", "dst": "/l/x.mkv"},
                      {"seq": 2, "state": "done"},
                      {"seq": 3, "state": "intent", "op": "unlink", "dst": "/d/y.mkv", "role": "identical-duplicate",
                       "ref": "/l/y.mkv"}, {"seq": 3, "state": "done"}):
                fh.write(json.dumps(r) + "\n")
        ops, notes = undo_plan(p)
        self.assertEqual([(o.kind, o.src, o.dst) for o in ops],
                         [("rename", "/l/x.mkv", "/d/x.mkv"), ("create", None, "/l/e.mkv")])
        self.assertEqual(ops[1].expect_mtime, 123.0)
        self.assertEqual(len(notes), 1)
        self.assertIn("/l/y.mkv", notes[0])


class TestQuietFingerprint(unittest.TestCase):
    def test_minutes_do_not_count_as_change(self):
        from mediaorg.model import Item, ItemPlan
        from mediaorg.report import fingerprint
        a = ItemPlan(Item("x", "/x", False), "wait", "modified 3 min ago (< 10 min)")
        b = ItemPlan(Item("x", "/x", False), "wait", "modified 7 min ago (< 10 min)")
        c = ItemPlan(Item("x", "/x", False), "wait", "Freebox task is seeding")
        self.assertEqual(fingerprint(a), fingerprint(b))
        self.assertNotEqual(fingerprint(a), fingerprint(c))


if __name__ == "__main__":
    unittest.main()
