NAME = "hackernews"
MIN_INTERVAL_S = 900
NEEDS_KEY = False


def fetch(params, ctx):
    limit = int(params.get("limit", 60))
    data = ctx.get_json("https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=" + str(limit))
    return [{"title": h.get("title", ""), "url": h.get("url") or "https://news.ycombinator.com/item?id=" + h["objectID"],
             "score": h.get("points") or 0, "rank": i + 1, "published": h.get("created_at"),
             "extra": {"comments": h.get("num_comments", 0)}} for i, h in enumerate(data["hits"][:limit])]
