"""Staged intelligence engine: ingest -> resolve -> annotate -> consume <name> -> render.

Each stage is its own failure domain with its own transactions, recorded in
the ``stage_runs`` ledger (stage, version, scope, start/finish, status,
counts, error, prerequisite run). ``tick`` sequences them; the CLI holds the
global process lock around it. Prerequisites (spec fix 5):

* ingest  -- none. A failed ingest still lets later stages process work that
  was already committed.
* resolve -- none.
* annotate, consume -- the most recent ``resolve`` run must not have failed
  (unresolved observations must never be treated as processed).
* a failed annotate does not block consume: annotation-dependent evaluations
  simply stay ``awaiting_annotation``/``blocked_dependency``.
* consumers are independent: one failing consumer never blocks another.
* render -- none; a failed render leaves records for the next render.

The legacy trend path (``run``) is not part of tick and is unchanged except
for the P0 fixes.
"""
import json
import statistics
import sys
import time
from .annotator import Annotator
from .consumers import REGISTRY, namespace
from .ids import from_iso, iso, next_utc_midnight, payload_hash, uid, utc_day
from .intake import sec_edgar
from .jev import api_key
from .observations import record as observe
from .records import create as create_record, render as render_records
from .safexml import XMLRejected
from .serialize import canonical
from .watchlist import Watchlist, WatchlistError, parse_cik

STAGE_VERSIONS = {"ingest": "ingest/1", "resolve": "resolve/1", "annotate": "annotate/1",
                  "consume": "consume/1", "render": "render/1"}
NEEDS_RESOLVE = ("annotate", "consume")


class Skip(Exception):
    """A stage chose not to run (disabled, rate floor, prerequisite)."""


class Context:
    def __init__(self, paths, cfg, now=None, opener=None, client_factory=None, key_provider=api_key,
                 report=None, tick_uid=None, replay_batch=""):
        self.paths, self.cfg = paths, cfg
        self.now = time.time() if now is None else now
        self.opener, self.client_factory, self.key_provider = opener, client_factory, key_provider
        self.report = report or (lambda line: print(line, file=sys.stderr))
        self.tick_uid = tick_uid
        self.replay_batch = replay_batch


# ------------------------------------------------------------------ ledger
def latest_run(db, stage, scope=""):
    return db.execute("SELECT * FROM stage_runs WHERE stage=? AND scope=? AND status!='running' ORDER BY id DESC LIMIT 1", (stage, scope)).fetchone()


def run_stage(db, ctx, stage, fn, scope=""):
    """Run one stage in its own failure domain. Returns (status, counts)."""
    if db.conn.in_transaction:
        db.conn.commit()
    prerequisite = None
    if stage in NEEDS_RESOLVE:
        prerequisite = latest_run(db, "resolve")
    started = iso(ctx.now)
    run_uid = uid("stage", stage, scope, started, ctx.tick_uid or "", db.execute("SELECT COALESCE(MAX(id),0)+1 FROM stage_runs").fetchone()[0])
    with db.conn:
        run_id = db.execute("INSERT INTO stage_runs(uid,tick_uid,stage,scope,stage_version,started_at,status,prerequisite_run_id) VALUES (?,?,?,?,?,?,'running',?)",
                            (run_uid, ctx.tick_uid, stage, scope, STAGE_VERSIONS[stage], started, prerequisite["id"] if prerequisite else None)).lastrowid
    counts, error = {}, None
    try:
        if prerequisite is not None and prerequisite["status"] == "failed":
            raise Skip("prerequisite resolve run {} failed".format(prerequisite["uid"]))
        counts = fn(db, ctx) or {}
        status = "partial" if counts.get("errors") else "success"
    except Skip as exc:
        db.conn.rollback()
        status, error = "skipped", str(exc)
        counts = {"reason": error}
    except Exception as exc:
        db.conn.rollback()
        status, error = "failed", "{}: {}".format(type(exc).__name__, exc)
        counts = {"error": error}
    with db.conn:
        db.execute("UPDATE stage_runs SET finished_at=?, status=?, counts_json=?, error=? WHERE id=?",
                   (iso(ctx.now), status, canonical(counts), error, run_id))
    return status, counts


