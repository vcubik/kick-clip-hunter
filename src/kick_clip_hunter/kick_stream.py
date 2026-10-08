"""Fetches a currently-live channel's real HLS playback URL.

The playback_url embedded in a channel page's server-rendered HTML fails
AWS IVS signature verification when used directly (confirmed by testing) -
only the URL the page's own player actually requests, once it's loaded and
initialized, works. Capturing that live network request requires a headed
`patchright` browser (headless gets blocked by kick.com's Cloudflare
protection even with a valid login) using the persisted session from
kick_session.py.

While that browser is open it also looks up the stream's own recording (its
VOD), which Kick writes as the broadcast goes along and serves from a host a
plain HTTP client can read. The recorder uses it to replace footage that an
ad stitched into the live playlist covered up.
"""

import time
from dataclasses import dataclass

from patchright.sync_api import sync_playwright

from .kick_session import BROWSER_PROFILE_DIR

POLL_INTERVAL_SECONDS = 0.5
MAX_WAIT_SECONDS = 15


class StreamUrlError(RuntimeError):
    pass


@dataclass
class StreamUrls:
    live: str
    # Master playlist of the recording of the current broadcast, or None if
    # the channel doesn't keep one (or the lookup failed).
    vod: str | None = None


def _vod_url(page, channel_slug: str) -> str | None:
    # Fetched from inside the page: the endpoint is behind the same
    # Cloudflare protection as the site itself.
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
