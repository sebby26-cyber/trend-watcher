"""Zero-config path resolution, setup idempotency and scheduler toggles.

Every test uses a temporary HOME and a fake ``setup.call``; nothing touches the
real home folder, LaunchAgents, crontab or /opt/homebrew/bin.
"""
import _isolation  # noqa: F401  (must precede trend_watch imports)
import contextlib
import io
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend_watch import setup as installer
from trend_watch.cli import main
from trend_watch.config import Paths



class FakeSystem:
    """Stands in for launchctl and crontab."""

    def __init__(self, loaded=False, crontab=None):
        self.loaded = loaded
        self.crontab = crontab
        self.calls = []

    def __call__(self, args, input=None):
        args = list(args)
        self.calls.append(args)
        ok = subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["launchctl", "print"]:
            return ok if self.loaded else subprocess.CompletedProcess(args, 113, "", "not found")
        if args[:2] == ["launchctl", "bootstrap"]:
            self.loaded = True
            return ok
        if args[:2] == ["launchctl", "bootout"]:
            self.loaded = False
            return ok
        if args == ["crontab", "-l"]:
            if self.crontab is None:
                return subprocess.CompletedProcess(args, 1, "", "no crontab for user")
            return subprocess.CompletedProcess(args, 0, self.crontab, "")
        if args == ["crontab", "-"]:
            self.crontab = input
            return ok
        raise AssertionError("unexpected command " + repr(args))

    def count(self, verb):
        return sum(1 for c in self.calls if c[:2] == ["launchctl", verb])


class HomeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve() / "home"
        self.home.mkdir()
        self.env = patch.dict(os.environ, {"HOME": str(self.home), "PATH": "/usr/bin:/bin"}, clear=True)
        self.env.start()
        self.fake = FakeSystem()
        self.call = patch.object(installer, "call", self.fake)
        self.call.start()
        self.candidates = patch.object(installer, "LINK_CANDIDATES", [])
        self.candidates.start()

    def tearDown(self):
        self.candidates.stop()
        self.call.stop()
        self.env.stop()
        self.tmp.cleanup()

    def run_quiet(self, func, *args, **kwargs):
        lines = []
        kwargs.setdefault("out", lines.append)
        return func(*args, **kwargs), lines

    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main(list(argv))
        return code, out.getvalue()


class PathResolutionTests(HomeTest):
    def test_explicit_roots_win(self):
        os.environ.update({"TW_CONFIG_DIR": "/tw/c", "TW_STATE_DIR": "/tw/s", "TW_OUT_DIR": "/tw/o",
                           "TREND_WATCH_HOME": "/common", "XDG_CONFIG_HOME": "/xdg/c", "XDG_DATA_HOME": "/xdg/d"})
        self.assertEqual(Paths.resolve(), Paths(Path("/tw/c"), Path("/tw/s"), Path("/tw/o")))

    def test_common_home_wins_over_xdg(self):
        os.environ.update({"TREND_WATCH_HOME": "/common", "XDG_CONFIG_HOME": "/xdg/c", "XDG_DATA_HOME": "/xdg/d"})
        self.assertEqual(Paths.resolve(), Paths(Path("/common/config"), Path("/common"), Path("/common/out")))

    def test_each_root_independent(self):
        os.environ["TW_STATE_DIR"] = "/tw/s"
        paths = Paths.resolve()
        self.assertEqual(paths.state, Path("/tw/s"))
        self.assertEqual(paths.config, self.home / ".config/trend-watch")
        self.assertEqual(paths.out, Path("/tw/s/out"))

    def test_defaults_under_home(self):
        self.assertEqual(Paths.resolve(), Paths(self.home / ".config/trend-watch",
                         self.home / ".local/share/trend-watch", self.home / ".local/share/trend-watch/out"))

    def test_xdg_roots(self):
        os.environ.update({"XDG_CONFIG_HOME": "/xdg/c", "XDG_DATA_HOME": "/xdg/d"})
        self.assertEqual(Paths.resolve(), Paths(Path("/xdg/c/trend-watch"), Path("/xdg/d/trend-watch"),
                                               Path("/xdg/d/trend-watch/out")))

    def test_common_home_output_independent_of_state_override(self):
        os.environ.update({"TREND_WATCH_HOME": "/common", "TW_STATE_DIR": "/tw/s"})
        self.assertEqual(Paths.resolve().out, Path("/common/out"))

    def test_tilde_expansion(self):
        os.environ["TREND_WATCH_HOME"] = "~/research"
        self.assertEqual(Paths.resolve().config, self.home / "research/config")

    def test_empty_environment_uses_defaults(self):
        os.environ.update({"TREND_WATCH_HOME": "", "TW_OUT_DIR": "", "XDG_DATA_HOME": ""})
        self.assertEqual(Paths.resolve().out, self.home / ".local/share/trend-watch/out")


