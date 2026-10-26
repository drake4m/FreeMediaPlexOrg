"""2.0 configuration: optional sections, derived defaults, validation guards, the commented TOML writer,
and the `mediaorg config ...` / `mediaorg tmm set-key` commands."""

import dataclasses
import io
import json
import os
import shutil
import stat
import tempfile
import tomllib
import unittest
from unittest import mock

from mediaorg import config as C
from mediaorg import schema
from mediaorg.guards import MountEntry

from .helpers import run_cli


def base(**sections):
    d = {"paths": {"downloads": "/srv/media/downloads", "movies": "/srv/media/movies", "series": "/srv/media/tv"}}
    for k, v in sections.items():
        d.setdefault(k, {}).update(v)
    return d


class TestSchema(unittest.TestCase):
    def test_every_field_is_documented_and_nothing_else(self):
        fields = {f"{s}.{f.name}" for s, cls in C.SECTIONS.items() for f in dataclasses.fields(cls)}
        self.assertEqual(fields, set(schema.BY_NAME))
        for key in schema.KEYS:
            self.assertTrue(key.doc.endswith("."), key.dotted)
            self.assertIn(key.section, schema.SECTION_DOCS)

    def test_written_file_reads_back_identically(self):
        raw = base(media={"ignore_names": ["_review", 'a "quoted" \\ name', "tab\there", "ünïcödé", "x\x01y"]},
                   tmm={"enabled": True, "path_map": [["/srv/media/movies", "/media/movies"]],
                        "after_move_tvshow": [{"action": "update", "scope": {"name": "all"}},
                                              {"action": "scrape", "scope": {"name": "show", "args": ["a b"]}}]},
                   mount={"mountpoint": "/srv/media", "fstype": "ext4", "source": "/dev/sdb1"},
                   review={"tmm_trash_retention_minutes": 30}, schedule={"tmm_sync_time": ""})
        text = schema.render_file(raw, "2.0.0")
        back = {k: v for k, v in tomllib.loads(text).items() if v}
        self.assertEqual(back, raw)
        C.from_dict(back)  # and it is a valid configuration

    def test_unset_keys_are_written_as_documented_comments(self):
        text = schema.render_file(base(), "2.0.0")
        self.assertIn('# state_dir = "/var/lib/mediaorg"   (default)', text)
        self.assertIn("# review = ...   (default: <downloads>/_review)", text)
        self.assertIn('# mountpoint = "/mnt/nas"   (example)', text)
        self.assertIn('#     { action = "update", scope = { name = "all" } },', text)  # built-in tMM calls shown

    def test_parse_values_from_the_command_line(self):
        k = schema.find_key
        self.assertEqual(schema.parse_value(k("media.video_extensions"), ".mkv, .mp4"), [".mkv", ".mp4"])
        self.assertEqual(schema.parse_value(k("media.video_extensions"), '[".mkv"]'), [".mkv"])
        self.assertEqual(schema.parse_value(k("tmm.path_map"), "/a=/x,/b=/y"), [["/a", "/x"], ["/b", "/y"]])
        self.assertEqual(schema.parse_value(k("tmm.enabled"), "yes"), True)
        self.assertEqual(schema.parse_value(k("schedule.run_every_minutes"), "10"), 10)
        self.assertEqual(schema.parse_value(k("paths.downloads"), "/srv//x/../y"), "/srv/y")
        for key, value in (("tmm.enabled", "maybe"), ("schedule.run_every_minutes", "ten"),
                           ("completeness.method", "magic"), ("paths.movies", "relative"),
                           ("tmm.after_move_movie", "[{action=}]"), ("tmm.path_map", "/a:/x")):
            with self.assertRaises(C.ConfigError, msg=key):
                schema.parse_value(k(key), value)

    def test_unknown_key_suggests(self):
        with self.assertRaisesRegex(C.ConfigError, "did you mean paths.movies"):
            schema.find_key("paths.movie")


