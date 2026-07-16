"""Creates real Kick clips through the site's own (undocumented) internal API.

Driven through a headed `patchright` browser using the persisted login
session in data/kick_browser_profile (see kick_session.py and
scripts/kick_login.py for how that session is captured). Headless mode gets
blocked by kick.com's Cloudflare protection even with valid login cookies -
only a headed, visible browser window passes, so every call briefly opens
one against the target channel's page.

The actual clip creation is a two-step call the website's own JS makes,
reverse-engineered by watching real "Create Clip" clicks in DevTools:

1. POST /api/internal/v1/livestreams/{livestream_slug}/clips
   body: {"duration": 180}
   -> creates a 180s source buffer, returns {id, url, thumbnails, source_duration}

2. POST /api/internal/v1/livestreams/{livestream_slug}/clips/{clip_id}/finalize
   body: {"duration": <final length>, "start_time": <offset into the 180s
          source, seconds>, "title": <str>}
   -> returns the finished public clip: {id, clip_url, thumbnail_url, ...}

`livestream_slug` (e.g. "236fdbf1-some-slugified-title") is not the same as
the channel slug or the stable numeric livestream id - it's embedded in the
channel page's server-rendered HTML and appears to rotate, so it's fetched
fresh immediately before each call rather than cached.
"""

import json
import re

from patchright.sync_api import sync_playwright

from .kick_session import BROWSER_PROFILE_DIR

SOURCE_BUFFER_SECONDS = 180

_LIVESTREAM_RE = re.compile(r'\\"livestream\\":\{\\"id\\":(\d+),\\"slug\\":\\"([^"\\]+)\\"')


class ClipCreationError(RuntimeError):
    pass


def _extract_livestream(html: str) -> tuple[int, str]:
    match = _LIVESTREAM_RE.search(html)
    if not match:
        raise ClipCreationError("No live livestream found on the channel page - is it live?")
    return int(match.group(1)), match.group(2)


_FETCH_JS = """
async ([path, body]) => {
    const res = await fetch(path, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
    });
    return {status: res.status, body: await res.text()};
}
"""


def create_clip(channel_slug: str, start_time: int, duration: int = 30, title: str = "") -> dict:
    """Create and publish a Kick clip for the given channel's current livestream.

    start_time is an offset in seconds into the trailing 180s source buffer
    (0-150 for a 30s clip), not an absolute stream timestamp.
    """
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_PROFILE_DIR),
            headless=False,
        )
        try:
            page = context.new_page()
            page.goto(f"https://kick.com/{channel_slug}", wait_until="load", timeout=30000)
            page.wait_for_timeout(2000)

            _livestream_id, livestream_slug = _extract_livestream(page.content())

            source = page.evaluate(
                _FETCH_JS,
                [
                    f"/api/internal/v1/livestreams/{livestream_slug}/clips",
                    {"duration": SOURCE_BUFFER_SECONDS},
                ],
            )
            if source["status"] != 201:
                raise ClipCreationError(f"source clip creation failed: {source}")
            source_clip = json.loads(source["body"])

            final = page.evaluate(
                _FETCH_JS,
                [
                    f"/api/internal/v1/livestreams/{livestream_slug}/clips/{source_clip['id']}/finalize",
                    {"duration": duration, "start_time": start_time, "title": title},
                ],
            )
            if final["status"] != 201:
                raise ClipCreationError(f"clip finalize failed: {final}")
            return json.loads(final["body"])
        finally:
            context.close()
