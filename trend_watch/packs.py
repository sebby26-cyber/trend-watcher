"""Nudge-only alert packs over the shared trend-watch state.

Discovery is first match per pack name: CLI --packs-dir, then the path-separated
TREND_WATCH_PACKS_DIR environment variable, live config.json packs_dir, then
$XDG_CONFIG_HOME/trend-watch/packs (default ~/.config/trend-watch/packs), then
this repository's gitignored packs/ directory. A pack is <dir>/<name>.json;
domains resolve from <dir>/domains/<domain>.json before seed/domains/.
The live <config>/<name>.json override merges over the pack. State stays in
<state>/<name>/<name>.db, with outbox/, sent/, failed/ and day-named log/ files.

The engine reads shared state.db read-only, clusters fresh stories and scores
velocity, relevance, source quality, novelty and audience fit. Above a strict
bar it writes a short Markdown nudge and optionally invokes nudge.command.
It never drafts, posts or trades. defer_to checks other packs' nudges read-only.

Optional tuning harvests DONE verdicts (drafted / already-covered / stale) from
an external reply-state directory, using the compatible tuning.link_state and
tuning.peer keys. Absent or disabled tuning makes harvest a no-op. A bounded
bias over recent verdicts raises the bar for misses and lowers it for drafts.
"""
import contextlib
import fcntl
import json
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import ROOT, merge
from .scoring import STOP

GENERIC = set("""stock stocks market markets price prices says said report reports today news after amid over
its will could may up down us year years week weeks day days more than first last into about against
amp via report latest top big new set sees see hits hit just ahead says while why how what when where""".split())
NEWS_SUFFIX = re.compile(r"\s+[-|–—]\s+([^-|–—]{2,60})$")
MONEY = re.compile(r"\$\s?\d[\d,.]*\s?(?:billion|trillion|bn|tn|b|t)\b", re.I)


# ----------------------------------------------------------------- config/paths
PACK_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
PACK_DEFAULTS = {"title": None, "labels": {}, "defer_to": [], "finance_first_terms": [], "finance_domain_terms": [],
                 "tuning": {"enabled": False, "window": 10, "step_up": 0.02, "step_down": 0.01,
                            "max_up": 0.10, "max_down": 0.05, "link_state": "", "peer": ""}}
VERDICT_RE = re.compile(r"\b(drafted|already[- ]covered|stale)\b", re.I)


def directories(paths, packs_dirs=None):
    """Ordered search roots, deduplicated without changing precedence."""
    explicit = [packs_dirs] if isinstance(packs_dirs, (str, Path)) else (packs_dirs or [])
    roots = list(explicit)
    roots.extend(p for p in os.environ.get("TREND_WATCH_PACKS_DIR", "").split(os.pathsep) if p)
    config_file = paths.config / "config.json"
    if config_file.is_file():
        configured = json.loads(config_file.read_text(encoding="utf-8")).get("packs_dir", "")
        if not isinstance(configured, str):
            raise ValueError("packs_dir must be a string")
        roots.extend(p for p in configured.split(os.pathsep) if p)
    roots.append(Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "trend-watch/packs")
    roots.append(ROOT / "packs")
    return list(dict.fromkeys(Path(p).expanduser().resolve() for p in roots))


def discover(paths, packs_dirs=None):
    """Return pack name -> JSON file; the first match for each name wins."""
    found = {}
    for folder in directories(paths, packs_dirs):
        for path in sorted(folder.glob("*.json")):
            if PACK_RE.fullmatch(path.stem) and path.stem != "list" and path.is_file():
                found.setdefault(path.stem, path)
    return found


def resolve(paths, pack, packs_dirs=None):
    if not isinstance(pack, str) or not PACK_RE.fullmatch(pack) or pack == "list":
        raise ValueError("bad pack name")
    path = discover(paths, packs_dirs).get(pack)
    if path is None:
        raise ValueError("pack not found: " + pack)
    return path


