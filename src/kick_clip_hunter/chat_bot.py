"""The bot in chat: what it writes in a channel's chat, and how.

A channel can have the bot write in its chat - its "Write in chat" setting,
off to begin with, like everything else here (see tuning.py). It then says
there when a recording of the stream starts and when it ends, when a clip
of it was made if that is asked for too, and it answers the commands the
channel's streamer and moderators post: start a recording, stop it, say how
things stand. What it says is worded here, in sentences short enough for a
chat (the `*_words` functions), from what main.py tells it; main.py decides
when, and carries the commands out.

It writes as an account, each platform's own way (`Writer`): on Kick through
the public API, as the account signed in for the bot (kick_auth.py); on
Twitch through Helix, as the account the service reads chat as
(twitch_auth.py), once that has let it write. YouTube's chat is read as a
visitor who is not signed in, so there the bot writes nothing - its
commands are carried out all the same.

Writing holds nothing up and never fails loudly. A message goes out in the
background, and one that can't be written is logged and kept for the Health
page, as is when one last went out in each channel (`ChatBot.written`). The
same message is not written in the same chat twice within REPEAT_SECONDS, so
a moderator asking twice in a row gets one answer; and a command a platform
delivers twice is carried out once (`Recent`).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

import httpx

from . import account_tokens, kick_auth, kick_client, stream_recording, twitch_auth, twitch_client
from .account_tokens import AccountToken, SignInNeeded
from .dashboard_view import count_words, stream_time_words

logger = logging.getLogger("kick_clip_hunter")

# What a chat takes: Kick and Twitch both take 500 characters at the most.
MAX_LENGTH = 500
# The same message is not written in the same chat again for this long.
REPEAT_SECONDS = 30
# How many command messages are remembered, to carry each out once.
REMEMBERED_COMMANDS = 1000

# Where an account the bot writes as is signed in, for what it says when
# one has to be.
KICK_SIGN_IN = "on the Health page"
TWITCH_SIGN_IN = "with scripts/twitch_login.py"


@dataclass(frozen=True)
class Channel:
    """A channel the bot may write in: its id in the database, its name on
    the watchlist, its platform and its id there."""

    broadcaster_user_id: int
    slug: str
    platform: str
    platform_user_id: str


# -- what it says -------------------------------------------------------------


def length_words(seconds: float) -> str:
    """A length of time as a chat says it: seconds within a minute, minutes
    within an hour, hours and minutes past it."""
    whole = max(0, round(seconds))
    if whole < 60:
        return count_words(whole, "second")
    hours, minutes = divmod(whole // 60, 60)
    if not hours:
        return count_words(minutes, "minute")
    return f"{count_words(hours, 'hour')} {count_words(minutes, 'minute')}" if minutes else count_words(hours, "hour")


def started_words(live: bool | None) -> str:
    """What the bot says when a recording of the stream starts - whoever
    started it. `live` is whether the channel was last seen live; None where
    it has not been looked at, which is taken to be live: someone asking in
    chat is watching the stream."""
    if live is False:
        return "The stream will be recorded from when it starts."
    return "Recording the stream from now on."


# Why a recording ended, as the bot says it (the keys are stream_recording.py's).
ENDED_WORDS = {
    stream_recording.STOPPED: "Recording stopped.",
    stream_recording.STREAM_ENDED: "The stream is over.",
    stream_recording.DISK_FULL: "Recording stopped: the disk is nearly full.",
    stream_recording.WRITE_FAILED: "Recording stopped: its footage could not be written.",
    stream_recording.SHUTDOWN: "Recording stopped: the bot is shutting down.",
}


def ended_words(ended_by: str, kept: bool, seconds: float = 0.0) -> str:
    """What the bot says when a recording of the stream ends: why, and how
    much of the stream it holds. `kept` is whether it holds any footage at
    all, `seconds` how long that runs where it could be measured."""
    said = ENDED_WORDS.get(ended_by, ENDED_WORDS[stream_recording.STOPPED])
    if not kept:
        return f"{said} Nothing had been recorded yet."
    if not seconds:
        return f"{said} What was recorded is being saved."
    return f"{said} {length_words(seconds)} of the stream recorded."


def recording_already_words(seconds: float, waiting: bool) -> str:
    """The answer to a command to record a stream that is being recorded."""
    if waiting:
        return "Already recording, from when the stream starts."
    return f"Already recording: {length_words(seconds)} so far."


NOT_RECORDING_WORDS = "Not recording."
# The answers to a command to record that could not be carried out: the disk
# is too full (`True`), or there was nowhere to keep a recording.
NOT_STARTED_WORDS = {
    True: "Cannot record: the disk is too full.",
    False: "Cannot record: the recording could not be started.",
}


@dataclass(frozen=True)
class Status:
    """How things stand for a channel, as the bot is asked."""

    # The service's own switch, and the channel's: whether it looks for clips.
    watching: bool
    tracked: bool
    # How many clips were made of the stream that is on; None where it is
    # not known since when it is on.
    clips: int | None = None
    # Whether its stream is being recorded, how long what was kept runs,
    # and whether nothing has been yet (the stream has not begun).
    recording: bool = False
    seconds: float = 0.0
    waiting: bool = False
    # Whether its last recording is being made into its file.
    saving: bool = False


def status_words(status: Status) -> str:
    """The answer to a command asking how things stand: clips, and the
    recording."""
    if not status.watching:
        clips = "Not looking for clips: watching is paused."
    elif not status.tracked:
        clips = "Not looking for clips here."
    elif status.clips is None:
        clips = "Looking for clips."
    elif status.clips == 0:
        clips = "Looking for clips, none yet this stream."
    else:
        clips = f"Looking for clips, {count_words(status.clips, 'clip')} this stream so far."
    if status.recording and status.waiting:
        recording = "Recording from when the stream starts."
    elif status.recording:
        recording = f"Recording: {length_words(status.seconds)} so far."
    elif status.saving:
        recording = "Not recording - the last recording is being saved."
    else:
        recording = NOT_RECORDING_WORDS
    return f"{clips} {recording}"


def clip_words(seconds: float, stream_seconds: int | None) -> str:
    """What the bot says when a clip's file is saved: how long the clip is,
    and how far into the stream chat reacted."""
    into = stream_time_words(stream_seconds)
    where = f", at {into} into the stream" if into else ""
    return f"Clip saved: {length_words(seconds)}{where}."


# -- how it writes ------------------------------------------------------------


class CannotWrite(Exception):
    """Why a message could not be written, in words for the Health page.
    `sign_in` where that is the account's - none has signed in, or one
    that did not let the bot write - which the account's keeper says for
    as long as it holds, rather than the channel."""

    def __init__(self, words: str, *, sign_in: bool = False) -> None:
        super().__init__(words)
        self.words = words
        self.sign_in = sign_in


class Writer(Protocol):
    """How the bot writes on one platform."""

    async def write(self, channel: Channel, text: str) -> None:
        """Writes `text` in the channel's chat, or raises CannotWrite."""


