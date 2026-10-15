"""Thin client for the parts of YouTube this project needs, asked the way
YouTube's own web pages ask them (InnerTube: youtube.com/youtubei/v1).

YouTube's official API counts every call against a daily quota that would
not see one busy chat through a day, and has no way to watch a stream at
all. The site's pages are served by an API of their own that takes no key,
and that is what the service asks, as a visitor who is not signed in: who a
channel is, whether it is live, and its chat (youtube_chat.py). That API is
not published, and YouTube changes it when it likes - so an answer is read
for the few things wanted of it, and one without them is said to be what it
is (YouTubeChanged) rather than guessed at.

A channel is found from any address on youtube.com that leads to it
(`navigation/resolve_url`) - a link to its page, or to one of its videos -
and its own page (`browse`) says its id, its title and its handle. Whether
it is live is where its /live address leads: to the video it is streaming
(or has scheduled), or to its channel page while there is none - and that
video's player (`player`) says whether it is live, and since when.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote, unquote

import httpx

from .platforms import YOUTUBE_SITE

API_BASE = f"{YOUTUBE_SITE}/youtubei/v1"
# What the service asks as: YouTube's web client, of a version the site was
# on lately. A page of the site says which version it is on now, and the
# service goes by that from then on (see remember_client_version).
CLIENT_NAME = "WEB"
CLIENT_VERSION = "2.20261014.01.00"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
# A visitor from Europe is shown YouTube's question about cookies before any
# page, unless it has been answered - here, with everything that may be
# declined declined.
CONSENT_COOKIES = {"SOCS": "CAI"}
TIMEOUT_SECONDS = 15

# What a channel's id looks like, and what a handle may be to be the name a
# channel is known by here - which also keeps it safe in a file's name: the
# letters of any script, digits, "_", "-" and "·", and dots between them.
CHANNEL_ID = re.compile(r"UC[0-9A-Za-z_-]{22}")
_HANDLE = re.compile(r"[\w\-·]+(?:\.[\w\-·]+)*")
HANDLE_MAX_LENGTH = 100
# A version as the site writes it.
_CLIENT_VERSION = re.compile(r"\d+\.\d{8}\.\d+\.\d+")

_client_version = CLIENT_VERSION


class YouTubeChanged(RuntimeError):
    """YouTube answered without what is read of its answer - as a rule
    because it changed how it answers."""


class Refused(RuntimeError):
    """YouTube would not answer a visitor that is not signed in - as it does
    when it takes the visitor for a robot."""


def remember_client_version(version: object) -> None:
    """Goes by the version of the web client a page of the site was served
    for, from now on."""
    global _client_version
    if isinstance(version, str) and _CLIENT_VERSION.fullmatch(version):
        _client_version = version


def client_context() -> dict[str, Any]:
    """Who is asking, as InnerTube is told it with every request."""
    return {"client": {"clientName": CLIENT_NAME, "clientVersion": _client_version, "hl": "en"}}


def new_client() -> httpx.AsyncClient:
    """A client that asks YouTube as a browser would: whoever holds one
    open for long keeps the cookies YouTube hands out, as a browser does."""
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
        cookies=CONSENT_COOKIES,
        timeout=TIMEOUT_SECONDS,
        follow_redirects=True,
    )


@asynccontextmanager
async def _using(client: httpx.AsyncClient | None) -> AsyncIterator[httpx.AsyncClient]:
    """The client given, or one made for the while."""
    if client is not None:
        yield client
        return
    async with new_client() as made:
        yield made


async def ask(
    endpoint: str,
    body: dict[str, Any],
    client: httpx.AsyncClient | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """What InnerTube answers at one of its endpoints ("player", "browse",
    ...) to a request with that body. Raises httpx.HTTPStatusError for an
    answer that is not a success."""
    async with _using(client) as asking:
        response = await asking.post(
            f"{API_BASE}/{endpoint}",
            params={"prettyPrint": "false"},
            json={"context": context or client_context(), **body},
        )
        response.raise_for_status()
    try:
        answer = response.json()
    except ValueError:
        raise YouTubeChanged(f"YouTube's {endpoint} answered with something that is not JSON") from None
    if not isinstance(answer, dict):
        raise YouTubeChanged(f"YouTube's {endpoint} answered with something that is not an object")
    return answer


def _field(value: object, *path: str) -> Any:
    """What lies at a path of keys in an answer, or None where any of them
    is missing."""
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def channel_page(login: str) -> str:
    """The address of a channel's page, by the name it is known by here:
    its handle - or its id, for a channel without one. A handle is kept in
    small letters, so one cannot be taken for an id, which begins "UC"."""
    if CHANNEL_ID.fullmatch(login):
        return f"{YOUTUBE_SITE}/channel/{login}"
    return f"{YOUTUBE_SITE}/@{quote(login)}"


async def _resolve(url: str, client: httpx.AsyncClient) -> dict[str, Any]:
    """Where an address on youtube.com leads, as the site's navigation says
    it. Raises ValueError for one that leads nowhere."""
    try:
        answer = await ask("navigation/resolve_url", {"url": url}, client)
    except httpx.HTTPStatusError as error:
        if error.response.status_code == 404:
            raise ValueError(f"YouTube has nothing at {url}") from error
        raise
    endpoint = answer.get("endpoint")
    if not isinstance(endpoint, dict):
        raise YouTubeChanged(f"YouTube did not say where {url} leads")
    return endpoint


def _browse_channel(endpoint: dict[str, Any]) -> str | None:
    """The channel an address leads to, by its id, if it leads to one's page."""
    browse_id = _field(endpoint, "browseEndpoint", "browseId")
    return browse_id if isinstance(browse_id, str) and CHANNEL_ID.fullmatch(browse_id) else None


