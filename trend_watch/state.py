"""Append-only observations and durable dedupe, pending, budget and draft state."""
import json
import sqlite3
from datetime import datetime, timezone
from . import migrations
from .serialize import dumps


def day_at(now):
    return datetime.fromtimestamp(now, timezone.utc).date().isoformat()


CLASSIFY_CACHE_VERSION = "classify/1"
KINDS = ("classify", "draft", "materiality")


class State:
    def __init__(self, path, migrate=True):
        """Open state.db; migrations run before WAL and before any new-table write.

        ``migrate=False`` is for read-only status probes: no schema change, no
        journal-mode change, and R1 tables are treated as empty if absent.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(str(path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.migration = None
        if migrate:
            self.migration = migrations.migrate(self.conn, path)
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.commit()

    def has_table(self, name):
        return migrations.table_exists(self.conn, name)

    # Time-series tables pruned by retention: (table, timestamp column).
    # items/aliases (durable dedupe), sources, llm_cache, budget, drafts and
    # pending are durable state and are never pruned here.
    RETAINED = (("signals", "ts"), ("scores", "ts"), ("runs", "ts"), ("llm_calls", "ts"))

    def prune(self, now, settings):
        """Delete time-series rows older than settings['days']; VACUUM when due.

        settings['table_days'] overrides the window per table: signals keep
        2 days because scoring only reads the last cooccurrence_h hours of
        them, while 30 days of signals would grow to ~1.7 GB. VACUUM runs
        when the last one is older than vacuum_interval_days, or when free
        pages exceed vacuum_freelist_ratio of the file. Returns a
        dict of deleted counts plus 'vacuumed' (bool)."""
        days = settings.get("days", 0)
        result = {"vacuumed": False}
        if not days or days <= 0:
            return result
        per_table = settings.get("table_days") or {}
        self.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        with self.conn:
            for table, column in self.RETAINED:
                window = per_table.get(table) or days
                cutoff = now - window * 86400
                result[table] = self.execute("DELETE FROM {} WHERE {} < ?".format(table, column), (cutoff,)).rowcount
        last = self.execute("SELECT value FROM meta WHERE key='last_vacuum'").fetchone()
        if last is None:  # first run with retention: start the interval clock now
            self.execute("INSERT INTO meta VALUES ('last_vacuum', ?)", (str(now),))
            self.conn.commit()
            last_ts = now
        else:
            last_ts = float(last[0])
        pages = self.execute("PRAGMA page_count").fetchone()[0] or 1
        free = self.execute("PRAGMA freelist_count").fetchone()[0]
        interval = settings.get("vacuum_interval_days", 7) * 86400
        ratio = settings.get("vacuum_freelist_ratio", 0.25)
        if free and (now - last_ts >= interval or free / pages >= ratio):
            self.conn.commit()
            self.execute("VACUUM")
            self.execute("INSERT INTO meta VALUES ('last_vacuum', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(now),))
            self.conn.commit()
            result["vacuumed"] = True
        return result

    def close(self):
        if self.conn is None:
            return
        self.conn.commit()
        self.conn.close()
        self.conn = None

    def execute(self, sql, args=()):
        return self.conn.execute(sql, args)

    def source(self, name):
        return self.execute("SELECT * FROM sources WHERE name=?", (name,)).fetchone()

    def source_status(self, name, status):
        self.execute("INSERT INTO sources(name,last_status) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET last_status=excluded.last_status", (name, status))
        self.conn.commit()

    def record_source(self, name, now, status, settings):
        old = self.source(name)
        failures = (old["consecutive_failures"] if old else 0) + 1 if status in ("error", "partial") else 0
        threshold = max(1, settings["failure_threshold"])
        cooldown = now + min(settings["max_cooldown_s"], settings["cooldown_s"] * 2 ** min(20, failures - threshold)) if failures >= threshold else 0
        self.execute("INSERT INTO sources VALUES (?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET last_fetch=excluded.last_fetch,last_status=excluded.last_status,consecutive_failures=excluded.consecutive_failures,cooldown_until=excluded.cooldown_until", (name, now, status, failures, cooldown))
        self.conn.commit()

    def canonical(self, url_hash, title_hash, title_fallback=None):
        row = self.execute("SELECT item_hash FROM aliases WHERE hash=?", (url_hash,)).fetchone()
        if row:
            return row[0]
        row = self.item(url_hash)
        if row:
            return row["hash"]
        row = self.execute("SELECT hash FROM items WHERE title_hash=? ORDER BY first_seen LIMIT 1", (title_hash,)).fetchone()
        return row[0] if row else title_fallback or url_hash

    def item(self, hash_):
        return self.execute("SELECT * FROM items WHERE hash=?", (hash_,)).fetchone()

    def previous(self, hash_, platform):
        return self.execute("SELECT * FROM scores WHERE hash=? AND platform=? ORDER BY ts DESC LIMIT 1", (hash_, platform)).fetchone()

    def cache(self, hash_):
        row = self.execute("SELECT result_json FROM llm_cache WHERE hash=?", (hash_,)).fetchone()
        return json.loads(row[0]) if row else None

    def cache_result(self, hash_, model, result, now):
        # Versioned key: new rows carry cache_version; pre-R1 rows (NULL) are left untouched.
        self.execute("INSERT OR IGNORE INTO llm_cache(hash,model,result_json,ts,cache_version) VALUES (?,?,?,?,?)",
                     (hash_, model, dumps(result), now, CLASSIFY_CACHE_VERSION))
        self.conn.commit()

    def budget(self, now):
        row = self.execute("SELECT * FROM budget WHERE day=?", (day_at(now),)).fetchone()
        return dict(row) if row else {"day": day_at(now), "calls": 0, "input_tokens": 0, "draft_calls": 0}

    def shadow_usage(self, now):
        """Calls/tokens held or consumed by R1 workload reservations on this UTC day."""
        if not self.has_table("llm_reservations"):
            return 0, 0
        row = self.execute("SELECT COALESCE(SUM(calls),0), COALESCE(SUM(COALESCE(actual_tokens, est_tokens)),0) FROM llm_reservations WHERE day=? AND state IN ('held','consumed')", (day_at(now),)).fetchone()
        return row[0], row[1]

    def kind_calls(self, now, kind):
        start = int(now // 86400) * 86400
        return self.execute("SELECT COUNT(*) FROM llm_calls WHERE kind=? AND ts>=? AND ts<?", (kind, start, start + 86400)).fetchone()[0]

    def budget_reason(self, cfg, now, kind=None):
        """Global UTC-day budget (legacy + reserved R1 usage), then the per-kind cap."""
        budget = self.budget(now)
        shadow_calls, shadow_tokens = self.shadow_usage(now)
        if budget["calls"] + shadow_calls >= cfg["budget"]["max_calls_per_day"]:
            return "daily call budget exhausted"
        if budget["input_tokens"] + shadow_tokens >= cfg["budget"]["max_input_tokens_per_day"]:
            return "daily token budget exhausted"
        if kind is not None:
            limit = cfg["budget"].get("kinds", {}).get(kind, {}).get("max_calls_per_day")
            if limit is not None and self.kind_calls(now, kind) >= limit:
                return "daily {} call budget exhausted".format(kind)
        return None

    def charge_call(self, now, draft=False):
        self.execute("INSERT INTO budget(day,calls,draft_calls) VALUES (?,1,?) ON CONFLICT(day) DO UPDATE SET calls=calls+1,draft_calls=draft_calls+excluded.draft_calls", (day_at(now), int(draft)))
        self.execute("INSERT INTO llm_calls VALUES (?,?)", (now, "draft" if draft else "classify"))
        self.conn.commit()

    def charge_tokens(self, now, tokens):
        self.execute("UPDATE budget SET input_tokens=input_tokens+? WHERE day=?", (tokens, day_at(now)))
        self.conn.commit()

    def last_call(self, kind=None):
        """Most recent Jev call; with ``kind`` only that kind gates (draft no longer delays classify)."""
        if kind is None:
            return self.execute("SELECT MAX(ts) FROM llm_calls").fetchone()[0]
        return self.execute("SELECT MAX(ts) FROM llm_calls WHERE kind=?", (kind,)).fetchone()[0]

    def mark_candidates(self, items, now):
        for item in items:
            self.execute("UPDATE items SET candidate_at=COALESCE(candidate_at,?) WHERE hash=?", (now, item["hash"]))
            self.execute("INSERT OR IGNORE INTO pending VALUES (?,?,?)", (item["hash"], dumps(item), now))
        self.conn.commit()

    def pending(self):
        return [json.loads(r[0]) for r in self.execute("SELECT p.data_json FROM pending p JOIN items i ON i.hash=p.hash WHERE i.llm_at IS NULL")]

    def mark_sent(self, items, now):
        for item in items:
            self.execute("UPDATE items SET llm_at=? WHERE hash=?", (now, item["hash"]))
        self.conn.commit()
