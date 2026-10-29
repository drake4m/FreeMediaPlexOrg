import json
import os
import shutil
import tempfile
import unittest

from mediaorg.model import Item, ItemPlan, Op
from mediaorg.transfer import Journal, execute, read_journal, reconcile, rename_noreplace, undo_ops


def read(path):
    with open(path, "rb") as fh:
        return fh.read()


class TransferBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.jdir = os.path.join(self.tmp, "journal")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def f(self, name, data=b"data"):
        p = os.path.join(self.tmp, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data)
        return p

    def plan(self, *ops, name="item"):
        path = os.path.join(self.tmp, name)
        os.makedirs(path, exist_ok=True)  # the Downloads entry exists when its plan is executed
        return ItemPlan(item=Item(name=name, path=path, is_dir=True), status="ready", ops=list(ops))


class TestRenameNoReplace(TransferBase):
    def test_refuses_to_overwrite(self):
        a, b = self.f("a", b"A"), self.f("b", b"B")
        with self.assertRaises(FileExistsError):
            rename_noreplace(a, b)
        self.assertEqual(read(b), b"B")
        self.assertTrue(os.path.exists(a))

    def test_moves_to_free_name(self):
        a = self.f("a")
        rename_noreplace(a, os.path.join(self.tmp, "c"))
        self.assertFalse(os.path.exists(a))


class TestExecute(TransferBase):
    def test_collision_created_after_planning_aborts_item(self):
        src = self.f("dl/ep.mkv", b"new")
        dst = os.path.join(self.tmp, "lib/ep.mkv")
        left = self.f("dl/extra.nfo")
        p = self.plan(Op("rename", src=src, dst=dst, role="media", expect_size=3),
                      Op("rename", src=os.path.dirname(left), dst=os.path.join(self.tmp, "rev"), role="leftovers"))
        self.f("lib/ep.mkv", b"existing")  # appears between plan and execute
        j = Journal(self.jdir, "r1")
        res = execute([p], j)
        j.close()
        self.assertEqual(res.failed, 1)
        self.assertEqual(read(dst), b"existing")
        self.assertTrue(os.path.exists(src))
        self.assertTrue(os.path.exists(left))  # cleanup of a failed item never happens

    def test_source_changed_since_scan(self):
        src = self.f("dl/ep.mkv", b"growing")
        p = self.plan(Op("rename", src=src, dst=os.path.join(self.tmp, "x.mkv"), role="media", expect_size=3))
        j = Journal(self.jdir, "r2")
        res = execute([p], j)
        j.close()
        self.assertEqual(res.failed, 1)
        self.assertIn("changed since the scan", res.errors[0])
        self.assertTrue(os.path.exists(src))

    def test_rmdir_never_removes_content(self):
        self.f("dl/item/keep.txt")
        p = self.plan(Op("rmdir", dst=os.path.join(self.tmp, "dl/item"), role="cleanup"))
        j = Journal(self.jdir, "r3")
        res = execute([p], j)
        j.close()
        self.assertEqual(res.failed, 1)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "dl/item/keep.txt")))

    def test_mkdir_existing_dir_is_not_claimed(self):
        os.makedirs(os.path.join(self.tmp, "lib/Show"))
        p = self.plan(Op("mkdir", dst=os.path.join(self.tmp, "lib/Show"), role="library-dir"))
        j = Journal(self.jdir, "r4")
        execute([p], j)
        j.close()
        self.assertEqual(undo_ops(j.path), [])  # undo will not remove a folder it did not create


class TestReconcileAndUndo(TransferBase):
    def write_journal(self, run, *recs):
        os.makedirs(self.jdir, exist_ok=True)
        path = os.path.join(self.jdir, f"{run}.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            for r in recs:
                fh.write(json.dumps(r) + "\n")
        return path

    def test_reconcile_completed_and_not_started(self):
        moved_dst = self.f("lib/a.mkv")
        not_src = self.f("dl/b.mkv")
        path = self.write_journal(
            "run1", {"state": "begin"},
            {"seq": 1, "state": "intent", "op": "rename", "src": os.path.join(self.tmp, "dl/a.mkv"), "dst": moved_dst},
            {"seq": 2, "state": "intent", "op": "rename", "src": not_src, "dst": os.path.join(self.tmp, "lib/b.mkv")},
        )
        resolved, conflicts = reconcile(self.jdir)
        self.assertEqual(conflicts, [])
        _, ops, end = read_journal(path)
        self.assertEqual(ops[1]["state"], "done")
        self.assertEqual(ops[2]["state"], "not_done")
        self.assertEqual(end["status"], "interrupted")
        self.assertEqual(reconcile(self.jdir), (0, []))  # idempotent

    def test_torn_last_line_is_tolerated(self):
        path = self.write_journal("run2", {"state": "begin"})
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"seq": 1, "state": "inte')
        self.assertEqual(reconcile(self.jdir)[1], [])

    def test_undo_order(self):
        path = self.write_journal(
            "run3", {"state": "begin"},
            {"seq": 1, "state": "intent", "op": "mkdir", "dst": "/l/Show"}, {"seq": 1, "state": "done"},
            {"seq": 2, "state": "intent", "op": "rename", "src": "/d/x", "dst": "/l/Show/x"}, {"seq": 2, "state": "done"},
            {"seq": 3, "state": "intent", "op": "rmdir", "dst": "/d"}, {"seq": 3, "state": "done"},
            {"seq": 4, "state": "intent", "op": "rename", "src": "/d/y", "dst": "/l/y"}, {"seq": 4, "state": "failed"},
            {"state": "end", "status": "errors"},
        )
        inv = undo_ops(path)
        self.assertEqual([(o.kind, o.src, o.dst) for o in inv],
                         [("mkdir", None, "/d"), ("rename", "/l/Show/x", "/d/x"), ("rmdir", None, "/l/Show")])


if __name__ == "__main__":
    unittest.main()