def domain_path(pack_file, domain):
    """Resolve a domain beside its pack, then from the shared repository seeds."""
    if not isinstance(domain, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", domain):
        raise ValueError("bad domain name")
    for path in (Path(pack_file).parent / "domains" / (domain + ".json"),
                 ROOT / "seed/domains" / (domain + ".json")):
        if path.is_file():
            return path
    raise ValueError("pack domain not found: " + domain)


def defaults(pack_file):
    raw = json.loads(Path(pack_file).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(str(pack_file) + " must be an object")
    return merge(PACK_DEFAULTS, raw)


def load(paths, pack, packs_dirs=None):
    pack_file = resolve(paths, pack, packs_dirs)
    cfg = defaults(pack_file)
    path = paths.config / (pack + ".json")
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(pack + ".json must be an object")
        cfg = merge(cfg, raw)
    cfg["pack_file"] = str(pack_file)
    cfg["pack"] = pack
    cfg["title"] = cfg.get("title") or pack
    w = cfg["weights"]
    if abs(sum(w.values()) - 1.0) > 1e-6:
        raise ValueError(pack + " weights must sum to 1")
    if not 0 < cfg["alert"]["threshold"] <= 1:
        raise ValueError(pack + " alert.threshold must be in (0,1]")
    cmd = cfg["nudge"]["command"]
    if not isinstance(cmd, list) or not all(isinstance(a, str) for a in cmd):
        raise ValueError(pack + " nudge.command must be a list of strings")
    if not all(isinstance(d, str) and PACK_RE.fullmatch(d) and d != pack for d in cfg["defer_to"]):
        raise ValueError(pack + " defer_to must list other pack names")
    cfg["domain_files"] = [str(domain_path(pack_file, d)) for d in cfg["domains"]]
    return cfg


def home(paths, pack):
    root = paths.state / pack
    for sub in ("outbox", "sent", "failed", "log"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


@contextlib.contextmanager
def locked(root):
    fd = os.open(str(root / (root.name + ".lock")), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def own_db(root):
    db = sqlite3.connect(str(root / (root.name + ".db")))
    db.row_factory = sqlite3.Row
    db.executescript("""
      CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
      CREATE TABLE IF NOT EXISTS nudges (id INTEGER PRIMARY KEY, ts REAL, kind TEXT, status TEXT, domain TEXT,
        headline TEXT, score REAL, signature TEXT, hashes TEXT, file TEXT, detail TEXT);
      CREATE TABLE IF NOT EXISTS evals (ts REAL PRIMARY KEY, run_ts REAL, stories INTEGER, eligible INTEGER,
        top_score REAL, top_headline TEXT, nudged INTEGER, summary TEXT);
    """)
    cols = {r["name"] for r in db.execute("PRAGMA table_info(nudges)")}
    for col, decl in (("event_id", "TEXT"), ("verdict", "TEXT"), ("verdict_ts", "REAL")):
        if col not in cols:
            db.execute("ALTER TABLE nudges ADD COLUMN {} {}".format(col, decl))
    db.commit()
    return db


def meta(db, key, value=None):
    if value is None:
        row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None
    db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))
    db.commit()
    return value


# ----------------------------------------------------------------- text helpers
def split_outlet(title, platform, extra):
    if platform == "reddit":
        return title.strip(), "r/" + str((extra or {}).get("subreddit", "reddit")).lower()
    if platform == "hackernews":
        return title.strip(), "hacker news"
    m = NEWS_SUFFIX.search(title)
    if m:
        return title[:m.start()].strip(), m.group(1).strip().lower()
    return title.strip(), platform


def tokens(text):
    out = set()
    for w in re.findall(r"[\w&$.%-]+", text.lower()):
        w = w.strip(".-%")
        if len(w) < 2 or w in STOP or w in GENERIC:
            continue
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(w)
    return out


def has_term(text, term):
    return bool(re.search(r"(?<![\w&])" + re.escape(term) + r"(?![\w&])", text, re.I))


def overlap(a, b):
    shared = len(a & b)
    return shared, (shared / min(len(a), len(b)) if a and b else 0.0)


def tier(outlet, cfg):
    o = outlet.lower().strip()
    if o in cfg["outlets"]["tier1"]:
        return 1
    if o in cfg["outlets"]["tier2"]:
        return 2
    return 4 if o.startswith("r/") or o == "hacker news" else 3


# ----------------------------------------------------------------- read state
def read_items(state_db, cfg, now):
    lookback = now - cfg["window"]["lookback_h"] * 3600
    uri = "file:{}?mode=ro".format(state_db)
    con = sqlite3.connect(uri, uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        run = con.execute("SELECT MAX(ts) AS ts FROM runs").fetchone()
        run_ts = run["ts"] if run and run["ts"] is not None else now
        rows = con.execute("SELECT hash,title,url,platform,first_seen,last_seen,published,domains,data_json FROM items "
                           "WHERE last_seen>=? AND COALESCE(published, first_seen)>=?", (lookback, lookback)).fetchall()
    finally:
        con.close()
    wanted = set(cfg["domains"])
    items = []
    for r in rows:
        try:
            doms = [d for d in json.loads(r["domains"] or "[]") if d in wanted]
            data = json.loads(r["data_json"] or "{}")
        except ValueError:
            continue
        if not doms and r["platform"] != "coingecko_markets":
            continue
        items.append({"hash": r["hash"], "title": r["title"], "url": r["url"], "platform": r["platform"],
                      "first_seen": r["first_seen"], "last_seen": r["last_seen"],
                      "published": r["published"] if r["published"] is not None else r["first_seen"],
                      "domains": doms, "extra": data.get("extra") or {}, "live": r["last_seen"] >= run_ts - 120})
    return items, run_ts


# ----------------------------------------------------------------- stories
def cluster(items, cfg):
    news = [i for i in items if i["platform"] in cfg["news_platforms"]]
    for i in news:
        i["headline"], i["outlet"] = split_outlet(i["title"], i["platform"], i["extra"])
        i["tokens"] = tokens(i["headline"])
    news.sort(key=lambda i: (i["published"], i["hash"]))
    min_shared, min_overlap = cfg["cluster"]["min_shared"], cfg["cluster"]["min_overlap"]
    stories = []
    for item in news:
        if len(item["tokens"]) < 2:
            continue
        best = None
        for s in stories:
            seed = s["members"][0]["tokens"]
            k, ov = overlap(item["tokens"], seed)
            hit = k >= min_shared and ov >= min_overlap
            if not hit:
                votes = sum(1 for m in s["members"] if (lambda kk, oo: kk >= min_shared and oo >= min_overlap)(*overlap(item["tokens"], m["tokens"])))
                hit = votes * 2 >= len(s["members"]) and votes > 0
            if hit and (best is None or ov > best[1]):
                best = (s, ov)
        if best:
            best[0]["members"].append(item)
        else:
            stories.append({"members": [item]})
    return stories


def corroborators(items, cfg):
    """Price moves (CoinGecko) and event momentum (prediction markets, Google Trends)."""
    coins, momentum = [], []
    for i in items:
        if not i["live"]:
            continue
        if i["platform"] == "coingecko_markets":
            coins.append(i)
        elif i["platform"] in ("polymarket", "kalshi", "google_trends") and i["domains"]:
            ex = i["extra"]
            move = 0.0
            for key in ("oneDayPriceChange", "price_delta_dollars"):
                try:
                    move = max(move, abs(float(ex.get(key) or 0)))
                except (TypeError, ValueError):
                    pass
            if i["platform"] == "google_trends" or move >= 0.10:
                momentum.append(dict(i, tokens=tokens(i["title"]), move=move))
    return coins, momentum


def relevant_domains(text, cfg):
    hits = {}
    for dom in cfg["domains"]:
        groups = cfg["relevance"].get(dom) or []
        found = []
        for group in groups:
            g = [t for t in group if has_term(text, t)]
            if not g:
                found = None
                break
            found.extend(g)
        if found:
            hits[dom] = sorted(set(found))
    return hits


def evaluate(story, coins, momentum, cfg, now, recent_nudges, deferred=(), threshold=None):
    members = story["members"]
    a = cfg["alert"]
    win = cfg["window"]
    text = " | ".join(m["headline"] for m in members)
    rel = relevant_domains(text, cfg)
    item_domains = {d for m in members for d in m["domains"]}
    rel = {d: v for d, v in rel.items() if d in item_domains}
    started = min(m["published"] for m in members)
    age_h = max(0.0, (now - started) / 3600)
    fresh = [m for m in members if now - m["published"] <= win["fresh_h"] * 3600]
    burst = [m for m in members if now - m["published"] <= win["burst_h"] * 3600]
    outlets = {m["outlet"] for m in members}
    fresh_outlets = {m["outlet"] for m in fresh}
    burst_outlets = {m["outlet"] for m in burst}
    tiers = {o: tier(o, cfg) for o in outlets}
    tier1 = sorted(o for o, t in tiers.items() if t == 1)
    sig = set()
    counts = {}
    for m in members:
        for t in m["tokens"]:
            counts[t] = counts.get(t, 0) + 1
    sig = {t for t, c in counts.items() if c >= 2} or set(members[0]["tokens"])
    sig |= set(members[0]["tokens"])

    # Velocity: outlet density in the fresh window and the last hour, plus market corroboration.
    velocity = 0.5 * min(1.0, len(fresh_outlets) / 6.0) + 0.5 * min(1.0, len(burst_outlets) / 3.0)
    why_extra, price_hit = [], None
    for c in coins:
        ex = c["extra"]
        names = {str(ex.get("symbol", "")).lower(), str(ex.get("coin_id", "")).lower()}
        if str(ex.get("coin_id")) == "ethereum":
            names.add("ether")
        if not any(has_term(text, n) for n in names if n):
            continue
        h1, h24 = abs(ex.get("pct_1h") or 0), abs(ex.get("pct_24h") or 0)
        mm = a["market_move"]
        if h1 >= mm["pct_1h"] or h24 >= mm["pct_24h"]:
            price_hit = c
            velocity = max(velocity, 0.9)
            why_extra.append("{} {:+.1f}% 1h / {:+.1f}% 24h".format(ex.get("symbol"), ex.get("pct_1h") or 0, ex.get("pct_24h") or 0))
    corr = []
    for mo in momentum:
        k, ov = overlap(mo["tokens"], sig)
        if (mo["platform"] == "google_trends" and k >= 1 and ov >= 0.5) or (k >= 2 and ov >= 0.4):
            corr.append(mo)
    if corr:
        velocity = min(1.0, velocity + 0.15)
        for mo in corr[:2]:
            label = "Google Trends" if mo["platform"] == "google_trends" else mo["platform"].capitalize()
            why_extra.append("{} momentum: {}".format(label, mo["title"][:80]))

    # Relevance: the domain's required groups hit, more distinct strong terms = stronger.
    if rel:
        distinct = {t for v in rel.values() for t in v}
        strong = [t for t in cfg["strong_terms"] if has_term(text, t)]
        relevance = min(1.0, 0.5 + 0.15 * (len(distinct) - 1) + 0.1 * len(strong))
    else:
        relevance = 0.0

    # Source quality: the best outlet tier, a bonus for independent tier-1 confirmation.
    best = min(tiers.values()) if tiers else 4
    quality = {1: 0.85, 2: 0.65, 3: 0.4, 4: 0.3}[best] + (0.15 if len(tier1) >= 2 else 0.0)
    quality = min(1.0, quality)

    # Novelty: how new the story is, minus recurring formats and anything already nudged.
    novelty = 1.0 if age_h <= 1 else 0.8 if age_h <= 2 else 0.6 if age_h <= 3 else 0.2
    stale = [p for p in cfg["stale_patterns"] if any(p in m["headline"].lower() for m in members[:3])]
    if stale:
        novelty *= 0.3
    hashes = {m["hash"] for m in members}
    dup = None
    for n in recent_nudges:
        prior_sig = set(json.loads(n["signature"] or "[]"))
        prior_hashes = set(json.loads(n["hashes"] or "[]"))
        k, ov = overlap(sig, prior_sig)
        if hashes & prior_hashes or (k >= 3 and ov >= 0.5):
            dup = n
            break
    if dup:
        novelty = 0.0
    owner = None
    for n in deferred:
        prior_sig = set(json.loads(n["signature"] or "[]"))
        prior_hashes = set(json.loads(n["hashes"] or "[]"))
        k, ov = overlap(sig, prior_sig)
        if hashes & prior_hashes or (k >= 3 and ov >= 0.5):
            owner = n
            break
    lead_text = sorted(members, key=lambda m: (tier(m["outlet"], cfg), m["published"]))[0]["headline"]
    fin = [t for t in cfg["finance_first"] if has_term(lead_text, t)] if cfg.get("finance_first") else []
    # Finance-domain terms (M&A, deals) hold the story on ANY headline, not just the lead:
    # the domain decides, whatever the framing (owner rule, 2026-10-12).
    fin += [t for t in cfg.get("finance_domain", []) if has_term(text, t) and t not in fin]

    # Audience fit: configured markers for the desired editorial focus.
    low = text.lower()
    markers = [p for p in cfg["persona_markers"] if p in low]
    if MONEY.search(text):
        markers.append("$-figure")
    persona = min(1.0, 0.3 + 0.15 * len(set(markers))) if markers else 0.25

    w = cfg["weights"]
    parts = {"velocity": velocity, "relevance": relevance, "source_quality": quality, "novelty": novelty, "persona_fit": persona}
    score = sum(w[k] * parts[k] for k in w)

    gates = []
    if not rel:
        gates.append("not-relevant")
    if age_h > a["max_story_age_h"]:
        gates.append("stale>{}h".format(a["max_story_age_h"]))
    if not any(m["live"] for m in members):
        gates.append("not-live")
    mm = a["market_move"]
    coverage_ok = len(fresh_outlets) >= a["min_outlets"] and len(tier1) >= a["min_tier1_outlets"]
    move_ok = price_hit is not None and len(fresh_outlets) >= mm["min_outlets"]
    if not (coverage_ok or move_ok):
        gates.append("coverage {} outlets/{} tier1".format(len(fresh_outlets), len(tier1)))
    if dup:
        gates.append("dup-of-nudge-{}".format(dup["id"]))
    if owner:
        gates.append("owned-by-{}-nudge-{}".format(owner["pack"], owner["id"]))
    if fin:
        gates.append("finance-first({})".format(fin[0]))
    bar = a["threshold"] if threshold is None else threshold
    if score < bar:
        gates.append("below-threshold")

    lead = sorted(members, key=lambda m: (tier(m["outlet"], cfg), m["published"]))[0]
    sources = sorted(members, key=lambda m: (tier(m["outlet"], cfg), -m["published"]))
    seen, urls = set(), []
    for m in sources:
        if m["outlet"] in seen:
            continue
        seen.add(m["outlet"])
        urls.append({"outlet": m["outlet"], "url": m["url"], "title": m["headline"]})
    domain = max(rel, key=lambda d: len(rel[d])) if rel else (sorted(item_domains)[0] if item_domains else "-")
    return {"headline": lead["headline"], "lead_outlet": lead["outlet"], "domain": domain, "score": round(score, 3),
            "parts": {k: round(v, 3) for k, v in parts.items()}, "age_min": int(age_h * 60), "started": started,
            "outlets": sorted(outlets), "fresh_outlets": len(fresh_outlets), "burst_outlets": len(burst_outlets),
            "tier1": tier1, "members": len(members), "why_extra": why_extra, "stale": stale, "markers": sorted(set(markers)),
            "relevance_hits": rel, "gates": gates, "eligible": not gates, "signature": sorted(sig),
            "hashes": sorted(hashes), "sources": urls}


# ----------------------------------------------------------------- nudge
def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def render(story, now, cfg, test=False):
    label = cfg["labels"].get(story["domain"], story["domain"])
    lines = ["# {}{} trend alert — {}".format("[TEST] " if test else "", cfg["title"], label), "",
             "**{}**".format(story["headline"]), "",
             "Why hot:"]
    lines.append("- {} outlets on it in the last 3h ({} in the last hour); story is {} min old.".format(
        story["fresh_outlets"], story["burst_outlets"], story["age_min"]))
    if story["tier1"]:
        lines.append("- Tier-1 confirmation: {}.".format(", ".join(story["tier1"][:4])))
    for extra in story["why_extra"][:2]:
        lines.append("- " + extra + ".")
    lines += ["", "Sources:"]
    for s in story["sources"][:cfg["nudge"]["max_sources"]]:
        lines.append("- {} — {}".format(s["outlet"], s["url"]))
    p = story["parts"]
    lines += ["", "Detected: {} · score {:.2f} (velocity {:.2f}, relevance {:.2f}, source {:.2f}, novelty {:.2f}, persona {:.2f}; bar {:.2f})".format(
        iso(now), story["score"], p["velocity"], p["relevance"], p["source_quality"], p["novelty"], p["persona_fit"],
        cfg.get("_bar", cfg["alert"]["threshold"]))]
    if test:
        lines += ["", "TEST nudge (go-live check): a real current story sent regardless of the alert bar{}.".format(
            "" if story["eligible"] else " — it did NOT pass: " + "; ".join(story["gates"]))]
    lines += ["", "Reply DONE with a verdict word — drafted / already-covered / stale — so the bar can tune.",
              "", "— {}-trend-watch · nudge-only (no drafts, no posts). Your call what to do with it.".format(cfg["pack"])]
    return "\n".join(lines) + "\n"


def hand_off(cfg, path, marker, action):
    cmd = cfg["nudge"]["command"]
    if not cmd:
        return "outbox-only", ""
    argv = [a.replace("{marker}", marker).replace("{action}", action).replace("{file}", str(path)) for a in cmd]
    if str(path) not in argv:
        argv.append(str(path))
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=cfg["nudge"]["timeout_s"])
    except (OSError, subprocess.SubprocessError) as exc:
        return "failed", type(exc).__name__
    tail = (p.stdout + p.stderr).strip().splitlines()[-3:]
    return ("sent" if p.returncode == 0 else "failed"), " | ".join(tail)[:500]


def deliver(root, db, cfg, story, now, kind, text, marker, action):
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    name = "{}-{}-{}.md".format(cfg["pack"], "report" if kind == "report" else "alert", stamp)
    path = root / "outbox" / name
    path.write_text(text, encoding="utf-8")
    status, detail = hand_off(cfg, path, marker, action)
    if status in ("sent", "failed"):
        dest = root / ("sent" if status == "sent" else "failed") / name
        os.replace(path, dest)
        path = dest
    db.execute("INSERT INTO nudges(ts,kind,status,domain,headline,score,signature,hashes,file,detail) VALUES (?,?,?,?,?,?,?,?,?,?)",
               (now, kind, status, story.get("domain", "-") if story else "-", story["headline"] if story else "24h report",
                story["score"] if story else None, json.dumps(story["signature"] if story else []),
                json.dumps(story["hashes"] if story else []), str(path), detail))
    db.commit()
    return status, path, detail


# ----------------------------------------------------------------- entry points
def deferred_nudges(paths, cfg, now):
    """Alerts another pack already pushed (read-only on that pack's db)."""
    out = []
    for other in cfg["defer_to"]:
        path = paths.state / other / (other + ".db")
        if not path.exists():
            continue
        con = sqlite3.connect("file:{}?mode=ro".format(path), uri=True, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute("SELECT id,signature,hashes FROM nudges WHERE kind IN ('alert','test') "
                               "AND status IN ('sent','outbox-only') AND ts>=?",
                               (now - cfg["alert"]["dedup_h"] * 3600,)).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            con.close()
        out += [dict(r, pack=other) for r in rows]
    return out


def _envelopes(link, peer):
    base = link / "lanes" / peer / "outbox"
    for d in (base, base / "sent"):
        if not d.is_dir():
            continue
        for f in d.glob("*.json"):
            try:
                yield json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue


def _iso_ts(text):
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def harvest(db, cfg, now):
    """Map sent nudges to reply threads and record the peer's DONE verdict (read-only)."""
    t = cfg["tuning"]
    if not t["enabled"] or not t.get("link_state") or not t.get("peer"):
        return 0
    link = Path(os.path.expanduser(t["link_state"]))
    pending = db.execute("SELECT id,ts,event_id FROM nudges WHERE kind='alert' AND status='sent' AND verdict IS NULL "
                         "AND ts>=?", (now - 14 * 86400,)).fetchall()
    if not pending or not link.is_dir():
        return 0
    envs = None
    found = 0
    for n in pending:
        eid = n["event_id"]
        if not eid:
            if envs is None:
                envs = sorted((e for e in _envelopes(link, t["peer"])
                               if e.get("action") == cfg["nudge"]["action"] and e.get("marker") == cfg["nudge"]["marker"]),
                              key=lambda e: e.get("seq") or 0)
            taken = {r["event_id"] for r in db.execute("SELECT event_id FROM nudges WHERE event_id IS NOT NULL")}
            for e in envs:
                ts = _iso_ts(e.get("emitted_at"))
                if ts is not None and n["ts"] - 5 <= ts <= n["ts"] + cfg["nudge"]["timeout_s"] + 60 \
                        and e.get("event_id") not in taken:
                    eid = e["event_id"]
                    db.execute("UPDATE nudges SET event_id=? WHERE id=?", (eid, n["id"]))
                    break
        if not eid or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", eid):
            continue
        transcript = link / "threads" / eid / "transcript.jsonl"
        try:
            lines = transcript.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("dir") != "peer" or ev.get("marker") != "DONE":
                continue
            m = VERDICT_RE.search(str(ev.get("text") or "")[:2000])
            if m:
                verdict = m.group(1).lower().replace(" ", "-")
                db.execute("UPDATE nudges SET verdict=?, verdict_ts=? WHERE id=?",
                           (verdict, _iso_ts(ev.get("at")) or now, n["id"]))
                found += 1
                break
    db.commit()
    return found


def tuned_bar(db, cfg):
    """Base threshold plus a bounded bias from the last N verdicts (only with tuning.enabled)."""
    t, base = cfg["tuning"], cfg["alert"]["threshold"]
    rows = db.execute("SELECT verdict FROM nudges WHERE verdict IS NOT NULL ORDER BY verdict_ts DESC LIMIT ?",
                      (int(t["window"]),)).fetchall()
    bias = sum(t["step_up"] if r["verdict"] in ("stale", "already-covered") else -t["step_down"] for r in rows)
    bias = max(-t["max_down"], min(t["max_up"], bias))
    verdicts = {}
    for r in rows:
        verdicts[r["verdict"]] = verdicts.get(r["verdict"], 0) + 1
    bar = min(1.0, max(0.05, base + bias)) if t["enabled"] else base
    return round(bar, 3), round(bias, 3), verdicts


def tick(paths, pack, dry_run=False, test_nudge=False, now=None, packs_dirs=None):
    now = time.time() if now is None else now
    cfg = load(paths, pack, packs_dirs)
    root = home(paths, pack)
    out = {"ts": now, "nudged": [], "stories": 0, "eligible": 0, "top": []}
    if not cfg["enabled"] and not test_nudge:
        out["note"] = "disabled"
        return out
    if not (paths.state / "state.db").is_file():
        raise ValueError("no shared state.db; run trend-watch run before evaluating a pack")
    with locked(root):
        db = own_db(root)
        try:
            if meta(db, "go_live") is None and not dry_run:
                meta(db, "go_live", now)
            if not dry_run:
                out["verdicts_harvested"] = harvest(db, cfg, now)
            bar, bias, verdicts = tuned_bar(db, cfg)
            cfg["_bar"] = bar
            out.update(bar=bar, bias=bias, verdicts=verdicts)
            cfg["finance_first"] = [t.lower() for t in cfg["finance_first_terms"]]
            cfg["finance_domain"] = [t.lower() for t in cfg["finance_domain_terms"]]
            items, run_ts = read_items(paths.state / "state.db", cfg, now)
            coins, momentum = corroborators(items, cfg)
            recent = db.execute("SELECT * FROM nudges WHERE kind IN ('alert','test') AND status IN ('sent','outbox-only') AND ts>=?",
                                (now - cfg["alert"]["dedup_h"] * 3600,)).fetchall()
            deferred = deferred_nudges(paths, cfg, now)
            stories = [evaluate(s, coins, momentum, cfg, now, recent, deferred, bar) for s in cluster(items, cfg)]
            stories = [s for s in stories if s["relevance_hits"]]
            stories.sort(key=lambda s: (-s["score"], s["headline"]))
            eligible = [s for s in stories if s["eligible"]]
            out.update(stories=len(stories), eligible=len(eligible), run_ts=run_ts,
                       top=[{k: s[k] for k in ("score", "headline", "lead_outlet", "domain", "age_min", "fresh_outlets", "tier1", "gates", "parts")} for s in stories[:5]])
            sent_24h = db.execute("SELECT COUNT(*) AS n FROM nudges WHERE kind='alert' AND status IN ('sent','outbox-only') AND ts>=?",
                                  (now - 86400,)).fetchone()["n"]
            budget = max(0, min(cfg["alert"]["max_nudges_per_run"], cfg["alert"]["max_nudges_per_24h"] - sent_24h))
            if eligible and budget == 0:
                out["note"] = "eligible story held: nudge cap reached"
            picks = [] if dry_run else eligible[:budget]
            if test_nudge and not dry_run:
                fresh = [s for s in stories if "not-live" not in s["gates"]
                         and not any(g.startswith(("dup-", "owned-by-", "finance-first")) for g in s["gates"])]
                picks = (eligible or fresh or stories)[:1]
            for story in picks:
                kind = "test" if test_nudge else "alert"
                status, path, detail = deliver(root, db, cfg, story, now, kind, render(story, now, cfg, test=test_nudge),
                                               cfg["nudge"]["marker"], cfg["nudge"]["action"])
                out["nudged"].append({"kind": kind, "status": status, "headline": story["headline"], "score": story["score"],
                                      "file": str(path), "detail": detail})
            if not dry_run:
                summary = json.dumps(out, sort_keys=True)
                db.execute("INSERT OR REPLACE INTO evals VALUES (?,?,?,?,?,?,?,?)",
                           (now, run_ts, len(stories), len(eligible), stories[0]["score"] if stories else None,
                            stories[0]["headline"] if stories else None, len(out["nudged"]), summary))
                db.commit()
                with (root / "log" / (time.strftime("%Y-%m-%d", time.gmtime(now)) + ".jsonl")).open("a", encoding="utf-8") as fh:
                    fh.write(summary + "\n")
                maybe_report(root, db, cfg, now, out)
        finally:
            db.close()
    return out


def report_text(db, cfg, since, now):
    evals = db.execute("SELECT * FROM evals WHERE ts>=? ORDER BY ts", (since,)).fetchall()
    nudges = db.execute("SELECT * FROM nudges WHERE ts>=? AND kind IN ('alert','test') ORDER BY ts", (since,)).fetchall()
    near = {}
    for e in evals:
        for s in json.loads(e["summary"]).get("top", []):
            key = s["headline"]
            if key not in near or s["score"] > near[key]["score"]:
                near[key] = s
    best = sorted(near.values(), key=lambda s: -s["score"])[:5]
    lines = ["# {} trend watch — first 24h".format(cfg["title"]), "",
             "Window: {} → {} · runs evaluated: {} · bar {:.2f}".format(iso(since), iso(now), len(evals), cfg.get("_bar", cfg["alert"]["threshold"])), ""]
    alerts = [n for n in nudges if n["kind"] == "alert"]
    lines.append("Alerts sent: {}".format(len(alerts)) + ("" if alerts else " (quiet — nothing cleared the bar)"))
    for n in nudges:
        lines.append("- {} [{}] {} — {} (score {})".format(iso(n["ts"]), n["kind"], n["status"], n["headline"], n["score"]))
    lines += ["", "Closest stories (best score seen):"]
    for s in best:
        lines.append("- {:.2f} {} — {} ({}; held by: {})".format(s["score"], s["domain"], s["headline"][:110], s["lead_outlet"],
                                                                 ", ".join(s["gates"]) or "none"))
    if not best:
        lines.append("- (none)")
    lines += ["", "— {}-trend-watch · nudge-only.".format(cfg["pack"])]
    return "\n".join(lines) + "\n"


def maybe_report(root, db, cfg, now, out):
    go_live = meta(db, "go_live")
    if go_live is None or meta(db, "first_report_sent") is not None:
        return
    after = cfg["nudge"]["first_report_after_h"]
    if after <= 0 or now - float(go_live) < after * 3600:
        return
    text = report_text(db, cfg, float(go_live), now)
    status, path, _ = deliver(root, db, cfg, None, now, "report", text, cfg["nudge"]["report_marker"], cfg["nudge"]["report_action"])
    if status in ("sent", "outbox-only"):
        meta(db, "first_report_sent", now)
    out["report"] = {"status": status, "file": str(path)}
