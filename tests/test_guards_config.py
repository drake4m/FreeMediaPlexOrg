import os
import tempfile
import unittest
from unittest import mock

from mediaorg import config as C
from mediaorg.guards import LockBusy, MountEntry, PreconditionError, RunLock, check_mount, read_mountinfo

from .helpers import FakeNas

MOUNTINFO = (
    "22 1 253:0 / / rw,relatime shared:1 - xfs /dev/mapper/vg-root rw\n"
    "98 22 0:47 / /mnt/other-share rw,relatime shared:50 - autofs systemd-1 rw,fd=50\n"
    "101 98 0:52 / /mnt/other-share rw,relatime shared:52 - cifs //nas.example/share rw,vers=3.0\n"
    "110 22 0:60 /VIDEOS /mnt/media\\040share rw,relatime - cifs //nas.example/media rw,vers=3.0\n"
)


class TestMountinfo(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp()
        with os.fdopen(fd, "w") as fh:
            fh.write(MOUNTINFO)

    def tearDown(self):
        os.unlink(self.path)

    def test_parse_and_escape(self):
        e = read_mountinfo(self.path)
        self.assertEqual(e[3].mountpoint, "/mnt/media share")
        self.assertEqual(e[3].source, "//nas.example/media")
        self.assertEqual([x.fstype for x in e if x.mountpoint == "/mnt/other-share"], ["autofs", "cifs"])


class TestCheckMount(unittest.TestCase):
    def setUp(self):
        self.nas = FakeNas()
        self.cfg = self.nas.config()

    def tearDown(self):
        self.nas.cleanup()

    def check(self, entries):
        with mock.patch("mediaorg.guards.read_mountinfo", return_value=entries):
            return check_mount(self.cfg)

    def test_ok(self):
        self.assertEqual(self.check(self.nas.mount_entries()).fstype, "cifs")

    def test_unmounted_autofs_only(self):
        with self.assertRaises(PreconditionError):
            self.check([MountEntry(self.nas.mp, "autofs", "systemd-1", "")])

    def test_overmount_uses_top(self):
        with self.assertRaises(PreconditionError):
            self.check(self.nas.mount_entries() + [MountEntry(self.nas.mp, "tmpfs", "tmpfs", "")])

    def test_missing_mountpoint_dir(self):
        cfg = C.from_dict(self.nas.config_dict(mount={"mountpoint": "/nonexistent/x"},
                                               paths={"downloads": "/nonexistent/x/D", "movies": "/nonexistent/x/M",
                                                      "series": "/nonexistent/x/S", "review": "/nonexistent/x/D/_review"}))
        with mock.patch("mediaorg.guards.read_mountinfo", return_value=[]), self.assertRaises(PreconditionError):
            check_mount(cfg)


class TestLock(unittest.TestCase):
    def test_second_holder_refused(self):
        with tempfile.TemporaryDirectory() as d:
            with RunLock(os.path.join(d, "lock")):
                with self.assertRaises(LockBusy):
                    RunLock(os.path.join(d, "lock")).__enter__()
            with RunLock(os.path.join(d, "lock")):  # released after exit
                pass


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.nas = FakeNas()

    def tearDown(self):
        self.nas.cleanup()

    def bad(self, **over):
        with self.assertRaises(C.ConfigError):
            C.from_dict(self.nas.config_dict(**over))

    def test_example_config_loads(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cfg = C.load(os.path.join(here, "config", "config.example.toml"))
        self.assertIsNone(cfg.mount)
        self.assertEqual(cfg.completeness.method, "quiescence")
        self.assertEqual(cfg.paths.review, "/srv/media/downloads/_review")

    def test_validation(self):
        self.bad(paths={"downloads": "relative/path"})
        self.bad(paths={"state_dir": os.path.join(self.nas.mp, "state")})  # state on NAS
        self.bad(paths={"review": os.path.join(self.nas.movies, "_review")})  # review in library
        self.bad(paths={"movies": os.path.join(self.nas.dl, "Movies")})  # nested roots
        self.bad(completeness={"method": "magic"})
        self.bad(completeness={"min_quiet_minutes": 1})
        self.bad(freebox={"base_url": "http://192.0.2.10"})  # plain http needs explicit opt-in
        self.bad(tmm={"enabled": True, "path_map": [],  # "{paths}" template without path_map
                      "after_move_tvshow": [{"action": "update", "scope": {"name": "show", "args": "{paths}"}}]})
        self.bad(tmm={"enabled": True, "path_map": [],
                      "after_move_movie": [{"action": "update", "scope": {"name": "path", "args": "{paths}"}}]})
        self.bad(safety={"max_moves_per_run": 0})
        self.bad(paths={"typo_key": "/x"})
        self.bad(paths={"review": os.path.join(self.nas.dl, "sub", "_review")})  # not a direct child
        self.bad(paths={"review": os.path.join(self.nas.dl, "Review")})  # not an ignored name
        self.bad(completeness={"untracked": "move"})
        C.from_dict(self.nas.config_dict(paths={"review": os.path.join(self.nas.mp, "Review")}))  # outside: ok


if __name__ == "__main__":
    unittest.main()