class AutoInitTests(HomeTest):
    def test_jev_off_on_fresh_machine(self):
        code, out = self.cli("jev", "off")
        self.assertEqual((code, out.strip()), (0, "jev off"))
        config = self.home / ".config/trend-watch/config.json"
        self.assertFalse(json.loads(config.read_text())["llm"]["enabled"])
        self.assertTrue(any((self.home / ".config/trend-watch/domains").glob("*.json")))
        self.assertTrue((self.home / ".local/share/trend-watch").is_dir())
        self.assertEqual(self.cli("jev", "status")[1].strip(), "jev off")

    def test_status_without_state_db(self):
        code, out = self.cli("status")
        self.assertEqual(code, 0)
        self.assertIn("Last run: never", out)
        self.assertIn("Scheduler: off", out)
        self.assertIn("Jev: on", out)
        self.assertIn("calls 0/", out)
        self.assertFalse((self.home / ".local/share/trend-watch/state.db").exists())

    def test_status_reports_last_run_and_outbox(self):
        paths = Paths.resolve()
        installer.record_run(paths, True, "trend-watch: 3 fetched", queued=paths.state / "outbox/digests/x.md", now=0)
        (paths.state / "outbox/digests").mkdir(parents=True)
        (paths.state / "outbox/digests/x.md").write_text("x")
        out = self.cli("status")[1]
        self.assertIn("- ok - trend-watch: 3 fetched", out)
        self.assertIn("Waiting in outbox: 1 file(s)", out)


