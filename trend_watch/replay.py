"""Offline fixture replay for the investment@shadow consumer.

Every case runs in its own temporary config/state directory with a fixture
feed transport and a scripted Jev transport, so replay can never touch the
network, the live config/state, or a paid API. Outcomes per (CIK, accession)
are compared with the manifest's expected labels; decision precision, stage
failures/exceptions and tripwire hits over the case outputs are reported.

Labels: REVIEW / IGNORE / INCOMPLETE (decisions), BLOCKED_DEPENDENCY,
AWAITING_ANNOTATION, DEFERRED_BUDGET, DEFERRED_CAP, DEFERRED_ANOMALY,
NOT_ADMITTED, DEAD_LETTER, NOT_INGESTED.
"""
import copy
import io
import json
import socket
import tempfile
import traceback
import urllib.error
from email.message import Message
from pathlib import Path
from .config import ROOT, Paths, defaults, merge
from .engine import Context, tick
from .jev import JevClient
from .state import State
from .tripwire import scan_paths

FIXTURES = ROOT / "fixtures" / "edgar-8k"
REPLAY_KEY = "replay-fixture-not-a-key"
DECISIONS = ("REVIEW", "IGNORE", "INCOMPLETE")


class FixtureResponse(io.BytesIO):
    def __init__(self, body, url, headers, status=200):
        super().__init__(body)
        self.status, self.url = status, url
        self.headers = Message()
        for k, v in headers.items():
            self.headers[k] = v

    def geturl(self):
        return self.url

    def getcode(self):
        return self.status


class FixtureOpener:
    """Serves one fixture body; honours If-None-Match like a conditional-GET server."""

    def __init__(self, body, etag=None, fail=None):
        self.body, self.etag, self.fail = body, etag, fail
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        if self.fail:
            raise urllib.error.URLError(ConnectionRefusedError(self.fail))
        if self.etag and request.get_header("If-none-match") == self.etag:
            raise urllib.error.HTTPError(request.full_url, 304, "Not Modified", {}, None)
        headers = {"Content-Type": "application/atom+xml"}
        if self.etag:
            headers["ETag"] = self.etag
        return FixtureResponse(self.body, request.full_url, headers)


def scripted_answers(request, material):
    answers = {}
    for key, question in request["questions"].items():
        options = list(question["criteria"])
        selected = material if key == "material" else options[0]
        answers[key] = {"type": "choice", "choice": selected, "confidence": 0.5,
                        "probabilities": {o: (1.0 if o == selected else 0.0) for o in options}}
    return {"answers": answers, "usage": {"input_tokens": 400}}


class ScriptedJev:
    """Stand-in *transport* for replay/tests only (never a substitute model in production)."""

    def __init__(self, mode):
        self.mode = mode
        self.requests = []

    def post_json(self, endpoint, request, key, before_attempt=None):
        self.requests.append(copy.deepcopy(request))
        if self.mode == "outage":
            raise urllib.error.URLError(ConnectionRefusedError("connection refused"))
        if self.mode == "rate_limited":
            raise urllib.error.HTTPError(endpoint, 429, "rate limited", {}, None)
        if self.mode == "timeout":
            raise socket.timeout("timed out")
        if self.mode == "malformed":
            return {"answers": {"material": {"type": "choice", "choice": "post-now"}}}
        mode = self.mode
        if isinstance(mode, dict):  # per-filer script: {"<company>": answer, "*": default}
            company = request["state"]["filing"]["company"]
            mode = mode.get(company, mode.get("*", "unclear"))
            if mode in ("outage", "timeout", "rate_limited", "malformed"):
                return ScriptedJev(mode).post_json(endpoint, request, key)
        return scripted_answers(request, mode)


def outcome(db, consumer, env, cik, accession):
    event = db.execute("SELECT * FROM events WHERE cik=? AND accession=?", (int(cik), accession)).fetchone() if cik.isdigit() else None
    if event is None:
        for row in db.execute("SELECT o.payload_json FROM dead_letters d JOIN observations o ON o.id=d.subject_id"):
            payload = json.loads(row[0])
            if payload.get("accession") == accession and payload.get("cik") == cik:
                return "DEAD_LETTER"
        return "NOT_INGESTED"
    admission = db.execute("SELECT status FROM intake_admissions WHERE consumer=? AND env=? AND event_id=?", (consumer, env, event["id"])).fetchone()
    if admission is None:
        return "NOT_ADMITTED"
    if admission[0] != "admitted":
        return admission[0].upper()
    head = db.execute("SELECT r.decision FROM record_heads h JOIN records r ON r.id=h.record_id WHERE h.consumer=? AND h.env=? AND h.event_id=?",
                      (consumer, env, event["id"])).fetchone()
    request = db.execute("SELECT id FROM evaluation_requests WHERE consumer=? AND env=? AND event_id=? ORDER BY id DESC LIMIT 1", (consumer, env, event["id"])).fetchone()
    if request:
        last = db.execute("SELECT processing_status, decision FROM evaluation_attempts WHERE request_id=? ORDER BY attempt_no DESC LIMIT 1", (request[0],)).fetchone()
        if last and last[0] != "done":
            return last[0].upper()
    return head[0] if head else "NOT_EVALUATED"


