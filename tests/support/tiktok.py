"""Stand-ins for TikTok, as far as the service asks it anything.

`FakeTikTok` answers what the service asks of TikTok's own web API: what a
channel is and whether it is live (`api-live/user/room`), in the shape TikTok
answers in, cut down to what is read of it. Like the other fakes it plugs in
as an `httpx` transport (the `tiktok` fixture), so the real client code runs
unmodified.

A TikTok stream is one FLV download: `FakeFlvServer` serves one over real
HTTP on 127.0.0.1, for the recorder's real networking and threading to read.
The functions below it make FLV streams of the test pattern ffmpeg draws,
dressed the way TikTok sends them - its onMetaData saying when the stream
began, its times counted from then, and a SEI unit after every keyframe's
picture.

A room's chat is read through TikTokLive, which the tests never reach:
`FakeTikTokChats` stands in for the connection to a room's chat (the
reader's `connect`), and `comment` makes a comment as TikTokLive hands one
over.
"""

from __future__ import annotations

import asyncio
import json
import struct
import subprocess
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

from kick_clip_hunter import flv
from kick_clip_hunter.tiktok_chat import ENDED
from tests.support.media import FFMPEG

HOSTS = frozenset({"www.tiktok.com"})
USER_NOT_FOUND = 19881007
# What a live room's status is while it is live, and while it is not.
LIVE, OFFLINE = 2, 4
FLV_HOST = "https://pull-flv.example"


@dataclass
class FakeTikTokChannel:
    # As TikTok writes it.
    handle: str
    user_id: str
    room_id: str = "7600000000000000001"
    live: bool = False
    # When its stream began, in seconds since 1970, while it is live.
    started_at: int = 0
    # Where its stream is while it is live: (codec, FLV address) by quality.
    streams: dict[str, tuple[str, str]] = field(default_factory=dict)

    def go_live(self, started_at: int = 1792080000, streams: dict[str, tuple[str, str]] | None = None) -> None:
        self.live, self.started_at = True, started_at
        self.streams = streams if streams is not None else {"origin": ("h265", f"{FLV_HOST}/{self.handle}.flv")}

    def go_offline(self) -> None:
        self.live, self.started_at = False, 0


@dataclass
class FakeTikTok:
    channels: list[FakeTikTokChannel] = field(default_factory=list)
    # Force an HTTP status for every request whose URL contains the key.
    failures: dict[str, int] = field(default_factory=dict)
    # Answer with this body instead, for every request whose URL contains
    # the key - TikTok answering otherwise than it does: as JSON, or as it
    # is where it is text.
    answers: dict[str, object] = field(default_factory=dict)
    # The query of every request received.
    requests: list[dict[str, str]] = field(default_factory=list)

    def add_channel(self, handle: str, user_id: str | None = None, **kwargs) -> FakeTikTokChannel:
        channel = FakeTikTokChannel(handle=handle, user_id=user_id or str(6_800_000_000 + len(self.channels)), **kwargs)
        self.channels.append(channel)
        return channel

    def by_handle(self, handle: str) -> FakeTikTokChannel | None:
        return next((c for c in self.channels if c.handle.lower() == handle.lower()), None)

    def owns(self, request: httpx.Request) -> bool:
        return request.url.host in HOSTS

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(dict(request.url.params))
        for fragment, status in self.failures.items():
            if fragment in str(request.url):
                return httpx.Response(status, text="forced failure")
        for fragment, body in self.answers.items():
            if fragment in str(request.url):
                return httpx.Response(200, text=body) if isinstance(body, str) else httpx.Response(200, json=body)
        if request.method != "GET" or request.url.path != "/api-live/user/room":
            return httpx.Response(404, text=f"fake TikTok has no route for {request.method} {request.url}")
        channel = self.by_handle(request.url.params.get("uniqueId", ""))
        if channel is None:
            return httpx.Response(200, json={"statusCode": USER_NOT_FOUND, "message": "user_not_found", "data": {}})
        return httpx.Response(200, json={"statusCode": 0, "message": "", "data": room_answer(channel)})