def _said(response: httpx.Response) -> str:
    """What a platform said of a request it turned down, as a status and
    its message."""
    try:
        answer = response.json()
    except ValueError:
        answer = None
    message = answer.get("message") if isinstance(answer, dict) else None
    return f"{response.status_code} {message}" if message else str(response.status_code)


async def _token(tokens: account_tokens.TokenKeeper, sign_in: str, *, renew_now: bool = False) -> AccountToken:
    try:
        return await tokens.token(renew_now=renew_now)
    except SignInNeeded as missing:
        raise CannotWrite(f"{missing} Sign one in {sign_in}.", sign_in=True) from None


async def _as_account(
    tokens: account_tokens.TokenKeeper,
    scope: str,
    platform: str,
    sign_in: str,
    send: Callable[[AccountToken], Awaitable[dict]],
) -> dict:
    """What `send` gets from the platform, asked with the account's token:
    once more with a renewed one if the platform turned that down, the way
    it does one that ran out or was taken back. CannotWrite if there is no
    account, one that did not let the bot write in chat (`scope`) - which a
    renewal can tell of one kept before that was known - or one whose token
    the platform does not take even renewed."""
    for renew_now in (False, True):
        token = await _token(tokens, sign_in, renew_now=renew_now)
        if token.may(scope) is False:
            raise CannotWrite(
                f"{token.login} did not let the bot write in chat when it signed in. Sign it in again {sign_in}.",
                sign_in=True,
            )
        try:
            return await send(token)
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 401:
                raise
            refused = error.response
    raise CannotWrite(
        f"{platform} does not let {token.login} write in chat ({_said(refused)}). Sign it in again {sign_in}."
    )


