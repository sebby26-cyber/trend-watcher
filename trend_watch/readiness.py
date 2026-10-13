"""Readiness gate for promoting investment@shadow beyond shadow (read-only).

All must hold before the owner is even asked about production rendering:
1. fixture replay passes (all labels match, no exceptions, no tripwire hits);
2. >= ``min_shadow_days`` distinct UTC days with a successful consume run;
3. >= ``min_spot_checks`` owner verdicts (``ack --verdict``) on REVIEW records;
4. owner REVIEW precision (correct / verdicts) >= ``min_review_precision``.
Passing the gate does not enable anything: production rendering stays off until
the owner names a vault folder and approves go-live separately.
"""
from .replay import run_replay
from .state import State


def readiness(paths, cfg):
    gate = cfg["investment"]["readiness"]
    ns = "investment@" + cfg["investment"]["env"]
    report = run_replay()
    days = verdicts = correct = 0
    db_path = paths.state / "state.db"
    if db_path.exists():
        db = State(db_path, migrate=False)
        try:
            if db.has_table("stage_runs"):
                days = db.execute("SELECT COUNT(DISTINCT substr(started_at,1,10)) FROM stage_runs WHERE stage='consume' AND scope=? AND status IN ('success','partial')", (ns,)).fetchone()[0]
            if db.has_table("acks"):
                rows = db.execute("""SELECT a.verdict FROM acks a JOIN records r ON r.id=a.record_id
                                     WHERE r.decision='REVIEW' AND r.env=? AND a.verdict IS NOT NULL
                                     AND a.id=(SELECT MAX(b.id) FROM acks b WHERE b.record_id=a.record_id AND b.verdict IS NOT NULL)""",
                                  (cfg["investment"]["env"],)).fetchall()
                verdicts, correct = len(rows), sum(r[0] == "correct" for r in rows)
        finally:
            db.close()
    precision = correct / verdicts if verdicts else None
    checks = [
        ("fixture replay", report["passed"], "{}/{} labels, {} exceptions, {} tripwire hits".format(
            report["matched"], report["total"], len(report["exceptions"]), len(report["tripwire_hits"]))),
        ("shadow days", days >= gate["min_shadow_days"], "{}/{} UTC days with a successful consume run".format(days, gate["min_shadow_days"])),
        ("owner spot-checks", verdicts >= gate["min_spot_checks"], "{}/{} REVIEW verdicts".format(verdicts, gate["min_spot_checks"])),
        ("owner REVIEW precision", precision is not None and precision >= gate["min_review_precision"],
         "{} (min {})".format("n/a" if precision is None else "{:.3f}".format(precision), gate["min_review_precision"])),
    ]
    ready = all(ok for _, ok, _ in checks)
    lines = ["{} {}: {}".format("PASS" if ok else "FAIL", name, detail) for name, ok, detail in checks]
    lines.append(("READY for an owner go-live decision" if ready else "NOT READY") + "; production rendering remains disabled in R1.")
    return ready, lines
