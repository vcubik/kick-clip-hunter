"""Finds where a live YouTube channel's stream is.

YouTube's API has no way to watch a stream: the address a player follows is
handed to the site's own players, by an API of the site's own. Streamlink
knows how to ask for one - and keeps up with YouTube changing how - so it is
used here for that and nothing else, as for Twitch (twitch_stream.py): told
which video the channel is streaming (youtube_client.live_video), it answers
with the master playlist of that stream, and the recorder follows the stream
from there as it follows any (see recorder.py).

YouTube's live playlists are plainer than Kick's and Twitch's: MPEG-TS
segments of a few seconds, H.264 and AAC, with one program date-time at the
top, for the first segment listed - the recorder carries it forward. They
carry no marks of ads: YouTube's ads are played by the viewer's player, over
the stream rather than in it, and there is no recording of the stream to
stand in for any (no VOD). Their addresses stop working six hours after they
were handed out and say when, which the recorder is told
(StreamUrls.expires_at), to ask for new ones in time.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone

import httpx

from . import youtube_client
from .platforms import YOUTUBE_SITE, StreamUrlError, StreamUrls
from .streamlink_support import NOT_INSTALLED, Streamlink, StreamlinkError

VIDEO_URL = f"{YOUTUBE_SITE}/watch?v={{video_id}}"
# Where an address of YouTube's says when it stops working, in seconds since
# 1970: a part of its path ("/expire/<seconds>/") or of its query.
_EXPIRY = re.compile(r"[/?&]expire[/=](\d{9,11})(?=[/&]|$)")


def _master(url: str) -> str | None:
    """The master playlist streamlink finds at a YouTube address, if any."""
    if Streamlink is None:  # pragma: no cover - see streamlink_support.py
        raise StreamUrlError(NOT_INSTALLED)
    stream = Streamlink().streams(url).get("best")
    return stream.to_manifest_url() if stream is not None else None


def expires_at(url: str) -> datetime | None:
    """When an address of YouTube's stops working, where it says."""
    found = _EXPIRY.search(url)
    return datetime.fromtimestamp(int(found.group(1)), timezone.utc) if found else None


def get_stream_urls(login: str) -> StreamUrls:
    """The master playlist of the stream a channel is streaming, by the name
    it is known by here. Blocks while it is asked for; raises
    StreamUrlError if the channel is not live, or YouTube would not say
    where its stream is."""
    try:
        # A thread of the recorder's own, with no event loop running in it.
        video = asyncio.run(youtube_client.live_video(login))
    except (ValueError, httpx.HTTPError, youtube_client.YouTubeChanged, youtube_client.Refused) as error:
        raise StreamUrlError(f"YouTube did not say what {login!r} is streaming: {error}") from error
    if video is None:
        raise StreamUrlError(f"No live stream found for {login!r} on YouTube - is it live?")
    try:
        live = _master(VIDEO_URL.format(video_id=video.video_id))
    except StreamlinkError as error:
        raise StreamUrlError(f"YouTube did not say where the stream of {login!r} is: {error}") from error
    if live is None:
        raise StreamUrlError(f"YouTube did not say where the stream of {login!r} is")
    return StreamUrls(live=live, expires_at=expires_at(live))
