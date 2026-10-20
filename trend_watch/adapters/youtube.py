from urllib.parse import quote
from .common import rss, hinted
NAME = "youtube"
MIN_INTERVAL_S = 1800


def fetch(params, ctx):
    origins = [("channel_ids", "channel_id", v) for v in params.get("channel_ids", [])]
    origins += [("playlist_ids", "playlist_id", v) for v in params.get("playlist_ids", [])]
    if not origins:
        ctx.status = "not-configured"
        return []
    return ctx.collect(origins, lambda o: hinted(rss(ctx.get_text("https://www.youtube.com/feeds/videos.xml?" + o[1] + "=" + quote(o[2], safe=""))), ctx, o[0], o[2]))
