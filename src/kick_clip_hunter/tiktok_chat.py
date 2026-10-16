"""TikTok's chat, read while a TikTok channel's stream is being recorded.

TikTok channels are only ever recorded (platforms.RECORDED_ONLY), so their
chat is wanted only to go with a recording: the reader reads the chat of
every TikTok channel whose stream is being kept whole right now
(`channels`), for as long as that recording runs.

TikTok sends a live room's chat to its viewers over a WebSocket of its own,
in protobuf. The request that opens it has to be signed the way TikTok's own
web pages sign theirs - with values made by scripts of TikTok's that are
hidden on purpose - and unsigned, TikTok answers it with nothing. TikTokLive,
a library made for reading TikTok LIVE, has that request signed by Euler
Stream, a service that does it for anyone (a few thousand a day, more with a
key of its own: EULER_STREAM_API_KEY), and does the rest itself - the
WebSocket, its keepalive, the protobuf. One signed request is spent on each
connection and nothing else goes through Euler Stream, which learns which
rooms are read, and from where. TikTokLive takes seconds to import, so it is
imported the first time a chat is read, in a worker thread.

For a channel whose stream is recorded, the reader looks every LOOK_SECONDS
whether it is live (tiktok_client.room, which also says which room its chat
is in) and once it is, connects to its chat and takes in every comment
written from then on, a batch at a time (BATCH_SECONDS). A connection that
ends - the stream is over, or TikTok closed it - is made again soon after; one
that cannot be made, or keeps ending at once, is tried again later and later
(RETRY_SECONDS), so that a sign server turning requests away is not asked
again straight away. What was written before a connection was made is left
out: it would arrive all at once, as if just written.

A comment is turned into a ChatMessage (chat_message.py) under the name
TikTok shows its writer by - their nickname, not their handle - with whether
they are the streamer, a moderator or a subscriber as badges. Emoji are in
the text as they were typed. A subscriber's own emotes, which TikTok sends
as pictures, are not taken in, nor gifts, likes, follows or anything else a
room says.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from . import chat_identity, platforms, tiktok_client
from .chat_message import ChatMessage, typed

logger = logging.getLogger("kick_clip_hunter")

# How often the recordings are looked at for a TikTok channel whose chat has
# come to be wanted - soon after its recording starts.
CHANNELS_SECONDS = 5
# How often a channel that is recorded but not live is looked at, whether it
# has gone live - as often as the recording loop looks at it.
LOOK_SECONDS = 30
# How long after a connection that ended it is made again; a connection
# that lasted less than STEADY_SECONDS counts as one that failed.
AGAIN_SECONDS = 5
STEADY_SECONDS = 60
# How long until a connection that failed is tried again, growing with
# every failure in a row.
RETRY_SECONDS = (10.0, 30.0, 60.0, 120.0, 300.0)
# How long comments are gathered before they are handed on together.
BATCH_SECONDS = 1.0

# What a chat's queue holds once its connection is over.
ENDED = None

# A comment as it arrived: when, and TikTokLive's CommentEvent.
Received = tuple[datetime, Any]
# What connects to a room's chat (by its id, and the channel's handle) for
# as long as a `with` block runs, and hands over a queue its comments arrive
# in, followed by ENDED once the connection is over. `tiktoklive_chat`,
# unless a test says otherwise.
Connect = Callable[[int, str], AbstractAsyncContextManager["asyncio.Queue[Received | None]"]]


@dataclass(frozen=True)
class Channel:
    """A TikTok channel whose chat is read."""

    # The id it is kept by in the database (see platforms.py).
    broadcaster_user_id: int
    # Its name in the service, `handle@tiktok`.
    channel: str

    @property
    def login(self) -> str:
        """Its handle on TikTok."""
        return platforms.login_of(self.channel)


# The key of Euler Stream's the requests are signed with, if there is one.
_sign_key = ""


def use_sign_key(key: str) -> None:
    """Has Euler Stream asked with a key of its own from the next connection
    on, where there is one (see config.Settings.euler_stream_api_key)."""
    global _sign_key
    _sign_key = key


@asynccontextmanager
async def tiktoklive_chat(room_id: int, handle: str) -> AsyncIterator[asyncio.Queue[Received | None]]:
    """A live room's comments as they arrive, read through TikTokLive (see
    the module's docstring)."""
    await asyncio.to_thread(importlib.import_module, "TikTokLive")
    from TikTokLive import TikTokLiveClient
    from TikTokLive.client.web.web_settings import WebDefaults
    from TikTokLive.events import CommentEvent, DisconnectEvent

    WebDefaults.tiktok_sign_api_key = _sign_key or None
    client = TikTokLiveClient(unique_id=handle)
    comments: asyncio.Queue[Received | None] = asyncio.Queue()

    async def comment(event: Any) -> None:
        comments.put_nowait((datetime.now(timezone.utc), event))

    async def disconnected(_event: Any) -> None:
        comments.put_nowait(ENDED)

    def over(task: asyncio.Task) -> None:
        # A connection that broke ends without TikTokLive saying so.
        if not task.cancelled() and task.exception() is not None:
            logger.info("tiktok chat: the connection to %s broke off: %r", handle, task.exception())
        comments.put_nowait(ENDED)

    client.add_listener(CommentEvent, comment)
    client.add_listener(DisconnectEvent, disconnected)
    try:
        # The room is known, and so is that it is live: nothing more is
        # asked of TikTok than the connection itself.
        task = await client.start(room_id=room_id, fetch_live_check=False, process_connect_events=False)
        task.add_done_callback(over)
        yield comments
    finally:
        try:
            await client.disconnect()
        finally:
            await client.web.close()


