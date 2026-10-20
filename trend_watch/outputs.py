"""Append deliverables; queue inaccessible vault writes without losing content."""
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit
from .state import day_at

HEADER = "---\ntype: deliverable\n---\n"


def append(path, content, header=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(header)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def deliver(paths, relative, content, warnings):
    try:
        append(paths.out / relative, content, HEADER)
    except OSError as exc:
        append(paths.state / "outbox" / relative, content, HEADER)
        warnings.append("WARN: output queued in outbox: {} ({})".format(relative, type(exc).__name__))


def flush(paths):
    moved, errors = 0, []
    outbox = paths.state / "outbox"
    for source in sorted(outbox.rglob("*.md")):
        relative = source.relative_to(outbox)
        target = paths.out / relative
        try:
            content = source.read_text(encoding="utf-8")
            if content.startswith(HEADER):
                content = content[len(HEADER):]
            # An idempotent marker protects a retry after successful append but failed move.
            import hashlib
            marker = "<!-- outbox:{} -->".format(hashlib.sha256(source.read_bytes()).hexdigest())
            existing = target.read_text(encoding="utf-8") if target.exists() else ""
            if marker not in existing:
                append(target, content + "\n" + marker + "\n", HEADER)
            archive = paths.state / "outbox-delivered" / uuid.uuid4().hex / relative
            archive.parent.mkdir(parents=True, exist_ok=True)
            source.rename(archive)
            moved += 1
        except OSError as exc:
            errors.append("WARN: flush {}: {}".format(relative, type(exc).__name__))
    return moved, errors


def cell(value):
    return str(value).replace("\n", " ").replace("\r", " ").replace("|", "\\|").replace("[", "\\[").replace("]", "\\]").replace("<", "&lt;").replace(">", "&gt;")


def link(item):
    url = item["url"]
    if urlsplit(url).scheme not in ("http", "https"):
        return cell(item["title"])
    return "[{}]({})".format(cell(item["title"]), quote(url, safe=":/?&=%#@+;,~.-_"))


def digest_section(items, domains, now, reason, errors, statuses, cfg, notes=()):
    lines = ["\n## " + datetime.fromtimestamp(now, timezone.utc).isoformat(), ""]
    for domain in domains:
        rows = [i for i in items if domain["name"] in i["domains"]]
        if not rows:
            continue
        lines += ["### " + domain["name"], "", "| Item | Platforms | Age | Velocity | Score | Classification |", "|---|---|---|---|---|---|"]
        for item in rows:
            c = item.get("classification", {})
            if c.get("status") == "ok":
                def uncertain(field):
                    return c[field] + ("?" if c["confidence"][field] < cfg["llm"]["min_confidence"] else "")
                classification = "domain={}; lane={}; strength={}; {}; {}; action={}".format(uncertain("domain"), uncertain("lane"), c["strength"], c["why_trending"], c["angle"], c["action_hint"])
                if c["lane"] in ("trading", "both"):
                    classification += "; asset={}; direction={}; sensitivity={}".format(c.get("asset", "none"), c["direction"], c["time_sensitivity"])
            elif c:
                classification = "unclassified (jev: {})".format(c["status"])
            else:
                classification = "unclassified (no-llm: {})".format(reason or "pending batch limit")
            lines.append("| {} | {} | {:.1f}h | {} | {:.3f} | {} |".format(link(item), cell(", ".join(item["platforms"])), item["age_hours"], cell(item["velocity_note"]), item["score"], cell(classification)))
        lines.append("")
    lines += ["Sources: " + "; ".join(cell(k + "=" + v) for k, v in statuses.items()), ""]
    lines += ["- " + cell(e) for e in list(errors) + list(notes)]
    return "\n".join(lines) + "\n"


def trading_records(items, domains, now):
    trading_domains = {d["name"] for d in domains if "trading" in d["lanes"]}
    result = []
    for item in items:
        c = item.get("classification", {})
        classified = c.get("status") == "ok"
        if (classified and c["lane"] not in ("trading", "both")) or (not classified and not trading_domains.intersection(item["domains"])):
            continue
        result.append({"ts": datetime.fromtimestamp(now, timezone.utc).isoformat(), "hash": item["hash"], "title": item["title"],
                       "url": item["url"], "platforms": item["platforms"], "domains": item["domains"], "score": item["score"],
                       "velocity": item["velocity"], "age_hours": item["age_hours"], "assets": item.get("assets", []),
                       "direction_hint": c.get("direction") if classified else None, "time_sensitivity": c.get("time_sensitivity") if classified else None,
                       "strength": c.get("strength") if classified else None, "confidence": c.get("confidence", {}), "classified": classified})
    return result


def write_drafts(paths, items, db, now, warnings):
    if not items:
        return
    blocks = []
    for item in items:
        c = item["classification"]
        blocks.append("\n## DRAFT — NOT POSTED — needs owner approval\n\n{}\n\nLane: {}; strength: {}\n\nVoice: {}\n".format(link(item), c["lane"], c["strength"], item["draft_settings"]["voice"]))
        for platform, text in item["drafts"].items():
            blocks.append("\n### {}\n\n{}\n".format(platform, text))
    deliver(paths, Path("drafts-for-approval") / (day_at(now) + ".md"), "".join(blocks), warnings)
    for item in items:
        db.execute("INSERT OR IGNORE INTO drafts VALUES (?,?,?)", (item["hash"], day_at(now), now))
    db.conn.commit()