class MacSetupTests(HomeTest):
    def setup_twice(self, **kwargs):
        first = self.run_quiet(installer.setup, enable_after=False, system="darwin", **kwargs)
        plist = installer.plist_path().read_bytes()
        second = self.run_quiet(installer.setup, enable_after=False, system="darwin", **kwargs)
        return first, second, plist

    def test_setup_is_idempotent(self):
        (code1, lines1), (code2, lines2), plist = self.setup_twice()
        self.assertEqual((code1, code2), (0, 0))
        self.assertEqual(installer.plist_path().read_bytes(), plist)
        link = self.home / ".local/bin/trend-watch"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.path.realpath(link), os.path.realpath(installer.SCRIPT))
        self.assertEqual(sorted(p.name for p in (self.home / ".local/bin").iterdir()), ["trend-watch"])
        self.assertEqual(sorted(p.name for p in installer.plist_path().parent.iterdir()), [installer.LABEL + ".plist"])
        self.assertTrue(any("already installed" in l for l in lines2))
        self.assertTrue(any("unchanged" in l for l in lines2))
        self.assertTrue(any("not on your PATH" in l for l in lines1))
        self.assertEqual(self.fake.count("bootstrap"), 0)

    def test_direct_mode_plist(self):
        self.run_quiet(installer.setup, enable_after=False, system="darwin")
        data = plistlib.loads(installer.plist_path().read_bytes())
        self.assertEqual(data["ProgramArguments"][1:], [str(installer.SCRIPT), "run"])
        self.assertEqual(data["StartCalendarInterval"], [{"Minute": 7}, {"Minute": 37}])
        self.assertFalse(data["RunAtLoad"])
        env = data["EnvironmentVariables"]
        self.assertEqual(env["PATH"], "/usr/bin:/bin:/usr/sbin:/sbin")
        self.assertEqual(env["TW_STATE_DIR"], str(self.home / ".local/share/trend-watch"))
        self.assertEqual(env["TW_REPO"], str(installer.CHECKOUT))
        self.assertEqual(data["StandardOutPath"], str(self.home / ".local/share/trend-watch/logs/launchd.out.log"))

    def test_custom_wrapper_plist(self):
        wrapper = self.home / "scripts/watch.sh"
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text("#!/bin/bash\n")
        wrapper.chmod(0o755)
        os.environ["TREND_WATCH_WRAPPER"] = str(wrapper)
        self.run_quiet(installer.setup, enable_after=False, system="darwin")
        data = plistlib.loads(installer.plist_path().read_bytes())
        self.assertEqual(data["ProgramArguments"], [str(wrapper)])
        self.assertEqual(data["EnvironmentVariables"]["TW_STATE_DIR"], str(Paths.resolve().state))

    def test_setup_enables_by_default_and_reloads_changed_plist(self):
        code, lines = self.run_quiet(installer.setup, system="darwin")
        self.assertEqual(code, 0)
        self.assertTrue(self.fake.loaded)
        self.assertEqual(self.fake.count("bootstrap"), 1)
        self.assertIn("Scheduler: on (runs at :07 and :37 every hour)", lines)
        # Switching to custom wrapper mode changes the plist: a loaded job is reloaded.
        wrapper = self.home / "scripts/watch.sh"
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text("#!/bin/bash\n")
        wrapper.chmod(0o755)
        os.environ["TREND_WATCH_WRAPPER"] = str(wrapper)
        self.run_quiet(installer.setup, enable_after=False, system="darwin")
        self.assertEqual((self.fake.count("bootout"), self.fake.count("bootstrap")), (1, 2))
        self.assertTrue(self.fake.loaded)

    def test_enable_when_loaded_and_disable_when_not(self):
        paths = Paths.resolve()
        self.fake.loaded = True
        code, lines = self.run_quiet(installer.enable, paths, system="darwin")
        self.assertEqual(code, 0)
        self.assertEqual(self.fake.count("bootstrap"), 0)
        self.assertIn("already on", lines[0])
        self.assertTrue(installer.plist_path().exists())
        self.fake.loaded = False
        code, lines = self.run_quiet(installer.disable, paths, system="darwin")
        self.assertEqual((code, lines), (0, ["Scheduler: off"]))
        self.assertEqual(self.fake.count("bootout"), 0)
        self.assertTrue(installer.plist_path().exists())

    def test_link_prefers_writable_candidate_on_path(self):
        good = Path(self.tmp.name).resolve() / "brew/bin"
        good.mkdir(parents=True)
        missing = Path(self.tmp.name).resolve() / "missing/bin"
        link, message, on_path = installer.install_link(candidates=[missing, good], path_env=str(good) + ":/usr/bin")
        self.assertEqual((link, message, on_path), (good / "trend-watch", "installed", True))

    def test_link_falls_back_when_not_on_path_or_unwritable(self):
        off_path = Path(self.tmp.name).resolve() / "off/bin"
        off_path.mkdir(parents=True)
        locked = Path(self.tmp.name).resolve() / "locked/bin"
        locked.mkdir(parents=True)
        with patch.object(installer.os, "access", lambda p, mode: False):
            link, _, _ = installer.install_link(candidates=[off_path, locked], path_env=str(locked))
        self.assertEqual(link, self.home / ".local/bin/trend-watch")
        self.assertFalse((off_path / "trend-watch").exists())

    def test_never_overwrites_regular_file_or_foreign_link(self):
        first = Path(self.tmp.name).resolve() / "a/bin"
        second = Path(self.tmp.name).resolve() / "b/bin"
        for folder in (first, second):
            folder.mkdir(parents=True)
        (first / "trend-watch").write_text("someone else's tool")
        other = Path(self.tmp.name).resolve() / "other-tool"
        other.write_text("x")
        (second / "trend-watch").symlink_to(other)
        link, _, _ = installer.install_link(candidates=[first, second], path_env="{}:{}".format(first, second))
        self.assertEqual((first / "trend-watch").read_text(), "someone else's tool")
        self.assertEqual(os.path.realpath(second / "trend-watch"), str(other))
        self.assertEqual(link, self.home / ".local/bin/trend-watch")

    def test_replaces_dangling_or_old_checkout_link(self):
        folder = Path(self.tmp.name).resolve() / "c/bin"
        folder.mkdir(parents=True)
        (folder / "trend-watch").symlink_to(Path(self.tmp.name) / "gone/bin/trend-watch")
        link, message, _ = installer.install_link(candidates=[folder], path_env=str(folder))
        self.assertEqual((link, message), (folder / "trend-watch", "updated"))
        self.assertEqual(os.path.realpath(link), os.path.realpath(installer.SCRIPT))