class TestValidation(unittest.TestCase):
    def ok(self, **kw):
        return C.from_dict(base(**kw))

    def bad(self, pattern=None, data=None, **kw):
        with self.assertRaises(C.ConfigError) as cm:
            C.from_dict(data if data is not None else base(**kw))
        if pattern:
            self.assertRegex(str(cm.exception), pattern)

    def test_minimal_configuration_and_derived_defaults(self):
        cfg = self.ok()
        self.assertIsNone(cfg.mount)
        self.assertEqual(cfg.completeness.method, "quiescence")
        self.assertEqual(cfg.paths.review, "/srv/media/downloads/_review")
        self.assertEqual(cfg.freebox.token_file, "/var/lib/mediaorg/freebox_app_token.json")
        self.assertEqual(cfg.base_dir, "/srv/media")
        self.assertIsNone(self.ok(mount={}).mount)  # an empty [mount] = no check

    def test_folders_must_be_safe(self):
        self.bad("system folder", paths={"movies": "/usr/share/movies"})
        self.bad("system folder", paths={"downloads": "/etc/x"})
        self.bad("system folder", paths={"series": "/home"})
        self.bad("control characters", paths={"movies": "/srv/media/mo\nvies"})
        self.bad("absolute", paths={"movies": "media/movies"})
        self.bad("must not be inside", paths={"movies": "/srv/media/downloads/movies"})
        self.bad("separate from Downloads", paths={"state_dir": "/srv/media/downloads/.state"})
        self.bad("must not contain Downloads", paths={"review": "/srv/media"})
        self.bad("outside the media folders", logging={"file": "/srv/media/movies/log.txt"})
        self.bad("mount.mountpoint", mount={"mountpoint": "/mnt/nas", "fstype": "cifs", "source": "//nas/x"})
        self.bad("needs mountpoint", mount={"mountpoint": "/srv/media", "fstype": "", "source": ""})

    def test_other_values(self):
        self.bad("placeholder", naming={"season_folder": "Season {episode}"})
        self.bad("must contain", naming={"movie_folder": "{title}"})
        self.bad("single folder name", naming={"series_folder": "{title}/{year}"})
        self.bad("HH:MM", schedule={"tmm_sync_time": "25:00"})
        self.bad("between 1 and 1440", schedule={"run_every_minutes": 0})
        self.bad("plain http", tmm={"base_url": "http://192.0.2.5:7878"})
        self.ok(tmm={"base_url": "http://192.0.2.5:7878", "allow_insecure_http": True})
        self.ok(tmm={"base_url": "http://localhost:7878"})
        self.bad("must be true or false", tmm={"enabled": "yes"})
        self.bad("must be an integer", safety={"max_moves_per_run": "50"})
        self.bad("longer than the review retention", reports={"journal_retention_days": 7})
        self.ok(reports={"journal_retention_days": 0})
        self.bad("did you mean", review={"duplicate_retention_days": 3})
        self.bad("did you mean", data={**base(), "loging": {}})

    def test_freebox_section_without_method_is_refused(self):
        """1.x defaulted to the Freebox; 2.0 defaults to quiescence: never switch silently."""
        self.bad("changed to 'quiescence'", freebox={"remote_downloads_dir": "/Freebox/Downloads"})
        cfg = self.ok(completeness={"method": "freebox"}, freebox={"remote_downloads_dir": "/Freebox/Downloads"})
        self.assertEqual(cfg.completeness.method, "freebox")

    def test_a_1x_configuration_still_loads(self):
        one_x = {
            "paths": {"downloads": "/mnt/share/Downloads", "movies": "/mnt/share/Movies", "series": "/mnt/share/Series",
                      "review": "/mnt/share/Downloads/_review", "state_dir": "/var/lib/mediaorg"},
            "mount": {"mountpoint": "/mnt/share", "fstype": "cifs", "source": "//nas.example/media"},
            "completeness": {"method": "freebox", "min_quiet_minutes": 10, "untracked": "quiescence"},
            "freebox": {"base_url": "https://mafreebox.freebox.fr", "ca_file": "/etc/mediaorg/freebox-ca.pem",
                        "app_id": "mediaorg", "app_name": "mediaorg", "device_name": "my-server",
                        "token_file": "/var/lib/mediaorg/freebox_app_token.json",
                        "remote_downloads_dir": "/Freebox/Downloads", "timeout_seconds": 10},
            "safety": {"max_moves_per_run": 50}, "reports": {"retention_days": 30},
            "review": {"delete_identical_duplicates": True, "duplicates_retention_days": 7,
                       "leftovers_retention_days": 7, "tmm_trash_retention_minutes": 30},
            "tmm": {"enabled": True, "base_url": "http://127.0.0.1:7878", "api_key_file": "/etc/mediaorg/tmm.key",
                    "path_map": [["/mnt/share/Movies", "/media/movies"], ["/mnt/share/Series", "/media/series"]]},
        }
        cfg = C.from_dict(one_x)
        self.assertEqual(cfg.mount.mountpoint, "/mnt/share")
        self.assertEqual(cfg.freebox.device_name, "my-server")
        self.assertEqual(cfg.review.tmm_trash_retention_seconds, 1800)
        self.assertEqual(cfg.schedule.run_every_minutes, 5)

    def test_permissions_of_the_file(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "config.toml")
            with open(path, "w") as fh:
                fh.write(schema.render_file(base(), "2.0.0"))
            os.chmod(path, 0o646)
            with self.assertRaisesRegex(C.ConfigError, "writable by every user"):
                C.load(path)
            os.chmod(path, 0o640)
            C.load(path)
            key = os.path.join(tmp, "key")
            with open(key, "w") as fh:
                fh.write("secret-key")
            os.chmod(key, 0o644)
            with self.assertRaisesRegex(C.ConfigError, "readable by every user"):
                C.check_file_permissions(key, "the key", secret=True)
        finally:
            shutil.rmtree(tmp)


