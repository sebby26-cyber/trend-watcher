"""File decay: dry-run default, keep_last, never-list and symlink refusal.

All tests use temporary configuration and state directories.
"""
import _isolation  # noqa: F401  (must precede trend_watch imports)
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend_watch.cli import main, mutating, parser
from trend_watch.config import ROOT, Paths
from trend_watch.decay import decay, load_policy, plan, validate

DAY = 86400


def touch(path, age_days, now, data=b"x" * 10):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(data)
    os.utime(path, (now - age_days * DAY, now - age_days * DAY))


class DecayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.paths = Paths(config=root / "config", state=root / "state with space", out=root / "out")
        self.paths.config.mkdir()
        self.now = time.time()
        s = self.paths.state
        for i in range(5):
            touch(s / "logs" / "run {}.log".format(i), 20 + i, self.now)
        touch(s / "logs" / "fresh.log", 1, self.now)
        touch(s / "logs" / "last-run.json", 40, self.now)
        touch(s / "state.db", 40, self.now)
        touch(s / "feeds" / "trading" / "2026-01-01.jsonl", 90, self.now)
        outside = Path(self.tmp.name) / "outside.log"
        touch(outside, 90, self.now)
        os.symlink(outside, s / "logs" / "link.log")
        policy = {"rules": [
            {"name": "run-logs", "path": "logs", "glob": "*.log", "kind": "file", "max_age_days": 14, "keep_last": 2},
            {"name": "feeds", "path": "feeds/trading", "glob": "*", "kind": "file", "max_age_days": 14, "keep_last": 0},
        ], "never": ["state.db", "feeds", "logs/last-run.json"]}
        (self.paths.config / "decay.json").write_text(json.dumps(policy))
        self.outside = outside

    def tearDown(self):
        self.tmp.cleanup()

    def test_dry_run_removes_nothing(self):
        lines = []
        result = decay(self.paths, apply=False, now=self.now, out=lines.append)
        # 6 real logs, newest 2 kept (fresh + run 0), so run 1..4 eligible; feeds are never-listed.
        self.assertEqual(result["entries"], 4)
        self.assertTrue((self.paths.state / "logs" / "run 4.log").exists())
        self.assertFalse((self.paths.state / "logs" / "decay.jsonl").exists())

    def test_apply_honours_keep_last_never_and_symlinks(self):
        decay(self.paths, apply=True, now=self.now, out=lambda *_: None)
        logs = self.paths.state / "logs"
        self.assertTrue((logs / "fresh.log").exists())
        self.assertTrue((logs / "run 0.log").exists())
        for i in range(1, 5):
            self.assertFalse((logs / "run {}.log".format(i)).exists())
        self.assertTrue((logs / "last-run.json").exists())
        self.assertTrue((self.paths.state / "state.db").exists())
        self.assertTrue((self.paths.state / "feeds" / "trading" / "2026-01-01.jsonl").exists())
        self.assertTrue(self.outside.exists())
        self.assertTrue((logs / "link.log").is_symlink())
        entries = [json.loads(l) for l in (logs / "decay.jsonl").read_text().splitlines()]
        self.assertEqual(sum(1 for e in entries if e.get("status") == "removed"), 4)

    def test_validate_rejects_escaping_paths(self):
        for bad in ("..", "../x", "/tmp", ""):
            with self.assertRaises(ValueError):
                validate({"rules": [{"name": "b", "path": bad, "glob": "*", "kind": "file",
                                     "max_age_days": 1, "keep_last": 0}]})

    def test_config_policy_replaces_seed(self):
        source = self.paths.config / "decay.json"
        expected = json.loads(source.read_text())
        policy, loaded_source = load_policy(self.paths)
        self.assertEqual(loaded_source, source)
        self.assertEqual(policy, expected)
        self.assertEqual(len(policy["rules"]), 2)
        lines = []
        result = decay(self.paths, now=self.now, out=lines.append)
        self.assertIn("policy={}".format(source), lines[0])
        # The config keeps two logs; the seed keeps 96 and would select none.
        self.assertEqual(result["entries"], 4)

    def test_missing_config_policy_uses_seed(self):
        (self.paths.config / "decay.json").unlink()
        policy, source = load_policy(self.paths)
        self.assertEqual(source, ROOT / "seed/decay.json")
        self.assertEqual(policy, json.loads(source.read_text()))
        validate(policy)
        self.assertEqual(decay(self.paths, now=self.now, out=lambda *_: None)["entries"], 0)

    def test_cli_dry_run_and_mutating(self):
        self.assertFalse(mutating(parser().parse_args(["decay"])))
        self.assertTrue(mutating(parser().parse_args(["decay", "--apply"])))
        before = set(self.paths.state.rglob("*"))
        env = {"TW_CONFIG_DIR": str(self.paths.config),
               "TW_STATE_DIR": str(self.paths.state),
               "TW_OUT_DIR": str(self.paths.out)}
        output = io.StringIO()
        with patch.dict(os.environ, env), contextlib.redirect_stdout(output), \
                patch("trend_watch.cli.state_lock") as lock:
            self.assertEqual(main(["decay"]), 0)
            lock.assert_not_called()
        self.assertIn("decay (dry-run)", output.getvalue())
        self.assertIn("total 4 entries", output.getvalue())
        self.assertEqual(set(self.paths.state.rglob("*")), before)
        self.assertFalse((self.paths.state / "logs/decay.jsonl").exists())

    def test_seed_policy_is_valid(self):
        seed = json.loads((Path(__file__).resolve().parent.parent / "seed/decay.json").read_text())
        validate(seed)
        names = {r["path"] for r in seed["rules"]}
        self.assertNotIn("feeds/trading", names)
        self.assertIn("state.db", seed["never"])


if __name__ == "__main__":
    unittest.main()