class KickWriter:
    """Writes in Kick's chat as the account signed in for the bot."""

    def __init__(self, tokens: kick_auth.TokenKeeper) -> None:
        self._tokens = tokens

    async def write(self, channel: Channel, text: str) -> None:
        async def send(token: AccountToken) -> dict:
            return await kick_client.send_chat_message(int(channel.platform_user_id), text, token.access_token)

        try:
            sent = await _as_account(self._tokens, kick_auth.WRITE_SCOPE, "Kick", KICK_SIGN_IN, send)
        except httpx.HTTPStatusError as error:
            raise CannotWrite(f"Kick did not take the message ({_said(error.response)}).") from None
        if sent.get("is_sent") is False:
            raise CannotWrite("Kick did not send the message.")


class TwitchWriter:
    """Writes in Twitch's chat as the account the service reads chat as."""

    def __init__(self, tokens: twitch_auth.TokenKeeper, client_id: str) -> None:
        self._tokens = tokens
        self._client_id = client_id

    async def write(self, channel: Channel, text: str) -> None:
        async def send(token: AccountToken) -> dict:
            return await twitch_client.send_chat_message(
                channel.platform_user_id, token.user_id, text, self._client_id, token.access_token
            )

        try:
            sent = await _as_account(self._tokens, twitch_auth.WRITE_SCOPE, "Twitch", TWITCH_SIGN_IN, send)
        except httpx.HTTPStatusError as error:
            raise CannotWrite(f"Twitch did not take the message ({_said(error.response)}).") from None
        if sent.get("is_sent") is False:
            dropped = sent.get("drop_reason")
            why = (dropped.get("message") or dropped.get("code")) if isinstance(dropped, dict) else None
            raise CannotWrite(f"Twitch did not send the message: {why}." if why else "Twitch did not send the message.")


@dataclass
class Written:
    """What became of the bot's messages in one channel: when one last went
    out, and why the last one did not, if it did not."""

    sent_at: datetime | None = None
    trouble: str | None = None


class ChatBot:
    """Writes in channels' chat (`say`) and keeps what became of it, for the
    Health page. `writer` is asked, message by message, how the bot writes
    on a channel's platform: None where it can't."""

    def __init__(self, writer: Callable[[str], Writer | None], clock: Callable[[], float] = time.monotonic) -> None:
        self._writer = writer
        self._clock = clock
        # When each message was last written, by channel and text.
        self._said: dict[tuple[int, str], float] = {}
        # What became of the messages in each channel, by its id in the database.
        self.written: dict[int, Written] = {}

    def _repeated(self, channel: Channel, text: str) -> bool:
        """Whether the message was written in the channel just now - and
        if not, that it is being written now."""
        now = self._clock()
        for key, at in list(self._said.items()):
            if now - at >= REPEAT_SECONDS:
                del self._said[key]
        key = (channel.broadcaster_user_id, text)
        if key in self._said:
            return True
        self._said[key] = now
        return False

    async def say(self, channel: Channel, text: str) -> bool:
        """Writes `text` in the channel's chat, unless it was written there
        just now or the bot can't write on the channel's platform. Returns
        whether it went out; never raises."""
        text = " ".join(text.split())[:MAX_LENGTH]
        writer = self._writer(channel.platform)
        if writer is None or not text or self._repeated(channel, text):
            return False
        written = self.written.setdefault(channel.broadcaster_user_id, Written())
        of_account = False
        try:
            await writer.write(channel, text)
        except CannotWrite as refused:
            why, of_account = refused.words, refused.sign_in
        except httpx.HTTPError as error:
            why = f"{type(error).__name__}: {error}" if str(error) else type(error).__name__
        except Exception:
            # Whatever went wrong, it is not worth more than this message.
            logger.exception("[%s] the bot could not write in chat", channel.slug)
            why = "Something went wrong. The server log says what."
        else:
            written.sent_at, written.trouble = datetime.now(timezone.utc), None
            logger.info("[%s] the bot wrote in chat: %s", channel.slug, text)
            return True
        # The account's trouble is said as long as it holds, and no longer -
        # a sign-in ends it, not the next message.
        written.trouble = None if of_account else why
        logger.warning("[%s] the bot could not write in chat: %s", channel.slug, why)
        return False


class Recent:
    """The ids of the last REMEMBERED_COMMANDS command messages. A platform
    delivers a message again when it did not see it taken in time - Kick
    does - which would have its command carried out twice."""

    def __init__(self, size: int = REMEMBERED_COMMANDS) -> None:
        self._size = size
        # In the order they came: a dict keeps it.
        self._ids: dict[str, None] = {}

    def first_time(self, message_id: str) -> bool:
        """Whether the message has not come before - and from now on, that it has."""
        if message_id in self._ids:
            return False
        self._ids[message_id] = None
        if len(self._ids) > self._size:
            del self._ids[next(iter(self._ids))]
        return True