# ------------------------------------------------------------------ ingest
def ingest(db, ctx):
    settings = ctx.cfg["intake"]["sec_edgar"]
    if not settings["enabled"] and ctx.opener is None:
        raise Skip("intake.sec_edgar.enabled is false")
    source = sec_edgar.SOURCE
    previous = db.source(source)
    if previous and previous["cooldown_until"] and previous["cooldown_until"] > ctx.now:
        raise Skip("cooldown until " + iso(previous["cooldown_until"]))
    if previous and previous["last_fetch"] is not None and ctx.now - previous["last_fetch"] < settings["min_interval_s"]:
        raise Skip("rate floor (min_interval_s)")
    url = settings["feed_url"]
    row = db.execute("SELECT * FROM fetch_checkpoints WHERE source=? AND url=?", (source, url)).fetchone()
    checkpoint = dict(row) if row else None
    try:
        result = sec_edgar.fetch(settings, checkpoint, opener=ctx.opener)
    except sec_edgar.ConfigError:
        raise
    except (sec_edgar.FeedError, XMLRejected, OSError, ValueError) as exc:
        db.record_source(source, ctx.now, "error", settings)
        raise sec_edgar.FeedError("{}: {}".format(type(exc).__name__, exc))
    fetch_uid = uid("fetch", source, iso(ctx.now))
    created = refetched = 0
    # Observations and the conditional-fetch validators commit in ONE transaction:
    # a crash can never advance the ETag/Last-Modified checkpoint past uncommitted observations.
    with db.conn:
        for payload in result.entries:
            _, _, new = observe(db, source, sec_edgar.external_id(payload), payload, ctx.now, None, fetch_uid)
            created += int(new)
            refetched += int(not new)
        db.execute("""INSERT INTO fetch_checkpoints(source,url,etag,last_modified,last_status,last_fetch_at,updated_at) VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(source,url) DO UPDATE SET etag=excluded.etag, last_modified=excluded.last_modified,
            last_status=excluded.last_status, last_fetch_at=excluded.last_fetch_at, updated_at=excluded.updated_at""",
                   (source, url, result.etag, result.last_modified, result.http_status, iso(ctx.now), iso(ctx.now)))
    db.record_source(source, ctx.now, "ok", settings)
    return {"entries": len(result.entries), "new_observations": created, "identical_refetches": refetched,
            "not_modified": int(result.status == "not_modified")}


# ----------------------------------------------------------------- resolve
class Malformed(ValueError):
    pass


def validate_payload(payload):
    """Strict source-field validation; anything else is terminal (dead letter)."""
    try:
        cik, cik_source = parse_cik(payload.get("cik"))
    except ValueError as exc:
        raise Malformed(str(exc))
    accession = payload.get("accession")
    if not isinstance(accession, str) or not sec_edgar.ACCESSION.match(accession):
        raise Malformed("malformed accession {!r}".format(accession))
    form = payload.get("form")
    if not isinstance(form, str) or not form.strip():
        raise Malformed("missing form type")
    items = payload.get("items")
    if not isinstance(items, list) or not all(isinstance(i, dict) and isinstance(i.get("number"), str) for i in items):
        raise Malformed("malformed item list")
    amends = payload.get("amends_accession")
    if amends is not None and (not isinstance(amends, str) or not sec_edgar.ACCESSION.match(amends)):
        raise Malformed("malformed amends_accession")
    numbers = sorted({i["number"] for i in items})
    labels = {i["number"]: (i.get("label") or "") for i in items}
    content = {"cik": cik, "accession": accession, "form": form.strip().upper(), "company": payload.get("company"),
               "filed_date": payload.get("filed_date"), "url": payload.get("url"),
               "items": [{"number": n, "label": labels[n]} for n in numbers]}
    if amends:
        content["amends_accession"] = amends
    return cik, cik_source, content


def latest_revision(db, event_id):
    return db.execute("SELECT * FROM event_revisions WHERE event_id=? ORDER BY revision_no DESC LIMIT 1", (event_id,)).fetchone()


