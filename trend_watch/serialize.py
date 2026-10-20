"""Serialization boundary helpers: floats are rounded only when written out.

Scoring math keeps full precision in memory; anything persisted (JSON columns,
JSONL feeds, run logs) passes through ``dumps`` so binary-float expansions such
as repr(0.1 + 0.2) never produce long digit runs on disk.
"""
import json
import math

FLOAT_DIGITS = 6


def round_floats(value, digits=FLOAT_DIGITS):
    if isinstance(value, float):
        return round(value, digits) if math.isfinite(value) else value
    if isinstance(value, dict):
        return {k: round_floats(v, digits) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [round_floats(v, digits) for v in value]
    return value


def dumps(value, **kwargs):
    return json.dumps(round_floats(value), **kwargs)


def canonical(value):
    """Deterministic JSON used for hashing: sorted keys, no whitespace, rounded floats."""
    return json.dumps(round_floats(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
