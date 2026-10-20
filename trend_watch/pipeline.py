"""One tick, with injectable sources and Jev client for completely offline tests."""
import json
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from .config import load_config, load_domains
from .drafts import draft_batch
from .jev import JevClient, api_key, assets_for
from .outputs import append, deliver, digest_section, trading_records, write_drafts
from .registry import discover, fetch_all
from .scoring import score_items, select_candidates
from .state import State, day_at
from .serialize import dumps
from .observations import record_legacy_fetch


def run(paths, domain=None, no_llm=False, no_drafts=False, dry_run=False, max_candidates=None,
        registry=None, context_factory=None, client_factory=JevClient, now=None, key_provider=api_key, verbose=False, packs_dirs=None):
    now = time.time() if now is None else now
    cfg = load_config(paths)
    discovered, errors = discover(paths) if registry is None else (registry, [])
    domains, domain_errors = load_domains(paths, cfg, discovered, packs_dirs=packs_dirs)
    errors.extend(domain_errors)
    domains = [d for d in domains if d["enabled"] and (domain is None or d["name"] == domain)]
    if domain is not None and not domains:
        errors.append("No enabled valid domain named " + domain)
    limit = cfg["max_candidates"] if max_candidates is None else max_candidates
    if limit < 0:
        raise ValueError("max-candidates must be nonnegative")
    db = State(paths.state / "state.db")
    try:
        before = db.budget(now)
        kwargs = {"context_factory": context_factory} if context_factory else {}
        raw, statuses, fetch_errors = fetch_all(domains, cfg, discovered, db, now, **kwargs)
        errors.extend(fetch_errors)
        # Immutable observations are recorded before any candidate selection. A failure
        # here is reported but can never stop the legacy trend path from completing.
        try:
            record_legacy_fetch(db, raw, now)
        except Exception as exc:
            db.conn.rollback()
            errors.append("observations: {}".format(type(exc).__name__))
        scored = score_items(raw, domains, cfg, db, now)
        symbols = [r.get("extra", {}).get("symbol", "") for r in raw if r["platform"] == "coingecko"]
        for item in scored:
            item["assets"] = assets_for(item, domains, cfg, symbols)
        fresh = select_candidates(scored, domains, db, limit)
        reason = "dry-run" if dry_run else "--no-llm" if no_llm else None
        if not reason and not cfg["llm"].get("enabled", True):
            reason = "Jev disabled in config (llm.enabled=false)"
        key = None if reason else key_provider(cfg)
        if not reason and not key:
            reason = "no API key; behaving as --no-llm"
        if not reason:
            reason = db.budget_reason(cfg, now, "classify")
        # Per-kind gate: only classify calls delay the next classify batch.
        last = db.last_call("classify")
        if not reason and last is not None and now - last < cfg["llm"]["min_interval_s"]:
            reason = "Jev min interval; candidates pending"
        candidates = list(fresh)
        client = None
        if not reason:
            names = {d["name"]: d for d in domains}
            current = {i["hash"]: i for i in scored}
            pending = {i["hash"]: i for i in db.pending()}
            pending.update({i["hash"]: i for i in fresh})
            ready = []
            for hash_, stored in pending.items():
                item = dict(current.get(hash_, stored))
                item["age_hours"] = max(0, (now - (item.get("published") if item.get("published") is not None else item["first_seen"])) / 3600)
                item["domains"] = [n for n in item["domains"] if n in names and item["age_hours"] <= names[n]["freshness"]["hard_max_age_h"]]
                if item["domains"]:
                    item["assets"] = assets_for(item, domains, cfg, symbols)
                    ready.append(item)
            ready.sort(key=lambda i: (-i["score"], i["hash"]))
            candidates = ready[:limit]
            db.mark_candidates(candidates, now)
            client = client_factory(cfg, db, key, now)
            candidates = client.classify(candidates, domains, limit)
        elif not dry_run:
            db.mark_candidates(candidates, now)
        drafts, notes = [], []
        last_draft = db.last_call("draft")
        draft_gap = cfg["llm"].get("kind_min_interval_s", {}).get("draft", 0)
        if not dry_run and not no_drafts and client and (last_draft is None or now - last_draft >= draft_gap):
            drafts, notes = draft_batch(db, cfg, domains, client, now)
        warnings = []
        if not dry_run:
            write_drafts(paths, drafts, db, now, warnings)
            if candidates:
                section = digest_section(candidates, domains, now, reason, errors, statuses, cfg, notes + warnings)
                deliver(paths, Path("digests") / (day_at(now) + ".md"), section, warnings)
                records = trading_records(candidates, domains, now)
                if records:
                    append(paths.state / "feeds/trading" / (day_at(now) + ".jsonl"), "".join(dumps(r, ensure_ascii=False) + "\n" for r in records))
        after = db.budget(now)
        summary = {"fetched": len(raw), "sources_ok": sum(s in ("ok", "partial") for s in statuses.values()),
                   "sources_total": len(statuses), "sources": statuses, "candidates": len(candidates),
                   "jev_calls": after["calls"] - before["calls"], "jev_tokens": after["input_tokens"] - before["input_tokens"],
                   "drafts": len(drafts), "no_llm_reason": reason, "errors": errors, "warnings": warnings, "notes": notes, "dry_run": dry_run, "ts": now}
        line = "trend-watch: {fetched} fetched, {sources_ok}/{sources_total} sources ok, {candidates} candidates, jev={jev_calls} ({jev_tokens} tok), drafts={drafts}".format(**summary)
        summary["summary"] = line
        db.execute("INSERT INTO runs(ts,summary_json) VALUES (?,?)", (now, dumps(summary)))
        db.conn.commit()
        if not dry_run:
            try:
                pruned = db.prune(now, cfg["retention"])
                if pruned.get("vacuumed") or any(v for k, v in pruned.items() if k != "vacuumed"):
                    print("retention: " + json.dumps(pruned, sort_keys=True), file=sys.stderr)
            except sqlite3.Error as exc:
                print("retention: prune failed: {}".format(exc), file=sys.stderr)
            logs = paths.state / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            (logs / "last-run.json").write_text(dumps(summary, indent=2) + "\n", encoding="utf-8")
            if cfg["hooks"]["notify_enabled"]:
                from .hooks.notify import notify
                notify(summary)
        stream = sys.stdout if dry_run else sys.stderr
        print_report(raw, statuses, candidates, domains, errors, stream)
        if verbose:
            for item in scored:
                print(json.dumps(item, ensure_ascii=False), file=stream)
        if reason:
            print("unclassified (no-llm: {})".format(reason), file=stream)
        for error in errors + warnings + notes:
            print(error, file=stream)
        print(line)
        return summary
    finally:
        db.close()


