"""P0 hardening: migrations, WAL, backup/restore, lock, rounding, per-kind gates, observations."""
import _isolation  # noqa: F401  (must precede trend_watch imports)
import fcntl
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from test_trend_watch import NOW, Base, FakeJevHTTP, ROOT, fake_adapter, item
from trend_watch import migrations
from trend_watch.cli import LOCK_BUSY, main
from trend_watch.config import defaults, load_config
from trend_watch.lock import LOCK_NAME
from trend_watch.serialize import canonical, dumps, round_floats
from trend_watch.state import State

# The schema exactly as the pre-R1 code created it.
BASELINE_SQL = '''
CREATE TABLE IF NOT EXISTS items (
  hash TEXT PRIMARY KEY, title_hash TEXT, title TEXT, url TEXT, platform TEXT,
  first_seen REAL, last_seen REAL, published REAL, domains TEXT,
  candidate_at REAL, llm_at REAL, data_json TEXT);
CREATE INDEX IF NOT EXISTS items_title ON items(title_hash);
CREATE TABLE IF NOT EXISTS aliases (hash TEXT PRIMARY KEY, item_hash TEXT);
CREATE TABLE IF NOT EXISTS scores (hash TEXT, platform TEXT, ts REAL, score REAL, rank INTEGER);
CREATE INDEX IF NOT EXISTS score_history ON scores(hash, platform, ts);
CREATE TABLE IF NOT EXISTS sources (name TEXT PRIMARY KEY, last_fetch REAL,
  last_status TEXT, consecutive_failures INTEGER DEFAULT 0, cooldown_until REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS llm_cache (hash TEXT PRIMARY KEY, model TEXT, result_json TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS budget (day TEXT PRIMARY KEY, calls INTEGER DEFAULT 0,
  input_tokens INTEGER DEFAULT 0, draft_calls INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS llm_calls (ts REAL, kind TEXT);
CREATE TABLE IF NOT EXISTS drafts (hash TEXT PRIMARY KEY, day TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, ts REAL, summary_json TEXT);
CREATE TABLE IF NOT EXISTS pending (hash TEXT PRIMARY KEY, data_json TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS signals (key TEXT, platform TEXT, ts REAL);
CREATE INDEX IF NOT EXISTS signal_window ON signals(ts);
'''
LEGACY_HASH = "a" * 64
LEGACY_DATA = json.dumps({"score": 0.1 + 0.2})  # an unrounded pre-R1 row (built at runtime: no digit runs in source)


