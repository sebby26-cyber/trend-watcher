"""Versioned, forward-only schema migrations.

``migrate`` runs before WAL is enabled and before any new-table write. An
existing database with pending migrations is first copied with the SQLite
online backup API (``backups/pre-migrate-…``). Each migration commits in its
own ``BEGIN IMMEDIATE`` transaction together with its ``schema_version`` row,
so a crash leaves the database at a well-defined version. A database newer than
this code is refused rather than silently used.

Version 1 is the baseline schema exactly as the pre-R1 code created it, so a
baseline ``state.db`` is recognised and upgraded in place.
"""
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path


def immutable(table):
    return [
        "CREATE TRIGGER {0}_no_update BEFORE UPDATE ON {0} BEGIN SELECT RAISE(ABORT, '{0} rows are immutable'); END".format(table),
        "CREATE TRIGGER {0}_no_delete BEFORE DELETE ON {0} BEGIN SELECT RAISE(ABORT, '{0} rows are immutable'); END".format(table),
    ]


BASELINE = [
    """CREATE TABLE IF NOT EXISTS items (
      hash TEXT PRIMARY KEY, title_hash TEXT, title TEXT, url TEXT, platform TEXT,
      first_seen REAL, last_seen REAL, published REAL, domains TEXT,
      candidate_at REAL, llm_at REAL, data_json TEXT)""",
    "CREATE INDEX IF NOT EXISTS items_title ON items(title_hash)",
    "CREATE TABLE IF NOT EXISTS aliases (hash TEXT PRIMARY KEY, item_hash TEXT)",
    "CREATE TABLE IF NOT EXISTS scores (hash TEXT, platform TEXT, ts REAL, score REAL, rank INTEGER)",
    "CREATE INDEX IF NOT EXISTS score_history ON scores(hash, platform, ts)",
    """CREATE TABLE IF NOT EXISTS sources (name TEXT PRIMARY KEY, last_fetch REAL,
      last_status TEXT, consecutive_failures INTEGER DEFAULT 0, cooldown_until REAL DEFAULT 0)""",
    "CREATE TABLE IF NOT EXISTS llm_cache (hash TEXT PRIMARY KEY, model TEXT, result_json TEXT, ts REAL)",
    """CREATE TABLE IF NOT EXISTS budget (day TEXT PRIMARY KEY, calls INTEGER DEFAULT 0,
      input_tokens INTEGER DEFAULT 0, draft_calls INTEGER DEFAULT 0)""",
    "CREATE TABLE IF NOT EXISTS llm_calls (ts REAL, kind TEXT)",
    "CREATE TABLE IF NOT EXISTS drafts (hash TEXT PRIMARY KEY, day TEXT, ts REAL)",
    "CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, ts REAL, summary_json TEXT)",
    "CREATE TABLE IF NOT EXISTS pending (hash TEXT PRIMARY KEY, data_json TEXT, ts REAL)",
    "CREATE TABLE IF NOT EXISTS signals (key TEXT, platform TEXT, ts REAL)",
    "CREATE INDEX IF NOT EXISTS signal_window ON signals(ts)",
]

# P0 hardening + observations for every adapter fetch.
V2 = [
    # Versioned Jev cache key. NULL = a pre-R1 row, honoured as-is and never reinterpreted.
    "ALTER TABLE llm_cache ADD COLUMN cache_version TEXT",
    "CREATE INDEX llm_calls_kind ON llm_calls(kind, ts)",
    """CREATE TABLE source_items (
      id INTEGER PRIMARY KEY, uid TEXT NOT NULL UNIQUE, source TEXT NOT NULL, external_id TEXT NOT NULL,
      first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, sightings INTEGER NOT NULL DEFAULT 1,
      UNIQUE(source, external_id))""",
    """CREATE TABLE observations (
      id INTEGER PRIMARY KEY, uid TEXT NOT NULL UNIQUE,
      source_item_id INTEGER NOT NULL REFERENCES source_items(id),
      payload_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
      fetched_at TEXT NOT NULL, published_at TEXT, fetch_uid TEXT,
      UNIQUE(source_item_id, payload_hash))""",
    *immutable("observations"),
    """CREATE TABLE fetch_checkpoints (
      source TEXT NOT NULL, url TEXT NOT NULL, etag TEXT, last_modified TEXT,
      last_status INTEGER, last_fetch_at TEXT, updated_at TEXT NOT NULL, PRIMARY KEY(source, url))""",
    """CREATE TABLE stage_runs (
      id INTEGER PRIMARY KEY, uid TEXT NOT NULL UNIQUE, tick_uid TEXT, stage TEXT NOT NULL,
      scope TEXT NOT NULL DEFAULT '', stage_version TEXT NOT NULL, started_at TEXT NOT NULL,
      finished_at TEXT, status TEXT NOT NULL, counts_json TEXT NOT NULL DEFAULT '{}',
      error TEXT, prerequisite_run_id INTEGER)""",
    "CREATE INDEX stage_runs_stage ON stage_runs(stage, scope, id)",
]