def _watched_video(endpoint: dict[str, Any]) -> str | None:
    """The video an address leads to, by its id, if it leads to one."""
    video_id = _field(endpoint, "watchEndpoint", "videoId")
    return video_id if isinstance(video_id, str) and video_id else None


@dataclass(frozen=True)
class Channel:
    """A YouTube channel, as its own page describes it."""

    channel_id: str
    title: str
    # Its handle as YouTube writes it, without the "@" - None for a channel
    # that has none, or one that is not fit to be a name here.
    handle: str | None

    @property
    def login(self) -> str:
        """The name it is known by here: its handle in small letters (YouTube
        does not tell a handle's capitals apart) - or its id, for a channel
        without one."""
        return self.handle.lower() if self.handle else self.channel_id


def _handle(vanity_url: object) -> str | None:
    """A channel's handle, from the address of its page by it."""
    if not isinstance(vanity_url, str) or "/@" not in vanity_url:
        return None
    handle = unquote(vanity_url.rpartition("/@")[2]).strip("/")
    return handle if _HANDLE.fullmatch(handle) and len(handle) <= HANDLE_MAX_LENGTH else None


async def find_channel(where: str) -> Channel:
    """The channel a handle leads to - or a link to anything of it on
    youtube.com, its page or one of its videos (see platforms.parse).
    Raises ValueError if it leads to no channel."""
    url = where if where.startswith(f"{YOUTUBE_SITE}/") else channel_page(where)
    async with new_client() as client:
        endpoint = await _resolve(url, client)
        channel_id = _browse_channel(endpoint)
        video_id = _watched_video(endpoint)
        if channel_id is None and video_id is not None:
            player = await ask("player", {"videoId": video_id}, client)
            found = _field(player, "videoDetails", "channelId")
            channel_id = found if isinstance(found, str) and CHANNEL_ID.fullmatch(found) else None
        if channel_id is None:
            raise ValueError(f"No YouTube channel found for {where!r}")
        page = await ask("browse", {"browseId": channel_id}, client)
    metadata = _field(page, "metadata", "channelMetadataRenderer")
    if not isinstance(metadata, dict) or metadata.get("externalId") != channel_id:
        raise ValueError(f"No YouTube channel found for {where!r}")
    title = metadata.get("title")
    return Channel(channel_id, title if isinstance(title, str) else "", _handle(metadata.get("vanityChannelUrl")))


@dataclass(frozen=True)
class LiveVideo:
    """The video a channel is streaming."""

    video_id: str
    # When the stream began, where YouTube says.
    started_at: datetime | None = None


def _moment(text: object) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


async def live_video(login: str, client: httpx.AsyncClient | None = None) -> LiveVideo | None:
    """The video a channel is streaming right now, by the name it is known
    by here - or None while it streams none. One it has only scheduled is
    not live yet. Raises ValueError for a channel YouTube no longer has at
    that name."""
    async with _using(client) as asking:
        video_id = _watched_video(await _resolve(f"{channel_page(login)}/live", asking))
        if video_id is None:
            return None
        player = await ask("player", {"videoId": video_id}, asking)
    details = player.get("videoDetails")
    if not isinstance(details, dict):
        status = _field(player, "playabilityStatus", "status")
        if status == "LOGIN_REQUIRED":
            raise Refused(str(_field(player, "playabilityStatus", "reason") or "YouTube asks to sign in"))
        raise YouTubeChanged(f"YouTube said nothing of the video {video_id} ({status})")
    if details.get("isLive") is not True:
        return None
    broadcast = _field(player, "microformat", "playerMicroformatRenderer", "liveBroadcastDetails")
    return LiveVideo(video_id, _moment(_field(broadcast, "startTimestamp")))