class _Tree:
    """Temporary media folders and a config path that this (non-root) test may write."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="mediaorg-cfg-")
        self.root = os.path.join(self.tmp, "media")
        for d in ("downloads", "movies", "tv"):
            os.makedirs(os.path.join(self.root, d))
        self.state = os.path.join(self.tmp, "state")
        self.etc = os.path.join(self.tmp, "etc")
        os.makedirs(self.etc)
        self.cfg = os.path.join(self.etc, "config.toml")

    def p(self, name):
        return os.path.join(self.root, name)

    def init(self, *extra, mounts=None):
        mounts = mounts if mounts is not None else [MountEntry("/", "xfs", "/dev/root", "rw")]
        with mock.patch("os.geteuid", return_value=1000), \
                mock.patch("mediaorg.guards.read_mountinfo", return_value=mounts), \
                mock.patch("mediaorg.service.UNIT_DIR", os.path.join(self.tmp, "units")):
            return run_cli("-c", self.cfg, "config", "init", "--non-interactive",
                           "--downloads", self.p("downloads"), "--movies", self.p("movies"),
                           "--series", self.p("tv"), "--state-dir", self.state, *extra)

    def cli(self, *argv, stdin=None):
        with mock.patch("os.geteuid", return_value=1000), \
                mock.patch("mediaorg.service.UNIT_DIR", os.path.join(self.tmp, "units")), \
                mock.patch("sys.stdin", stdin or io.StringIO("")):
            return run_cli("-c", self.cfg, *argv)

    def raw(self):
        with open(self.cfg, "rb") as fh:
            return tomllib.load(fh)

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestConfigCommands(unittest.TestCase):
    def setUp(self):
        self.t = _Tree()

    def tearDown(self):
        self.t.cleanup()

    def test_init_writes_a_valid_documented_file(self):
        code, out = self.t.init()
        self.assertEqual(code, 0, out)
        self.assertIn("configuration written to", out)
        self.assertIn("no mount check (the media folders are on the root filesystem)", out)
        self.assertEqual(stat.S_IMODE(os.stat(self.t.cfg).st_mode), 0o640)
        raw = self.t.raw()
        self.assertEqual(raw["paths"]["downloads"], self.t.p("downloads"))
        self.assertEqual(raw["completeness"]["method"], "quiescence")
        self.assertNotIn("mount", {k for k, v in raw.items() if v})
        with open(self.t.cfg, encoding="utf-8") as fh:
            self.assertIn("# Folder where completed downloads appear", fh.read())
        code, out = self.t.cli("config", "validate")
        self.assertEqual(code, 0, out)

    def test_init_adds_the_mount_check_for_a_separate_mount(self):
        mounts = [MountEntry("/", "xfs", "/dev/root", "rw"), MountEntry(self.t.root, "cifs", "//nas.example/media", "")]
        code, out = self.t.init(mounts=mounts)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.t.raw()["mount"], {"mountpoint": self.t.root, "fstype": "cifs",
                                                  "source": "//nas.example/media"})
        self.assertIn(f"mount check: {self.t.root} must be mounted from //nas.example/media", out)

    def test_init_options(self):
        code, out = self.t.init("--mode", "freebox", "--freebox-downloads-dir", "/Freebox/Downloads",
                                "--tmm", "--tmm-path-map", f"{self.t.p('movies')}=/media/movies", "--every", "10",
                                "--mount", "none")
        self.assertEqual(code, 0, out)
        raw = self.t.raw()
        self.assertEqual(raw["completeness"]["method"], "freebox")
        self.assertEqual(raw["freebox"]["remote_downloads_dir"], "/Freebox/Downloads")
        self.assertEqual(raw["tmm"]["path_map"], [[self.t.p("movies"), "/media/movies"]])
        self.assertEqual(raw["schedule"]["run_every_minutes"], 10)
        self.assertIn("sudo mediaorg freebox authorize", out)
        self.assertIn("sudo mediaorg tmm set-key", out)

    def test_init_refuses_bad_folders_and_existing_file(self):
        with mock.patch("os.geteuid", return_value=1000):
            code, _ = run_cli("-c", self.t.cfg, "config", "init", "--non-interactive", "--downloads",
                              self.t.p("downloads"), "--movies", self.t.p("missing"), "--series", self.t.p("tv"))
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(self.t.cfg))
        os.symlink(self.t.p("movies"), self.t.p("movies-link"))
        with mock.patch("os.geteuid", return_value=1000):
            code, _ = run_cli("-c", self.t.cfg, "config", "init", "--non-interactive", "--downloads",
                              self.t.p("downloads"), "--movies", self.t.p("movies-link"), "--series", self.t.p("tv"))
        self.assertEqual(code, 2)  # a symbolic link would be refused at every run: refused here already
        self.assertEqual(self.t.init()[0], 0)
        self.assertEqual(self.t.init()[0], 2)  # exists: needs --force
        self.assertEqual(self.t.init("--force")[0], 0)
        self.assertTrue(os.path.exists(self.t.cfg + ".bak"))

    def test_init_refuses_folders_on_two_filesystems(self):
        real_stat = os.stat

        def fake_stat(path, *a, **kw):
            st = real_stat(path, *a, **kw)
            if str(path).endswith("tv"):
                return os.stat_result((st.st_mode, st.st_ino, st.st_dev + 1, *tuple(st)[3:10]))
            return st
        with mock.patch("os.stat", side_effect=fake_stat):
            code, _ = self.t.init()
        self.assertEqual(code, 2)

    def test_set_unset_get(self):
        self.t.init()
        code, out = self.t.cli("config", "set", "schedule.run_every_minutes=15", "review.leftovers_retention_days=3")
        self.assertEqual(code, 0, out)
        self.assertIn("schedule.run_every_minutes: (default) -> 15", out)
        self.assertTrue(os.path.exists(self.t.cfg + ".bak"))
        self.assertEqual(self.t.cli("config", "get", "schedule.run_every_minutes")[1].strip(), "15")
        code, out = self.t.cli("config", "set", "completeness.min_quiet_minutes", "20")  # KEY VALUE form
        self.assertEqual(code, 0, out)
        self.assertEqual(self.t.raw()["completeness"]["min_quiet_minutes"], 20)
        code, out = self.t.cli("config", "unset", "schedule.run_every_minutes")
        self.assertEqual(code, 0, out)
        self.assertNotIn("run_every_minutes", self.t.raw().get("schedule", {}))
        self.assertEqual(self.t.cli("config", "get", "schedule.run_every_minutes")[1].strip(), "5")
        self.assertEqual(self.t.cli("config", "get", "paths.review")[1].strip(), self.t.p("downloads/_review"))

    def test_no_change_keeps_the_file_and_its_backup(self):
        self.t.init()
        self.t.cli("config", "set", "completeness.min_quiet_minutes=15")
        with open(self.t.cfg + ".bak", encoding="utf-8") as fh:
            backup = fh.read()  # the file as `config init` wrote it
        mtime = os.stat(self.t.cfg).st_mtime_ns
        for argv in (["config", "set", "completeness.min_quiet_minutes=15"],
                     ["config", "unset", "schedule.run_every_minutes"],
                     ["config", "set", "paths.review="]):  # already the default
            code, out = self.t.cli(*argv)
            self.assertEqual(code, 0, out)
            self.assertIn("nothing changed", out)
        self.assertEqual(os.stat(self.t.cfg).st_mtime_ns, mtime)
        with open(self.t.cfg + ".bak", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), backup)

    def test_set_validates_before_writing(self):
        self.t.init()
        with open(self.t.cfg, encoding="utf-8") as fh:
            before = fh.read()
        for args in (["completeness.min_quiet_minutes=2"], ["paths.movies=" + self.t.p("downloads/x")],
                     ["tmm.enabled=maybe"], ["paths.movie=/srv/x"], ["nonsense"], ["a=1", "b"]):
            code, _ = self.t.cli("config", "set", *args)
            self.assertEqual(code, 2, args)
        with open(self.t.cfg, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before)

    def test_trash_retention_keys_replace_each_other(self):
        self.t.init()
        self.t.cli("config", "set", "review.tmm_trash_retention_days=3")
        code, out = self.t.cli("config", "set", "review.tmm_trash_retention_minutes=30")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.t.raw()["review"], {"tmm_trash_retention_minutes": 30})

    def test_show_keys_path(self):
        self.t.init()
        code, out = self.t.cli("config", "show")
        self.assertEqual(code, 0)
        self.assertIn('state_dir = "' + self.t.state + '"', out)
        self.assertIn("[schedule]\nrun_every_minutes = 5", out)
        code, out = self.t.cli("config", "show", "--file")
        self.assertIn("# mediaorg configuration - written by mediaorg", out)
        code, out = self.t.cli("config", "keys")
        self.assertIn("paths.downloads  [path; required]", out)
        self.assertNotIn("tmm.after_move_movie", out)  # advanced
        self.assertIn("tmm.after_move_movie", self.t.cli("config", "keys", "--all")[1])
        self.assertIn("schedule.tmm_sync_time", self.t.cli("config", "keys", "schedule")[1])
        self.assertEqual(self.t.cli("config", "path")[1].strip(), self.t.cfg)

    def test_unit_relevant_change_reminds_to_reinstall_units(self):
        self.t.init()
        units = os.path.join(self.t.tmp, "units")
        os.makedirs(units)
        from mediaorg import service
        cfg = C.load(self.t.cfg)
        with mock.patch("mediaorg.service.UNIT_DIR", units):
            service.install(cfg, self.t.cfg, unit_dir=units)
        code, out = self.t.cli("config", "set", "schedule.run_every_minutes=10")
        self.assertEqual(code, 0, out)
        self.assertIn("run `sudo mediaorg service install`", out)


class TestTmmKey(unittest.TestCase):
    def setUp(self):
        self.t = _Tree()
        self.t.init()
        self.key = os.path.join(self.t.etc, "tmm.key")
        self.t.cli("config", "set", f"tmm.api_key_file={self.key}")

    def tearDown(self):
        self.t.cleanup()

    def test_key_from_standard_input(self):
        code, out = self.t.cli("tmm", "set-key", stdin=io.StringIO("0123456789abcdef\n"))
        self.assertEqual(code, 0, out)
        with open(self.key) as fh:
            self.assertEqual(fh.read(), "0123456789abcdef\n")
        self.assertEqual(stat.S_IMODE(os.stat(self.key).st_mode), 0o640)
        self.assertNotIn("0123456789abcdef", out)  # never echoed

    def test_bad_keys_are_refused(self):
        for bad in ("short", "has space inside", "", "x" * 300):
            code, _ = self.t.cli("tmm", "set-key", stdin=io.StringIO(bad))
            self.assertEqual(code, 2, bad)
        self.assertFalse(os.path.exists(self.key))

    def test_key_from_tmm_settings_and_generated(self):
        tj = os.path.join(self.t.tmp, "tmm.json")
        with open(tj, "w") as fh:
            json.dump({"httpApiKey": "ffffffff-1111-2222-3333-444444444444"}, fh)
        self.assertEqual(self.t.cli("tmm", "set-key", "--from-tmm-json", tj)[0], 0)
        with open(self.key) as fh:
            self.assertEqual(fh.read().strip(), "ffffffff-1111-2222-3333-444444444444")
        code, out = self.t.cli("tmm", "set-key", "--generate")
        self.assertEqual(code, 0)
        with open(self.key) as fh:
            self.assertIn(f"new key: {fh.read().strip()}", out)


class TestPrivileges(unittest.TestCase):
    def setUp(self):
        self.t = _Tree()
        self.t.init()

    def tearDown(self):
        self.t.cleanup()

    def test_media_commands_drop_to_the_service_account(self):
        from mediaorg.cli import main
        with mock.patch("os.geteuid", return_value=0), \
                mock.patch("mediaorg.system.drop_to_service_user") as drop, \
                mock.patch("mediaorg.cli.cmd_runs", return_value=0) as runs:
            self.assertEqual(main(["-c", self.t.cfg, "runs"]), 0)
        drop.assert_called_once()
        runs.assert_called_once()

    def test_setup_commands_stay_privileged(self):
        from mediaorg.cli import main
        with mock.patch("os.geteuid", return_value=0), \
                mock.patch("mediaorg.system.drop_to_service_user") as drop, \
                mock.patch("mediaorg.configcmd.write_config", return_value=(None, None)), \
                mock.patch("mediaorg.configcmd._refresh_units"):
            main(["-c", self.t.cfg, "config", "set", "schedule.run_every_minutes=10"])
            main(["-c", self.t.cfg, "config", "show"])
        drop.assert_not_called()

    def test_missing_service_account_is_a_clear_error(self):
        from mediaorg.cli import main
        from mediaorg.system import drop_to_service_user
        with mock.patch("pwd.getpwnam", side_effect=KeyError("x")):
            with self.assertRaisesRegex(Exception, "does not exist"):
                drop_to_service_user("nobody-like-this")
        with mock.patch("os.geteuid", return_value=0), mock.patch("pwd.getpwnam", side_effect=KeyError("x")), \
                mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(main(["-c", self.t.cfg, "runs"]), 2)

    def test_non_root_cannot_change_a_protected_configuration(self):
        os.chmod(self.t.etc, 0o555)
        os.chmod(self.t.cfg, 0o444)
        try:
            if os.access(self.t.cfg, os.W_OK):  # running as root: permissions do not apply
                self.skipTest("root ignores file permissions")
            code, _ = self.t.cli("config", "set", "schedule.run_every_minutes=10")
            self.assertEqual(code, 3)
        finally:
            os.chmod(self.t.etc, 0o755)
            os.chmod(self.t.cfg, 0o640)


class TestCommandLine(unittest.TestCase):
    def test_1x_command_names_still_work(self):
        from mediaorg.cli import _translate_aliases
        self.assertEqual(_translate_aliases(["tmm-sync", "--apply"]), ["tmm", "sync", "--apply"])
        self.assertEqual(_translate_aliases(["-c", "x.toml", "freebox-status"]), ["-c", "x.toml", "freebox", "status"])
        self.assertEqual(_translate_aliases(["run", "--only", "tmm-sync"]), ["run", "--only", "tmm-sync"])

    def test_every_help_works_and_shows_examples(self):
        from mediaorg.cli import build_parser
        for argv in ([], ["run"], ["status"], ["check"], ["undo"], ["identify"], ["config"], ["config", "init"],
                     ["config", "set"], ["config", "keys"], ["service"], ["service", "install"], ["freebox"],
                     ["tmm"], ["tmm", "set-key"], ["tmm", "sync"]):
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf), self.assertRaises(SystemExit) as cm:
                build_parser().parse_args([*argv, "--help"])
            self.assertEqual(cm.exception.code, 0, argv)
            self.assertIn("usage: mediaorg", buf.getvalue())
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf), self.assertRaises(SystemExit):
            build_parser().parse_args(["--help"])
        text = buf.getvalue()
        for word in ("quick start", "exit codes", "privileges", "config init", "service install --enable"):
            self.assertIn(word, text)

    def test_config_option_after_the_command(self):
        from mediaorg.cli import build_parser
        a = build_parser().parse_args(["run", "-c", "/x.toml", "--apply"])
        self.assertEqual((a.config, a.apply), ("/x.toml", True))
        a = build_parser().parse_args(["-c", "/y.toml", "run"])
        self.assertEqual(a.config, "/y.toml")


if __name__ == "__main__":
    unittest.main()
