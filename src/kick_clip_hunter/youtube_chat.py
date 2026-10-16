"""YouTube chat, read while a channel is live.

A YouTube channel's chat lives as long as one of its streams does, and
nothing pushes it anywhere: it is read the way the site's own chat window
reads it (youtube_client.py). The chat of the stream a channel is streaming
is opened, with every message in it, and looked at again and again, each
look bringing what was written since the one before. So the reader keeps a
task for every YouTube channel on the watchlist (`run_forever`): while the
channel is not live it looks every LOOK_SECONDS whether it has gone live;
once it has, its chat is read until the chat says it is over - the stream
has ended - and the channel is looked at again. A channel added to the
watchlist is taken up within CHANNELS_SECONDS.

How often a chat is looked at follows how busy it is: soon again after a
look that brought something, less and less often after looks that brought
nothing (POLL_SECONDS) - much as often as the site's own chat window asks
when it is told there is more. The first look at a chat brings what was
written before the reader came, which is left out: it would arrive all at
once, as if just written. So is what is written while the reader cannot
look: a chat it has to open again is read from then on, as Twitch's is after
a lost connection.

A look brings what was written over the last second or few at once, so the
messages of a look are not taken to have arrived when it was made: each is
taken to have arrived when YouTube says it was sent - never after the look,
and in the order they came (`arrived`). That spreads them against the
picture as they were written, where they would otherwise land in bursts.
The detector still counts them as they are handed on.

A message is turned into a ChatMessage (chat_message.py). YouTube sends
every emoji as a picture of its own, and those stand in the text as tokens
like Kick's emotes - a channel's own and YouTube's as
[emote:youtube.<picture>:<name>], the ones everyone has as
[emote:emoji.<code points>:<name>] (see dashboard_view.NATIVE_EMOTE_IMAGES);
an emoji that cannot is kept as it would be typed. The sender's badges are
put in the form chat_identity.py keeps. A Super Chat with words in it is a
message like any other; what the chat says of members, gifts and itself is
not taken in.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

import httpx

from . import chat_identity, platforms, youtube_client
from .chat_message import ChatMessage, typed

logger = logging.getLogger("kick_clip_hunter")

# How often the watchlist is looked at again for channels added since.
CHANNELS_SECONDS = 30
# How often a channel that is not live is looked at, whether it has gone
# live - as often as the recording loop looks at it.
LOOK_SECONDS = 30
# How long a stream whose chat cannot be read (turned off, or for members
# only) is left before its chat is tried again.
UNAVAILABLE_SECONDS = 300
# How long until the next look at a chat: after a look that brought
# messages, the first; after each further one that brought none, the next,
# up to the last.
POLL_SECONDS = (1.5, 2.0, 3.0, 5.0)
# How long until the next look after one that failed, growing with every
# failure in a row; one more after the last of them, and the chat is opened
# anew.
RETRY_SECONDS = (2.0, 5.0, 15.0)

# The chat items that are messages someone wrote: a plain one, and a Super
# Chat - the words that came with one, if any did.
MESSAGE_ITEMS = ("liveChatTextMessageRenderer", "liveChatPaidMessageRenderer")
# What stands before the id of an emoji's token: a picture on YouTube's own
# host, or one of the emoji everyone has, by its code points.
CUSTOM_EMOJI_PREFIX = "youtube."
EMOJI_PREFIX = "emoji."
# What an emoji's name may be to stand in a token (see
# detector.NATIVE_EMOTE_TOKEN_PATTERN), and where its picture may be.
_EMOTE_NAME = re.compile(r"[^\]\s:]{1,64}")
_CUSTOM_PICTURE = re.compile(r"https://yt3\.ggpht\.com/([0-9A-Za-z_-]{1,200})(?:=|$)")
_EMOJI_PICTURE = re.compile(r"https://fonts\.gstatic\.com/s/e/notoemoji/[^/]+/([0-9a-f]{1,8}(?:_[0-9a-f]{1,8}){0,15})/")


@dataclass(frozen=True)
class Channel:
    """A YouTube channel whose chat is read."""

    # The id it is kept by in the database (see platforms.py).
    broadcaster_user_id: int
    # Its name in the service, `handle@youtube`.
    channel: str

    @property
    def login(self) -> str:
        """Its name on YouTube: its handle (or its id, for one without)."""
        return platforms.login_of(self.channel)


def _at(value: object, *path: str) -> Any:
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _emoji_token(emoji: Mapping[str, Any]) -> tuple[str, str] | None:
    """The id and the name an emoji stands in a message's text as - or None
    for one whose name or picture could not stand in a token."""
    shortcuts = emoji.get("shortcuts")
    first = shortcuts[0] if isinstance(shortcuts, list) and shortcuts else None
    name = first.strip(":") if isinstance(first, str) else ""
    thumbnails = _at(emoji, "image", "thumbnails")
    picture = _at(thumbnails[0], "url") if isinstance(thumbnails, list) and thumbnails else None
    if not _EMOTE_NAME.fullmatch(name) or not isinstance(picture, str):
        return None
    custom = bool(emoji.get("isCustomEmoji"))
    found = (_CUSTOM_PICTURE if custom else _EMOJI_PICTURE).match(picture)
    if found is None:
        return None
    return f"{CUSTOM_EMOJI_PREFIX if custom else EMOJI_PREFIX}{found.group(1)}", name


def _emoji_as_typed(emoji: Mapping[str, Any]) -> str:
    """An emoji that cannot be a token, as it would be typed: one everyone
    has as itself, a channel's own as its shortcut (":name:")."""
    if not emoji.get("isCustomEmoji") and isinstance(emoji.get("emojiId"), str):
        return emoji["emojiId"]
    shortcuts = emoji.get("shortcuts")
    return shortcuts[0] if isinstance(shortcuts, list) and shortcuts and isinstance(shortcuts[0], str) else ""


