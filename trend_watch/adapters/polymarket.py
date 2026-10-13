NAME = "polymarket"
MIN_INTERVAL_S = 900


def fetch(params, ctx):
    data = ctx.get_json("https://gamma-api.polymarket.com/markets?active=true&closed=false&order=volume24hr&ascending=false&limit=50")
    return [{"title": m["question"], "url": "https://polymarket.com/market/" + m["slug"],
             "score": float(m.get("volume24hr") or 0), "published": m.get("createdAt"),
             "extra": {k: m[k] for k in ("price", "outcomePrices", "oneDayPriceChange") if k in m}}
            for m in data]
