from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from .common import hinted
NAME = "github_trending"
MIN_INTERVAL_S = 1800


def fetch(params, ctx):
    # Unauthenticated search is limited to 10 requests/minute, including retries.
    ctx.settings["request_interval_s"] = max(6.1, ctx.settings.get("request_interval_s", 0))
    since = (datetime.now(timezone.utc) - timedelta(days=params.get("days", 7))).date().isoformat()
    def one(topic):
        query = "created:>" + since + (" topic:" + topic if topic else "")
        data = ctx.get_json("https://api.github.com/search/repositories?" + urlencode({"q": query, "sort": "stars", "order": "desc", "per_page": 20}))
        return hinted([{"title": r["full_name"], "url": r["html_url"], "score": r["stargazers_count"],
                        "rank": i + 1, "published": r.get("created_at"), "snippet": r.get("description") or ""}
                       for i, r in enumerate(data["items"])], ctx, "topics", topic)
    return ctx.collect([""] + params.get("topics", []), one)