def _content(runs: Iterable[Any]) -> tuple[str, list[dict[str, Any]]]:
    """A message's text with YouTube's emoji in it as tokens, and where each
    one stands in it, as Kick lists its emotes."""
    parts: list[str] = []
    positions: dict[str, list[dict[str, int]]] = {}
    length = 0
    for run in runs:
        if not isinstance(run, Mapping):
            continue
        emoji = run.get("emoji")
        token = _emoji_token(emoji) if isinstance(emoji, Mapping) else None
        if token is not None:
            emote_id, name = token
            piece = f"[emote:{emote_id}:{name}]"
            positions.setdefault(emote_id, []).append({"s": length, "e": length + len(piece) - 1})
        elif isinstance(emoji, Mapping):
            piece = typed(_emoji_as_typed(emoji))
        else:
            text = run.get("text")
            piece = typed(text) if isinstance(text, str) else ""
        parts.append(piece)
        length += len(piece)
    return "".join(parts), [{"emote_id": emote_id, "positions": where} for emote_id, where in positions.items()]


def _sent_at(microseconds: object) -> str:
    """When YouTube says a message was sent, as it is kept."""
    if not isinstance(microseconds, str) or not microseconds.isdigit():
        return ""
    return datetime.fromtimestamp(int(microseconds) / 1_000_000, timezone.utc).isoformat()


def chat_message(item: Mapping[str, Any], channel: Channel, received_at: datetime | None = None) -> ChatMessage | None:
    """What a chat item says, as the service takes chat in - or None for one
    that is not a message someone wrote, or has no words in it."""
    message = next((item[kind] for kind in MESSAGE_ITEMS if isinstance(item.get(kind), Mapping)), None)
    if message is None:
        return None
    message_id, runs = message.get("id"), _at(message, "message", "runs")
    if not isinstance(message_id, str) or not message_id or not isinstance(runs, list):
        return None
    content, emotes = _content(runs)
    if not content.strip():
        return None
    identity = chat_identity.from_youtube(message.get("authorBadges"))
    badges = {badge["type"] for badge in json.loads(identity[1])}
    sender = _at(message, "authorName", "simpleText")
    return ChatMessage(
        broadcaster_user_id=channel.broadcaster_user_id,
        channel=channel.channel,
        message_id=message_id,
        # YouTube writes a handle with an "@" before it, Kick and Twitch
        # their names without.
        sender=sender.removeprefix("@") if isinstance(sender, str) else "",
        content=content,
        created_at=_sent_at(message.get("timestampUsec")),
        emotes=emotes,
        identity=identity,
        runs_channel=bool(badges & chat_identity.RUNNING_BADGES),
        received_at=received_at,
    )


