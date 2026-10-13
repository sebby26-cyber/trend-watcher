import math
from urllib.parse import quote
NAME = "kalshi"
MIN_INTERVAL_S = 900


def fetch(params, ctx):
    data = ctx.get_json("https://api.elections.kalshi.com/trade-api/v2/markets?status=open&limit=1000&mve_filter=exclude")
    rows = []
    for market in data["markets"]:
        if market["ticker"].startswith("KXMVE"):
            continue
        volume = market.get("volume_24h_fp")
        if volume is None:
            volume = market.get("volume_24h", 0)
        try:
            score = float(volume or 0)
        except (ValueError, TypeError):
            continue
        if score <= 0 or not math.isfinite(score):
            continue
        extra = {k: market[k] for k in ("ticker", "last_price", "previous_price", "last_price_dollars", "previous_price_dollars") if k in market}
        if market.get("last_price_dollars") is not None and market.get("previous_price_dollars") is not None:
            try:
                extra["price_delta_dollars"] = float(market["last_price_dollars"]) - float(market["previous_price_dollars"])
            except (ValueError, TypeError):
                pass
        title = market.get("event_title") or market["title"]
        subtitle = market.get("yes_sub_title")
        if subtitle and subtitle.casefold() not in title.casefold():
            title += " — " + subtitle
        rows.append({"title": title, "url": market.get("url") or "https://kalshi.com/markets/" + quote(market["ticker"], safe=""),
                     "score": score, "published": market.get("created_time"), "extra": extra})
    rows.sort(key=lambda row: row["score"], reverse=True)
    rows = rows[:params.get("limit", 100)]
    for rank, row in enumerate(rows, 1):
        row["rank"] = rank
    return rows
