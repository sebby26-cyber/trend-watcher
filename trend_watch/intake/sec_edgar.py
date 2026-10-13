"""SEC EDGAR "Latest Filings" 8-K Atom intake (primary source, not a trend adapter).

Verified 2026-10-04 (see docs/R1-SPEC-DELTAS.md): SEC fair access allows at
most 10 requests/second and requires a declared User-Agent carrying a contact
email; an undeclared tool receives HTTP 403 "Undeclared Automated Tool". The
``getcurrent`` Atom feed is ISO-8859-1; each entry carries the form type
(category term), "<form> - <company> (<10-digit CIK>) (<role>)" title, an
index-page link, an HTML summary with Filed / AccNo / Size and one
"Item N.NN: description" line per reported item, and an id of the form
``urn:tag:sec.gov,2008:accession-number=NNNNNNNNNN-NN-NNNNNN``. The feed has
no field naming the filing an 8-K/A amends.

Network rules: https only, a finite host allowlist for the URL and every
redirect, a compressed-size cap and a decompressed-size cap, conditional GET
(If-None-Match / If-Modified-Since) when validators were stored, and refusal to
run live while the user agent still carries the contact placeholder.
"""
import re
import time
import urllib.error
import urllib.request
import zlib
from urllib.parse import urlsplit
from ..adapters.common import text as strip_html
from .. import safexml

SOURCE = "sec_edgar_8k"
PLACEHOLDER = "<CONTACT_EMAIL_REQUIRED>"
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ACCESSION = re.compile(r"^\d{10}-\d{2}-\d{6}$")
TITLE = re.compile(r"^(?P<form>.+?) - (?P<company>.+) \((?P<cik>[^()]*)\) \((?P<role>[^()]+)\)\s*$")
ITEM = re.compile(r"Item\s+(\d{1,2}\.\d{2})\s*:?\s*([^\n]*)")
CONTENT_TYPES = ("application/atom+xml", "application/xml", "text/xml")
ATOM = "{http://www.w3.org/2005/Atom}"


class FeedError(Exception):
    """A fetch failed in a way that should count against the source (status error)."""


class ConfigError(Exception):
    """The adapter is not allowed to run with the current configuration."""


def check_user_agent(user_agent):
    if PLACEHOLDER in user_agent or not EMAIL.search(user_agent) or "example.invalid" in user_agent:
        raise ConfigError("intake.sec_edgar.user_agent must declare a real contact email (SEC fair access); "
                          "replace " + PLACEHOLDER + " before enabling live fetches")


def check_host(url, hosts):
    parts = urlsplit(url)
    if parts.scheme != "https" or (parts.hostname or "").lower() not in hosts:
        raise FeedError("URL not on the SEC host allowlist: " + (parts.hostname or "?"))


class AllowlistRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts):
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_host(newurl, self.hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def live_opener(hosts):
    return urllib.request.build_opener(AllowlistRedirect(hosts)).open


class RateLimiter:
    """Minimum spacing between requests to the SEC host (<= 10 req/s)."""

    def __init__(self, interval_s, sleep=time.sleep, clock=time.monotonic):
        self.interval, self.sleep, self.clock = max(0.1, interval_s), sleep, clock
        self.last = None

    def wait(self):
        if self.last is not None:
            delay = self.interval - (self.clock() - self.last)
            if delay > 0:
                self.sleep(delay)
        self.last = self.clock()


def read_capped(stream, cap):
    data = stream.read(cap + 1)
    if len(data) > cap:
        raise FeedError("response larger than {} bytes".format(cap))
    return data


def decompress(data, encoding, cap):
    encoding = (encoding or "identity").strip().lower()
    if encoding in ("", "identity"):
        if len(data) > cap:
            raise FeedError("decompressed size exceeds cap")
        return data
    if encoding not in ("gzip", "x-gzip", "deflate"):
        raise FeedError("unsupported content-encoding " + encoding)
    modes = [16 + zlib.MAX_WBITS] if "gzip" in encoding else [zlib.MAX_WBITS, -zlib.MAX_WBITS]
    for wbits in modes:
        try:
            engine = zlib.decompressobj(wbits)
            out = engine.decompress(data, cap + 1)
            if len(out) > cap or engine.unconsumed_tail:
                raise FeedError("decompressed size exceeds cap")
            out += engine.flush()
            if len(out) > cap:
                raise FeedError("decompressed size exceeds cap")
            return out
        except zlib.error:
            continue
    raise FeedError("corrupt compressed body")


class FetchResult:
    def __init__(self, status, entries, etag=None, last_modified=None, http_status=None, size=0):
        self.status, self.entries = status, entries
        self.etag, self.last_modified, self.http_status, self.size = etag, last_modified, http_status, size


def fetch(settings, checkpoint=None, opener=None, limiter=None):
    """One conditional GET of the feed. ``opener`` is injectable; ``None`` means live network."""
    url = settings["feed_url"]
    hosts = [h.lower() for h in settings["allowed_hosts"]]
    check_host(url, hosts)
    if opener is None:
        check_user_agent(settings["user_agent"])
        opener = live_opener(hosts)
    headers = {"User-Agent": settings["user_agent"], "Accept-Encoding": "gzip, deflate",
               "Accept": "application/atom+xml, application/xml;q=0.9, text/xml;q=0.8"}
    if checkpoint is not None:
        if checkpoint.get("etag"):
            headers["If-None-Match"] = checkpoint["etag"]
        if checkpoint.get("last_modified"):
            headers["If-Modified-Since"] = checkpoint["last_modified"]
    (limiter or RateLimiter(settings["request_interval_s"])).wait()
    try:
        response = opener(urllib.request.Request(url, headers=headers), timeout=settings["timeout_s"])
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return FetchResult("not_modified", [], checkpoint.get("etag") if checkpoint else None,
                               checkpoint.get("last_modified") if checkpoint else None, 304)
        raise FeedError("HTTP {}".format(exc.code))
    with response:
        status = getattr(response, "status", None) or response.getcode() or 200
        if status == 304:
            return FetchResult("not_modified", [], checkpoint.get("etag") if checkpoint else None,
                               checkpoint.get("last_modified") if checkpoint else None, 304)
        final = response.geturl() if hasattr(response, "geturl") else url
        check_host(final or url, hosts)
        info = response.headers
        ctype = (info.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype not in CONTENT_TYPES:
            raise FeedError("unexpected content type " + (ctype or "(none)"))
        raw = read_capped(response, int(settings["max_bytes"]))
        body = decompress(raw, info.get("Content-Encoding"), int(settings["max_decompressed_bytes"]))
    entries = parse_feed(body, settings)
    return FetchResult("ok", entries, info.get("ETag"), info.get("Last-Modified"), status, len(body))


def child_text(node, name):
    found = node.find(ATOM + name)
    if found is None:
        found = node.find(name)
    return "".join(found.itertext()).strip() if found is not None else ""


def parse_feed(body, settings):
    root = safexml.parse(body, int(settings.get("max_elements", 50000)), int(settings.get("max_depth", 16)))
    if root.tag not in (ATOM + "feed", "feed"):
        raise FeedError("not an Atom feed")
    entries = []
    for entry in list(root.findall(ATOM + "entry")) + list(root.findall("entry")):
        entries.append(normalize_entry(entry))
    return entries


def normalize_entry(entry):
    """Return the observation payload. Values are kept as published; validation happens in resolve."""
    title = " ".join(child_text(entry, "title").split())
    summary_html = child_text(entry, "summary")
    lines = re.sub(r"(?i)<br\s*/?>", "\n", summary_html)
    summary = "\n".join(strip_html(line) for line in lines.split("\n") if strip_html(line))
    form = None
    for category in list(entry.findall(ATOM + "category")) + list(entry.findall("category")):
        if (category.get("label") or "").lower() == "form type" and category.get("term"):
            form = category.get("term").strip()
    match = TITLE.match(title)
    company = cik = role = None
    if match:
        form = form or match.group("form").strip()
        company, cik, role = match.group("company").strip(), match.group("cik").strip(), match.group("role").strip()
    entry_id = child_text(entry, "id")
    accession = None
    found = re.search(r"accession-number=([0-9-]+)$", entry_id)
    if found:
        accession = found.group(1)
    else:
        found = re.search(r"AccNo:\s*([0-9-]+)", summary)
        accession = found.group(1) if found else None
    filed = re.search(r"Filed:\s*(\d{4}-\d{2}-\d{2})", summary)
    link = None
    for node in list(entry.findall(ATOM + "link")) + list(entry.findall("link")):
        if node.get("rel", "alternate") == "alternate" and node.get("href"):
            link = node.get("href")
    items = [{"number": number, "label": label.strip()} for number, label in ITEM.findall(summary)]
    return {"form": form, "company": company, "cik": cik, "role": role, "accession": accession,
            "filed_date": filed.group(1) if filed else None, "updated": child_text(entry, "updated") or None,
            "items": items, "url": link, "entry_id": entry_id, "title": title, "summary": summary[:2000]}


def external_id(payload):
    """Source-item identity: one accession can be listed once per co-registrant CIK."""
    return "{}|{}".format(payload.get("accession") or ("entry:" + (payload.get("entry_id") or payload.get("title") or "?")),
                          payload.get("cik") or "?")
