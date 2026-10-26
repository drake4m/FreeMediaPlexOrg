"""Deployment pieces: tMM settings import, report retention, quiet output, tmm sync when disabled,
tMM endpoint check, the optional tMM container unit and the shell scripts."""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest

from mediaorg.report import prune_reports

from .fakes import FakeTmm
from .helpers import FakeNas, cli_env, run_cli, touch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_migrate():
    spec = importlib.util.spec_from_file_location("tmm_migrate", os.path.join(ROOT, "contrib", "tmm-docker", "tmm_migrate.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


OLD_KEY = "11111111-2222-3333-4444-555555555555"
SCRAPER_SECRET = "apiKey=ENCRYPTED-SECRET-VALUE"


def _backup(d):
    """Synthetic backup with the keys that matter (same structure as tMM 5.2.5 on Windows)."""
    files = {
        "tmm.json": {"configFilename": "tmm.json", "version": "5.2.5", "httpApiKey": OLD_KEY,
                     "enableHttpServer": True, "httpServerPort": 7878, "ignoreSSLProblems": True,
                     "mediaPlayer": "C:\\Program Files\\Windows Media Player\\wmplayer.exe", "language": "en"},
        "movies.json": {"configFilename": "movies.json", "movieDataSource": ["D:\\Media\\Movies"],
                        "movieScraper": "tmdb", "artworkScrapers": ["tmdb", "fanarttv"],
                        "renamerPathname": "${title} (${year})", "renameAfterScrape": True},
        "tvShows.json": {"configFilename": "tvShows.json", "tvShowDataSource": ["D:\\Media\\TV"],
                         "scraper": "tvdb", "artworkScrapers": ["tmdb", "tvdb", "fanarttv"],
                         "renamerTvShowFoldername": "${showTitle}", "renamerSeasonFoldername": "Season ${seasonNr}"},
    }
    for name, data in files.items():
        with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    with open(os.path.join(d, "scraper_tmdb_movie.conf"), "w") as fh:
        fh.write("#\n" + SCRAPER_SECRET + "\n")


class TestTmmMigrate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.backup = os.path.join(self.tmp, "backup")
        os.makedirs(self.backup)
        _backup(self.backup)
        self.out = os.path.join(self.tmp, "data", "data")
        self.key = os.path.join(self.tmp, "tmm.key")
        self.mod = _load_migrate()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_(self, *extra):
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            code = self.mod.main(["--backup", self.backup, "--out", self.out, "--api-key-file", self.key,
                                  "--key-group", "", *extra])
        return code, buf.getvalue() + err.getvalue()

    def read(self, name):
        with open(os.path.join(self.out, name), encoding="utf-8") as fh:
            return json.load(fh)

    def test_free_migration(self):
        code, out = self.run_()
        self.assertEqual(code, 0, out)
        tmm, movies, tv = self.read("tmm.json"), self.read("movies.json"), self.read("tvShows.json")
        with open(self.key) as fh:
            key = fh.read().strip()
        self.assertEqual(tmm["httpApiKey"], key)
        self.assertNotEqual(key, OLD_KEY)
        self.assertEqual(len(key), 36)
        self.assertFalse(tmm["ignoreSSLProblems"])
        self.assertTrue(tmm["enableHttpServer"])
        self.assertEqual(tmm["httpServerPort"], 7878)
        self.assertEqual(tmm["language"], "en")  # everything else kept
        self.assertEqual(movies["movieDataSource"], ["/media/movies"])
        self.assertEqual(tv["tvShowDataSource"], ["/media/series"])
        self.assertEqual(tv["renamerTvShowFoldername"], "${showTitle} (${showYear})")
        self.assertEqual(tv["renamerSeasonFoldername"], "Season ${seasonNr}")
        self.assertEqual(tv["scraper"], "tmdb")
        self.assertEqual(tv["artworkScrapers"], ["tmdb"])
        self.assertEqual(movies["artworkScrapers"], ["tmdb"])
        self.assertTrue(movies["renameAfterScrape"])
        with open(os.path.join(self.out, "scraper_tmdb_movie.conf")) as fh:
            self.assertIn(SCRAPER_SECRET, fh.read())
        for name in os.listdir(self.out):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.out, name)).st_mode), 0o600, name)
        self.assertEqual(stat.S_IMODE(os.stat(self.key).st_mode), 0o640)
        # no secret ever printed
        for secret in (OLD_KEY, key, SCRAPER_SECRET):
            self.assertNotIn(secret, out)
        self.assertIn("httpApiKey: <replaced>", out)

    def test_secret_files_keep_modes_under_a_strict_umask(self):
        old = os.umask(0o077)
        try:
            code, out = self.run_()
        finally:
            os.umask(old)
        self.assertEqual(code, 0, out)
        self.assertEqual(stat.S_IMODE(os.stat(self.key).st_mode), 0o640)  # service group must read it

    def test_ffmpeg_artwork_is_pro_and_windows_paths_are_reported(self):
        with open(os.path.join(self.backup, "movies.json")) as fh:
            movies = json.load(fh)
        movies["artworkScrapers"] = ["tmdb", "ffmpeg"]
        movies["movieSetDataFolder"] = "D:\\Movie Sets"
        with open(os.path.join(self.backup, "movies.json"), "w") as fh:
            json.dump(movies, fh)
        code, out = self.run_()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read("movies.json")["artworkScrapers"], ["tmdb"])
        self.assertIn("WARNING  movies.json: movieSetDataFolder", out)
        self.assertNotIn("Z:", out.split("WARNING")[-1])  # data sources were converted, not reported

    def test_pro_keeps_tvdb_and_fanart(self):
        code, out = self.run_("--pro")
        self.assertEqual(code, 0, out)
        tv = self.read("tvShows.json")
        self.assertEqual(tv["scraper"], "tvdb")
        self.assertEqual(tv["artworkScrapers"], ["tmdb", "tvdb", "fanarttv"])
        self.assertEqual(tv["renamerTvShowFoldername"], "${showTitle} (${showYear})")

    def test_refuses_to_overwrite_existing_settings(self):
        os.makedirs(self.out)
        with open(os.path.join(self.out, "tmm.json"), "w") as fh:
            fh.write("{}")
        code, out = self.run_()
        self.assertEqual(code, 2)
        self.assertIn("not empty", out)
        self.assertFalse(os.path.exists(self.key))  # nothing written

    def test_existing_key_file_is_kept_as_backup(self):
        with open(self.key, "w") as fh:
            fh.write("old\n")
        code, out = self.run_()
        self.assertEqual(code, 0, out)
        baks = [n for n in os.listdir(self.tmp) if n.startswith("tmm.key.bak-")]
        self.assertEqual(len(baks), 1)

    def test_rejects_wrong_files(self):
        with open(os.path.join(self.backup, "movies.json"), "w") as fh:
            json.dump({"configFilename": "tvShows.json"}, fh)
        code, out = self.run_()
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(self.out))


