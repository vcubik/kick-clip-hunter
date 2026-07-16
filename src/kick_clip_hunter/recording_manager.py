"""Background management of one ChannelRecorder per watched channel.

Before doing anything that touches a browser, each tick checks is_live via
the official (Cloudflare-free) Kick public API - the cheap, no-browser way
to answer "is there any point recording this channel right now." Only a
channel confirmed live gets its recorder ticked, which is what actually
launches the headed patchright browser to (re)fetch the stream URL. Without
this check, an offline watchlist entry would pop a browser window open
every TICK_INTERVAL_SECONDS forever, since ChannelRecorder.tick() alone has
no way to know a channel is offline until it's already tried and failed.
"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from .config import load_settings
from .kick_client import get_app_access_token, get_channel_by_slug
from .kick_stream import StreamUrlError
from .recorder import ChannelRecorder, RecorderError, extract_clip

logger = logging.getLogger("kick_clip_hunter")

TICK_INTERVAL_SECONDS = 30

_recorders: dict[str, ChannelRecorder] = {}


def _get_recorder(channel_slug: str) -> ChannelRecorder:
    if channel_slug not in _recorders:
        _recorders[channel_slug] = ChannelRecorder(channel_slug)
    return _recorders[channel_slug]


async def _is_live(channel_slug: str) -> bool:
    settings = load_settings()
    token = await get_app_access_token(settings.kick_client_id, settings.kick_client_secret)
    channel = await get_channel_by_slug(channel_slug, token)
    return bool((channel.get("stream") or {}).get("is_live"))


def _tick_one(channel_slug: str) -> None:
    recorder = _get_recorder(channel_slug)
    try:
        recorder.tick()
    except StreamUrlError:
        logger.info("[%s] stream ended between the is_live check and recording", channel_slug)


async def run_forever(get_channel_slugs, recording_enabled=lambda: True) -> None:
    while True:
        if not recording_enabled():
            # Recording paused from the dashboard: tear down any running
            # ffmpeg processes and don't touch a browser until it's back on.
            for recorder in _recorders.values():
                if recorder.is_active:
                    await asyncio.to_thread(recorder.stop)
        else:
            for slug in get_channel_slugs():
                try:
                    if await _is_live(slug):
                        await asyncio.to_thread(_tick_one, slug)
                    elif _get_recorder(slug).is_active:
                        await asyncio.to_thread(_get_recorder(slug).stop)
                except Exception:
                    logger.exception("[%s] recording tick failed", slug)
        await asyncio.sleep(TICK_INTERVAL_SECONDS)


async def create_clip_for_moment(
    channel_slug: str, window_start: datetime, window_end: datetime, output_name: str
) -> Path:
    recorder = _get_recorder(channel_slug)
    return await asyncio.to_thread(extract_clip, recorder, window_start, window_end, output_name)


__all__ = ["run_forever", "create_clip_for_moment", "RecorderError"]
