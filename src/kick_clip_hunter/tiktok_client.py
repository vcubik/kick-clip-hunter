"""Thin client for what this project asks of TikTok: who a channel is,
whether it is live, and where its stream is.

TikTok has no API for live streams at all (its developer products are about
posting videos and signing in). The site's own pages ask an API of their own,
and one of its answers says all three things at once: `api-live/user/room`,
asked about a channel by its handle, answers with the account (its id and
handle, and the room its streams are in) and its live room - whether it is
live (`status`), since when, and where its stream is in every quality TikTok
serves it in. It takes no signature and no sign-in, which most of what the
site asks does. It is the question streamlink's own TikTok plugin asks.

That API is not published, and TikTok changes it when it likes - so an
answer is read for the few things wanted of it, and one without them is said
to be what it is (TikTokChanged) rather than guessed at.

Where a stream is, TikTok says in each quality it serves - "origin", what the
streamer sends, and the qualities it makes of it, in H.264 or HEVC - as an
FLV address, and for some also as HLS or DASH. Only FLV can be had for every
stream (HLS answers 403 to anyone but TikTok's own players), so that is what
is recorded (see flv.py), in the quality the streamer sends (`best_stream`).
The addresses carry when they stop working (`expire`), weeks after they were
handed out.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from .platforms import TIKTOK_SITE

ROOM_API = f"{TIKTOK_SITE}/api-live/user/room"
# What the site's own pages say they are, which the API answers to.
APP_ID = 1988
SOURCE_TYPE = 54
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
TIMEOUT_SECONDS = 15

# What the answer's statusCode is for an account that does not exist.
USER_NOT_FOUND = 19881007
# What a live room's status is while it is live.
LIVE = 2

# A handle as TikTok allows it: letters, digits, "_" and ".", up to 24 of
# them - which also keeps it safe in a file's name.
HANDLE = re.compile(r"[A-Za-z0-9_.]{1,24}")
# The qualities TikTok serves a stream in, the best first. "origin" is what
# the streamer sends; the rest TikTok makes of it.
QUALITIES = ("origin", "uhd_60", "uhd", "hd_60", "hd", "sd", "ld")
# What TikTok calls a codec, as ffprobe does.
CODECS = {"avc": "h264", "h264": "h264", "hevc": "h265", "h265": "h265", "bytevc1": "h265"}
# Where an address of TikTok's says when it stops working, in seconds since
# 1970.
_EXPIRY = re.compile(r"[?&]expire=(\d{9,11})(?=&|$)")


class TikTokChanged(RuntimeError):
    """TikTok answered without what is read of its answer - as a rule
    because it changed how it answers."""


@dataclass(frozen=True)
class Stream:
    """A live stream as TikTok serves it in one quality: as FLV, and what
    TikTok says the picture is encoded with."""

    quality: str
    codec: str
    flv: str


@dataclass(frozen=True)
class Room:
    """A TikTok account and its live room, as `room` found them."""

    # TikTok's id of the account, and its handle as TikTok writes it.
    user_id: str
    handle: str
    # The room its streams are in - what its chat is read from. None for an
    # account that has never been live.
    room_id: int | None
    live: bool
    # When the stream began, while it is live.
    started_at: datetime | None = None
    # Where the stream is, while it is live: every quality TikTok serves it
    # in as FLV.
    streams: tuple[Stream, ...] = ()


def new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
        timeout=TIMEOUT_SECONDS,
        follow_redirects=True,
    )


def _parse_json(text: object) -> Any:
    try:
        return json.loads(text) if isinstance(text, str) and text else None
    except ValueError:
        return None


def streams_of(live_room: dict[str, Any]) -> tuple[Stream, ...]:
    """Every quality a live room's stream is served in as FLV, as its
    answer lists them: H.264 first, then HEVC, each quality once."""
    found: dict[str, Stream] = {}
    for key, default in (("streamData", "h264"), ("hevcStreamData", "h265")):
        pull = live_room.get(key)
        stream_data = pull.get("pull_data", {}).get("stream_data") if isinstance(pull, dict) else None
        listed = _parse_json(stream_data)
        qualities = listed.get("data") if isinstance(listed, dict) else None
        if not isinstance(qualities, dict):
            continue
        for quality, entry in qualities.items():
            main = entry.get("main") if isinstance(entry, dict) else None
            flv = main.get("flv") if isinstance(main, dict) else None
            if not isinstance(flv, str) or not flv.startswith("https://") or quality in found:
                continue
            said = _parse_json(main.get("sdk_params"))
            codec = str((said.get("VCodec") or said.get("v_codec") if isinstance(said, dict) else None) or default)
            found[quality] = Stream(quality, CODECS.get(codec.lower(), codec.lower()), flv)
    return tuple(found.values())


def best_stream(streams: tuple[Stream, ...]) -> Stream | None:
    """The stream a recorder follows: the quality the streamer sends where
    TikTok serves it, otherwise the best TikTok makes of it."""
    ranked = [stream for stream in streams if stream.quality in QUALITIES]
    return min(ranked, key=lambda stream: QUALITIES.index(stream.quality), default=None)


def expires_at(url: str) -> datetime | None:
    """When an address of TikTok's stops working, where it says."""
    found = _EXPIRY.search(url)
    return datetime.fromtimestamp(int(found.group(1)), timezone.utc) if found else None


