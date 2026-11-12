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

    # Time-series tables pruned on every run: (table, timestamp column).
    RETAINED = (("signals", "ts"), ("scores", "ts"), ("runs", "ts"), ("llm_calls", "ts"))
    # Sources whose observations feed the R1 engine are never pruned.
    PROTECTED_SOURCES = ("sec_edgar_8k",)
    # items rows that dedupe or pending work still needs are never pruned:
    # anything that ever became a candidate or was sent to Jev, and anything
    # with a pending, draft or classify-cache row.
    ITEM_KEEP = ("candidate_at IS NULL AND llm_at IS NULL"
                 " AND hash NOT IN (SELECT hash FROM pending)"
                 " AND hash NOT IN (SELECT hash FROM drafts)"
                 " AND hash NOT IN (SELECT hash FROM llm_cache)")

    def _window(self, settings, table):
        """Retention window in days for ``table``; 0 means never prune it."""
        per_table = settings.get("table_days") or {}
        return per_table[table] if table in per_table else settings.get("days", 0)

    def _plans(self, now, settings, deep):
        """(label, table, where, args, guard_trigger) for every table due this pass."""
        plans = []
        for table, column in self.RETAINED:
            days = self._window(settings, table)
            if days and days > 0:
                plans.append((table, table, "{} < ?".format(column), (now - days * 86400,), None))
        if not deep:
            return plans
        days = self._window(settings, "items")
        if days and days > 0:
            plans.append(("items", "items", "last_seen < ? AND " + self.ITEM_KEEP, (now - days * 86400,), None))
            plans.append(("aliases", "aliases", "item_hash NOT IN (SELECT hash FROM items)", (), None))
        days = self._window(settings, "observations")
        if days and days > 0 and self.has_table("observations"):
            cutoff = datetime.fromtimestamp(int(now - days * 86400), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            marks = ",".join("?" * len(self.PROTECTED_SOURCES))
            # Only an observation that is old AND either belongs to a source item
            # unseen for the whole window or is superseded by a newer payload of
            # the same item; the newest payload of a live item stays so refetch
            # dedupe keeps working. Rows any R1 table references are kept.
            where = ("fetched_at < ? AND source_item_id IN (SELECT id FROM source_items WHERE source NOT IN ({m}))"
                     " AND (source_item_id IN (SELECT id FROM source_items WHERE last_seen_at < ?)"
                     " OR EXISTS (SELECT 1 FROM observations n WHERE n.source_item_id=observations.source_item_id AND n.id>observations.id))"
                     ).format(m=marks)
            for ref, column, extra in (("event_revisions", "observation_id", ""),
                                       ("observation_resolutions", "observation_id", ""),
                                       ("dead_letters", "subject_id", " WHERE subject_kind='observation'")):
                if self.has_table(ref):
                    where += " AND id NOT IN (SELECT {} FROM {}{})".format(column, ref, extra)
            plans.append(("observations", "observations", where, (cutoff,) + self.PROTECTED_SOURCES + (cutoff,), "observations_no_delete"))
            where = ("last_seen_at < ? AND source NOT IN ({m})"
                     " AND NOT EXISTS (SELECT 1 FROM observations o WHERE o.source_item_id=source_items.id)").format(m=marks)
            plans.append(("source_items", "source_items", where, (cutoff,) + self.PROTECTED_SOURCES, None))
        return plans

    def _delete_batched(self, table, where, args, batch, guard_trigger=None):
        """Delete matching rows ``batch`` rowids per short IMMEDIATE transaction.

        ``guard_trigger`` names an immutability trigger that is dropped and
        recreated verbatim inside each transaction, so no other connection
        ever sees the table unguarded."""
        guard = None
        if guard_trigger:
            row = self.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (guard_trigger,)).fetchone()
            guard = row[0] if row else None
        select = "SELECT rowid FROM {t} WHERE {w} LIMIT ?".format(t=table, w=where)
        total = 0
        self.conn.commit()
        while True:
            self.execute("BEGIN IMMEDIATE")
            try:
                ids = [r[0] for r in self.execute(select, args + (batch,))]
                if ids:
                    if guard:
                        self.execute("DROP TRIGGER " + guard_trigger)
                    marks = ",".join("?" * len(ids))
                    total += self.execute("DELETE FROM {} WHERE rowid IN ({})".format(table, marks), ids).rowcount
                    if guard:
                        self.execute(guard)
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise
            if len(ids) < batch:
                return total

    def count_prunable(self, now, settings):
        """Dry run: rows a forced (deep) prune would delete now, per table."""
        result = {}
        for label, table, where, args, _ in self._plans(now, settings, deep=True):
            if label == "aliases":
                continue  # depends on the items rows the same pass deletes first
            result[label] = self.execute("SELECT COUNT(*) FROM {} WHERE {}".format(table, where), args).fetchone()[0]
        return result

    def size(self):
        """Database size: file and WAL bytes, plus page_count*page_size and free pages."""
        page = self.execute("PRAGMA page_size").fetchone()[0]
        pages = self.execute("PRAGMA page_count").fetchone()[0]
        free = self.execute("PRAGMA freelist_count").fetchone()[0]
        wal = self.path.with_name(self.path.name + "-wal")
        return {"file_bytes": self.path.stat().st_size if self.path.exists() else 0,
                "wal_bytes": wal.stat().st_size if wal.exists() else 0,
                "page_bytes": pages * page, "free_bytes": free * page, "page_count": pages, "freelist_count": free}

    def _meta(self, key, value=None):
        self.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        if value is None:
            row = self.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return float(row[0]) if row else None
        self.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
        self.conn.commit()
        return value

    def _disk_allows_vacuum(self, size, settings):
        """A full VACUUM writes a temp copy plus (in WAL mode) the WAL: ~2x live data."""
        import shutil
        live = size["page_bytes"] - size["free_bytes"]
        need = live * settings.get("vacuum_disk_factor", 2.2) + 512 * 1024 * 1024
        return shutil.disk_usage(str(self.path.parent)).free >= need

    def prune(self, now, settings, force=False):
        """Retention pass: batched deletes, then space reclamation.

        Every run: the time-series tables (signals, scores, runs, llm_calls)
        by their ``ts``. Once per ``deep_interval_h`` (or ``force``): items no
        dedupe needs, orphaned aliases, and legacy observations/source_items.
        ``days`` is the default window and ``table_days`` overrides it per
        table (0 = never prune that table). signals keep 2 days because
        scoring only reads the last cooccurrence_h hours of them.

        Space: the first due VACUUM also switches the file to
        auto_vacuum=INCREMENTAL (only a VACUUM can); after that each deep pass
        ends with batched ``incremental_vacuum`` and a TRUNCATE checkpoint, so
        the periodic full rewrite is no longer needed. A VACUUM is skipped
        when free disk is below ~2.2x the live data. Returns deleted counts
        per table plus size_before/size_after and what reclamation ran."""
        days = settings.get("days", 0)
        result = {"vacuumed": False}
        if not days or days <= 0:
            return result
        batch = max(1, int(settings.get("batch_rows", 5000)))
        result["size_before"] = self.size()
        last_deep = self._meta("last_deep_prune")
        deep = force or last_deep is None or now - last_deep >= settings.get("deep_interval_h", 24) * 3600
        for label, table, where, args, guard in self._plans(now, settings, deep):
            result[label] = self._delete_batched(table, where, args, batch, guard)
        if deep:
            self._meta("last_deep_prune", now)
            result["deep"] = True
        last = self._meta("last_vacuum")
        if last is None:  # first run with retention: start the interval clock now
            last = self._meta("last_vacuum", now)
        size = self.size()
        pages = size["page_count"] or 1
        free = size["freelist_count"]
        incremental = self.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
        ratio = settings.get("vacuum_freelist_ratio", 0.25)
        if incremental:
            if free and (deep or free / pages >= ratio):
                step = max(1, int(settings.get("incremental_vacuum_pages", 20000)))
                freed = 0
                while True:
                    before = self.execute("PRAGMA freelist_count").fetchone()[0]
                    if not before:
                        break
                    self.execute("PRAGMA incremental_vacuum({})".format(step)).fetchall()
                    self.conn.commit()
                    after = self.execute("PRAGMA freelist_count").fetchone()[0]
                    freed += before - after
                    if after >= before:
                        break
                result["incremental_vacuum_pages"] = freed
        elif free and (force or now - last >= settings.get("vacuum_interval_days", 7) * 86400 or free / pages >= ratio):
            if self._disk_allows_vacuum(size, settings):
                self.conn.commit()
                self.execute("PRAGMA auto_vacuum=INCREMENTAL")
                self.execute("VACUUM")
                self._meta("last_vacuum", now)
                result["vacuumed"] = True
            else:
                result["vacuum_skipped"] = "low disk"
        if result["vacuumed"] or result.get("incremental_vacuum_pages"):
            try:
                result["checkpoint"] = list(self.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
            except sqlite3.Error as exc:
                result["checkpoint"] = str(exc)
        result["size_after"] = self.size()
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