def _stream_data(streams: dict[str, tuple[str, str]], codec: str) -> dict:
    qualities = {
        quality: {"main": {"flv": url, "hls": "", "sdk_params": json.dumps({"VCodec": said, "resolution": "720x1280"})}}
        for quality, (said, url) in streams.items()
        if (said == "h265") == (codec == "h265")
    }
    return {"pull_data": {"stream_data": json.dumps({"data": qualities})}}


def room_answer(channel: FakeTikTokChannel) -> dict:
    """What api-live/user/room says of a channel, as TikTok says it."""
    live_room: dict[str, Any] = {
        "status": LIVE if channel.live else OFFLINE,
        "startTime": channel.started_at,
        "title": "",
        "liveRoomStats": {"userCount": 12},
    }
    if channel.live:
        live_room["streamData"] = _stream_data(channel.streams, "h264")
        live_room["hevcStreamData"] = _stream_data(channel.streams, "h265")
    return {
        "user": {
            "id": channel.user_id,
            "uniqueId": channel.handle,
            "nickname": channel.handle.title(),
            "roomId": channel.room_id,
        },
        "liveRoom": live_room,
    }


# -- a stream as FLV ------------------------------------------------------------


class FakeFlvServer:
    """Serves `/stream.flv` over real HTTP on 127.0.0.1: the bytes it was
    given, then the end of the download - a stream that has ended, as far as
    whoever reads it can tell. `fail_next` holds statuses to answer with
    first, one per request."""

    def __init__(self, content: bytes) -> None:
        self.content = content
        self.fail_next: list[int] = []
        self.requests = 0
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # keep test output quiet
                pass

            def do_GET(self):
                server.requests += 1
                if self.path != "/stream.flv":
                    self.send_error(404)
                    return
                if server.fail_next:
                    self.send_error(server.fail_next.pop(0))
                    return
                self.send_response(200)
                self.send_header("Content-Type", "video/x-flv")
                self.send_header("Content-Length", str(len(server.content)))
                self.end_headers()
                self.wfile.write(server.content)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/stream.flv"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


def make_flv(path: Path, seconds: float, keyframe_every: float = 1.0, size: str = "160x120") -> bytes:
    """An FLV of the test pattern with a tone, `seconds` long, H.264 with a
    keyframe every `keyframe_every` seconds and AAC - as ffmpeg writes one,
    its times from zero."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fps = 10
    interval = str(int(fps * keyframe_every))
    # fmt: off
    subprocess.run(
        [
            FFMPEG, "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc=size={size}:rate={fps}:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-g", interval, "-keyint_min", interval, "-sc_threshold", "0",
            "-c:a", "aac", "-b:a", "32k",
            "-f", "flv", str(path),
        ],
        check=True,
    )
    # fmt: on
    return path.read_bytes()


def amf0(name: str, properties: dict[str, str | float]) -> bytes:
    """A script tag's data: `name` and an ECMA array of `properties`, as
    AMF0 writes them."""

    def string(text: str) -> bytes:
        encoded = text.encode()
        return struct.pack(">H", len(encoded)) + encoded

    out = b"\x02" + string(name) + b"\x08" + struct.pack(">I", len(properties))
    for key, value in properties.items():
        out += string(key)
        out += b"\x02" + string(value) if isinstance(value, str) else b"\x00" + struct.pack(">d", value)
    return out + b"\x00\x00\x09"


def tags_of(content: bytes) -> list[flv.Tag]:
    return list(flv.FlvReader().feed(content))


def flv_file(tags: list[flv.Tag], flags: int = flv.HAS_AUDIO | flv.HAS_VIDEO) -> bytes:
    """An FLV file of these tags, at their own times."""
    header = flv.SIGNATURE + bytes([1, flags]) + struct.pack(">I", flv.HEADER_SIZE) + b"\0\0\0\0"
    return header + b"".join(tag.encoded(tag.time) for tag in tags)


# A SEI unit as TikTok's pictures carry one: user data, of no use to a player.
SEI_UNIT = b"\x06\x05\x11" + bytes(range(1, 17)) + b"\x41\x80"


def _sei_after_picture(tag: flv.Tag) -> flv.Tag:
    """A keyframe's picture with a SEI unit after it rather than before."""
    if not tag.is_keyframe or not tag.is_frame or tag.codec != "h264":
        return tag
    return flv.Tag(tag.kind, tag.time, tag.data + struct.pack(">I", len(SEI_UNIT)) + SEI_UNIT)


