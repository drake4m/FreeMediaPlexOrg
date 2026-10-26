"""systemd units written from the configuration (`mediaorg service ...`)."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from mediaorg import config as C
from mediaorg import service


def cfg(**sections):
    d = {"paths": {"downloads": "/srv/media/downloads", "movies": "/srv/media/movies", "series": "/srv/media/tv"}}
    for k, v in sections.items():
        d.setdefault(k, {}).update(v)
    return C.from_dict(d)


class TestRender(unittest.TestCase):
    def test_run_unit_carries_folders_mounts_and_sandbox(self):
        units = service.render_units(cfg(), "/etc/mediaorg/config.toml")
        self.assertEqual(sorted(units), ["mediaorg.service", "mediaorg.timer"])  # tMM off: no nightly sync
        s = units["mediaorg.service"]
        for line in ("User=mediaorg", "Type=oneshot", "SuccessExitStatus=4", "NoNewPrivileges=yes",
                     "ProtectSystem=strict", "ProtectHome=yes", "CapabilityBoundingSet=",
                     "SystemCallFilter=@system-service", "LogRateLimitIntervalSec=30s",
                     "ExecStart=/usr/local/bin/mediaorg --config /etc/mediaorg/config.toml run --apply --quiet",
                     "ConditionPathExists=/etc/mediaorg/config.toml",
                     "RequiresMountsFor=/srv/media/downloads /srv/media/movies /srv/media/tv",
                     # one writable folder holds every move: each ReadWritePaths= entry is a mount of its own,
                     # and a rename between two of them fails with EXDEV (seen in CI: Downloads -> TV)
                     "ReadWritePaths=/var/lib/mediaorg -/srv/media"):
            self.assertIn(line + "\n", s)
        self.assertTrue(s.startswith(service.MARK))

    def test_mount_check_and_home_folders(self):
        c = cfg(paths={"downloads": "/home/me/media/dl", "movies": "/home/me/media/movies",
                       "series": "/home/me/media/tv"},
                mount={"mountpoint": "/home/me/media", "fstype": "cifs", "source": "//nas/x"},
                logging={"file": "/var/log/mediaorg/mediaorg.log"})
        s = service.render_units(c, "/etc/mediaorg/config.toml")["mediaorg.service"]
        self.assertIn("RequiresMountsFor=/home/me/media\n", s)
        self.assertIn("ProtectHome=read-only\n", s)  # writable folders below /home: read-only, plus ReadWritePaths
        self.assertIn(" -/var/log/mediaorg\n", s)

    def test_quoting_of_unusual_paths(self):
        c = cfg(paths={"downloads": "/srv/my media/dl", "movies": "/srv/my media/100% movies",
                       "series": "/srv/my media/tv"})
        units = service.render_units(c, "/etc/media org/config.toml")
        s = units["mediaorg.service"]
        self.assertIn('ReadWritePaths=/var/lib/mediaorg "-/srv/my media"\n', s)
        self.assertIn('"/srv/my media/100%% movies"', s)  # RequiresMountsFor=, % escaped for systemd
        self.assertIn('ExecStart=/usr/local/bin/mediaorg --config "/etc/media org/config.toml" run', s)
        self.assertIn('ConditionPathExists=/etc/media org/config.toml\n', s)

    def test_timers(self):
        def timer(n):
            return service.render_units(cfg(schedule={"run_every_minutes": n}), "/c.toml")["mediaorg.timer"]
        self.assertIn("OnCalendar=*:0/5\n", timer(5))
        self.assertIn("OnCalendar=*:0/15\n", timer(15))
        self.assertIn("OnCalendar=0/2:00:00\n", timer(120))
        self.assertIn("OnUnitInactiveSec=7min\n", timer(7))
        self.assertIn("OnUnitInactiveSec=90min\n", timer(90))
        self.assertIn("OnBootSec=2min\n", timer(90))
        for n in (5, 120, 1440):  # a fixed time missed while the machine was off runs at the next boot
            self.assertIn("\nPersistent=true\n", timer(n))
        units = service.render_units(cfg(tmm={"enabled": True}, schedule={"tmm_sync_time": "04:10"}), "/c.toml")
        self.assertIn("OnCalendar=*-*-* 04:10:00\n", units["mediaorg-tmm-sync.timer"])
        self.assertIn("ExecStart=/usr/local/bin/mediaorg --config /c.toml tmm sync --apply\n",
                      units["mediaorg-tmm-sync.service"])
        self.assertIn("Restart=on-failure\n", units["mediaorg-tmm-sync.service"])
        self.assertNotIn("mediaorg-tmm-sync.timer",
                         service.render_units(cfg(tmm={"enabled": True}, schedule={"tmm_sync_time": ""}), "/c.toml"))

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze not installed")
    def test_systemd_accepts_the_units(self):
        """systemd parses every line of the generated units (unusual paths included)."""
        tmp = tempfile.mkdtemp()
        try:
            c = cfg(paths={"downloads": "/srv/my media/dl", "movies": "/srv/my media/100% movies",
                           "series": "/srv/my media/tv"}, tmm={"enabled": True}, logging={"file": "/home/x/log/m.log"})
            units = service.render_units(c, "/etc/media org/config.toml", exe=sys.executable)
            for name, text in units.items():
                with open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
                    fh.write(text)
            res = subprocess.run(["systemd-analyze", "verify", "--man=no", *sorted(
                os.path.join(tmp, n) for n in units)], capture_output=True, text=True, cwd=tmp)
            complaints = [ln for ln in (res.stdout + res.stderr).splitlines()
                          if tmp in ln and ("ignoring" in ln or "Failed" in ln or "nknown" in ln)]
            self.assertEqual(complaints, [])
        finally:
            shutil.rmtree(tmp)


class TestInstall(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.patches = [mock.patch("mediaorg.service.UNIT_DIR", self.dir),
                        mock.patch("mediaorg.service.systemd_running", return_value=False),
                        mock.patch("mediaorg.service.ensure_folders", return_value=[])]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.dir)

    def test_install_update_remove(self):
        c = cfg(tmm={"enabled": True})
        self.assertEqual(service.units_up_to_date(c, "/c.toml"), (False, []))
        lines = service.install(c, "/c.toml")
        self.assertEqual(len(lines), 4)
        self.assertEqual(service.units_up_to_date(c, "/c.toml"), (True, []))
        self.assertEqual(service.install(c, "/c.toml"), [])  # nothing changed: nothing written
        c2 = cfg(tmm={"enabled": True}, schedule={"run_every_minutes": 10})
        self.assertEqual(service.units_up_to_date(c2, "/c.toml"), (True, ["mediaorg.timer"]))
        c3 = cfg()  # tMM switched off: the nightly units go
        self.assertIn("mediaorg-tmm-sync.service", service.units_up_to_date(c3, "/c.toml")[1])
        service.install(c3, "/c.toml")
        self.assertEqual(sorted(os.listdir(self.dir)), ["mediaorg.service", "mediaorg.timer"])
        self.assertEqual(len(service.remove()), 2)
        self.assertEqual(os.listdir(self.dir), [])

    def test_refresh_after_a_configuration_change(self):
        c = cfg()
        service.install(c, "/c.toml")
        c2 = cfg(schedule={"run_every_minutes": 15})
        with mock.patch("mediaorg.service.is_root", return_value=False):
            self.assertIn("sudo mediaorg service install", service.refresh_if_installed(c2, "/c.toml"))
        real = service.atomic_write

        def write_unowned(path, data, mode, uid=None, gid=None, backup=None):  # a non-root test cannot chown
            return real(path, data, mode)
        with mock.patch("mediaorg.service.is_root", return_value=True), \
                mock.patch("mediaorg.service.atomic_write", side_effect=write_unowned) as aw, \
                mock.patch("sys.stdout"):
            self.assertEqual(service.refresh_if_installed(c2, "/c.toml"), "systemd units updated")
        self.assertEqual(aw.call_count, 1)  # only the timer changed
        self.assertIsNone(service.refresh_if_installed(c2, "/c.toml"))


    def test_a_new_sync_timer_follows_the_main_timer(self):
        for state, expect_enabled in (("enabled", True), ("disabled", False)):
            for name in os.listdir(self.dir):
                os.unlink(os.path.join(self.dir, name))
            calls = []

            def fake(*args, check=False, state=state, calls=calls):
                calls.append(args)
                out = f"{state}\n" if args == ("is-enabled", "mediaorg.timer") else ""
                return mock.Mock(returncode=0, stdout=out, stderr="")
            with mock.patch("mediaorg.service.systemd_running", return_value=True), \
                    mock.patch("mediaorg.service.systemctl", side_effect=fake):
                service.install(cfg(), "/c.toml")  # first install without --enable: nothing enabled
                self.assertNotIn("enable", [a[0] for a in calls])
                lines = service.install(cfg(tmm={"enabled": True}), "/c.toml")  # tMM switched on later
                again = service.install(cfg(tmm={"enabled": True}), "/c.toml")
            enabled = [a for a in calls if a[0] == "enable"]
            if expect_enabled:
                self.assertEqual(enabled, [("enable", "--now", "mediaorg-tmm-sync.timer")])
                self.assertIn("enabled and started: mediaorg-tmm-sync.timer (as mediaorg.timer is enabled)", lines)
            else:
                self.assertEqual(enabled, [])
            self.assertEqual(again, [])  # nothing new: nothing written, nothing enabled again

    def test_units_of_another_configuration_are_left_alone(self):
        service.install(cfg(), "/etc/mediaorg/config.toml")
        self.assertEqual(service.installed_config(), "/etc/mediaorg/config.toml")
        other = cfg(schedule={"run_every_minutes": 15})
        with mock.patch("mediaorg.service.is_root", return_value=True):
            msg = service.refresh_if_installed(other, "/tmp/experiment.toml")
        self.assertIn("left unchanged", msg)
        self.assertEqual(service.installed_config(), "/etc/mediaorg/config.toml")

    def test_units_of_1x_belong_to_the_default_configuration(self):
        with open(os.path.join(self.dir, "mediaorg.service"), "w") as fh:
            fh.write("[Service]\nExecStart=/usr/local/bin/mediaorg run --apply --quiet\n")
        self.assertEqual(service.installed_config(), C.DEFAULT_CONFIG)

class TestEnsureFolders(unittest.TestCase):
    def test_missing_state_and_review_folders_are_created_for_the_service(self):
        tmp = tempfile.mkdtemp()
        try:
            media = os.path.join(tmp, "media")
            for d in ("downloads", "movies", "tv"):
                os.makedirs(os.path.join(media, d))
            os.chmod(media, 0o2775)
            c = C.from_dict({"paths": {"downloads": f"{media}/downloads", "movies": f"{media}/movies",
                                       "series": f"{media}/tv", "review": f"{media}/review",
                                       "state_dir": f"{tmp}/state"}})
            ids = (os.getuid(), os.getgid())
            with mock.patch("mediaorg.service.is_root", return_value=True), \
                    mock.patch("mediaorg.service.service_account", return_value=ids):
                self.assertEqual(service.ensure_folders(c), [f"created {tmp}/state", f"created {media}/review"])
                self.assertEqual(service.ensure_folders(c), [])  # already there: nothing to do
            self.assertEqual(os.stat(f"{tmp}/state").st_mode & 0o7777, 0o750)
            self.assertEqual(os.stat(f"{media}/review").st_mode & 0o7777, 0o2775)  # like its parent
            units = service.render_units(c, "/etc/mediaorg/config.toml")
            self.assertIn(f"ReadWritePaths={tmp}/state -{media}\n", units["mediaorg.service"])
        finally:
            shutil.rmtree(tmp)

    def test_folders_without_a_common_folder_cannot_be_served_by_the_sandbox(self):
        apart = cfg(paths={"downloads": "/downloads", "movies": "/srv/media/movies", "series": "/srv/media/tv"})
        with self.assertRaisesRegex(C.ConfigError, "no common folder other than /"):
            service.render_units(apart, "/c.toml")
        review_apart = cfg(paths={"review": "/review"})  # moves into review must stay inside the writable folder
        with self.assertRaisesRegex(C.ConfigError, "no common folder other than /"):
            service.render_units(review_apart, "/c.toml")
        outside = cfg(paths={"review": "/srv/review"})
        self.assertIn("ReadWritePaths=/var/lib/mediaorg -/srv\n", service.render_units(outside, "/c.toml")["mediaorg.service"])

    def test_a_daily_schedule_and_a_config_path_with_a_newline(self):
        units = service.render_units(cfg(schedule={"run_every_minutes": 1440}), "/c.toml")
        self.assertIn("OnCalendar=*-*-* 00:00:00\n", units["mediaorg.timer"])
        with self.assertRaises(C.ConfigError):
            service.render_units(cfg(), "/etc/x\nExecStartPre=/bin/true\n#.toml")


if __name__ == "__main__":
    unittest.main()
