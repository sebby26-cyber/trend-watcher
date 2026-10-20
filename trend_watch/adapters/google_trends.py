from urllib.parse import quote
from .common import rss, hinted
NAME = "google_trends"
MIN_INTERVAL_S = 1800


def fetch(params, ctx):
    geos = params.get("geo", "US")
    if isinstance(geos, str):
        geos = [geos]
    return ctx.collect(geos, lambda geo: hinted(rss(ctx.get_text("https://trends.google.com/trending/rss?geo=" + quote(geo))), ctx, "geo", geo))