def _sent_at(milliseconds: object) -> str:
    """When TikTok says a comment was sent, as it is kept."""
    if not isinstance(milliseconds, int) or milliseconds <= 0:
        return ""
    return datetime.fromtimestamp(milliseconds / 1000, timezone.utc).isoformat()


def chat_message(comment: Any, channel: Channel, received_at: datetime | None = None) -> ChatMessage | None:
    """What a comment (TikTokLive's CommentEvent) says, as the service takes
    chat in - or None for one without an id, a writer or any words."""
    common, user = comment.common, comment.user
    message_id = common.msg_id if common is not None else 0
    if not message_id or user is None:
        return None
    content = typed(comment.content or "")
    if not content.strip():
        return None
    identity = comment.user_identity
    streamer = bool(identity is not None and identity.is_anchor)
    moderator = bool(identity is not None and identity.is_moderator_of_anchor)
    subscriber = bool(identity is not None and identity.is_subscriber_of_anchor)
    return ChatMessage(
        broadcaster_user_id=channel.broadcaster_user_id,
        channel=channel.channel,
        message_id=str(message_id),
        # As TikTok shows them in its chat; the handle where there is none.
        sender=(user.nickname or user.display_id or "").strip(),
        content=content,
        created_at=_sent_at(common.create_time),
        identity=chat_identity.from_tiktok(streamer=streamer, moderator=moderator, subscriber=subscriber),
        runs_channel=streamer or moderator,
        received_at=received_at,
    )


def _why(error: BaseException) -> str:
    """What went wrong, in a few words."""
    if isinstance(error, httpx.HTTPStatusError):
        return f"TikTok answered {error.response.status_code}"
    if isinstance(error, httpx.HTTPError) or not str(error):
        return type(error).__name__
    return f"{type(error).__name__}: {error}"


