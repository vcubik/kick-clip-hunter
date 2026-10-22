"""What the service asks a channel's platform about its stream: whether it is
live and since when, where the stream is, and where the recording of a
stream of it is (its VOD). Each question goes to the platform the channel's
name says it is on (see platforms.py).
"""

from __future__ import annotations

from datetime import datetime

from . import (
    kick_client,
    kick_stream,
    platforms,
    tiktok_client,
    tiktok_stream,
    twitch_client,
    twitch_stream,
    youtube_client,
    youtube_stream,
)
from .config import load_settings
from .platforms import LiveStatus, NoVod, StreamUrlError, StreamUrls, Vod


def _moment(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text.replace("Z", "+00:00")) if text else None


async def live_status(channel: str, broadcaster_user_id: int | None = None) -> LiveStatus:
    """Asks the channel's platform whether it is live. The cheap question:
    a plain API call, no browser.

    A Kick channel whose user id is given is asked for by that rather than
    by its name, which it can change: a channel that renamed itself is then
    found whichever of its names the caller had."""
    platform, login = platforms.split(channel)
    if platform == platforms.KICK:
        return await _kick_live_status(login, broadcaster_user_id)
    if platform == platforms.TWITCH:
        return await _twitch_live_status(login)
    if platform == platforms.YOUTUBE:
        return await _youtube_live_status(login)
    if platform == platforms.TIKTOK:
        return await _tiktok_live_status(login)
    raise ValueError(f"there is no way to tell whether {channel!r} is live")


async def _kick_live_status(slug: str, broadcaster_user_id: int | None) -> LiveStatus:
    settings = load_settings()
    token = await kick_client.get_app_access_token(settings.kick_client_id, settings.kick_client_secret)
    if broadcaster_user_id is not None:
        channel = await kick_client.get_channel_by_id(broadcaster_user_id, token)
    else:
        channel = await kick_client.get_channel_by_slug(slug, token)
    stream = channel.get("stream") or {}
    if not stream.get("is_live"):
        return LiveStatus(live=False)
    return LiveStatus(live=True, started_at=_moment(stream.get("start_time")))


async def _twitch_live_status(login: str) -> LiveStatus:
    settings = load_settings()
    token = await twitch_client.app_token(settings)
    stream = await twitch_client.get_stream(login, settings.twitch_client_id, token)
    if stream is None:
        return LiveStatus(live=False)
    return LiveStatus(live=True, started_at=_moment(stream.get("started_at")))


async def _youtube_live_status(login: str) -> LiveStatus:
    video = await youtube_client.live_video(login)
    if video is None:
        return LiveStatus(live=False)
    return LiveStatus(live=True, started_at=video.started_at)


async def _tiktok_live_status(login: str) -> LiveStatus:
    room = await tiktok_client.room(login)
    return LiveStatus(live=room.live, started_at=room.started_at)


def stream_urls(channel: str) -> StreamUrls:
    """Where a live channel's stream is: its live playlist (TikTok's: the
    stream itself, as FLV) and, where the platform keeps one as the stream
    goes along, its VOD's. Blocks while it is found out; raises
    StreamUrlError if there is no stream to be found."""
    platform, login = platforms.split(channel)
    if platform == platforms.KICK:
        return kick_stream.get_stream_urls(login)
    if platform == platforms.TWITCH:
        return twitch_stream.get_stream_urls(login)
    if platform == platforms.YOUTUBE:
        return youtube_stream.get_stream_urls(login)
    if platform == platforms.TIKTOK:
        return tiktok_stream.get_stream_urls(login)
    raise StreamUrlError(f"there is no way to find the stream of {channel!r}")


def vod_at(channel: str, when: datetime) -> Vod:
    """Where the recording is of the channel's stream that was on at `when`:
    Kick's VOD, Twitch's past broadcast - of a stream that is on or one that
    is over - or, for a YouTube channel, the stream it is streaming, to be
    wound back. Blocks while it is found out, with no browser; raises NoVod,
    in words that say why, where there is none to be had."""
    platform, login = platforms.split(channel)
    if platform == platforms.KICK:
        return kick_stream.vod_at(login, when)
    if platform == platforms.TWITCH:
        return twitch_stream.vod_at(login, when)
    if platform == platforms.YOUTUBE:
        return youtube_stream.vod_at(login, when)
    raise NoVod(f"{platforms.TITLES.get(platform, platform)} keeps no VOD of a stream.")
