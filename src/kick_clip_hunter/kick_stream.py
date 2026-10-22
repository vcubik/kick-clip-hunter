"""Fetches a currently-live channel's real HLS playback URL - and where the
recording of any of its streams is (its VOD).

The playback_url embedded in a channel page's server-rendered HTML fails
AWS IVS signature verification when used directly (confirmed by testing) -
only the URL the page's own player actually requests, once it's loaded and
initialized, works. Capturing that live network request requires a headed
`patchright` browser (the page does not load reliably in a headless one)
using the persisted session from kick_session.py.

While that browser is open it also looks up the stream's own recording (its
VOD), which Kick writes as the broadcast goes along and serves from a host a
plain HTTP client can read. The recorder uses it to replace footage that an
ad stitched into the live playlist covered up. That look has come back empty
for whole streams although the VOD was there, so the recorder looks again
later with `vod_at`, which asks kick.com's list of a channel's streams over
plain HTTP, no browser (it answers one, as of October 2026) - and which is
also where footage of a stream that is over is found (see vod_footage.py).
A VOD's address carries no token and does not run out: one from weeks ago
still plays, as long as the channel keeps it.
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone

import httpx
from patchright.sync_api import sync_playwright

from . import kick_client
from .kick_session import BROWSER_PROFILE_DIR
from .platforms import NoVod, StreamUrlError, StreamUrls, Vod

POLL_INTERVAL_SECONDS = 0.5
MAX_WAIT_SECONDS = 15
# How far outside the time kick.com gives a stream a moment of it may lie:
# the start it gives is to the second, while the VOD begins a few seconds
# before it, and the length it gives is whole seconds.
VOD_SLACK_SECONDS = 60
# What kick.com says of a VOD it keeps for the channel's subscribers only.
SUBSCRIBERS_ONLY = "subscriber_only"


def _vod_url(page, channel_slug: str) -> str | None:
    # Fetched from inside the page, as the site itself does it.
    try:
        videos = page.evaluate(
            "slug => fetch(`/api/v2/channels/${slug}/videos`).then(r => (r.ok ? r.json() : []))",
            channel_slug,
        )
    except Exception:
        return None
    if not isinstance(videos, list):
        return None
    for video in videos:
        if isinstance(video, dict) and video.get("is_live") and video.get("source"):
            return video["source"]
    return None


def get_stream_urls(channel_slug: str) -> StreamUrls:
    m3u8_url = None
    vod_url = None

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_PROFILE_DIR),
            headless=False,
        )
        try:
            page = context.new_page()

            def on_request(request):
                nonlocal m3u8_url
                if (
                    m3u8_url is None
                    and "playback.live-video.net" in request.url
                    and ".m3u8" in request.url
                ):
                    m3u8_url = request.url

            page.on("request", on_request)
            page.goto(f"https://kick.com/{channel_slug}", wait_until="load", timeout=30000)

            deadline = time.monotonic() + MAX_WAIT_SECONDS
            while m3u8_url is None and time.monotonic() < deadline:
                page.wait_for_timeout(int(POLL_INTERVAL_SECONDS * 1000))
            if m3u8_url is not None:
                vod_url = _vod_url(page, channel_slug)
        finally:
            context.close()

    if m3u8_url is None:
        raise StreamUrlError(f"No live stream URL captured for {channel_slug!r} - is it live?")
    return StreamUrls(live=m3u8_url, vod=vod_url)


def get_live_stream_url(channel_slug: str) -> str:
    return get_stream_urls(channel_slug).live


def _began(video: dict) -> datetime | None:
    """When kick.com says a stream began: in UTC, without saying so."""
    try:
        began = datetime.fromisoformat(str(video.get("start_time") or ""))
    except ValueError:
        return None
    return began if began.tzinfo is not None else began.replace(tzinfo=timezone.utc)


def covering(videos: list, when: datetime) -> dict | None:
    """Which of the streams kick.com lists `when` was in: the one whose time
    it lies in, or failing that the nearest that begins or ends no more than
    VOD_SLACK_SECONDS from it - one that is on reaching to now. None if none
    does. Pure."""
    best: tuple[float, dict] | None = None
    for video in videos:
        began = _began(video) if isinstance(video, dict) else None
        if began is None:
            continue
        ended = None if video.get("is_live") else began + timedelta(milliseconds=video.get("duration") or 0)
        if when < began:
            outside = (began - when).total_seconds()
        elif ended is not None and when > ended:
            outside = (when - ended).total_seconds()
        else:
            outside = 0.0
        if outside <= VOD_SLACK_SECONDS and (best is None or outside < best[0]):
            best = (outside, video)
    return best[1] if best else None


def vod_at(channel_slug: str, when: datetime) -> Vod:
    """The VOD of the channel's stream that was on at `when` - one that is
    on, or one that is over. Asked over plain HTTP; blocks while it is.
    Raises NoVod, saying why, where there is none to be had."""
    try:
        # A thread of its own, with no event loop running in it.
        videos = asyncio.run(kick_client.get_videos(channel_slug))
    except (httpx.HTTPError, ValueError) as error:
        raise NoVod(f"Kick could not be asked for the channel's VODs ({type(error).__name__}).") from error
    video = covering(videos, when)
    if video is None:
        raise NoVod(
            "Kick lists no VOD of that stream: the channel keeps none, it was deleted, "
            "or it is older than the streams Kick lists."
        )
    if not video.get("source"):
        if (video.get("video") or {}).get("status") == SUBSCRIBERS_ONLY:
            raise NoVod("Kick keeps the VOD of that stream for the channel's subscribers only.")
        raise NoVod("Kick gives no address for the VOD of that stream.")
    return Vod(master=video["source"])
