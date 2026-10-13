import urllib.error
from urllib.parse import quote
from .common import hinted, rss
NAME = "reddit"
MIN_INTERVAL_S = 900


def fetch(params, ctx):
    # Count real responses, without transport retries hiding a 429 streak.
    ctx.settings = dict(ctx.settings, request_interval_s=params.get("request_interval_s", 2.0), retries=0)
    items, consecutive_429 = [], 0

    def request(method, url):
        nonlocal consecutive_429
        try:
            result = method(url)
        except urllib.error.HTTPError as exc:
            consecutive_429 = consecutive_429 + 1 if exc.code == 429 else 0
            if exc.code == 429:
                ctx.errors.append("HTTP 429: " + url)
            raise
        except ValueError:
            consecutive_429 = 0  # HTTP succeeded, but the body was not JSON.
            raise
        consecutive_429 = 0
        return result

    for sub in params.get("subreddits", []):
        for listing in params.get("listings", ["hot"]):
            base = "https://www.reddit.com/r/{}/{}".format(quote(sub, safe=""), quote(listing, safe=""))
            try:
                try:
                    data = request(ctx.get_json, base + ".json?limit=25")
                    rows = []
                    for rank, entry in enumerate(data["data"]["children"], 1):
                        d = entry["data"]
                        rows.append({"title": d["title"], "url": d.get("url") or "https://www.reddit.com" + d["permalink"],
                                     "score": d.get("ups", 0), "rank": rank, "published": d.get("created_utc"),
                                     "snippet": d.get("selftext", "")[:1000], "extra": {"subreddit": sub, "listing": listing}})
                except (urllib.error.HTTPError, ValueError, TypeError, KeyError) as exc:
                    if consecutive_429 >= 2:
                        ctx.status = "partial"
                        return items
                    if isinstance(exc, urllib.error.HTTPError) and exc.code not in (403, 429):
                        raise
                    rows = rss(request(ctx.get_text, base + "/.rss?limit=25"), prefer_updated=True)
                    for row in rows:
                        row["score"] = 0
                        row["extra"] = {"rank_only": True, "subreddit": sub, "listing": listing}
                items.extend(hinted(rows, ctx, "subreddits", sub))
            except Exception as exc:
                ctx.errors.append("{}/{}: {}".format(sub, listing, type(exc).__name__))
                if consecutive_429 >= 2:
                    ctx.status = "partial"
                    return items
    if ctx.errors:
        ctx.status = "partial" if items else "error"
    return items
