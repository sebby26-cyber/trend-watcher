"""Deterministic cross-platform identity and early-signal scoring."""
import hashlib
import json
import math
import re
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit
from .serialize import dumps

STOP = set("a an the and or but for to of in on at by with from is are was were be it this that as new now how why what who".split())
TRACKING = {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "ref", "ref_src", "source"}


def normalize_url(url):
    parsed = urlsplit(url if "://" in url else "https://" + url)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    host = re.sub(r":(?:80|443)$", "", host)
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if not k.lower().startswith("utm_") and k.lower() not in TRACKING]
    return host + parsed.path.rstrip("/") + ("?" + urlencode(sorted(query)) if query else "")


def normalize_title(title):
    return " ".join(t for t in re.findall(r"\w+", title.lower()) if t not in STOP)


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def timestamp(value):
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) else None
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(str(value))
        except (ValueError, TypeError, OverflowError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def matches(word, text):
    return bool(re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", text, flags=re.I))


def compile_domains(domains):
    """One boundary regex per domain, reused for matching and signal extraction."""
    result = []
    for domain in domains:
        keywords = sorted(set(domain["keywords"]), key=len, reverse=True)
        exclude = sorted(set(domain["exclude"]), key=len, reverse=True)
        branches = []
        if exclude:
            branches.append("(?P<exclude>" + "|".join(map(re.escape, exclude)) + ")")
        if keywords:
            branches.append("(?P<keyword>" + "|".join(map(re.escape, keywords)) + ")")
        pattern = re.compile(r"(?=(?<!\w)(?:" + "|".join(branches) + r")(?!\w))", re.I) if branches else None
        # Preserve nested keyword phrases when the longest alternative wins.
        expansions = {k.lower(): {w.lower() for w in keywords if matches(w, k)} for k in keywords}
        result.append((domain["name"], pattern, expansions))
    return result


def domain_matches(item, compiled):
    body = item["title"] + " " + (item.get("snippet") or "")
    hints = item.get("domain_hint", [])
    if isinstance(hints, str):
        hints = [hints]
    assigned, keywords = [], set()
    for name, pattern, expansions in compiled:
        found, veto = set(), False
        if pattern:
            for match in pattern.finditer(body):
                if "exclude" in pattern.groupindex and match.group("exclude"):
                    veto = True
                    break
                found.update(expansions.get(match.group("keyword").lower(), set()))
        if not veto and (found or name in hints):
            assigned.append(name)
            keywords.update(found)
    return assigned, keywords


def assign_domains(item, domains):
    return domain_matches(item, compile_domains(domains))[0]


def signal_keys(item, domains, keywords=None):
    words = [w for w in re.findall(r"\w+", item["title"]) if w.lower() not in STOP]
    salient = {w.lower() for w in words if w[0].isupper() and len(w) > 1}
    salient.update(a.lower() + " " + b.lower() for a, b in zip(words, words[1:]))
    if keywords is None:
        keywords = domain_matches(item, compile_domains([d for d in domains if d["name"] in item["domains"]]))[1]
    # A shared broad keyword alone is not evidence of the same event.
    return {k + ":" + s for k in keywords for s in salient if s != k} | {"title:" + s for s in salient if " " in s}


def percentile(value, values):
    if not values:
        return 0.0
    return sum(v <= value for v in values) / len(values)


def velocity(score, rank, previous, now, rank_only=False):
    if not previous or now <= previous["ts"]:
        return 0.0
    hours = (now - previous["ts"]) / 3600
    if rank_only and rank is not None and previous["rank"] is not None:
        return (previous["rank"] - rank) / hours
    return (score - previous["score"]) / hours


def composite(rank_pct, velocity_pct, cooccurrence, age, rising, cfg):
    fresh = cfg["freshness"]
    if age <= fresh["max_age_h"]:
        freshness = 1.0 if rising else 0.5
        decay = 1.0
    else:
        decay = math.exp(-(age - fresh["max_age_h"]) / max(1, fresh["max_age_h"]))
        freshness = 0.5 * decay
    values = dict(rank_pct=rank_pct, velocity_pct=velocity_pct, cooccurrence=cooccurrence, freshness=freshness)
    weights = cfg["weights"]
    return max(0.0, min(1.0, sum(weights[k] * values[k] for k in weights) / sum(weights.values()) * decay))


def score_items(raw, domains, cfg, db, now):
    compiled = compile_domains(domains)
    grouped, observations, old_items = {}, {}, {}
    aliases, titles, keywords = {}, {}, defaultdict(set)
    item_rows, alias_rows = [], []
    for row in raw:
        try:
            native = float(row.get("score") or 0)
            if not math.isfinite(native):
                continue
            rank = int(row["rank"]) if row.get("rank") is not None else None
            url_hash = digest(normalize_url(row["url"]))
            title_hash = digest(normalize_title(row["title"]) or normalize_url(row["url"]))
            # Persistent URL aliases take precedence over within-run title matches.
            hash_ = aliases.get(url_hash) or db.canonical(url_hash, title_hash, titles.get(title_hash))
            if hash_ not in old_items:
                old_items[hash_] = db.item(hash_)
            old = old_items[hash_]
            published = timestamp(row.get("published"))
            first_seen = old["first_seen"] if old else now
            if old and old["published"] is not None:
                published = min(old["published"], published) if published is not None else old["published"]
            if hash_ not in grouped:
                grouped[hash_] = dict(row, hash=hash_, title_hash=title_hash, first_seen=first_seen,
                                     published=published, platforms=[], domains=[], velocity=0.0)
                item_rows.append((hash_, title_hash, row["title"], row["url"], row["platform"], first_seen, now, published, "[]", "{}"))
            item = grouped[hash_]
            if published is not None:
                item["published"] = min(item["published"], published) if item["published"] is not None else published
            item["snippet"] = item.get("snippet") or ""
            platform = row["platform"]
            if platform not in item["platforms"]:
                item["platforms"].append(platform)
            assigned, found = domain_matches(row, compiled)
            item["domains"] = sorted(set(item["domains"]) | set(assigned))
            keywords[hash_].update(found)
            key = (hash_, platform)
            prev = db.previous(hash_, platform)
            vel = velocity(native, rank, prev, now, bool((row.get("extra") or {}).get("rank_only")) or (native == 0 and rank is not None and (not prev or prev["score"] == 0)))
            obs = observations.get(key)
            if obs is None or native > obs["native"]:
                observations[key] = {"native": native, "rank": rank, "velocity": vel}
            aliases[url_hash] = hash_
            titles.setdefault(title_hash, hash_)
            alias_rows.append((url_hash, hash_))
        except (ValueError, TypeError, KeyError):
            continue
    by_platform = defaultdict(list)
    keys_by_item = {h: signal_keys(i, domains, keywords[h]) for h, i in grouped.items()}
    signal_rows, score_rows = [], []
    current_signals = defaultdict(set)
    for (hash_, platform), obs in observations.items():
        by_platform[platform].append(obs)
        score_rows.append((hash_, platform, now, obs["native"], obs["rank"]))
        for key in keys_by_item[hash_]:
            current_signals[key].add(platform)
            signal_rows.append((key, platform, now))
    pools = {p: (sorted(-o["rank"] if o["rank"] is not None else o["native"] for o in pool),
                 sorted(o["velocity"] for o in pool)) for p, pool in by_platform.items()}
    # Read each historical window once; every item/key lookup is then O(1).
    windows = {d["freshness"]["cooccurrence_h"] for d in domains}
    historical = list(db.execute("SELECT key,platform,MAX(ts) AS ts FROM signals WHERE ts>=? GROUP BY key,platform",
                                (now - max(windows, default=0) * 3600,))) if windows else []
    indexes = {}
    for window in windows:
        index = defaultdict(set)
        for row in historical:
            if row["ts"] >= now - window * 3600:
                index[row["key"]].add(row["platform"])
        for key, platforms in current_signals.items():
            index[key].update(platforms)
        indexes[window] = index
    updates = []
    for hash_, item in grouped.items():
        age = max(0, (now - (item["published"] if item["published"] is not None else item["first_seen"])) / 3600)
        item["age_hours"] = age
        metrics = []
        for platform in item["platforms"]:
            obs = observations[(hash_, platform)]
            ranks, velocities = pools[platform]
            rank_value = -obs["rank"] if obs["rank"] is not None else obs["native"]
            v_pct = bisect_right(velocities, obs["velocity"]) / len(velocities) if obs["velocity"] > 0 else 0
            metrics.append((bisect_right(ranks, rank_value) / len(ranks), v_pct, obs["velocity"]))
        item["velocity"] = max(m[2] for m in metrics)
        item["velocity_note"] = "{:+.1f}/h".format(item["velocity"])
        domain_scores = {}
        for domain in domains:
            if domain["name"] not in item["domains"]:
                continue
            index = indexes[domain["freshness"]["cooccurrence_h"]]
            co = int(any(len(index[key]) >= 2 for key in keys_by_item[hash_]))
            domain_scores[domain["name"]] = composite(max(m[0] for m in metrics), max(m[1] for m in metrics), co, age, item["velocity"] > 0, domain)
        item["domain_scores"] = domain_scores
        item["score"] = max(domain_scores.values(), default=0.0)
        updates.append((now, item["published"], json.dumps(item["domains"]), dumps(item), hash_))
    with db.conn:
        db.conn.executemany("INSERT OR IGNORE INTO items(hash,title_hash,title,url,platform,first_seen,last_seen,published,domains,data_json) VALUES (?,?,?,?,?,?,?,?,?,?)", item_rows)
        db.conn.executemany("INSERT OR IGNORE INTO aliases VALUES (?,?)", alias_rows)
        db.conn.executemany("INSERT INTO signals VALUES (?,?,?)", signal_rows)
        db.conn.executemany("INSERT INTO scores VALUES (?,?,?,?,?)", score_rows)
        db.conn.executemany("UPDATE items SET last_seen=?,published=?,domains=?,data_json=? WHERE hash=?", updates)
    return list(grouped.values())


def select_candidates(items, domains, db, limit):
    configs = {d["name"]: d for d in domains}
    candidates = []
    counts = defaultdict(int)
    for item in sorted(items, key=lambda i: (-i["score"], i["hash"])):
        old = db.item(item["hash"])
        if old["candidate_at"] is not None or old["llm_at"] is not None or db.cache(item["hash"]):
            continue
        eligible = [name for name in item["domains"] if name in configs
                    and item["age_hours"] <= configs[name]["freshness"]["hard_max_age_h"]
                    and item["domain_scores"][name] >= configs[name]["thresholds"]["candidate_score"]
                    and counts[name] < configs[name]["max_candidates"]]
        if eligible and len(candidates) < limit:
            candidates.append(item)
            for name in eligible:
                counts[name] += 1
    return candidates