def messages(
    actions: Iterable[Mapping[str, Any]],
    channel: Channel,
    received_at: datetime | None = None,
    not_before: datetime | None = None,
) -> list[ChatMessage]:
    """The messages someone wrote among what a look at a chat brought, in
    the order they came.

    With `received_at`, the time of the look, each is taken to have arrived
    when YouTube says it was sent (see `arrived`), so that the messages of a
    look are not all put at one instant; without it, "just now"."""
    taken = _messages(actions, channel, received_at)
    if received_at is None:
        return taken
    times = arrived(taken, received_at, not_before)
    return [replace(message, received_at=when) for message, when in zip(taken, times, strict=True)]


def arrived(taken: Sequence[ChatMessage], looked_at: datetime, not_before: datetime | None = None) -> list[datetime]:
    """When the messages a look brought are taken to have arrived: each when
    YouTube says it was sent.

    Nothing pushes YouTube's chat, so the time of the look itself is up to
    POLL_SECONDS after a message was written - and the same for every
    message it brought, which would put them against the picture in bursts.
    Their own send times spread them out as they were written. Never later
    than the look (YouTube's clock may run ahead of this machine's), never
    earlier than `not_before` (what the reader handed on before, so a chat's
    messages stay in the order they came in, across looks too), and never
    earlier than the message before - one that says nothing of when it was
    sent arrived with the look."""
    times: list[datetime] = []
    least = not_before
    for message in taken:
        sent = datetime.fromisoformat(message.created_at) if message.created_at else looked_at
        when = min(sent, looked_at)
        if least is not None:
            when = max(when, least)
        times.append(when)
        least = when
    return times


def _messages(
    actions: Iterable[Mapping[str, Any]], channel: Channel, received_at: datetime | None
) -> list[ChatMessage]:
    taken: list[ChatMessage] = []
    for action in actions:
        item = _at(action, "addChatItemAction", "item")
        if not isinstance(item, Mapping):
            continue
        try:
            message = chat_message(item, channel, received_at)
        except (KeyError, TypeError, ValueError):
            logger.warning("youtube chat: a message of %s could not be read", channel.channel, exc_info=True)
            continue
        if message is not None:
            taken.append(message)
    return taken


def _why(error: BaseException) -> str:
    """What went wrong, in a few words."""
    if isinstance(error, httpx.HTTPStatusError):
        return f"YouTube answered {error.response.status_code}"
    if isinstance(error, httpx.HTTPError) or not str(error):
        return type(error).__name__
    return f"{type(error).__name__}: {error}"