def print_report(raw, statuses, candidates, domains, errors, stream):
    counts = Counter(row["platform"] for row in raw)
    for name, status in statuses.items():
        error_count = sum(e.startswith(name + ":") for e in errors)
        print("source {}: {} | items={} | errors={}".format(name, status, counts[name], error_count), file=stream)
    platforms = sorted(set(statuses) | {p for i in candidates for p in i["platforms"]})
    print("\nCandidate counts by domain x platform:", file=stream)
    print("domain | " + " | ".join(platforms), file=stream)
    for domain in domains:
        counts = Counter(p for i in candidates if domain["name"] in i["domains"] for p in i["platforms"])
        print(domain["name"] + " | " + " | ".join(str(counts[p]) for p in platforms), file=stream)
    print("\nTop candidates:", file=stream)
    print("rank | score | domain | platforms | age h | title", file=stream)
    for rank, item in enumerate(sorted(candidates, key=lambda i: (-i["score"], i["hash"])), 1):
        title = " ".join(item["title"].split())
        title = title[:89] + "…" if len(title) > 90 else title
        print("{} | {:.3f} | {} | {} | {:.1f} | {}".format(rank, item["score"], ",".join(item["domains"]),
              ",".join(item["platforms"]), item["age_hours"], title), file=stream)
    if not candidates:
        print("(none)", file=stream)
