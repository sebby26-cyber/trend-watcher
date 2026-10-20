"""Stable external identifiers.

Internal rows use integer surrogate keys. Anything addressed from outside the
database (records, events, observations) gets a namespaced digest UID built
from a canonical, length-prefixed encoding of its identity inputs, so that
("ab", "c") and ("a", "bc") can never collide. Environment (shadow/production)
is deliberately *not* an input to event or observation UIDs; it belongs to
consumer records only.
"""
import hashlib
from datetime import datetime, timezone
from .serialize import canonical

UID_HEX = 32


def encode(*parts):
    out = bytearray()
    for part in parts:
        if part is None:
            out += b"n;"
        elif isinstance(part, bool):
            raise TypeError("booleans are not identity inputs")
        elif isinstance(part, int):
            out += b"i" + str(part).encode("ascii") + b";"
        elif isinstance(part, str):
            raw = part.encode("utf-8")
            out += b"s" + str(len(raw)).encode("ascii") + b":" + raw
        elif isinstance(part, (list, tuple)):
            out += b"l" + str(len(part)).encode("ascii") + b":" + encode(*part)
        else:
            raise TypeError("unsupported identity input: " + type(part).__name__)
    return bytes(out)


def digest(*parts):
    return hashlib.sha256(encode(*parts)).hexdigest()


def uid(namespace, *parts):
    return "tw:{}:{}".format(namespace, digest(namespace, *parts)[:UID_HEX])


def payload_hash(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def iso(ts):
    """UTC second-precision ISO-8601; never epoch milliseconds (tripwire)."""
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def utc_day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()


def next_utc_midnight(ts):
    day = int(ts // 86400)
    return float((day + 1) * 86400)
