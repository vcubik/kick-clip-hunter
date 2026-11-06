"""A VOD played on the review page whose host keeps a page elsewhere from
fetching it: Twitch's VOD hosts send no CORS header, so the page's player
(hls.js) cannot read their playlists or footage itself. The service fetches
them for it and hands them on, at `/vod/relay` (see main.vod_relay).

Only addresses the service handed out itself are fetched: each carries a
signature made with a key of this run, and every address in a playlist the
relay hands on is written as another such relay address - so the page
follows the VOD's variants, footage and initialization segments through the
service, and nothing can have the service fetch anything else. A restart
makes a new key: a player of the run before stops, and is started again.

Footage is fetched whole before it is handed on - a segment of Twitch's
VOD is ten seconds - and nothing of it is kept.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode, urljoin

import httpx

from . import recorder

ROUTE = "/vod/relay"
PLAYLIST_TYPE = "application/vnd.apple.mpegurl"
# What footage is handed on as where its host does not say.
FOOTAGE_TYPE = "video/mp2t"
# How long fetching anything for the page may take: a segment's length.
TIMEOUT_SECONDS = recorder.SEGMENT_TIMEOUT_SECONDS
_KEY = secrets.token_bytes(32)
_URI_ATTRIBUTE = re.compile(r'URI="([^"]*)"')


class RelayError(Exception):
    """What the VOD's host would not hand over, in words that say so."""


@dataclass(frozen=True)
class Fetched:
    body: bytes
    media_type: str
    playlist: bool


def signature(url: str) -> str:
    """The signature of an address the relay may fetch. Pure for a run."""
    digest = hmac.new(_KEY, url.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def signed(url: str, given: str) -> bool:
    """Whether an address was handed out by this run of the service."""
    return hmac.compare_digest(signature(url), given)


def address(url: str) -> str:
    """Where the page fetches `url` through the relay."""
    return f"{ROUTE}?{urlencode({'u': url, 's': signature(url)})}"


def rewritten(text: str, base: str) -> str:
    """A playlist fetched from `base` with every address in it - each line
    that is one and each URI="..." of a tag - resolved against `base` and
    written as a relay address. Pure for a run."""
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append(line)
        elif stripped.startswith("#"):
            lines.append(_URI_ATTRIBUTE.sub(lambda m: f'URI="{address(urljoin(base, m.group(1)))}"', line))
        else:
            lines.append(address(urljoin(base, stripped)))
    return "\n".join(lines) + "\n"


def fetch(url: str) -> Fetched:
    """What is at `url`, for the page: a playlist with its addresses
    rewritten, footage as it is. Blocks while it is fetched; raises
    RelayError where the host would not hand it over."""
    try:
        with httpx.Client(follow_redirects=True, timeout=TIMEOUT_SECONDS) as client:
            response = client.get(url)
            response.raise_for_status()
    except httpx.HTTPStatusError as error:
        raise RelayError(f"The VOD's host answered {error.response.status_code}.") from error
    except httpx.HTTPError as error:
        raise RelayError(f"The VOD's host could not be reached ({type(error).__name__}).") from error
    if response.content.lstrip().startswith(b"#EXTM3U"):
        return Fetched(rewritten(response.text, str(response.url)).encode(), PLAYLIST_TYPE, True)
    media_type = response.headers.get("content-type", "").split(";")[0].strip() or FOOTAGE_TYPE
    return Fetched(response.content, media_type, False)
