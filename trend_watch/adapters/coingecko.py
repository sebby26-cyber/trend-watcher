NAME = "coingecko"
MIN_INTERVAL_S = 900


def fetch(params, ctx):
    data = ctx.get_json("https://api.coingecko.com/api/v3/search/trending")
    return [{"title": c["item"]["name"] + " (" + c["item"]["symbol"] + ")",
             "url": "https://www.coingecko.com/en/coins/" + c["item"]["id"],
             "score": len(data["coins"]) - i, "rank": i + 1,
             "extra": {"symbol": c["item"]["symbol"], "rank_only": True}}
            for i, c in enumerate(data["coins"])]