def baseline_db(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(BASELINE_SQL)
    conn.execute("INSERT INTO items(hash,title,url,first_seen,candidate_at,llm_at,data_json) VALUES (?,?,?,?,?,?,?)",
                 (LEGACY_HASH, "legacy", "https://example.com/l", NOW - 10, NOW - 10, NOW - 10, LEGACY_DATA))
    conn.execute("INSERT INTO llm_cache VALUES (?,?,?,?)", (LEGACY_HASH, "jev-1.0", '{"status":"ok"}', NOW - 10))
    conn.execute("INSERT INTO llm_calls VALUES (?,?)", (NOW - 10, "classify"))
    conn.commit()
    conn.close()


class MigrationTests(Base):
    def test_upgrade_baseline_db_backup_first_then_wal(self):
        path = self.paths.state / "state.db"
        baseline_db(path)
        db = State(path)
        before, after, saved = db.migration
        self.assertEqual((before, after), (0, migrations.LATEST))
        self.assertTrue(saved.name.startswith("pre-migrate-v0-to-v3-"))
        # The backup holds the untouched baseline schema (taken before any migration write).
        conn = sqlite3.connect(str(saved))
        self.assertFalse(migrations.table_exists(conn, "schema_version"))
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        conn.close()
        self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual([r[0] for r in db.execute("SELECT version FROM schema_version ORDER BY version")], [1, 2, 3])
        # Legacy state is preserved and never reinterpreted.
        row = db.execute("SELECT * FROM items WHERE hash=?", (LEGACY_HASH,)).fetchone()
        self.assertEqual((row["candidate_at"], row["llm_at"], row["data_json"]), (NOW - 10, NOW - 10, LEGACY_DATA))
        self.assertIsNone(db.execute("SELECT cache_version FROM llm_cache").fetchone()[0])
        self.assertEqual(db.cache(LEGACY_HASH), {"status": "ok"})
        for table in ("observations", "events", "records", "acks", "stage_runs"):
            self.assertTrue(db.has_table(table))
        db.close()
        again = State(path)
        self.assertEqual(again.migration, (3, 3, None))
        again.close()
        self.assertEqual(len(list((self.paths.state / "backups").glob("*.db"))), 1)

    def test_fresh_db_needs_no_backup_and_newer_schema_is_refused(self):
        path = self.paths.state / "state.db"
        State(path).close()
        self.assertFalse((self.paths.state / "backups").exists())
        conn = sqlite3.connect(str(path))
        conn.execute("INSERT INTO schema_version VALUES (99, 'future', 'x')")
        conn.commit()
        conn.close()
        with self.assertRaises(migrations.SchemaTooNew):
            State(path)

    def test_failed_migration_rolls_back_to_previous_version(self):
        path = self.paths.state / "state.db"
        baseline_db(path)
        broken = list(migrations.MIGRATIONS)
        broken[2] = (3, "broken", ["CREATE TABLE events (id INTEGER PRIMARY KEY)", "THIS IS NOT SQL"])
        with patch.object(migrations, "MIGRATIONS", broken):
            with self.assertRaises(sqlite3.OperationalError):
                State(path)
        conn = sqlite3.connect(str(path))
        self.assertEqual(migrations.current_version(conn), 2)
        self.assertFalse(migrations.table_exists(conn, "events"))
        conn.close()
        self.assertEqual(State(path).migration[1], 3)

    def test_status_probe_does_not_migrate(self):
        path = self.paths.state / "state.db"
        baseline_db(path)
        db = State(path, migrate=False)
        self.assertEqual(db.budget(NOW)["calls"], 0)
        self.assertEqual(db.shadow_usage(NOW), (0, 0))
        db.close()
        conn = sqlite3.connect(str(path))
        self.assertEqual(migrations.current_version(conn), 0)
        conn.close()


class BackupRestoreTests(Base):
    def cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "bin/trend-watch")] + list(args), capture_output=True, text=True, env=dict(os.environ), cwd=ROOT)

    def test_backup_then_restore_round_trip_archives_current(self):
        self.tick(no_llm=True)
        result = self.cli("backup")
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = Path(result.stdout.strip())
        self.assertTrue(saved.is_file())
        db = State(self.paths.state / "state.db")
        items = db.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        db.execute("INSERT INTO runs(ts,summary_json) VALUES (?,?)", (NOW + 5, "{}"))
        db.execute("DELETE FROM items")
        db.conn.commit()
        db.close()
        result = self.cli("restore", str(saved))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("previous state archived", result.stdout)
        db = State(self.paths.state / "state.db")
        self.assertEqual(db.execute("SELECT COUNT(*) FROM items").fetchone()[0], items)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM runs WHERE ts=?", (NOW + 5,)).fetchone()[0], 0)
        db.close()
        archived = next((self.paths.state / "backups").glob("pre-restore-*.db"))
        conn = sqlite3.connect(str(archived))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM items").fetchone()[0], 0)
        conn.close()

    def test_restore_rejects_missing_or_corrupt_file(self):
        bad = self.paths.state / "bad.db"
        self.paths.state.mkdir(parents=True, exist_ok=True)
        bad.write_bytes(b"not a database")
        self.assertEqual(self.cli("restore", str(bad)).returncode, 1)
        self.assertEqual(self.cli("restore", str(self.paths.state / "missing.db")).returncode, 1)

    def test_migrate_command(self):
        result = self.cli("migrate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("-> v3", result.stdout)


class LockTests(Base):
    def test_mutating_commands_fail_fast_when_locked_and_readers_do_not_wait(self):
        self.paths.state.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.paths.state / LOCK_NAME), os.O_RDWR | os.O_CREAT)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        with patch("sys.stderr"), patch("sys.stdout"):
            self.assertEqual(main(["run", "--dry-run"]), LOCK_BUSY)
            with patch("trend_watch.lock.time.sleep"), patch("trend_watch.lock.time.monotonic", side_effect=[0, 100]):
                self.assertEqual(main(["budget"]), LOCK_BUSY)
            self.assertEqual(main(["jev", "status"]), 0)
        fcntl.flock(fd, fcntl.LOCK_UN)
        with patch("sys.stderr"), patch("sys.stdout"):
            self.assertEqual(main(["jev", "status"]), 0)

    def test_lock_is_reentrant_in_process(self):
        from trend_watch.lock import state_lock
        with state_lock(self.paths.state):
            with state_lock(self.paths.state):
                pass
        with state_lock(self.paths.state):
            pass


