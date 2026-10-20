from urllib.parse import quote
from .common import rss, hinted
NAME = "google_news"
MIN_INTERVAL_S = 900


def fetch(params, ctx):
    return ctx.collect(params.get("queries", []), lambda q: hinted(rss(ctx.get_text("https://news.google.com/rss/search?q=" + quote(q, safe="") + "&hl=en-US&gl=US&ceid=US:en")), ctx, "queries", q))