def add_revision(db, event, content, observation_id, reason, now):
    digest = payload_hash(content)
    existing = db.execute("SELECT * FROM event_revisions WHERE event_id=? AND content_hash=?", (event["id"], digest)).fetchone()
    if existing:
        return existing, False
    number = db.execute("SELECT COALESCE(MAX(revision_no),0)+1 FROM event_revisions WHERE event_id=?", (event["id"],)).fetchone()[0]
    rev_id = db.execute("INSERT INTO event_revisions(uid,event_id,revision_no,observation_id,content_hash,content_json,reason,created_at) VALUES (?,?,?,?,?,?,?,?)",
                        (uid("rev", event["uid"], number, digest), event["id"], number, observation_id, digest, canonical(content), reason, iso(now))).lastrowid
    return db.execute("SELECT * FROM event_revisions WHERE id=?", (rev_id,)).fetchone(), True


def record_entities(db, revision, cik, content, watchlist):
    if watchlist.decisive(cik):
        db.execute("INSERT OR IGNORE INTO event_entities(revision_id,cik,match_kind,decisive,evidence,watchlist_version) VALUES (?,?,?,?,?,?)",
                   (revision["id"], cik, "cik", 1, "source CIK", watchlist.version))
    for match_cik, kind, evidence in watchlist.lexical(content.get("company") or ""):
        db.execute("INSERT OR IGNORE INTO event_entities(revision_id,cik,match_kind,decisive,evidence,watchlist_version) VALUES (?,?,?,?,?,?)",
                   (revision["id"], match_cik, kind, 0, evidence, watchlist.version))


def event_by(db, lane, cik, accession):
    return db.execute("SELECT * FROM events WHERE lane=? AND cik=? AND accession=?", (lane, cik, accession)).fetchone()


def link_amendment(db, amendment, target, observation_id, watchlist, now):
    """Deterministic late link: projection columns move NULL -> target; a new revision records it."""
    db.execute("UPDATE events SET amends_event_id=?, amendment_status='linked', amendment_basis='source:amends_accession' WHERE id=? AND amends_event_id IS NULL",
               (target["id"], amendment["id"]))
    amendment = db.execute("SELECT * FROM events WHERE id=?", (amendment["id"],)).fetchone()
    content = json.loads(latest_revision(db, amendment["id"])["content_json"])
    content["amends_event_uid"] = target["uid"]
    revision, created = add_revision(db, amendment, content, observation_id, "amendment_linked", now)
    if created:
        record_entities(db, revision, amendment["cik"], content, watchlist)


def resolve_one(db, row, watchlist, lane, now):
    payload = json.loads(row["payload_json"])
    try:
        cik, cik_source, content = validate_payload(payload)
    except Malformed as exc:
        db.execute("INSERT INTO observation_resolutions VALUES (?,?,?,?,?,?)", (row["id"], "malformed", None, None, str(exc), iso(now)))
        db.execute("INSERT OR IGNORE INTO dead_letters(stage,subject_kind,subject_id,error,created_at) VALUES ('resolve','observation',?,?,?)",
                   (row["id"], str(exc), iso(now)))
        return "malformed"
    event = event_by(db, lane, cik, content["accession"])
    created_event = event is None
    if created_event:
        if content["form"].endswith("/A"):
            target = event_by(db, lane, cik, content["amends_accession"]) if content.get("amends_accession") else None
            status = "linked" if target else "unresolved"
            basis = "source:amends_accession" if target else ("awaiting:amends_accession" if content.get("amends_accession") else "no deterministic source field")
        else:
            target, status, basis = None, "not_amendment", None
        if target:
            content["amends_event_uid"] = target["uid"]
        event_id = db.execute("INSERT INTO events(uid,lane,cik,cik_source,accession,form,company,filed_date,created_at,amends_event_id,amendment_status,amendment_basis) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                              (uid("event", lane, cik, content["accession"]), lane, cik, cik_source, content["accession"], content["form"],
                               content.get("company"), content.get("filed_date"), iso(now), target["id"] if target else None, status, basis)).lastrowid
        event = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    else:
        if content["form"] != event["form"]:
            # Identity is (CIK, accession); a form change on the same accession is recorded, not merged.
            content["form_changed_from"] = event["form"]
        if event["amends_event_id"]:
            target = db.execute("SELECT uid FROM events WHERE id=?", (event["amends_event_id"],)).fetchone()
            content["amends_event_uid"] = target[0]
        elif event["amendment_status"] == "unresolved" and content.get("amends_accession"):
            target = event_by(db, lane, cik, content["amends_accession"])
            if target:
                db.execute("UPDATE events SET amends_event_id=?, amendment_status='linked', amendment_basis='source:amends_accession' WHERE id=? AND amends_event_id IS NULL",
                           (target["id"], event["id"]))
                content["amends_event_uid"] = target["uid"]
    revision, created = add_revision(db, event, content, row["id"], "initial" if created_event else "payload_changed", now)
    if created:
        record_entities(db, revision, cik, content, watchlist)
    db.execute("INSERT INTO observation_resolutions VALUES (?,?,?,?,?,?)",
               (row["id"], "resolved" if created else "duplicate", event["id"], revision["id"], None, iso(now)))
    if created_event and not content["form"].endswith("/A"):
        # An original arriving after its amendment: link waiting amendments deterministically.
        for waiting in db.execute("SELECT * FROM events WHERE lane=? AND cik=? AND amendment_status='unresolved'", (lane, cik)).fetchall():
            waiting_content = json.loads(latest_revision(db, waiting["id"])["content_json"])
            if waiting_content.get("amends_accession") == content["accession"]:
                link_amendment(db, waiting, event, latest_revision(db, waiting["id"])["observation_id"], watchlist, now)
    return "resolved" if created else "duplicate"