class ChatReader:
    """Reads the chat of the TikTok channels whose streams are being
    recorded, for as long as it runs (`run_forever`). `channels` says whose
    chat is wanted, `take` is handed what was written, a batch at a time,
    and `connect` connects to a room's chat (see Connect) - through
    TikTokLive unless it is given."""

    def __init__(
        self,
        channels: Callable[[], Iterable[Channel]],
        take: Callable[[list[ChatMessage]], Awaitable[object]],
        connect: Connect | None = None,
    ) -> None:
        self._channels = channels
        self._take = take
        self._connect = connect or tiktoklive_chat
        # For the Health page, by channel: whose chat is being read and
        # since when, which are waited for to go live, which could not be
        # read and why, since when - and how many channels' chat is wanted,
        # and when the last word of TikTok's came.
        self.reading: dict[str, datetime] = {}
        self.waiting: set[str] = set()
        self.failing: dict[str, tuple[str, datetime]] = {}
        self.wanted = 0
        self.last_heard_at: datetime | None = None

    async def run_forever(self) -> None:
        followed: dict[str, asyncio.Task] = {}
        try:
            while True:
                try:
                    wanted = {channel.channel: channel for channel in self._channels()}
                except Exception:
                    logger.exception("tiktok chat: could not tell whose chat to read")
                    await asyncio.sleep(CHANNELS_SECONDS)
                    continue
                self.wanted = len(wanted)
                for name in list(followed):
                    if name not in wanted:
                        followed.pop(name).cancel()
                        self._forget(name)
                for name, channel in wanted.items():
                    if name not in followed:
                        followed[name] = asyncio.create_task(self._follow(channel))
                await asyncio.sleep(CHANNELS_SECONDS)
        finally:
            for task in followed.values():
                task.cancel()
            await asyncio.gather(*followed.values(), return_exceptions=True)

    async def _follow(self, channel: Channel) -> None:
        """Reads a channel's chat whenever it is live, until it is no longer
        wanted."""
        name = channel.channel
        failures = 0
        while True:
            try:
                room = await tiktok_client.room(channel.login)
                self.last_heard_at = datetime.now(timezone.utc)
                if not room.live or room.room_id is None:
                    self.waiting.add(name)
                    self.failing.pop(name, None)
                    await asyncio.sleep(LOOK_SECONDS)
                    continue
                self.waiting.discard(name)
                lasted = await self._read(channel, room.room_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._failed(name, error)
                lasted = 0.0
            if lasted >= STEADY_SECONDS:
                failures = 0
                await asyncio.sleep(AGAIN_SECONDS)
            else:
                await asyncio.sleep(RETRY_SECONDS[min(failures, len(RETRY_SECONDS) - 1)])
                failures += 1

    async def _read(self, channel: Channel, room_id: int) -> float:
        """Reads a room's chat until the connection to it is over. Returns
        how many seconds it lasted."""
        name = channel.channel
        loop = asyncio.get_running_loop()
        async with self._connect(room_id, channel.login) as comments:
            began = loop.time()
            self.failing.pop(name, None)
            self.reading[name] = datetime.now(timezone.utc)
            logger.info("tiktok chat: reading the chat of %s", name)
            try:
                while True:
                    first = await comments.get()
                    if first is ENDED:
                        break
                    batch, over = [first], False
                    deadline = loop.time() + BATCH_SECONDS
                    while (left := deadline - loop.time()) > 0:
                        try:
                            more = await asyncio.wait_for(comments.get(), left)
                        except TimeoutError:
                            break
                        if more is ENDED:
                            over = True
                            break
                        batch.append(more)
                    await self._hand_on(channel, batch)
                    if over:
                        break
            finally:
                self.reading.pop(name, None)
            logger.info("tiktok chat: the connection to the chat of %s is over", name)
            return loop.time() - began

    async def _hand_on(self, channel: Channel, batch: list[Received]) -> None:
        messages = []
        for received_at, comment in batch:
            try:
                message = chat_message(comment, channel, received_at)
            except (AttributeError, TypeError, ValueError):
                logger.warning("tiktok chat: a comment of %s could not be read", channel.channel, exc_info=True)
                continue
            if message is not None:
                messages.append(message)
        self.last_heard_at = batch[-1][0]
        if not messages:
            return
        try:
            await self._take(messages)
        except Exception:
            # Lost, but not the chat that comes next.
            logger.exception("tiktok chat: %d message(s) of %s could not be taken in", len(messages), channel.channel)

    def _failed(self, name: str, error: BaseException) -> None:
        why = _why(error)
        self.reading.pop(name, None)
        if name not in self.failing:
            logger.warning("tiktok chat: the chat of %s could not be read (%s)", name, why)
        since = self.failing[name][1] if name in self.failing else datetime.now(timezone.utc)
        self.failing[name] = (why, since)

    def _forget(self, name: str) -> None:
        for kept in (self.reading, self.failing):
            kept.pop(name, None)
        self.waiting.discard(name)