# Intelligence engine R1: events, annotations, evaluations, records, acks.
V3 = [
    """CREATE TABLE events (
      id INTEGER PRIMARY KEY, uid TEXT NOT NULL UNIQUE, lane TEXT NOT NULL,
      cik INTEGER NOT NULL, cik_source TEXT NOT NULL, accession TEXT NOT NULL, form TEXT NOT NULL,
      company TEXT, filed_date TEXT, created_at TEXT NOT NULL,
      amends_event_id INTEGER REFERENCES events(id), amendment_status TEXT NOT NULL,
      amendment_basis TEXT, UNIQUE(lane, cik, accession))""",
    "CREATE INDEX events_lane_created ON events(lane, created_at)",
    # Identity is immutable; amends_event_id may only move NULL -> value (deterministic late link).
    """CREATE TRIGGER events_identity BEFORE UPDATE ON events
      WHEN NEW.uid IS NOT OLD.uid OR NEW.lane IS NOT OLD.lane OR NEW.cik IS NOT OLD.cik
        OR NEW.accession IS NOT OLD.accession OR NEW.form IS NOT OLD.form
        OR NEW.cik_source IS NOT OLD.cik_source OR NEW.created_at IS NOT OLD.created_at
        OR (OLD.amends_event_id IS NOT NULL AND NEW.amends_event_id IS NOT OLD.amends_event_id)
      BEGIN SELECT RAISE(ABORT, 'event identity is immutable'); END""",
    "CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'events rows are immutable'); END",
    """CREATE TABLE event_revisions (
      id INTEGER PRIMARY KEY, uid TEXT NOT NULL UNIQUE, event_id INTEGER NOT NULL REFERENCES events(id),
      revision_no INTEGER NOT NULL, observation_id INTEGER NOT NULL REFERENCES observations(id),
      content_hash TEXT NOT NULL, content_json TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL,
      UNIQUE(event_id, revision_no), UNIQUE(event_id, content_hash))""",
    *immutable("event_revisions"),
    """CREATE TABLE event_entities (
      id INTEGER PRIMARY KEY, revision_id INTEGER NOT NULL REFERENCES event_revisions(id),
      cik INTEGER NOT NULL, match_kind TEXT NOT NULL, decisive INTEGER NOT NULL,
      evidence TEXT NOT NULL, watchlist_version TEXT NOT NULL,
      UNIQUE(revision_id, cik, match_kind, evidence, watchlist_version))""",
    *immutable("event_entities"),
    """CREATE TABLE observation_resolutions (
      observation_id INTEGER PRIMARY KEY REFERENCES observations(id), status TEXT NOT NULL,
      event_id INTEGER, revision_id INTEGER, error TEXT, resolved_at TEXT NOT NULL)""",
    """CREATE TABLE dead_letters (
      id INTEGER PRIMARY KEY, stage TEXT NOT NULL, subject_kind TEXT NOT NULL, subject_id INTEGER NOT NULL,
      error TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(stage, subject_kind, subject_id))""",
    """CREATE TABLE intake_admissions (
      consumer TEXT NOT NULL, env TEXT NOT NULL, event_id INTEGER NOT NULL REFERENCES events(id),
      status TEXT NOT NULL, reason TEXT NOT NULL, window_day TEXT, eligible_at TEXT, decided_at TEXT NOT NULL,
      PRIMARY KEY(consumer, env, event_id))""",
    """CREATE TABLE intake_log (
      id INTEGER PRIMARY KEY, consumer TEXT NOT NULL, env TEXT NOT NULL, event_id INTEGER NOT NULL,
      status TEXT NOT NULL, reason TEXT NOT NULL, window_day TEXT, eligible_at TEXT, at TEXT NOT NULL)""",
    *immutable("intake_log"),
    """CREATE TABLE breaker_trips (
      id INTEGER PRIMARY KEY, consumer TEXT NOT NULL, env TEXT NOT NULL, lane TEXT NOT NULL,
      tripped_at TEXT NOT NULL, pause_until TEXT NOT NULL, observed INTEGER NOT NULL,
      baseline TEXT, threshold INTEGER NOT NULL, reason TEXT NOT NULL)""",
    *immutable("breaker_trips"),
    """CREATE TABLE annotations (
      id INTEGER PRIMARY KEY, uid TEXT NOT NULL UNIQUE, producer TEXT NOT NULL, producer_version TEXT NOT NULL,
      input_hash TEXT NOT NULL, status TEXT NOT NULL, value_json TEXT NOT NULL, created_at TEXT NOT NULL,
      UNIQUE(producer, producer_version, input_hash))""",
    *immutable("annotations"),
    """CREATE TABLE annotation_queue (
      producer TEXT NOT NULL, producer_version TEXT NOT NULL, input_hash TEXT NOT NULL, namespace TEXT NOT NULL,
      status TEXT NOT NULL, reason TEXT, eligible_at TEXT, updated_at TEXT NOT NULL,
      PRIMARY KEY(producer, producer_version, input_hash, namespace))""",
    """CREATE TABLE llm_reservations (
      id INTEGER PRIMARY KEY, day TEXT NOT NULL, workload TEXT NOT NULL, namespace TEXT NOT NULL,
      calls INTEGER NOT NULL, est_tokens INTEGER NOT NULL, actual_tokens INTEGER,
      state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    "CREATE INDEX llm_reservations_day ON llm_reservations(day, workload, state)",
    """CREATE TABLE annotation_attempts (
      id INTEGER PRIMARY KEY, uid TEXT NOT NULL UNIQUE, producer TEXT NOT NULL, producer_version TEXT NOT NULL,
      input_hash TEXT NOT NULL, namespace TEXT NOT NULL, attempt_no INTEGER NOT NULL, state TEXT NOT NULL,
      reservation_id INTEGER REFERENCES llm_reservations(id), error_code TEXT,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      UNIQUE(producer, producer_version, input_hash, attempt_no))""",
    """CREATE TABLE dependency_episodes (
      id INTEGER PRIMARY KEY, backend TEXT NOT NULL, reason TEXT NOT NULL, opened_at TEXT NOT NULL,
      closed_at TEXT, reported_at TEXT, probes INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE evaluation_requests (
      id INTEGER PRIMARY KEY, uid TEXT NOT NULL UNIQUE, consumer TEXT NOT NULL, env TEXT NOT NULL,
      event_id INTEGER NOT NULL REFERENCES events(id), revision_id INTEGER NOT NULL REFERENCES event_revisions(id),
      consumer_version TEXT NOT NULL, ruleset_version TEXT NOT NULL, input_hash TEXT NOT NULL,
      replay_batch_id TEXT NOT NULL DEFAULT '', trigger TEXT NOT NULL, requested_at TEXT NOT NULL,
      input_snapshot_json TEXT NOT NULL,
      UNIQUE(consumer, env, revision_id, consumer_version, ruleset_version, input_hash, replay_batch_id))""",
    *immutable("evaluation_requests"),
    """CREATE TABLE evaluation_attempts (
      id INTEGER PRIMARY KEY, request_id INTEGER NOT NULL REFERENCES evaluation_requests(id),
      attempt_no INTEGER NOT NULL, processing_status TEXT NOT NULL, decision TEXT, reason_code TEXT,
      eligible_at TEXT, trace_json TEXT NOT NULL, created_at TEXT NOT NULL,
      UNIQUE(request_id, attempt_no))""",
    *immutable("evaluation_attempts"),
    """CREATE TABLE records (
      id INTEGER PRIMARY KEY, uid TEXT NOT NULL UNIQUE, consumer TEXT NOT NULL, env TEXT NOT NULL,
      event_id INTEGER NOT NULL REFERENCES events(id), kind TEXT NOT NULL,
      request_id INTEGER NOT NULL REFERENCES evaluation_requests(id), decision TEXT NOT NULL,
      reason_code TEXT NOT NULL, body_json TEXT NOT NULL, body_hash TEXT NOT NULL, created_at TEXT NOT NULL)""",
    *immutable("records"),
    """CREATE TABLE record_supersessions (
      old_record_id INTEGER PRIMARY KEY REFERENCES records(id), new_record_id INTEGER NOT NULL UNIQUE REFERENCES records(id),
      created_at TEXT NOT NULL, CHECK (new_record_id > old_record_id))""",
    *immutable("record_supersessions"),
    """CREATE TABLE record_heads (
      consumer TEXT NOT NULL, env TEXT NOT NULL, event_id INTEGER NOT NULL, kind TEXT NOT NULL,
      record_id INTEGER NOT NULL UNIQUE REFERENCES records(id), PRIMARY KEY(consumer, env, event_id, kind))""",
    """CREATE TABLE acks (
      id INTEGER PRIMARY KEY, record_id INTEGER NOT NULL REFERENCES records(id), actor TEXT NOT NULL,
      acked_at TEXT NOT NULL, note TEXT, verdict TEXT, corrected_decision TEXT)""",
    *immutable("acks"),
    """CREATE TABLE render_log (
      id INTEGER PRIMARY KEY, consumer TEXT NOT NULL, env TEXT NOT NULL, path TEXT NOT NULL,
      rendered_at TEXT NOT NULL, max_record_id INTEGER NOT NULL, max_ack_id INTEGER NOT NULL)""",
]

MIGRATIONS = [
    (1, "baseline", BASELINE),
    (2, "p0-hardening-observations", V2),
    (3, "intel-engine-r1", V3),
]
LATEST = MIGRATIONS[-1][0]


class SchemaTooNew(ValueError):
    pass


def table_exists(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def current_version(conn):
    if not table_exists(conn, "schema_version"):
        return 0
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return row[0] or 0


def has_user_tables(conn):
    return conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchone()[0] > 0


def stamp(now=None):
    return datetime.fromtimestamp(time.time() if now is None else now, timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def backup_to(conn, folder, label, now=None):
    """Copy a live connection with the online backup API; never overwrites."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    base = "{}-{}".format(label, stamp(now))
    target = folder / (base + ".db")
    n = 1
    while target.exists():
        n += 1
        target = folder / "{}-{}.db".format(base, n)
    dest = sqlite3.connect(str(target))
    try:
        conn.backup(dest)
        ok = dest.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise sqlite3.DatabaseError("backup integrity check failed: " + ok)
    finally:
        dest.close()
    return target


def migrate(conn, db_path=None, backup_dir=None):
    """Bring ``conn`` to LATEST. Returns (from_version, to_version, backup_path)."""
    version = current_version(conn)
    if version > LATEST:
        raise SchemaTooNew("state.db schema v{} is newer than this code (v{}); refusing to open".format(version, LATEST))
    pending = [m for m in MIGRATIONS if m[0] > version]
    if not pending:
        return version, version, None
    backup = None
    if has_user_tables(conn) and db_path is not None:
        folder = backup_dir or Path(db_path).parent / "backups"
        backup = backup_to(conn, folder, "pre-migrate-v{}-to-v{}".format(version, LATEST))
    if conn.in_transaction:
        conn.commit()
    for number, name, statements in pending:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
            if current_version(conn) >= number:  # another process got here first
                conn.rollback()
                continue
            for statement in statements:
                conn.execute(statement)
            conn.execute("INSERT INTO schema_version VALUES (?,?,?)", (number, name, stamp()))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return version, LATEST, backup