class TestReportsAndQuiet(unittest.TestCase):
    def setUp(self):
        self.nas = FakeNas()

    def tearDown(self):
        self.nas.cleanup()

    def test_prune_reports_keeps_recent_and_foreign_files(self):
        d = os.path.join(self.nas.tmp, "runs")
        os.makedirs(d)
        old, new, other = (os.path.join(d, n) for n in
                           ("20250101T000000Z-abcd.json", "20261021T000000Z-abcd.json", "notes.json"))
        for p in (old, new, other):
            touch(p)
        os.utime(old, (time.time() - 40 * 86400,) * 2)
        os.utime(other, (time.time() - 40 * 86400,) * 2)
        self.assertEqual(prune_reports(d, 30), 1)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new) and os.path.exists(other))
        self.assertEqual(prune_reports(d, 0), 0)

    def test_run_prunes_old_reports(self):
        runs = os.path.join(self.nas.state, "runs")
        os.makedirs(runs)
        old = touch(os.path.join(runs, "20250101T000000Z-0000.json"))
        os.utime(old, (time.time() - 40 * 86400,) * 2)
        with cli_env(self.nas, reports={"retention_days": 30}) as path:
            code, _ = run_cli("-c", path, "run")
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(old))
        self.assertEqual(len(os.listdir(runs)), 1)  # this run's report

    def test_quiet_reports_only_what_is_new_or_changed(self):
        touch(os.path.join(self.nas.dl, "Cistern.S03E06.1080p.WEB.h264-GRP.mkv"))
        touch(os.path.join(self.nas.dl, "Brimstone.2016.S01E02.1080p.WEB.mkv"))          # SKIP (year conflict)
        touch(os.path.join(self.nas.dl, "Fresh.Movie.2025.1080p.WEB.mkv"), mtime=time.time())  # WAIT
        touch(os.path.join(self.nas.dl, "Some Software 1.0", "setup.exe"))            # IGNORED
        touch(os.path.join(self.nas.dl, "Thumbs.db"))                                 # ignored name: never listed
        with cli_env(self.nas) as path:
            code, out = run_cli("-c", path, "run", "--apply", "--quiet")
        self.assertEqual(code, 0, out)
        self.assertIn("[READY] Cistern.S03E06", out)
        self.assertIn("[SKIP] Brimstone.2016", out)
        self.assertIn("[WAIT] Fresh.Movie.2025", out)      # new: listed once
        self.assertIn("[IGNORED] Some Software 1.0", out)  # new: listed once
        self.assertNotIn("Thumbs.db", out)
        self.assertNotIn("legacy:", out)
        self.assertIn("wait 1", out)
        self.assertTrue(self.nas.exists("Series/Cistern (2022)/Season 3/Cistern.S03E06.1080p.WEB.h264-GRP.mkv"))
        reports = os.listdir(os.path.join(self.nas.state, "runs"))
        # same state again (the WAIT reason's minutes may differ): nothing printed or logged, no report
        with cli_env(self.nas) as path, self.assertNoLogs("mediaorg", level="INFO"):
            code, out2 = run_cli("-c", path, "run", "--apply", "--quiet")
        self.assertEqual(code, 0)
        self.assertEqual(out2, "")
        self.assertEqual(os.listdir(os.path.join(self.nas.state, "runs")), reports)
        # a change is listed again: the WAIT item is now ready and moved
        os.utime(os.path.join(self.nas.dl, "Fresh.Movie.2025.1080p.WEB.mkv"), (time.time() - 3600,) * 2)
        with cli_env(self.nas) as path:
            code, out3 = run_cli("-c", path, "run", "--apply", "--quiet")
        self.assertEqual(code, 0, out3)
        self.assertIn("[READY] Fresh.Movie.2025", out3)
        self.assertNotIn("Brimstone", out3)
        self.assertNotIn("Some Software", out3)

    def test_quiet_with_only_keeps_the_state_of_other_items(self):
        touch(os.path.join(self.nas.dl, "Fresh.Movie.2025.1080p.WEB.mkv"), mtime=time.time())
        touch(os.path.join(self.nas.dl, "Brimstone.2016.S01E02.1080p.WEB.mkv"))
        with cli_env(self.nas) as path:
            run_cli("-c", path, "run", "--apply", "--quiet")
            code, out = run_cli("-c", path, "run", "--apply", "--quiet", "--only", "Brimstone.2016.S01E02.1080p.WEB.mkv")
            self.assertEqual((code, out), (0, ""))
            code, out = run_cli("-c", path, "run", "--apply", "--quiet")
        self.assertEqual((code, out), (0, ""))

    def test_quiet_dry_run_does_not_hide_changes_from_the_timer(self):
        touch(os.path.join(self.nas.dl, "Brimstone.2016.S01E02.1080p.WEB.mkv"))
        with cli_env(self.nas) as path:
            code, out = run_cli("-c", path, "run", "--quiet")          # manual, dry-run
            self.assertIn("[SKIP] Brimstone.2016", out)
            code, out = run_cli("-c", path, "run", "--apply", "--quiet")  # the timer still reports it once
            self.assertIn("[SKIP] Brimstone.2016", out)
            code, out = run_cli("-c", path, "run", "--apply", "--quiet")
        self.assertEqual((code, out), (0, ""))

    def test_quiet_does_not_repeat_items_it_cannot_act_on(self):
        touch(os.path.join(self.nas.dl, "Cistern.S03E06.1080p.WEB.h264-GRP.mkv"))
        touch(os.path.join(self.nas.dl, "Cistern.S03E07.1080p.WEB.h264-GRP.mkv"))
        with cli_env(self.nas, safety={"max_moves_per_run": 1}) as path:
            code, out = run_cli("-c", path, "run", "--apply", "--quiet")
            self.assertEqual(code, 6)
            self.assertIn("[READY] Cistern.S03E06", out)  # new: reported once
            code, out = run_cli("-c", path, "run", "--apply", "--quiet")
        self.assertEqual((code, out), (6, ""))  # the cap error is logged each time, the items are not

    def test_bad_retention_rejected(self):
        from mediaorg import config as C
        with self.assertRaises(C.ConfigError):
            C.from_dict(self.nas.config_dict(reports={"retention_days": -1}))


