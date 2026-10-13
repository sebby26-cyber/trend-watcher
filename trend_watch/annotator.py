"""Paid Jev attempts for R1 annotations: reservation, attempt state machine, outage episodes.

Attempt states (spec fix 7)::

    reserved -> sending -> response_received -> validated
                                             -> ambiguous   (malformed / invalid answer)
             -> sending -> ambiguous                        (timeout, reset, 5xx after send)
             -> not_sent                                    (refused, DNS, 429/502/503/529, 401/403, crash before send)

* Budget reservation and the ``reserved`` attempt row are written in one
  ``BEGIN IMMEDIATE`` transaction (atomic). ``sending`` is committed before any
  byte leaves the process.
* Budget day is the UTC calendar day. A reservation is ``held`` until the
  outcome is known, then ``consumed`` (plausibly sent; tokens reconciled to the
  reported usage, else the estimate) or ``released`` (provably not sent).
* Crash recovery (start of every annotate run): ``reserved`` -> ``not_sent``
  and the reservation is released (no bytes can have been sent);
  ``sending``/``response_received`` -> ``ambiguous`` and the reservation is
  consumed at its estimate (it may have been billed).
* Only plausibly-sent attempts count toward ``max_ambiguous_attempts`` (2).
  After that the annotation is written as ``unavailable`` and the consumer
  decides REVIEW with reason ``annotation_unavailable`` -- never a positive
  materiality result.
* Priority/fairness: the legacy trend workload outranks the shadow workload.
  A shadow reservation succeeds only if it leaves ``budget.legacy_reserve_calls``
  of the global daily call budget untouched and fits both global and workload
  token budgets. Within the shadow workload, needs are served FIFO by event.
* No fallback (owner decision, permissive): on Jev outage, missing key or Jev
  disabled, the step becomes ``blocked_dependency``; one probe per run checks
  for recovery of the *same* backend; the episode is reported once. No other
  model or hand-written stand-in is ever substituted.
"""
import socket
import urllib.error
from .http import HTTPContext
from .config import merge
from .ids import iso, next_utc_midnight, uid, utc_day
from .jev import JevClient
from .serialize import canonical

BACKEND = "jev"
WORKLOAD = "materiality"


def default_client_factory(cfg, db, key, now):
    context = HTTPContext(merge(cfg["http"], {"timeout_s": cfg["llm"]["timeout_s"], "retries": 0}))
    return JevClient(cfg, db, key, now, context=context)


def classify_error(exc):
    """Returns (state, error_code, outage)."""
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (429, 502, 503, 529):
            return "not_sent", "http_{}".format(exc.code), True
        if exc.code in (401, 403):
            return "not_sent", "auth_{}".format(exc.code), True
        return "ambiguous", "http_{}".format(exc.code), False
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "ambiguous", "timeout", False
    if isinstance(exc, urllib.error.URLError):
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            return "ambiguous", "timeout", False
        if isinstance(exc.reason, (ConnectionResetError, BrokenPipeError)):
            return "ambiguous", "connection_reset", False
        return "not_sent", "connection_failed", True
    if isinstance(exc, (ConnectionRefusedError, socket.gaierror)):
        return "not_sent", "connection_failed", True
    if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
        return "ambiguous", "connection_reset", False
    if isinstance(exc, ValueError):
        return "ambiguous", "malformed_response", False
    return "ambiguous", type(exc).__name__, False