async def room(handle: str, client: httpx.AsyncClient | None = None) -> Room:
    """An account and its live room, by its handle. Raises ValueError if
    TikTok has no such account, TikTokChanged for an answer without what is
    read of it, httpx.HTTPError if TikTok could not be asked."""
    handle = handle.removeprefix("@")
    if not HANDLE.fullmatch(handle):
        raise ValueError(f"{handle!r} can't be a TikTok handle")
    params = {"aid": APP_ID, "sourceType": SOURCE_TYPE, "uniqueId": handle.lower()}
    headers = {"Referer": f"{TIKTOK_SITE}/@{handle.lower()}/live"}
    if client is None:
        async with new_client() as made:
            response = await made.get(ROOM_API, params=params, headers=headers)
    else:
        response = await client.get(ROOM_API, params=params, headers=headers)
    response.raise_for_status()
    try:
        answer = response.json()
    except ValueError:
        raise TikTokChanged("TikTok's room API answered with something that is not JSON") from None
    if not isinstance(answer, dict):
        raise TikTokChanged("TikTok's room API answered with something that is not an object")
    if answer.get("statusCode") == USER_NOT_FOUND:
        raise ValueError(f"no such TikTok account: {handle!r}")
    data = answer.get("data")
    user = data.get("user") if isinstance(data, dict) else None
    live_room = data.get("liveRoom") if isinstance(data, dict) else None
    if answer.get("statusCode") != 0 or not isinstance(user, dict) or not isinstance(live_room, dict):
        said = answer.get("message") or answer.get("statusCode")
        raise TikTokChanged(f"TikTok's room API said nothing of {handle!r} that can be read ({said})")
    user_id, unique_id, room_id = user.get("id"), user.get("uniqueId"), user.get("roomId")
    if not isinstance(user_id, str) or not user_id.isdigit() or not isinstance(unique_id, str):
        raise TikTokChanged(f"TikTok's room API did not say who {handle!r} is")
    live = live_room.get("status") == LIVE
    started = live_room.get("startTime")
    return Room(
        user_id=user_id,
        handle=unique_id,
        room_id=int(room_id) if isinstance(room_id, str) and room_id.isdigit() and room_id != "0" else None,
        live=live,
        started_at=(
            datetime.fromtimestamp(started, timezone.utc) if live and isinstance(started, int) and started > 0 else None
        ),
        streams=streams_of(live_room) if live else (),
    )
