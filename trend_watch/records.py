"""Immutable consumer records, supersession, append-only acks and the shadow report.

Records (spec fix 6):
* A record row is never updated or deleted (DB triggers).
* Exactly one active record per (consumer, env, event_id, kind): the
  ``record_heads`` primary key. A new record whose body differs from the head
  supersedes it: the supersession row links old -> new (one-to-one, new id >
  old id, so no cycles) and the head moves. Prior evaluations are untouched.
* Record UID = digest(consumer, env, event_uid, kind, request_uid): stable per
  evaluation request.
* Acks are append-only rows (actor, timestamp, note, verdict, corrected
  decision) addressed by record UID. Re-acking without a note is an idempotent
  no-op. An ack applies only to that record; it never propagates to a
  successor. Superseded records remain visible (and ackable) in the report.

Render: whole-file regeneration per UTC day, written to a temp file in the same
directory and ``os.replace``d (atomic on one filesystem; the shadow folder is
under the state directory, never the vault). Deterministic order: decision
priority (REVIEW, INCOMPLETE, IGNORE), then filed date, accession, record id.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from .ids import iso, payload_hash, uid, utc_day
from .serialize import canonical

ORDER = {"REVIEW": 0, "INCOMPLETE": 1, "IGNORE": 2}
VERDICTS = ("correct", "incorrect")


class AckError(ValueError):
    pass


def create(db, consumer, env, event, kind, request_id, request_uid, decision, reason, body, now):
    """Insert a record unless the active head already has an identical body. Returns (record_id, created)."""
    body_hash = payload_hash(body)
    head = db.execute("SELECT r.id, r.body_hash FROM record_heads h JOIN records r ON r.id=h.record_id WHERE h.consumer=? AND h.env=? AND h.event_id=? AND h.kind=?",
                      (consumer, env, event["id"], kind)).fetchone()
    if head and head["body_hash"] == body_hash:
        return head["id"], False
    record_uid = uid("record", consumer, env, event["uid"], kind, request_uid)
    existing = db.execute("SELECT id FROM records WHERE uid=?", (record_uid,)).fetchone()
    if existing:
        return existing[0], False
    new_id = db.execute("INSERT INTO records(uid,consumer,env,event_id,kind,request_id,decision,reason_code,body_json,body_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (record_uid, consumer, env, event["id"], kind, request_id, decision, reason, canonical(body), body_hash, iso(now))).lastrowid
    if head:
        db.execute("INSERT INTO record_supersessions VALUES (?,?,?)", (head["id"], new_id, iso(now)))
        db.execute("UPDATE record_heads SET record_id=? WHERE consumer=? AND env=? AND event_id=? AND kind=?", (new_id, consumer, env, event["id"], kind))
    else:
        db.execute("INSERT INTO record_heads VALUES (?,?,?,?,?)", (consumer, env, event["id"], kind, new_id))
    return new_id, True


def ack(db, record_uid, actor, now, note=None, verdict=None, corrected=None, env=None):
    """Append an acknowledgement. Returns (message, appended)."""
    row = db.execute("SELECT * FROM records WHERE uid=?", (record_uid,)).fetchone()
    if row is None:
        raise AckError("unknown record " + record_uid)
    if env is not None and row["env"] != env:
        raise AckError("record {} belongs to env {}, not {}".format(record_uid, row["env"], env))
    if verdict is not None and verdict not in VERDICTS:
        raise AckError("verdict must be one of " + ", ".join(VERDICTS))
    if corrected is not None and corrected not in ORDER:
        raise AckError("corrected decision must be REVIEW, IGNORE or INCOMPLETE")
    if corrected is not None and verdict != "incorrect":
        raise AckError("--correct requires --verdict incorrect")
    if not actor or not actor.strip():
        raise AckError("actor must be nonempty")
    successor = db.execute("SELECT n.uid FROM record_supersessions s JOIN records n ON n.id=s.new_record_id WHERE s.old_record_id=?", (row["id"],)).fetchone()
    previous = db.execute("SELECT COUNT(*) FROM acks WHERE record_id=?", (row["id"],)).fetchone()[0]
    suffix = " (superseded by {}; this ack does not carry over)".format(successor[0]) if successor else ""
    if previous and note is None and verdict is None:
        return "already acknowledged: " + record_uid + suffix, False
    with db.conn:
        db.execute("INSERT INTO acks(record_id,actor,acked_at,note,verdict,corrected_decision) VALUES (?,?,?,?,?,?)",
                   (row["id"], actor.strip(), iso(now), note, verdict, corrected))
    return "acknowledged: " + record_uid + suffix, True


def cell(value):
    """Markdown-safe external text: no links, tables, HTML or code spans can be injected."""
    text = "" if value is None else str(value)
    for a, b in (("\\", "\\\\"), ("\n", " "), ("\r", " "), ("|", "\\|"), ("[", "\\["), ("]", "\\]"),
                 ("<", "&lt;"), (">", "&gt;"), ("`", "\\`")):
        text = text.replace(a, b)
    return text


def link(text, url):
    from urllib.parse import quote, urlsplit
    if not url or urlsplit(url).scheme != "https":
        return cell(text)
    return "[{}]({})".format(cell(text), quote(url, safe=":/?&=%#@+;,~.-_"))


def render_day(db, consumer, env, day):
    rows = db.execute("""SELECT r.*, (SELECT n.uid FROM record_supersessions s JOIN records n ON n.id=s.new_record_id WHERE s.old_record_id=r.id) AS successor
                         FROM records r WHERE r.consumer=? AND r.env=? AND substr(r.created_at,1,10)=?""", (consumer, env, day)).fetchall()
    acks = {}
    for a in db.execute("SELECT a.* FROM acks a JOIN records r ON r.id=a.record_id WHERE r.consumer=? AND r.env=? ORDER BY a.id", (consumer, env)):
        acks.setdefault(a["record_id"], []).append(a)
    active, superseded = [], []
    for row in rows:
        (superseded if row["successor"] else active).append((row, json.loads(row["body_json"])))
    key = lambda pair: (ORDER.get(pair[0]["decision"], 9), pair[1].get("filed_date") or "", pair[1].get("accession") or "", pair[0]["id"])
    lines = ["---", "type: shadow-report", "consumer: " + consumer, "env: " + env, "day_utc: " + day, "---", "",
             "# {}@{} — {} (UTC)".format(consumer, env, day), "",
             "> SHADOW OUTPUT. Generated file: manual edits will be overwritten on the next render.",
             "> Not investment advice. No market data and no trade proposals. Nothing was posted, sent or notified.", ""]

    def table(pairs):
        out = ["| Record | Filer (CIK) | Form | Items | Filed | Reason | Ack |", "|---|---|---|---|---|---|---|"]
        for row, body in pairs:
            items = ", ".join(i["number"] for i in body.get("items", []))
            reason = row["reason_code"]
            if body.get("annotation") and body["annotation"]["status"] == "unavailable":
                reason += " (materiality annotation unavailable)"
            if body.get("form", "").endswith("/A"):
                reason += "; amendment " + ("linked" if body["amendment"]["status"] == "linked" else "target unresolved")
            ack_rows = acks.get(row["id"], [])
            ack_text = "; ".join("{} {}{}".format(cell(a["actor"]), a["acked_at"], (" " + a["verdict"]) if a["verdict"] else "") for a in ack_rows) or "—"
            out.append("| `{}` | {} | {} | {} | {} | {} | {} |".format(
                row["uid"], link("{} ({})".format(body.get("company") or "?", body.get("cik")), body.get("url")),
                cell(body.get("form")), cell(items), cell(body.get("filed_date")), cell(reason), ack_text))
        return out

    for decision in ("REVIEW", "INCOMPLETE", "IGNORE"):
        pairs = sorted((p for p in active if p[0]["decision"] == decision), key=key)
        lines += ["## {} ({})".format(decision, len(pairs)), ""]
        lines += table(pairs) if pairs else ["(none)"]
        lines.append("")
    if superseded:
        lines += ["## Superseded ({})".format(len(superseded)), ""]
        for row, body in sorted(superseded, key=key):
            lines.append("- `{}` {} → superseded by `{}`".format(row["uid"], row["decision"], row["successor"]))
        lines.append("")
    return "\n".join(lines)


def processing_summary(db, consumer, env):
    rows = db.execute("""SELECT a.processing_status, COUNT(*) FROM evaluation_attempts a
        JOIN (SELECT request_id, MAX(attempt_no) n FROM evaluation_attempts GROUP BY request_id) last
          ON last.request_id=a.request_id AND last.n=a.attempt_no
        JOIN evaluation_requests q ON q.id=a.request_id
        WHERE q.consumer=? AND q.env=? AND a.processing_status NOT IN ('done','superseded') GROUP BY a.processing_status""", (consumer, env)).fetchall()
    deferred = db.execute("SELECT status, COUNT(*) FROM intake_admissions WHERE consumer=? AND env=? AND status!='admitted' GROUP BY status", (consumer, env)).fetchall()
    episode = db.execute("SELECT * FROM dependency_episodes WHERE closed_at IS NULL ORDER BY id DESC LIMIT 1").fetchone()
    lines = ["## Processing (not decisions)", ""]
    for status, count in list(rows) + list(deferred):
        lines.append("- {}: {}".format(status, count))
    if episode:
        lines.append("- Jev unavailable since {} ({}); LLM steps wait, no substitute model".format(episode["opened_at"], cell(episode["reason"])))
    if len(lines) == 2:
        lines.append("- nothing pending")
    return "\n".join(lines) + "\n"


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(str(tmp), str(path))


def shadow_dir(paths, consumer):
    return paths.state / "shadow" / consumer


def render(db, paths, consumer, env, now):
    """Regenerate every UTC day touched since the last render, plus today. Returns written paths."""
    last = db.execute("SELECT MAX(max_record_id), MAX(max_ack_id) FROM render_log WHERE consumer=? AND env=?", (consumer, env)).fetchone()
    last_record, last_ack = last[0] or 0, last[1] or 0
    days = {utc_day(now)}
    days.update(r[0][:10] for r in db.execute("SELECT created_at FROM records WHERE consumer=? AND env=? AND id>?", (consumer, env, last_record)))
    days.update(r[0][:10] for r in db.execute("SELECT r.created_at FROM acks a JOIN records r ON r.id=a.record_id WHERE r.consumer=? AND r.env=? AND a.id>?", (consumer, env, last_ack)))
    max_record = db.execute("SELECT COALESCE(MAX(id),0) FROM records").fetchone()[0]
    max_ack = db.execute("SELECT COALESCE(MAX(id),0) FROM acks").fetchone()[0]
    written = []
    summary = processing_summary(db, consumer, env)
    for day in sorted(days):
        text = render_day(db, consumer, env, day)
        if day == utc_day(now):
            text += "\n" + summary
        path = shadow_dir(paths, consumer) / (day + ".md")
        atomic_write(path, text + "\n")
        written.append(path)
    with db.conn:
        for path in written:
            db.execute("INSERT INTO render_log(consumer,env,path,rendered_at,max_record_id,max_ack_id) VALUES (?,?,?,?,?,?)",
                       (consumer, env, str(path), iso(now), max_record, max_ack))
    return written
