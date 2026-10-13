from urllib.parse import quote
from .common import rss, hinted
NAME = "arxiv"
MIN_INTERVAL_S = 3600


def fetch(params, ctx):
    return ctx.collect(params.get("categories", []), lambda c: hinted(rss(ctx.get_text("http://export.arxiv.org/rss/" + quote(c, safe=""))), ctx, "categories", c))