class TestTmmSyncAndCheck(unittest.TestCase):
    def setUp(self):
        self.nas = FakeNas()
        self.tmm = FakeTmm(system=False)
        self.key = os.path.join(self.nas.tmp, "tmm.key")
        with open(self.key, "w") as fh:
            fh.write(FakeTmm.API_KEY)
        os.chmod(self.key, 0o600)

    def tearDown(self):
        self.tmm.close()
        self.nas.cleanup()

    def test_tmm_sync_disabled_is_not_a_failure(self):
        with cli_env(self.nas) as path:
            code, _ = run_cli("-c", path, "tmm-sync", "--apply")
        self.assertEqual(code, 0)
        self.assertEqual(self.tmm.posts, [])

    def test_tmm_sync_sends_two_calls_for_new_items_only(self):
        with cli_env(self.nas, tmm={"enabled": True, "base_url": self.tmm.url, "api_key_file": self.key}) as path:
            code, out = run_cli("-c", path, "tmm-sync", "--apply")
        self.assertEqual(code, 0, out)
        # TV shows then movies, each in one call; only what the update
        # of that call found is scraped and renamed (no "unscraped": the whole library is not retried)
        calls = [{"action": "update", "scope": {"name": "all"}}, {"action": "scrape", "scope": {"name": "new"}},
                 {"action": "rename", "scope": {"name": "new"}}]
        self.assertEqual(self.tmm.posts, [("/api/tvshow", calls), ("/api/movie", calls)])
        self.assertNotIn("unscraped", out)

    def test_configured_calls_replace_the_defaults(self):
        mine = [{"action": "update", "scope": {"name": "all"}}, {"action": "scrape", "scope": {"name": "unscraped"}}]
        cfg = {"enabled": True, "base_url": self.tmm.url, "api_key_file": self.key, "full_sync_tvshow": mine}
        with cli_env(self.nas, tmm=cfg) as path:
            code, out = run_cli("-c", path, "tmm-sync", "--apply")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.tmm.posts[0], ("/api/tvshow", mine))

    def test_check_verifies_endpoints_in_api_docs(self):
        with cli_env(self.nas, tmm={"enabled": True, "base_url": self.tmm.url, "api_key_file": self.key}) as path:
            code, out = run_cli("-c", path, "check")
        self.assertEqual(code, 0, out)
        self.assertIn("[ok] tMM endpoint /api/tvshow listed in /api/docs", out)
        self.assertIn("[ok] tMM endpoint /api/movie listed in /api/docs", out)

    def test_check_fails_on_wrong_endpoint_name(self):
        cfg = {"enabled": True, "base_url": self.tmm.url, "api_key_file": self.key, "movie_endpoint": "movies"}
        with cli_env(self.nas, tmm=cfg) as path:
            code, out = run_cli("-c", path, "check")
        self.assertEqual(code, 3)
        self.assertIn("[FAIL] tMM endpoint /api/movies listed in /api/docs", out)

    def test_check_warns_without_api_docs(self):
        self.tmm.docs = False
        with cli_env(self.nas, tmm={"enabled": True, "base_url": self.tmm.url, "api_key_file": self.key}) as path:
            code, out = run_cli("-c", path, "check")
        self.assertEqual(code, 0, out)
        self.assertIn("[warn] tMM has no /api/docs", out)