class LinuxCronTests(HomeTest):
    def test_enable_disable_preserve_other_lines(self):
        self.fake.crontab = "0 1 * * * backup.sh\nMAILTO=me\n"
        paths = Paths.resolve()
        self.run_quiet(installer.setup, system="linux")
        self.run_quiet(installer.enable, paths, system="linux")
        lines = self.fake.crontab.splitlines()
        self.assertEqual(lines[:2], ["0 1 * * * backup.sh", "MAILTO=me"])
        managed = [l for l in lines if l.endswith(installer.MARKER)]
        self.assertEqual(len(managed), 1)
        self.assertTrue(managed[0].startswith("7,37 * * * * "))
        self.assertIn("TW_STATE_DIR=", managed[0])
        self.assertIn(str(installer.SCRIPT) + " run", managed[0])
        self.assertIn("Scheduler: on", self.run_quiet(installer.status, paths, system="linux")[1][0])
        code, lines = self.run_quiet(installer.disable, paths, system="linux")
        self.assertEqual((code, lines), (0, ["Scheduler: off"]))
        self.assertEqual(self.fake.crontab, "0 1 * * * backup.sh\nMAILTO=me\n")

    def test_setup_no_enable_leaves_crontab_alone(self):
        self.run_quiet(installer.setup, enable_after=False, system="linux")
        self.assertIsNone(self.fake.crontab)
        self.assertTrue((self.home / ".local/share/trend-watch/cron-line.txt").exists())


class CustomSchedulerTests(HomeTest):
    def test_label_override_applies_to_plist_and_launchctl(self):
        os.environ["TREND_WATCH_LABEL"] = "org.example.research"
        paths = Paths.resolve()
        self.assertEqual(installer.plist_path().name, "org.example.research.plist")
        self.assertEqual(plistlib.loads(installer.plist_bytes(paths))["Label"], "org.example.research")
        installer.launchd_loaded()
        self.assertTrue(self.fake.calls[-1][-1].endswith("/org.example.research"))

    def test_nonexecutable_wrapper_falls_back_to_direct(self):
        wrapper = self.home / "wrapper.sh"
        wrapper.write_text("#!/bin/sh\n")
        wrapper.chmod(0o644)
        os.environ["TREND_WATCH_WRAPPER"] = str(wrapper)
        self.assertIsNone(installer.custom_wrapper())
        self.assertEqual(installer.mode_text(), "direct")

    def test_cron_wrapper_and_pack_environment(self):
        wrapper = self.home / "wrapper.sh"
        wrapper.write_text("#!/bin/sh\n")
        wrapper.chmod(0o755)
        os.environ.update({"TREND_WATCH_WRAPPER": str(wrapper), "TREND_WATCH_PACKS_DIR": "/opt/packs"})
        paths = Paths.resolve()
        line = installer.cron_line(paths)
        self.assertIn(str(wrapper), line)
        self.assertIn("TW_STATE_DIR=", line)
        self.assertIn("TREND_WATCH_PACKS_DIR=/opt/packs", line)
        self.assertEqual(installer.mode_text(), "custom wrapper")


if __name__ == "__main__":
    unittest.main()
