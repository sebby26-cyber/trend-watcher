"""Digit-run tripwire: flag 12-19 consecutive digits outside narrow typed-field exemptions.

A value is exempt only when its *field name* is listed in
``tripwire.exempt_fields`` AND the value matches that field's type:

* ``edgar-accession-dashed`` -- ``NNNNNNNNNN-NN-NNNNNN`` (an undashed 18-digit
  accession in an ``accession`` field is NOT exempt);
* ``digest-uid``  -- ``tw:<namespace>:<32 lowercase hex>``;
* ``hex-digest``  -- 64 lowercase hex characters (sha256);
* ``url``         -- an http(s) URL with a host and no whitespace.

Fields in ``tripwire.never_exempt`` (raw external text: snippet, summary,
title, company, ...) are always scanned, even if they contain a URL. In
rendered Markdown, only renderer-produced link targets ``](https://...)`` and
back-quoted digest UIDs are typed fields; external text is escaped by the
renderer, so it cannot forge either form.
"""
import json
import re
import sqlite3
from urllib.parse import urlsplit

TYPES = {
    "edgar-accession-dashed": re.compile(r"^\d{10}-\d{2}-\d{6}$"),
    "digest-uid": re.compile(r"^tw:[a-z_]+:[0-9a-f]{32}$"),
    "hex-digest": re.compile(r"^[0-9a-f]{64}$"),
}
MD_LINK = re.compile(r"(?<!\\)\]\((https?://[^)\s]+)\)")
MD_UID = re.compile(r"(?<!\\)`tw:[a-z_]+:[0-9a-f]{32}`")
R1_TABLES = ("source_items", "observations", "fetch_checkpoints", "stage_runs", "events", "event_revisions",
             "event_entities", "observation_resolutions", "dead_letters", "intake_admissions", "intake_log",
             "breaker_trips", "annotations", "annotation_queue", "llm_reservations", "annotation_attempts",
             "dependency_episodes", "evaluation_requests", "evaluation_attempts", "records",
             "record_supersessions", "record_heads", "acks", "render_log")
LEGACY_TABLES = ("items", "aliases", "scores", "sources", "llm_cache", "budget", "llm_calls", "drafts", "runs", "pending", "signals")


def valid_url(value):
    if any(c.isspace() for c in value):
        return False
    parts = urlsplit(value)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


class Scanner:
    def __init__(self, cfg):
        trip = cfg["tripwire"]
        self.min, self.max = trip["min_digits"], trip["max_digits"]
        self.exempt = dict(trip["exempt_fields"])
        self.never = set(trip["never_exempt"])
        # A run longer than max still contains a min..max run, so any run >= min is a hit.
        self.run = re.compile(r"\d{%d,}" % self.min)

    def exempt_value(self, field, value):
        if field is None or field in self.never or field not in self.exempt or not isinstance(value, str):
            return False
        kind = self.exempt[field]
        return valid_url(value) if kind == "url" else bool(TYPES[kind].match(value))

    def text(self, text, where, hits):
        for match in self.run.finditer(text):
            hits.append({"where": where, "digits": len(match.group(0)), "context": text[max(0, match.start() - 20):match.end() + 20]})

    def value(self, value, field, where, hits):
        if isinstance(value, dict):
            for key, item in value.items():
                self.value(item, key, where + "." + str(key), hits)
        elif isinstance(value, list):
            for n, item in enumerate(value):
                self.value(item, field, "{}[{}]".format(where, n), hits)
        elif isinstance(value, bool) or value is None:
            return
        elif isinstance(value, (int, float)):
            self.text(json.dumps(value), where, hits)
        elif isinstance(value, str):
            if not self.exempt_value(field, value):
                self.text(value, where, hits)

    def markdown(self, text, where, hits):
        stripped = MD_UID.sub("``", MD_LINK.sub("]()", text))
        for n, line in enumerate(stripped.splitlines(), 1):
            self.text(line, "{}:{}".format(where, n), hits)

    def file(self, path, hits):
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix == ".md":
            self.markdown(text, str(path), hits)
        elif path.suffix == ".jsonl":
            for n, line in enumerate(text.splitlines(), 1):
                try:
                    self.value(json.loads(line), None, "{}:{}".format(path, n), hits)
                except ValueError:
                    self.text(line, "{}:{}".format(path, n), hits)
        elif path.suffix == ".json":
            try:
                self.value(json.loads(text), None, str(path), hits)
            except ValueError:
                self.text(text, str(path), hits)
        else:
            self.text(text, str(path), hits)

    def database(self, db_path, hits, tables=R1_TABLES):
        conn = sqlite3.connect("file:{}?mode=ro".format(db_path), uri=True)
        conn.row_factory = sqlite3.Row
        try:
            present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in tables:
                if table not in present:
                    continue
                for row in conn.execute("SELECT rowid AS _rowid, * FROM " + table):
                    for column in row.keys():
                        if column == "_rowid":
                            continue
                        value = row[column]
                        where = "{}:{}#{}.{}".format(db_path.name, table, row["_rowid"], column)
                        if isinstance(value, str) and column.endswith("_json"):
                            try:
                                self.value(json.loads(value), None, where, hits)
                                continue
                            except ValueError:
                                pass
                        self.value(value, column, where, hits)
        finally:
            conn.close()


def scan_paths(cfg, files=(), databases=(), legacy=False):
    scanner, hits = Scanner(cfg), []
    for path in files:
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_file() and child.suffix in (".md", ".json", ".jsonl"):
                    scanner.file(child, hits)
        elif path.is_file():
            scanner.file(path, hits)
    for db in databases:
        if db.exists():
            scanner.database(db, hits, R1_TABLES + (LEGACY_TABLES if legacy else ()))
    return hits