class ChatReader:
    """Reads the chat of every YouTube channel on the watchlist while it is
    live, for as long as it runs (`run_forever`). `channels` says whose chat
    is wanted, `take` is handed what was written, a look at a chat at a
    time. `now` is the clock, when the service's is not wanted."""

    def __init__(
        self,
        channels: Callable[[], Iterable[Channel]],
        take: Callable[[list[ChatMessage]], Awaitable[object]],
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._channels = channels
        self._take = take
        self._now = now or (lambda: datetime.now(timezone.utc))
        # For the Health page, by channel: whose chat is being read and
        # since when, the live ones whose chat cannot be and why, the ones
        # YouTube could not be asked about and why, since when - and how
        # many channels are watched, and when YouTube last answered.
        self.reading: dict[str, datetime] = {}
        self.unavailable: dict[str, str] = {}
        self.failing: dict[str, tuple[str, datetime]] = {}
        self.watched = 0
        self.last_heard_at: datetime | None = None

    async def run_forever(self) -> None:
        followed: dict[str, asyncio.Task] = {}
        # One client for every channel, held open: a look at a chat every
        # second or two goes over a connection that is there already.
        async with youtube_client.new_client() as client:
            try:
                while True:
                    try:
                        wanted = {channel.channel: channel for channel in self._channels()}
                    except Exception:
                        logger.exception("youtube chat: could not tell whose chat to read")
                        await asyncio.sleep(CHANNELS_SECONDS)
                        continue
                    self.watched = len(wanted)
                    for name in list(followed):
                        if name not in wanted:
                            followed.pop(name).cancel()
                            self._forget(name)
                    for name, channel in wanted.items():
                        if name not in followed:
                            followed[name] = asyncio.create_task(self._follow(channel, client))
                    await asyncio.sleep(CHANNELS_SECONDS)
            finally:
                for task in followed.values():
                    task.cancel()
                await asyncio.gather(*followed.values(), return_exceptions=True)

    async def _follow(self, channel: Channel, client: httpx.AsyncClient) -> None:
        """Reads a channel's chat whenever it is live, until it is no longer
        watched."""
        while True:
            try:
                video = await youtube_client.live_video(channel.login, client)
                self._heard(channel.channel)
                if video is None:
                    self.reading.pop(channel.channel, None)
                    self.unavailable.pop(channel.channel, None)
                    await asyncio.sleep(LOOK_SECONDS)
                    continue
                await self._read(channel, video.video_id, client)
                # Its chat is over, or could not be read: the channel is
                # looked at again in a while.
                await asyncio.sleep(LOOK_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Whatever went wrong, the channel is looked at again.
                self._failed(channel.channel, error)
                await asyncio.sleep(LOOK_SECONDS)

    async def _read(self, channel: Channel, video_id: str, client: httpx.AsyncClient) -> None:
        """Reads the chat of a stream until it is over - or until it cannot
        be read, after a few failed looks in a row."""
        name = channel.channel
        try:
            chat = await youtube_client.open_chat(video_id, client)
        except youtube_client.ChatUnavailable as why:
            if self.unavailable.get(name) != str(why):
                logger.info("youtube chat: the chat of %s cannot be read: %s", name, why)
            self.unavailable[name] = str(why)
            self.reading.pop(name, None)
            await asyncio.sleep(UNAVAILABLE_SECONDS)
            return
        self.unavailable.pop(name, None)
        # What the first look brings was written before the reader came.
        continuation = (await youtube_client.look_at_chat(chat, client)).continuation
        # What comes after it was written since; nothing of it arrives
        # earlier than this (see `arrived`).
        handed_on = self._now()
        self._heard(name)
        if name not in self.reading:
            logger.info("youtube chat: reading the chat of %s", name)
            self.reading[name] = self._now()
        quiet = failures = 0
        while continuation is not None:
            await asyncio.sleep(
                RETRY_SECONDS[failures - 1] if failures else POLL_SECONDS[min(quiet, len(POLL_SECONDS) - 1)]
            )
            try:
                page = await youtube_client.look_at_chat(chat.at(continuation), client)
            except (httpx.HTTPError, youtube_client.YouTubeChanged) as error:
                self._failed(name, error)
                failures += 1
                if failures > len(RETRY_SECONDS):
                    return
                continue
            failures = 0
            self._heard(name)
            continuation = page.continuation
            batch = messages(page.actions, channel, self._now(), handed_on)
            quiet = 0 if batch else quiet + 1
            if not batch:
                continue
            handed_on = batch[-1].received_at or handed_on
            try:
                await self._take(batch)
            except Exception:
                # Lost, but not the chat that comes next.
                logger.exception("youtube chat: %d message(s) of %s could not be taken in", len(batch), name)
        self.reading.pop(name, None)
        logger.info("youtube chat: the chat of %s is over", name)

    def _heard(self, name: str) -> None:
        self.last_heard_at = self._now()
        if self.failing.pop(name, None) is not None:
            logger.info("youtube chat: YouTube answers about %s again", name)

    def _failed(self, name: str, error: BaseException) -> None:
        why = _why(error)
        if name not in self.failing:
            logger.warning("youtube chat: YouTube could not be asked about %s (%s)", name, why)
        since = self.failing[name][1] if name in self.failing else self._now()
        self.failing[name] = (why, since)

    def _forget(self, name: str) -> None:
        for kept in (self.reading, self.unavailable, self.failing):
            kept.pop(name, None)
