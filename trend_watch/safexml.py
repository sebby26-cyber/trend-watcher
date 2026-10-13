"""Hardened XML parsing for untrusted feeds (stdlib only).

Policy: the caller caps compressed and decompressed bytes; this module rejects
any DOCTYPE or ENTITY declaration (so no external entities and no entity
expansion bombs), rejects UTF-16/32 and any declared encoding outside a small
allowlist, and bounds element count and nesting depth while parsing
incrementally.
"""
import re
import xml.etree.ElementTree as ET

ALLOWED_ENCODINGS = {"utf-8", "utf8", "iso-8859-1", "latin-1", "latin1", "us-ascii", "ascii"}
DECL = re.compile(rb"^\s*<\?xml[^>]*?encoding\s*=\s*[\"']([A-Za-z0-9._-]+)[\"']")
FORBIDDEN = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.I)


class XMLRejected(ValueError):
    pass


def parse(data, max_elements=50000, max_depth=16, chunk=65536):
    if not isinstance(data, bytes):
        raise XMLRejected("XML input must be bytes")
    if data[:2] in (b"\xff\xfe", b"\xfe\xff") or data[:4] in (b"\x00\x00\xfe\xff", b"\xff\xfe\x00\x00"):
        raise XMLRejected("UTF-16/32 documents are not accepted")
    if FORBIDDEN.search(data):
        raise XMLRejected("DOCTYPE/ENTITY declarations are not accepted")
    match = DECL.match(data)
    encoding = match.group(1).decode("ascii").lower() if match else "utf-8"
    if encoding not in ALLOWED_ENCODINGS:
        raise XMLRejected("unsupported XML encoding " + encoding)
    parser = ET.XMLPullParser(events=("start", "end"))
    depth = elements = 0
    root = None
    try:
        for offset in range(0, len(data), chunk):
            parser.feed(data[offset:offset + chunk])
            for event, element in parser.read_events():
                if event == "start":
                    depth += 1
                    elements += 1
                    if root is None:
                        root = element
                    if depth > max_depth:
                        raise XMLRejected("XML nesting deeper than {}".format(max_depth))
                    if elements > max_elements:
                        raise XMLRejected("XML has more than {} elements".format(max_elements))
                else:
                    depth -= 1
        parser.close()
    except ET.ParseError as exc:
        raise XMLRejected("malformed XML: {}".format(exc))
    if root is None:
        raise XMLRejected("empty XML document")
    return root
