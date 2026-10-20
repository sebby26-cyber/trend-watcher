"""CoinGecko /coins/markets: free, keyless price velocity for the majors."""
from urllib.parse import quote
NAME = "coingecko_markets"
MIN_INTERVAL_S = 900


def fetch(params, ctx):
    ids = params.get("ids") or ["bitcoin", "ethereum", "solana"]
    data = ctx.get_json("https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&ids="
                        + quote(",".join(ids), safe=",") + "&price_change_percentage=1h,24h")
    rows = []
    for rank, coin in enumerate(data, 1):
        h1 = coin.get("price_change_percentage_1h_in_currency")
        h24 = coin.get("price_change_percentage_24h_in_currency", coin.get("price_change_percentage_24h"))
        h1 = float(h1) if isinstance(h1, (int, float)) else 0.0
        h24 = float(h24) if isinstance(h24, (int, float)) else 0.0
        symbol = str(coin.get("symbol", "")).upper()
        rows.append({"title": "{} ({}) {:+.1f}% 1h, {:+.1f}% 24h".format(coin.get("name", symbol), symbol, h1, h24),
                     "url": "https://www.coingecko.com/en/coins/" + quote(str(coin["id"]), safe=""),
                     "score": abs(h24), "rank": rank,
                     "extra": {"symbol": symbol, "coin_id": coin["id"], "pct_1h": round(h1, 3), "pct_24h": round(h24, 3),
                               "price_usd": coin.get("current_price")}})
    return rows
