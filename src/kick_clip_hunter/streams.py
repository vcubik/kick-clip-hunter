"""What the service asks a channel's platform about its stream: whether it is
live and since when, and where the stream is. Each question goes to the
platform the channel's name says it is on (see platforms.py).
"""

from __future__ import annotations

from datetime import datetime

from . import kick_client, kick_stream, platforms
from .config import load_settings
from .platforms import LiveStatus, StreamUrlError, StreamUrls


async def live_status(channel: str) -> LiveStatus:
    """Asks the channel's platform whether it is live. The cheap question:
    a plain API call, no browser."""
    platform, login = platforms.split(channel)
    if platform == platforms.KICK:
        return await _kick_live_status(login)
    raise ValueError(f"there is no way to tell whether {channel!r} is live")


async def _kick_live_status(slug: str) -> LiveStatus:
    settings = load_settings()
    token = await kick_client.get_app_access_token(settings.kick_client_id, settings.kick_client_secret)
    stream = (await kick_client.get_channel_by_slug(slug, token)).get("stream") or {}
    if not stream.get("is_live"):
        return LiveStatus(live=False)
    start = stream.get("start_time")
    return LiveStatus(live=True, started_at=datetime.fromisoformat(start.replace("Z", "+00:00")) if start else None)


def stream_urls(channel: str) -> StreamUrls:
    """Where a live channel's stream is: its live playlist and, where the
    platform keeps one as the stream goes along, its VOD's. Blocks while it
    is found out; raises StreamUrlError if there is no stream to be found."""
    platform, login = platforms.split(channel)
    if platform == platforms.KICK:
        return kick_stream.get_stream_urls(login)
    raise StreamUrlError(f"there is no way to find the stream of {channel!r}")
