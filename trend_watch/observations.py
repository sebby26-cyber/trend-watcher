"""Immutable observations for every adapter fetch (spec fix 1).

``source_items(source, external_id)`` is the stable identity of a thing a
source publishes; ``observations(source_item_id, payload_hash)`` is one
immutable payload version of it. An identical refetch only advances
``source_items.last_seen_at``/``sightings``; a changed payload is a new
observation row. Database triggers reject UPDATE/DELETE on observations.

The caller owns the transaction: ``record`` never commits, so an adapter's
conditional-fetch validators can be committed atomically with its observations.
"""
from .ids import iso, payload_hash, uid
from .scoring import normalize_url, timestamp
from .serialize import canonical


def record(db, source, external_id, payload, now, published_at=None, fetch_uid=None):
    """Returns ``(source_item_id, observation_id, created)``."""
    at = iso(now)
    row = db.execute("SELECT id FROM source_items WHERE source=? AND external_id=?", (source, external_id)).fetchone()
    if row is None:
        cursor = db.execute("INSERT INTO source_items(uid,source,external_id,first_seen_at,last_seen_at,sightings) VALUES (?,?,?,?,?,1)",
                            (uid("src", source, external_id), source, external_id, at, at))
        item_id = cursor.lastrowid
    else:
        item_id = row[0]
        db.execute("UPDATE source_items SET last_seen_at=?, sightings=sightings+1 WHERE id=?", (at, item_id))
    digest = payload_hash(payload)
    existing = db.execute("SELECT id FROM observations WHERE source_item_id=? AND payload_hash=?", (item_id, digest)).fetchone()
    if existing:
        return item_id, existing[0], False
    source_uid = db.execute("SELECT uid FROM source_items WHERE id=?", (item_id,)).fetchone()[0]
    cursor = db.execute("INSERT INTO observations(uid,source_item_id,payload_hash,payload_json,fetched_at,published_at,fetch_uid) VALUES (?,?,?,?,?,?,?)",
                        (uid("obs", source_uid, digest), item_id, digest, canonical(payload), at,
                         iso(published_at) if published_at is not None else None, fetch_uid))
    return item_id, cursor.lastrowid, True


def legacy_payload(row):
    """Content fields only; volatile metrics (score/rank/prices) stay in the ``scores`` time series."""
    hint = row.get("domain_hint", [])
    return {"title": row["title"], "url": row["url"], "snippet": row.get("snippet") or "",
            "published": row.get("published") if isinstance(row.get("published"), str) else timestamp(row.get("published")),
            "domain_hint": hint if isinstance(hint, list) else [hint]}


def record_legacy_fetch(db, raw, now):
    """Observe every valid row the trend adapters returned this tick, in one transaction."""
    created = 0
    fetch_uid = uid("fetch", "legacy", iso(now))
    with db.conn:
        for row in raw:
            published = timestamp(row.get("published"))
            _, _, new = record(db, row["platform"], normalize_url(row["url"]), legacy_payload(row), now, published, fetch_uid)
            created += int(new)
    return created