class TestJournalPriorities(unittest.TestCase):
    def test_prefix(self):
        import logging
        from mediaorg.cli import _JournalFormatter
        f = _JournalFormatter("%(levelname)s %(message)s")
        rec = logging.LogRecord("mediaorg", logging.WARNING, __file__, 1, "tMM is busy", None, None)
        self.assertEqual(f.format(rec), "<4>WARNING tMM is busy")
        rec = logging.LogRecord("mediaorg", logging.ERROR, __file__, 1, "x", None, None)
        self.assertTrue(f.format(rec).startswith("<3>"))


class TestDeployFiles(unittest.TestCase):
    TMM_DIR = os.path.join(ROOT, "contrib", "tmm-docker")

    def read(self, *rel):
        with open(os.path.join(ROOT, *rel), encoding="utf-8") as fh:
            return fh.read()

    @unittest.skipUnless(os.geteuid() == 0, "setup-tmm.sh checks for root before anything else")
    def test_tmm_data_folder_must_be_a_folder_of_its_own(self):
        # refused before any docker call and before anything is created or re-owned; the trees live in the
        # checkout because the script refuses /tmp itself
        if any(ROOT == p or ROOT.startswith(p + "/") for p in ("/tmp", "/var/tmp", "/root", "/opt/mediaorg")):
            self.skipTest(f"the checkout {ROOT} is in a folder setup-tmm.sh refuses")
        tmp = tempfile.mkdtemp(dir=ROOT, prefix=".tmp-setup-tmm-")
        try:
            movies, series = os.path.join(tmp, "lib", "movies"), os.path.join(tmp, "lib", "tv")
            os.makedirs(movies)
            os.makedirs(series)
            busy = os.path.join(tmp, "busy")
            touch(os.path.join(busy, "notes.txt"), b"x")
            cases = [("/", 2, "at least two levels deep"), ("/srv", 2, "at least two levels deep"),
                     ("/etc/tmm", 2, "must not be inside /etc"), ("/var/lib/mediaorg/tmm", 2, "inside /var/lib/mediaorg"),
                     (os.path.join(movies, "tmm"), 2, "inside a library folder"),
                     (os.path.join(tmp, "lib"), 2, "contain a library folder"),
                     (busy, 3, "already holds other files")]
            before = {p: os.stat(p) for p in (tmp, busy, movies)}
            for data, code, text in cases:
                r = subprocess.run(["bash", os.path.join(self.TMM_DIR, "setup-tmm.sh"), "--movies", movies,
                                    "--series", series, "--data", data], capture_output=True, text=True, timeout=60)
                self.assertEqual(r.returncode, code, (data, r.stderr))
                self.assertIn(text, r.stderr, data)
            for p, st in before.items():
                after = os.stat(p)
                self.assertEqual((after.st_uid, after.st_gid, after.st_mode), (st.st_uid, st.st_gid, st.st_mode), p)
            self.assertFalse(os.path.exists(os.path.join(movies, "tmm")))
        finally:
            shutil.rmtree(tmp)

    def test_tmm_unit_is_local_only_and_pinned(self):
        s = self.read("contrib", "tmm-docker", "tinymediamanager.service")
        # web UI: 127.0.0.1 unless /etc/tmm/tmm.conf sets TMM_UI_BIND (EnvironmentFile= overrides
        # Environment=); the API is always local
        lines = s.splitlines()
        self.assertIn("Environment=TMM_UI_BIND=127.0.0.1", lines)
        self.assertIn("EnvironmentFile=/etc/tmm/tmm.conf", lines)
        self.assertIn("-p ${TMM_UI_BIND}:4000:4000 -p 127.0.0.1:7878:7878", s)
        for volume in ("-v ${TMM_DATA}:/data", "-v ${TMM_MOVIES}:/media/movies", "-v ${TMM_SERIES}:/media/series"):
            self.assertIn(volume, s)
        setup = self.read("contrib", "tmm-docker", "setup-tmm.sh")
        self.assertIn("\nTMM_UI_BIND=$UI_BIND\n", setup)
        self.assertIn("\nTMM_DNS_OPTS=$DNS_OPTS\n", setup)  # written by the setup after its DNS test
        self.assertIn('DNS_OPTS="--dns 1.1.1.1 --dns 8.8.8.8"', setup)
        self.assertIn("--pull=never", s)
        self.assertIn("com.centurylinklabs.watchtower.enable=false", s)
        # an empty or malformed TMM_UI_BIND would publish the web UI on every interface: refused
        check = next(x for x in lines if x.startswith("ExecStartPre=/bin/sh -c ") and "TMM_UI_BIND" in x)
        self.assertLess(lines.index(check), next(i for i, x in enumerate(lines) if x.startswith("ExecStart=")))
        body = check[len("ExecStartPre=/bin/sh -c '"):-1].replace("$$", "$").replace("%%", "%")  # systemd escapes
        for value, ok in (("127.0.0.1", True), ("0.0.0.0", True), ("192.0.2.20", True), ("", False),
                          ("localhost", False), ("999.1.1.1", False), ("1.2.3", False), ("::1", False),
                          # a quoted EnvironmentFile value may hold a newline; dash's echo stops at \c
                          ("192.0.2.11\n0.0.0.0", False), ("\n127.0.0.1", False), ("127.0.0.1\\c:9999", False)):
            rc = subprocess.run(["/bin/sh", "-c", body], env={"PATH": "/usr/bin:/bin", "TMM_UI_BIND": value},
                                capture_output=True).returncode
            self.assertEqual(rc == 0, ok, value)
        # DNS for the container: only "--dns <IPv4>" pairs reach docker run, split by systemd ($VAR)
        self.assertIn("Environment=TMM_DNS_OPTS=", lines)
        self.assertIn("ExecStart=/usr/bin/docker run --rm --name tinymediamanager --pull=never $TMM_DNS_OPTS \\", lines)
        dns = next(x for x in lines if x.startswith("ExecStartPre=/bin/sh -c ") and "TMM_DNS_OPTS" in x)
        self.assertLess(lines.index(dns), next(i for i, x in enumerate(lines) if x.startswith("ExecStart=")))
        body = dns[len("ExecStartPre=/bin/sh -c '"):-1].replace("$$", "$").replace("%%", "%")
        for value, ok in (("", True), ("--dns 1.1.1.1", True), ("--dns 1.1.1.1 --dns 8.8.8.8", True),
                          (" --dns 9.9.9.9 ", True), ("   ", True), ("--dns 1.1.1.1--dns 8.8.8.8", False),
                          ("--dns", False), ("--dns 999.1.1.1", False), ("--privileged", False),
                          ("--dns 1.1.1.1 --privileged", False), ("--dns ::1", False), ("--dns 1.1.1.1;id", False),
                          ("--dns 1.1.1.1\n--privileged", False), ("\n-v /:/host --privileged", False),
                          ("--dns 1.1.1.1 \\c--privileged", False), ("--dns 1.1.1.1\n--dns 8.8.8.8", False)):
            rc = subprocess.run(["/bin/sh", "-c", body], env={"PATH": "/usr/bin:/bin", "TMM_DNS_OPTS": value},
                                capture_output=True).returncode
            self.assertEqual(rc == 0, ok, value)
        # folders: absolute, existing, no ':' or ','; never on an empty mount point
        folders = next(x for x in lines if x.startswith("ExecStartPre=/bin/sh -c ") and "TMM_REQUIRE_MOUNT" in x)
        body = folders[len("ExecStartPre=/bin/sh -c '"):-1].replace("$$", "$").replace("%%", "%")
        tmp = tempfile.mkdtemp()
        try:
            good = {"TMM_DATA": tmp, "TMM_MOVIES": tmp, "TMM_SERIES": tmp, "TMM_REQUIRE_MOUNT": ""}
            for change, ok in (({}, True), ({"TMM_MOVIES": "relative"}, False),
                               ({"TMM_SERIES": os.path.join(tmp, "missing")}, False),
                               ({"TMM_DATA": tmp + ":/etc"}, False), ({"TMM_MOVIES": tmp + ",ro"}, False),
                               ({"TMM_REQUIRE_MOUNT": tmp}, False), ({"TMM_REQUIRE_MOUNT": "/"}, True)):
                env = {"PATH": "/usr/bin:/bin", **good, **change}
                rc = subprocess.run(["/bin/sh", "-c", body], env=env, capture_output=True).returncode
                self.assertEqual(rc == 0, ok, change)
        finally:
            shutil.rmtree(tmp)
        self.assertIn("StartLimitIntervalSec=0", s)  # keeps retrying after a power cut
        self.assertIn("Restart=on-failure", s)
        self.assertIn("SuccessExitStatus=143", s)
        self.assertIn("--log-driver=none", s)  # no unbounded docker log file
        self.assertNotIn("5900", s)
        self.assertIn("@sha256:48c15784a12759f3e055492977f9b1dc9d80301f9b0286993c60ae0649e38db9", setup)

    SCRIPTS = ("install.sh", os.path.join("contrib", "tmm-docker", "setup-tmm.sh"),
               os.path.join("tools", "build-release.sh"))

    def test_scripts_parse(self):
        for script in self.SCRIPTS:
            path = os.path.join(ROOT, script)
            if not os.path.exists(path):  # installed copy has no install.sh
                continue
            res = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stderr)

    def test_scripts_pass_shellcheck(self):
        exe = shutil.which("shellcheck")
        if not exe:
            self.skipTest("shellcheck not installed")
        for script in self.SCRIPTS:
            path = os.path.join(ROOT, script)
            if os.path.exists(path):
                res = subprocess.run([exe, "-S", "warning", path], capture_output=True, text=True)
                self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

if __name__ == "__main__":
    unittest.main()