def load_watchlist(ctx):
    return Watchlist.load(ctx.paths.config / ctx.cfg["investment"]["watchlist_path"])


def resolve(db, ctx):
    watchlist = load_watchlist(ctx)  # an invalid watchlist fails resolve, which blocks annotate/consume
    counts = {"resolved": 0, "duplicate": 0, "malformed": 0}
    rows = db.execute("""SELECT o.* FROM observations o JOIN source_items s ON s.id=o.source_item_id
                         LEFT JOIN observation_resolutions r ON r.observation_id=o.id
                         WHERE s.source=? AND r.observation_id IS NULL ORDER BY o.id""", (sec_edgar.SOURCE,)).fetchall()
    for row in rows:
        with db.conn:
            counts[resolve_one(db, row, watchlist, "investment", ctx.now)] += 1
    for name, module in REGISTRY.items():
        if ctx.cfg[name]["enabled"]:
            counts["admission:" + namespace(module, ctx.cfg)] = admit(db, ctx, module, watchlist)
    return counts


# --------------------------------------------------- intake lane + breaker
def daily_counts(db, lane, days, now):
    """New lane events per complete UTC day for the ``days`` days before today (history start bounded)."""
    first = db.execute("SELECT MIN(created_at) FROM events WHERE lane=?", (lane,)).fetchone()[0]
    if first is None:
        return []
    first_day = first[:10]
    today = int(now // 86400)
    result = []
    for offset in range(days, 0, -1):
        start = (today - offset) * 86400
        if utc_day(start) < first_day:
            continue
        result.append(db.execute("SELECT COUNT(*) FROM events WHERE lane=? AND created_at>=? AND created_at<?",
                                 (lane, iso(start), iso(start + 86400))).fetchone()[0])
    return result


def breaker(db, ctx, module, consumer, env):
    """Returns pause_until (epoch) if intake is paused, tripping the breaker if volume is abnormal.

    Observed = lane events created today (UTC) since the later of midnight and
    the last pause expiry. Baseline = median of the previous ``baseline_days``
    complete UTC days (zero days count; days before the first event do not);
    with fewer than ``min_history_days`` days only ``absolute_ceiling`` applies.
    Threshold = min(absolute_ceiling, max(min_trip_count, multiplier x median)).
    """
    cfg = ctx.cfg[module.NAME]["circuit_breaker"]
    last = db.execute("SELECT * FROM breaker_trips WHERE consumer=? AND env=? AND lane=? ORDER BY id DESC LIMIT 1", (consumer, env, module.LANE)).fetchone()
    if last and from_iso(last["pause_until"]) > ctx.now:
        return from_iso(last["pause_until"])
    since = int(ctx.now // 86400) * 86400
    if last and from_iso(last["pause_until"]) > since:
        since = from_iso(last["pause_until"])
    observed = db.execute("SELECT COUNT(*) FROM events WHERE lane=? AND created_at>=?", (module.LANE, iso(since))).fetchone()[0]
    history = daily_counts(db, module.LANE, cfg["baseline_days"], ctx.now)
    if len(history) >= cfg["min_history_days"]:
        median = statistics.median(history)
        threshold = min(cfg["absolute_ceiling"], max(cfg["min_trip_count"], int(cfg["multiplier"] * median)))
        baseline = "{:g}".format(median)
    else:
        threshold, baseline = cfg["absolute_ceiling"], None
    if observed <= threshold:
        return None
    pause_until = ctx.now + cfg["pause_s"]
    with db.conn:
        db.execute("INSERT INTO breaker_trips(consumer,env,lane,tripped_at,pause_until,observed,baseline,threshold,reason) VALUES (?,?,?,?,?,?,?,?,?)",
                   (consumer, env, module.LANE, iso(ctx.now), iso(pause_until), observed, baseline, threshold, "volume_anomaly"))
    ctx.report("CIRCUIT BREAKER: {}@{} intake paused until {} ({} new events today > threshold {}); filings stay ingested, processing is deferred".format(
        consumer, env, iso(pause_until), observed, threshold))
    return pause_until


def set_admission(db, consumer, env, event_id, status, reason, window, eligible_at, now):
    old = db.execute("SELECT status, eligible_at FROM intake_admissions WHERE consumer=? AND env=? AND event_id=?", (consumer, env, event_id)).fetchone()
    if old and (old["status"], old["eligible_at"]) == (status, eligible_at):
        return
    db.execute("""INSERT INTO intake_admissions VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(consumer,env,event_id) DO UPDATE SET
                  status=excluded.status, reason=excluded.reason, window_day=excluded.window_day, eligible_at=excluded.eligible_at, decided_at=excluded.decided_at""",
               (consumer, env, event_id, status, reason, window, eligible_at, iso(now)))
    db.execute("INSERT INTO intake_log(consumer,env,event_id,status,reason,window_day,eligible_at,at) VALUES (?,?,?,?,?,?,?,?)",
               (consumer, env, event_id, status, reason, window, eligible_at, iso(now)))


def admit(db, ctx, module, watchlist):
    """Capped primary intake lane. Only a decisive CIK match admits (lexical evidence never does).

    First sighting bypasses any trend/novelty score: a watchlisted primary-source
    filing is eligible on the tick it is first observed. Caps defer to the next
    UTC day; anomalies defer to the pause expiry. Nothing is dropped.
    """
    consumer, env = module.NAME, ctx.cfg[module.NAME]["env"]
    cap = ctx.cfg[module.NAME]["intake"]["max_admissions_per_day"]
    day = utc_day(ctx.now)
    paused = breaker(db, ctx, module, consumer, env)
    ciks = sorted(watchlist.by_cik)
    candidates = db.execute("""SELECT e.* FROM events e LEFT JOIN intake_admissions a
                               ON a.event_id=e.id AND a.consumer=? AND a.env=?
                               WHERE e.lane=? AND e.cik IN ({}) AND (a.event_id IS NULL OR (a.status!='admitted' AND a.eligible_at<=?))
                               ORDER BY e.filed_date, e.accession, e.cik, e.id""".format(",".join("?" * len(ciks))),
                            [consumer, env, module.LANE] + ciks + [iso(ctx.now)]).fetchall() if ciks else []
    admitted = db.execute("SELECT COUNT(DISTINCT event_id) FROM intake_log WHERE consumer=? AND env=? AND status='admitted' AND window_day=?",
                          (consumer, env, day)).fetchone()[0]
    counts = {"admitted": 0, "deferred_cap": 0, "deferred_anomaly": 0}
    with db.conn:
        for event in candidates:
            if paused:
                set_admission(db, consumer, env, event["id"], "deferred_anomaly", "volume_anomaly", day, iso(paused), ctx.now)
                counts["deferred_anomaly"] += 1
            elif admitted >= cap:
                set_admission(db, consumer, env, event["id"], "deferred_cap", "daily_cap", day, iso(next_utc_midnight(ctx.now)), ctx.now)
                counts["deferred_cap"] += 1
            else:
                set_admission(db, consumer, env, event["id"], "admitted", "watchlist_cik", day, None, ctx.now)
                admitted += 1
                counts["admitted"] += 1
    return counts


# ---------------------------------------------------------------- annotate
def admitted_events(db, consumer, env, watchlist):
    rows = db.execute("""SELECT e.* FROM events e JOIN intake_admissions a ON a.event_id=e.id
                         WHERE a.consumer=? AND a.env=? AND a.status='admitted' ORDER BY e.filed_date, e.accession, e.id""", (consumer, env)).fetchall()
    return [e for e in rows if watchlist.decisive(e["cik"])]


def annotate_for(module):
    def stage(db, ctx):
        watchlist = load_watchlist(ctx)
        ns = namespace(module, ctx.cfg)
        annotator = Annotator(db, ctx.cfg, ctx.now, ns, ctx.key_provider, ctx.client_factory, ctx.report)
        annotator.recover()
        version = module.producer_version(ctx.cfg)
        for event in admitted_events(db, module.NAME, ctx.cfg[module.NAME]["env"], watchlist):
            content = json.loads(latest_revision(db, event["id"])["content_json"])
            if module.rules(content)[0] != "needs_annotation":
                continue
            annotator.run(module.PRODUCER, version, module.annotation_hash(content), module.build_request(content, ctx.cfg), module.parse_response)
        return {k: v for k, v in annotator.counts.items() if v}
    return stage


# ----------------------------------------------------------------- consume
def last_attempt(db, request_id):
    return db.execute("SELECT * FROM evaluation_attempts WHERE request_id=? ORDER BY attempt_no DESC LIMIT 1", (request_id,)).fetchone()


def add_attempt(db, request_id, status, decision, reason, eligible_at, trace, now):
    last = last_attempt(db, request_id)
    if last and (last["processing_status"], last["decision"], last["reason_code"], last["eligible_at"]) == (status, decision, reason, eligible_at):
        return False
    number = (last["attempt_no"] if last else 0) + 1
    db.execute("INSERT INTO evaluation_attempts(request_id,attempt_no,processing_status,decision,reason_code,eligible_at,trace_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
               (request_id, number, status, decision, reason, eligible_at, canonical(trace), iso(now)))
    return True


def evaluate(db, ctx, module, event, watchlist):
    cfg = ctx.cfg
    consumer, env = module.NAME, cfg[module.NAME]["env"]
    revision = latest_revision(db, event["id"])
    content = json.loads(revision["content_json"])
    entities = [dict(kind=r["match_kind"], cik=r["cik"], evidence=r["evidence"]) for r in
                db.execute("SELECT * FROM event_entities WHERE revision_id=? AND decisive=0 ORDER BY id", (revision["id"],))]
    outcome, decision, reason, trace = module.rules(content)
    annotation = None
    if outcome == "needs_annotation":
        version = module.producer_version(cfg)
        row = db.execute("SELECT * FROM annotations WHERE producer=? AND producer_version=? AND input_hash=?",
                         (module.PRODUCER, version, module.annotation_hash(content))).fetchone()
        if row:
            annotation = {"uid": row["uid"], "status": row["status"], "producer_version": version, "value": json.loads(row["value_json"])}
    snapshot = module.snapshot(event, revision, content, watchlist, entities, annotation, cfg)
    input_hash = payload_hash(snapshot)
    request = db.execute("""SELECT * FROM evaluation_requests WHERE consumer=? AND env=? AND revision_id=? AND consumer_version=?
                            AND ruleset_version=? AND input_hash=? AND replay_batch_id=?""",
                         (consumer, env, revision["id"], module.VERSION, module.RULESET_VERSION, input_hash, ctx.replay_batch)).fetchone()
    if request is not None:
        last = last_attempt(db, request["id"])
        if last and last["processing_status"] in ("done", "dead_letter"):
            return "noop"  # explicit idempotent replay of an identical input
    else:
        prior = db.execute("SELECT COUNT(*) FROM evaluation_requests WHERE consumer=? AND env=? AND event_id=?", (consumer, env, event["id"])).fetchone()[0]
        trigger = "replay" if ctx.replay_batch else ("initial" if not prior else "input_changed")
        request_uid = uid("evalreq", consumer, env, revision["uid"], module.VERSION, module.RULESET_VERSION, input_hash, ctx.replay_batch)
        request_id = db.execute("""INSERT INTO evaluation_requests(uid,consumer,env,event_id,revision_id,consumer_version,ruleset_version,input_hash,
                                   replay_batch_id,trigger,requested_at,input_snapshot_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (request_uid, consumer, env, event["id"], revision["id"], module.VERSION, module.RULESET_VERSION,
                                 input_hash, ctx.replay_batch, trigger, iso(ctx.now), canonical(snapshot))).lastrowid
        request = db.execute("SELECT * FROM evaluation_requests WHERE id=?", (request_id,)).fetchone()
        # Older open requests for this event are superseded by the new input (their history is kept).
        for old in db.execute("SELECT id FROM evaluation_requests WHERE consumer=? AND env=? AND event_id=? AND id<?", (consumer, env, event["id"], request_id)).fetchall():
            last = last_attempt(db, old["id"])
            if last is None or last["processing_status"] not in ("done", "dead_letter", "superseded"):
                add_attempt(db, old["id"], "superseded", None, "newer_input", None, [], ctx.now)
    eligible = None
    if decision is None and annotation is not None:
        decision, reason, trace = module.with_annotation(annotation, trace)
    if decision is not None:
        status = "done"
    else:
        queue = db.execute("SELECT * FROM annotation_queue WHERE producer=? AND producer_version=? AND input_hash=? AND namespace=?",
                           (module.PRODUCER, module.producer_version(cfg), module.annotation_hash(content), namespace(module, cfg))).fetchone()
        if queue and queue["status"] in ("blocked_dependency", "deferred_budget"):
            status, reason, eligible = queue["status"], queue["reason"], queue["eligible_at"]
        else:
            status, reason = "awaiting_annotation", "annotation_pending"
    add_attempt(db, request["id"], status, decision, reason, eligible, trace, ctx.now)
    if status != "done":
        return status
    body = module.record_body(event, revision, content, decision, reason, trace, annotation, entities, cfg)
    _, created = create_record(db, consumer, env, event, module.KIND, request["id"], request["uid"], decision, reason, body, ctx.now)
    return "record" if created else "done_same_record"


def consume_for(module):
    def stage(db, ctx):
        if not ctx.cfg[module.NAME]["enabled"]:
            raise Skip(module.NAME + ".enabled is false")
        watchlist = load_watchlist(ctx)
        counts = {}
        for event in admitted_events(db, module.NAME, ctx.cfg[module.NAME]["env"], watchlist):
            with db.conn:
                result = evaluate(db, ctx, module, event, watchlist)
            counts[result] = counts.get(result, 0) + 1
        return counts
    return stage


def render_for(module):
    def stage(db, ctx):
        written = render_records(db, ctx.paths, module.NAME, ctx.cfg[module.NAME]["env"], ctx.now)
        return {"files": len(written)}
    return stage


# -------------------------------------------------------------------- tick
def run_named(db, ctx, stage, consumer=None):
    if stage == "ingest":
        return {"ingest": run_stage(db, ctx, "ingest", ingest)}
    if stage == "resolve":
        return {"resolve": run_stage(db, ctx, "resolve", resolve)}
    modules = [REGISTRY[consumer]] if consumer else [m for n, m in REGISTRY.items() if ctx.cfg[n]["enabled"]]
    factories = {"annotate": annotate_for, "consume": consume_for, "render": render_for}
    result = {}
    for module in modules:
        # Each consumer is its own failure domain.
        ns = namespace(module, ctx.cfg)
        result[stage + ":" + ns] = run_stage(db, ctx, stage, factories[stage](module), scope=ns)
    return result


def tick(db, ctx):
    ctx.tick_uid = ctx.tick_uid or uid("tick", iso(ctx.now), str(time.monotonic()))
    results = {}
    for stage in ("ingest", "resolve", "annotate", "consume", "render"):
        results.update(run_named(db, ctx, stage))
    return results