class Annotator:
    def __init__(self, db, cfg, now, namespace, key_provider, client_factory=None, report=None):
        self.db, self.cfg, self.now, self.namespace = db, cfg, now, namespace
        self.key_provider = key_provider
        self.client_factory = client_factory or default_client_factory
        self.report = report or (lambda line: None)
        self.max_ambiguous = cfg["investment"]["materiality"]["max_ambiguous_attempts"]
        self.est_tokens = cfg["investment"]["materiality"]["estimated_input_tokens_per_call"]
        self.probed = False
        self.client = None
        self.counts = {"validated": 0, "ambiguous": 0, "not_sent": 0, "unavailable": 0,
                       "blocked_dependency": 0, "deferred_budget": 0, "deferred_interval": 0, "recovered": 0}

    # ------------------------------------------------------------- bookkeeping
    def queue(self, producer, version, input_hash, status, reason=None, eligible_at=None):
        self.db.execute("""INSERT INTO annotation_queue VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(producer,producer_version,input_hash,namespace) DO UPDATE SET
            status=excluded.status, reason=excluded.reason, eligible_at=excluded.eligible_at, updated_at=excluded.updated_at""",
                        (producer, version, input_hash, self.namespace, status, reason, eligible_at, iso(self.now)))

    def annotation(self, producer, version, input_hash):
        return self.db.execute("SELECT * FROM annotations WHERE producer=? AND producer_version=? AND input_hash=?",
                               (producer, version, input_hash)).fetchone()

    def write_annotation(self, producer, version, input_hash, status, value):
        self.db.execute("INSERT OR IGNORE INTO annotations(uid,producer,producer_version,input_hash,status,value_json,created_at) VALUES (?,?,?,?,?,?,?)",
                        (uid("ann", producer, version, input_hash), producer, version, input_hash, status, canonical(value), iso(self.now)))
        self.queue(producer, version, input_hash, "done", status)

    def ambiguous_count(self, producer, version, input_hash):
        return self.db.execute("SELECT COUNT(*) FROM annotation_attempts WHERE producer=? AND producer_version=? AND input_hash=? AND state='ambiguous'",
                               (producer, version, input_hash)).fetchone()[0]

    def set_attempt(self, attempt_id, state, error=None):
        self.db.execute("UPDATE annotation_attempts SET state=?, error_code=COALESCE(?, error_code), updated_at=? WHERE id=?",
                        (state, error, iso(self.now), attempt_id))

    def settle(self, reservation_id, state, tokens=None):
        self.db.execute("UPDATE llm_reservations SET state=?, actual_tokens=COALESCE(?, actual_tokens), updated_at=? WHERE id=?",
                        (state, tokens, iso(self.now), reservation_id))

    def open_episode(self, reason):
        row = self.db.execute("SELECT * FROM dependency_episodes WHERE backend=? AND closed_at IS NULL ORDER BY id DESC LIMIT 1", (BACKEND,)).fetchone()
        if row is None:
            cursor = self.db.execute("INSERT INTO dependency_episodes(backend,reason,opened_at,probes) VALUES (?,?,?,0)", (BACKEND, reason, iso(self.now)))
            row = self.db.execute("SELECT * FROM dependency_episodes WHERE id=?", (cursor.lastrowid,)).fetchone()
        self.probed = True  # the failing call (or config check) counts as this run's probe
        if row["reported_at"] is None:
            self.report("BLOCKED: {} unavailable ({}) since {}; LLM-dependent steps wait as blocked_dependency, deterministic stages continue; no substitute model is used (reported once per episode #{})".format(
                BACKEND, row["reason"], row["opened_at"], row["id"]))
            self.db.execute("UPDATE dependency_episodes SET reported_at=? WHERE id=?", (iso(self.now), row["id"]))
        return row

    def current_episode(self):
        return self.db.execute("SELECT * FROM dependency_episodes WHERE backend=? AND closed_at IS NULL ORDER BY id DESC LIMIT 1", (BACKEND,)).fetchone()

    def close_episode(self):
        self.db.execute("UPDATE dependency_episodes SET closed_at=? WHERE backend=? AND closed_at IS NULL", (iso(self.now), BACKEND))

    # ---------------------------------------------------------------- recovery
    def recover(self):
        """Resolve attempts left behind by a crash (see module docstring)."""
        with self.db.conn:
            for row in self.db.execute("SELECT * FROM annotation_attempts WHERE state IN ('reserved','sending','response_received')").fetchall():
                if row["state"] == "reserved":
                    self.set_attempt(row["id"], "not_sent", "crash_before_send")
                    if row["reservation_id"]:
                        self.settle(row["reservation_id"], "released")
                else:
                    self.set_attempt(row["id"], "ambiguous", "crash_after_send")
                    if row["reservation_id"]:
                        self.settle(row["reservation_id"], "consumed")
                self.counts["recovered"] += 1

    # ------------------------------------------------------------------ budget
    def reserve(self, producer, version, input_hash):
        """Atomically reserve budget and create the ``reserved`` attempt; returns (attempt_id, reservation_id) or a reason."""
        day = utc_day(self.now)
        conn = self.db.conn
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            budget = self.db.budget(self.now)
            shadow_calls, shadow_tokens = self.db.shadow_usage(self.now)
            global_cfg = self.cfg["budget"]
            kind_cfg = global_cfg["kinds"][WORKLOAD]
            work_calls, work_tokens = conn.execute(
                "SELECT COALESCE(SUM(calls),0), COALESCE(SUM(COALESCE(actual_tokens, est_tokens)),0) FROM llm_reservations WHERE day=? AND workload=? AND state IN ('held','consumed')",
                (day, WORKLOAD)).fetchone()
            reason = None
            if budget["calls"] + shadow_calls + 1 > global_cfg["max_calls_per_day"] - global_cfg["legacy_reserve_calls"]:
                reason = "global call budget (legacy reserve protected)"
            elif budget["input_tokens"] + shadow_tokens + self.est_tokens > global_cfg["max_input_tokens_per_day"]:
                reason = "global token budget"
            elif work_calls + 1 > kind_cfg["max_calls_per_day"]:
                reason = "materiality call budget"
            elif work_tokens + self.est_tokens > kind_cfg.get("max_input_tokens_per_day", global_cfg["max_input_tokens_per_day"]):
                reason = "materiality token budget"
            if reason:
                conn.rollback()
                return None, None, reason
            at = iso(self.now)
            reservation = conn.execute("INSERT INTO llm_reservations(day,workload,namespace,calls,est_tokens,state,created_at,updated_at) VALUES (?,?,?,1,?,'held',?,?)",
                                       (day, WORKLOAD, self.namespace, self.est_tokens, at, at)).lastrowid
            number = conn.execute("SELECT COALESCE(MAX(attempt_no),0)+1 FROM annotation_attempts WHERE producer=? AND producer_version=? AND input_hash=?",
                                  (producer, version, input_hash)).fetchone()[0]
            attempt = conn.execute("INSERT INTO annotation_attempts(uid,producer,producer_version,input_hash,namespace,attempt_no,state,reservation_id,created_at,updated_at) VALUES (?,?,?,?,?,?,'reserved',?,?,?)",
                                   (uid("attempt", producer, version, input_hash, number), producer, version, input_hash,
                                    self.namespace, number, reservation, at, at)).lastrowid
            conn.commit()
            return attempt, reservation, None
        except Exception:
            conn.rollback()
            raise

    # -------------------------------------------------------------------- run
    def run(self, producer, version, input_hash, request, parse):
        """Advance one annotation need by at most one paid attempt. Returns the resulting status."""
        if self.annotation(producer, version, input_hash):
            return "done"
        with self.db.conn:
            if self.ambiguous_count(producer, version, input_hash) >= self.max_ambiguous:
                self.write_annotation(producer, version, input_hash, "unavailable",
                                      {"reason": "max_ambiguous_attempts", "attempts": self.max_ambiguous})
                self.counts["unavailable"] += 1
                return "unavailable"
            blocked = None
            if not self.cfg["llm"].get("enabled", True):
                blocked = "jev disabled in config (llm.enabled=false)"
            key = None if blocked else self.key_provider(self.cfg)
            if not blocked and not key:
                blocked = "no API key"
            episode = self.current_episode()
            if blocked:
                self.open_episode(blocked)
                return self.block(producer, version, input_hash, blocked)
            if episode is not None and self.probed:
                return self.block(producer, version, input_hash, episode["reason"])
            gap = self.cfg["llm"].get("kind_min_interval_s", {}).get(WORKLOAD, 0)
            last = self.db.last_call(WORKLOAD)
            if last is not None and self.now - last < gap:
                self.queue(producer, version, input_hash, "pending", "min_interval")
                self.counts["deferred_interval"] += 1
                return "pending"
        attempt, reservation, reason = self.reserve(producer, version, input_hash)
        if reason:
            with self.db.conn:
                self.queue(producer, version, input_hash, "deferred_budget", reason, iso(next_utc_midnight(self.now)))
            self.counts["deferred_budget"] += 1
            return "deferred_budget"
        if episode is not None:
            self.probed = True
            self.db.execute("UPDATE dependency_episodes SET probes=probes+1 WHERE id=?", (episode["id"],))
        with self.db.conn:
            self.set_attempt(attempt, "sending")
            self.db.execute("INSERT INTO llm_calls VALUES (?,?)", (self.now, WORKLOAD))
        try:
            if self.client is None:
                self.client = self.client_factory(self.cfg, self.db, key, self.now)
            response = self.client.send(request)
        except Exception as exc:
            state, code, outage = classify_error(exc)
            with self.db.conn:
                self.set_attempt(attempt, state, code)
                self.settle(reservation, "released" if state == "not_sent" else "consumed")
                self.counts[state] += 1
                if outage:
                    self.open_episode(code)
                    return self.block(producer, version, input_hash, code)
                return self.after_ambiguous(producer, version, input_hash, code)
        tokens = response.get("usage", {}).get("input_tokens") if isinstance(response, dict) and isinstance(response.get("usage"), dict) else None
        tokens = tokens if type(tokens) is int and tokens >= 0 else None
        with self.db.conn:
            self.set_attempt(attempt, "response_received")
            self.settle(reservation, "consumed", tokens)
            self.close_episode()
        try:
            value = parse(request, response)
        except (ValueError, KeyError, TypeError) as exc:
            with self.db.conn:
                self.set_attempt(attempt, "ambiguous", "malformed_response")
                self.counts["ambiguous"] += 1
                return self.after_ambiguous(producer, version, input_hash, "malformed_response")
        with self.db.conn:
            self.set_attempt(attempt, "validated")
            self.write_annotation(producer, version, input_hash, "validated", value)
            self.counts["validated"] += 1
        return "validated"

    def block(self, producer, version, input_hash, reason):
        self.queue(producer, version, input_hash, "blocked_dependency", reason)
        self.counts["blocked_dependency"] += 1
        return "blocked_dependency"

    def after_ambiguous(self, producer, version, input_hash, code):
        if self.ambiguous_count(producer, version, input_hash) >= self.max_ambiguous:
            self.write_annotation(producer, version, input_hash, "unavailable",
                                  {"reason": "max_ambiguous_attempts", "attempts": self.max_ambiguous, "last_error": code})
            self.counts["unavailable"] += 1
            return "unavailable"
        self.queue(producer, version, input_hash, "pending", code)
        return "pending"