def counts(db):
    return {"observations": db.execute("SELECT COUNT(*) FROM observations").fetchone()[0],
            "source_items": db.execute("SELECT COUNT(*) FROM source_items").fetchone()[0],
            "revisions": db.execute("SELECT COUNT(*) FROM event_revisions").fetchone()[0],
            "records": db.execute("SELECT COUNT(*) FROM records").fetchone()[0],
            "superseded": db.execute("SELECT COUNT(*) FROM record_supersessions").fetchone()[0],
            "dead_letters": db.execute("SELECT COUNT(*) FROM dead_letters").fetchone()[0],
            "jev_requests": None}


def run_case(case, fixtures, base_overrides):
    result = {"name": case["name"], "checks": [], "exceptions": [], "tripwire": []}
    with tempfile.TemporaryDirectory(prefix="tw-replay-") as tmp:
        base = Path(tmp)
        paths = Paths(base / "config", base / "state", base / "out")
        paths.config.mkdir(parents=True)
        cfg = merge(merge(defaults(), base_overrides), case.get("config", {}))
        (paths.config / "config.json").write_text(json.dumps(cfg))
        watchlist = json.loads((fixtures / case.get("watchlist", "watchlist.json")).read_text())
        (paths.config / "watchlist.json").write_text(json.dumps(watchlist))
        now = float(case.get("start", 1790000000))
        jev_requests = 0
        db = State(paths.state / "state.db")
        try:
            for n, step in enumerate(case["steps"]):
                now += step.get("advance_s", 0)
                body = (fixtures / step["feed"]).read_bytes() if step.get("feed") else b""
                opener = FixtureOpener(body, step.get("etag"), step.get("feed_fail"))
                jev = ScriptedJev(step.get("jev", "unclear"))
                ctx = Context(paths, cfg, now=now, opener=opener,
                              client_factory=lambda c, d, k, t, jev=jev: JevClient(c, d, k, t, context=jev),
                              key_provider=lambda c: REPLAY_KEY, report=lambda line: None)
                try:
                    statuses = tick(db, ctx)
                except Exception:
                    result["exceptions"].append("step {}: {}".format(n, traceback.format_exc(limit=2).splitlines()[-1]))
                    continue
                jev_requests += len(jev.requests)
                for stage, (status, detail) in statuses.items():
                    if status == "failed" and stage.split(":")[0] not in step.get("allow_failed", []):
                        result["exceptions"].append("step {} {}: {}".format(n, stage, detail.get("error")))
                for key, expected in step.get("expect", {}).items():
                    cik, accession = key.split(":", 1)
                    got = outcome(db, "investment", cfg["investment"]["env"], cik, accession)
                    result["checks"].append({"case": case["name"], "step": n, "key": key, "expected": expected, "got": got})
                for name, expected in step.get("expect_counts", {}).items():
                    actual = counts(db)
                    actual["jev_requests"] = jev_requests
                    result["checks"].append({"case": case["name"], "step": n, "key": "count:" + name, "expected": expected, "got": actual[name]})
        finally:
            db.close()
        result["tripwire"] = scan_paths(cfg, files=[paths.state / "shadow"], databases=[paths.state / "state.db"])
    return result


def run_replay(fixtures=None, base_overrides=None):
    fixtures = Path(fixtures) if fixtures else FIXTURES
    manifest = json.loads((fixtures / "manifest.json").read_text())
    overrides = merge(manifest.get("config", {}), base_overrides or {})
    cases = []
    for case in manifest["cases"]:
        try:
            cases.append(run_case(case, fixtures, overrides))
        except Exception:
            cases.append({"name": case["name"], "checks": [], "tripwire": [],
                          "exceptions": [traceback.format_exc(limit=2).splitlines()[-1]]})
    checks = [c for case in cases for c in case["checks"]]
    decided = [c for c in checks if c["got"] in DECISIONS]
    review = [c for c in checks if c["got"] == "REVIEW"]
    report = {
        "cases": cases, "checks": checks,
        "matched": sum(c["expected"] == c["got"] for c in checks), "total": len(checks),
        "decision_precision": (sum(c["expected"] == c["got"] for c in decided) / len(decided)) if decided else None,
        "review_precision": (sum(c["expected"] == "REVIEW" for c in review) / len(review)) if review else None,
        "exceptions": [e for case in cases for e in case["exceptions"]],
        "tripwire_hits": [h for case in cases for h in case["tripwire"]],
    }
    report["passed"] = report["matched"] == report["total"] and not report["exceptions"] and not report["tripwire_hits"]
    return report


def format_report(report):
    lines = ["case | step | key | expected | got | ok", "---|---|---|---|---|---"]
    for c in report["checks"]:
        lines.append("{} | {} | {} | {} | {} | {}".format(c["case"], c["step"], c["key"], c["expected"], c["got"], "ok" if c["expected"] == c["got"] else "MISMATCH"))
    fmt = lambda v: "n/a" if v is None else "{:.3f}".format(v)
    lines += ["", "matched {}/{}; decision precision {}; REVIEW precision {}".format(
        report["matched"], report["total"], fmt(report["decision_precision"]), fmt(report["review_precision"])),
        "exceptions: {}".format(len(report["exceptions"]))]
    lines += ["  " + e for e in report["exceptions"]]
    lines.append("tripwire hits over replay outputs: {}".format(len(report["tripwire_hits"])))
    lines += ["  {where}: {digits} digits".format(**h) for h in report["tripwire_hits"][:20]]
    lines.append("REPLAY " + ("PASSED" if report["passed"] else "FAILED"))
    return "\n".join(lines)
