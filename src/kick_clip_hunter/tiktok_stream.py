"""Finds where a live TikTok channel's stream is.

TikTok says it itself, to anyone who asks the site's own API about the
channel (tiktok_client.room): no browser, no streamlink. Of the qualities it
serves the stream in, the one the streamer sends is taken where there is one
(tiktok_client.best_stream) - H.264 or HEVC, as they send it, at up to
1080x1920: TikTok's streams are as a rule shot upright.

The stream is had as FLV only - one download that does not end, not a
playlist - which the recorder reads as it arrives and cuts into segments of
its own (StreamUrls.flv, see flv.py and recorder.py). There are no ads in it
and no recording of the stream to fall back on (no VOD). Its address works
for weeks and says until when.
"""

from __future__ import annotations

import asyncio

import httpx

from . import tiktok_client
from .platforms import StreamUrlError, StreamUrls


def get_stream_urls(login: str) -> StreamUrls:
    """Where the stream of a channel is, by the name it is known by on
    TikTok. Blocks while it is asked for; raises StreamUrlError if the
    channel is not live, or TikTok would not say where its stream is."""
    try:
        # A thread of the recorder's own, with no event loop running in it.
        room = asyncio.run(tiktok_client.room(login))
    except (ValueError, httpx.HTTPError, tiktok_client.TikTokChanged) as error:
        raise StreamUrlError(f"TikTok did not say whether {login!r} is live: {error}") from error
    if not room.live:
        raise StreamUrlError(f"No live stream found for {login!r} on TikTok - is it live?")
    stream = tiktok_client.best_stream(room.streams)
    if stream is None:
        raise StreamUrlError(f"TikTok did not say where the stream of {login!r} is")
    return StreamUrls(live=stream.flv, expires_at=tiktok_client.expires_at(stream.flv), flv=True)
