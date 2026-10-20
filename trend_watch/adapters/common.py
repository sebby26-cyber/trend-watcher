import html
import re
import xml.etree.ElementTree as ET


def text(value):
    return html.unescape(re.sub(r"<[^>]+>", " ", value or "")).strip()


def rss(payload, prefer_updated=False):
    root = ET.fromstring(payload)
    result = []
    for node in root.iter():
        if node.tag.split("}")[-1] not in ("item", "entry"):
            continue
        fields = {}
        for child in node:
            key = child.tag.split("}")[-1]
            value = "".join(child.itertext()).strip()
            if key == "link":
                if child.get("rel", "alternate") != "alternate":
                    continue
                value = child.get("href", value)
            fields[key] = value
        traffic = fields.get("approx_traffic", "0").replace(",", "").replace("+", "").strip()
        scale = 1000000 if traffic.endswith("M") else 1000 if traffic.endswith("K") else 1
        try:
            score = float(traffic.rstrip("KM")) * scale
        except ValueError:
            score = 0
        result.append({"title": text(fields.get("title", "")), "url": fields.get("link", fields.get("id", "")),
                       "score": score, "rank": len(result) + 1, "extra": {"rank_only": score == 0}, "published": (fields.get("updated") if prefer_updated else None) or fields.get("pubDate") or fields.get("published") or fields.get("date") or fields.get("updated"),
                       "snippet": text(fields.get("description") or fields.get("summary", ""))})
    return result


def hinted(items, ctx, key, value):
    owners = ctx.owners(key, value)
    for item in items:
        item["domain_hint"] = owners
    return items
