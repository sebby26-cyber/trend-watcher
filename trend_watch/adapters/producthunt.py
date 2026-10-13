from .common import rss
NAME = "producthunt"
MIN_INTERVAL_S = 1800


def fetch(params, ctx):
    return rss(ctx.get_text("https://www.producthunt.com/feed"))