class RoundingTests(Base):
    def test_round_floats_only_at_serialization(self):
        self.assertEqual(round_floats({"a": [0.1 + 0.2, 1 / 3], "b": 7, "c": repr(0.1 + 0.2)}),
                         {"a": [0.3, 0.333333], "b": 7, "c": repr(0.1 + 0.2)})
        self.assertEqual(dumps(float("inf")), "Infinity")
        with self.assertRaises(ValueError):
            canonical(float("nan"))

    def test_feed_and_item_json_have_no_long_float_expansions(self):
        self.registry["fake"] = fake_adapter(items=[item(score=1 / 3), item(title="OpenAI GPT b", url="https://example.com/b", score=0.1 + 0.2)])
        self.tick(no_llm=True)
        long_fraction = re.compile(r"\.\d{7,}")
        feed = next((self.paths.state / "feeds/trading").glob("*.jsonl")).read_text()
        self.assertNotRegex(feed, long_fraction)
        db = self.state()
        for (data,) in db.execute("SELECT data_json FROM items"):
            self.assertNotRegex(data, long_fraction)
        for (data,) in db.execute("SELECT summary_json FROM runs"):
            self.assertNotRegex(data, long_fraction)
        self.assertNotRegex((self.paths.state / "logs/last-run.json").read_text(), long_fraction)


class PerKindGateTests(Base):
    def test_draft_call_no_longer_delays_classify(self):
        db = self.state()
        db.charge_call(NOW - 100, draft=True)
        http = FakeJevHTTP()
        summary = self.tick(client_factory=self.fake_client(http), no_drafts=True)
        self.assertIsNone(summary["no_llm_reason"])
        self.assertEqual(summary["jev_calls"], 1)

    def test_classify_interval_and_kind_budget(self):
        db = self.state()
        db.charge_call(NOW - 100)
        summary = self.tick(client_factory=self.fake_client(FakeJevHTTP()))
        self.assertIn("min interval", summary["no_llm_reason"])
        self.cfg["budget"]["kinds"]["classify"]["max_calls_per_day"] = 1
        self.write_config()
        summary = self.tick(now=NOW + 3601, client_factory=self.fake_client(FakeJevHTTP()))
        self.assertEqual(summary["no_llm_reason"], "daily classify call budget exhausted")
        self.assertIsNone(db.budget_reason(self.cfg, NOW, "draft"))

    def test_draft_kind_interval_gate(self):
        self.cfg["llm"]["kind_min_interval_s"]["draft"] = 7200
        self.write_config()
        db = self.state()
        db.charge_call(NOW - 60, draft=True)
        summary = self.tick(client_factory=self.fake_client(FakeJevHTTP()))
        self.assertEqual(summary["jev_calls"], 1)
        self.assertEqual(summary["drafts"], 0)

    def test_versioned_cache_key_on_new_rows(self):
        self.tick(client_factory=self.fake_client(FakeJevHTTP()), no_drafts=True)
        self.assertEqual(self.state().execute("SELECT cache_version FROM llm_cache").fetchone()[0], "classify/1")


class LegacyObservationTests(Base):
    def test_every_fetch_is_observed_before_selection_and_immutable(self):
        self.tick(no_llm=True)
        self.tick(no_llm=True, now=NOW + 60)
        db = self.state()
        self.assertEqual(db.execute("SELECT sightings FROM source_items").fetchone()[0], 2)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 1)
        self.registry["fake"] = fake_adapter(items=[item(snippet="corrected text")])
        self.tick(no_llm=True, now=NOW + 120)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 2)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM source_items").fetchone()[0], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE observations SET payload_json='{}'")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("DELETE FROM observations")

    def test_observation_failure_never_blocks_trend_path(self):
        with patch("trend_watch.pipeline.record_legacy_fetch", side_effect=RuntimeError("disk")):
            summary = self.tick(no_llm=True)
        self.assertEqual(summary["candidates"], 1)
        self.assertIn("observations: RuntimeError", summary["errors"])


class ConfigSchemaTests(Base):
    def test_seed_r1_keys_validate_and_bad_values_are_rejected(self):
        load_config(self.paths)
        for path, value in ((("investment", "env"), "production"), (("intake", "sec_edgar", "request_interval_s"), 0.05),
                            (("intake", "sec_edgar", "allowed_hosts"), ["evil.example"]),
                            (("intake", "sec_edgar", "feed_url"), "http://www.sec.gov/x"),
                            (("tripwire", "exempt_fields"), {"snippet": "url"}),
                            (("investment", "materiality", "max_ambiguous_attempts"), 3)):
            cfg = json.loads(json.dumps(self.cfg))
            target = cfg
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            (self.paths.config / "config.json").write_text(json.dumps(cfg))
            with self.assertRaises(ValueError, msg=str(path)):
                load_config(self.paths)

    def test_unknown_r1_key_rejected(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["investment"]["mystery"] = 1
        (self.paths.config / "config.json").write_text(json.dumps(cfg))
        with self.assertRaises(ValueError):
            load_config(self.paths)

    def test_seed_has_no_secrets_and_contact_placeholder(self):
        seed = (ROOT / "seed/config.json").read_text()
        self.assertIn("<CONTACT_EMAIL_REQUIRED>", seed)
        self.assertNotRegex(seed, r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
        self.assertFalse(defaults()["intake"]["sec_edgar"]["enabled"])