def as_tiktok_sends_it(
    content: bytes, *, started_at_ms: int, live_for_ms: int, sei_after_picture: bool = True
) -> bytes:
    """An FLV stream dressed the way TikTok sends one: onMetaData saying
    when the stream began (`start_time`, as digits), every picture and
    stretch of sound timed from then - the stream has been on for
    `live_for_ms` by the first - the sequence headers at zero, and a SEI unit
    after every keyframe's picture."""
    tags = []
    for tag in tags_of(content):
        if tag.kind == flv.SCRIPT:
            tag = flv.Tag(flv.SCRIPT, 0, amf0("onMetaData", {"start_time": str(started_at_ms), "interval": "2"}))
        elif tag.is_frame:
            tag = flv.Tag(tag.kind, tag.time + live_for_ms, tag.data)
            if sei_after_picture:
                tag = _sei_after_picture(tag)
        tags.append(tag)
    return flv_file(tags)


# -- a room's chat ---------------------------------------------------------------


class FakeChat:
    """One connection to a room's chat: what the test says in it arrives,
    until it is ended."""

    def __init__(self, room_id: int, handle: str) -> None:
        self.room_id, self.handle = room_id, handle
        self.queue: asyncio.Queue = asyncio.Queue()
        self.closed = False

    def say(self, *comments: Any, at: datetime | None = None) -> None:
        for one in comments:
            self.queue.put_nowait((at or datetime.now(timezone.utc), one))

    def end(self) -> None:
        self.queue.put_nowait(ENDED)


class FakeTikTokChats:
    """Stands in for the connection to a room's chat (tiktok_chat's
    `connect`): every connection made, in order, and the failures to give
    instead of the next ones."""

    def __init__(self) -> None:
        self.connections: list[FakeChat] = []
        self.refuse: list[BaseException] = []

    @asynccontextmanager
    async def connect(self, room_id: int, handle: str) -> AsyncIterator[asyncio.Queue]:
        if self.refuse:
            raise self.refuse.pop(0)
        chat = FakeChat(room_id, handle)
        self.connections.append(chat)
        try:
            yield chat.queue
        finally:
            chat.closed = True

    def latest(self) -> FakeChat:
        return self.connections[-1]


def comment(
    message_id: int,
    nickname: str,
    content: str,
    *,
    handle: str = "",
    sent_at_ms: int = 1792090811314,
    streamer: bool = False,
    moderator: bool = False,
    subscriber: bool = False,
) -> Any:
    """A comment as TikTokLive hands one over: its CommentEvent."""
    from TikTokLive.events import CommentEvent
    from TikTokLive.proto.custom_proto import ExtendedUser
    from TikTokLiveProto.v3.webcast.model.data import UserIdentity
    from TikTokLiveProto.v3.webcast.shared.message import CommonMessageData

    return CommentEvent(
        common=CommonMessageData(msg_id=message_id, create_time=sent_at_ms),
        content=content,
        user=ExtendedUser(id=7_000_000_000 + message_id, nickname=nickname, display_id=handle or nickname.lower()),
        user_identity=UserIdentity(
            is_anchor=streamer, is_moderator_of_anchor=moderator, is_subscriber_of_anchor=subscriber
        ),
    )
