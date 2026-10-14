"""Twitch chat, read over an EventSub WebSocket.

Twitch pushes a watched channel's chat messages over a WebSocket the service
keeps open (EVENTSUB_URL): unlike Kick's webhooks, nothing has to reach the
service from outside. Every new connection is greeted with a session of its
own (`session_welcome`), and the chat of every Twitch channel on the
watchlist is subscribed to that session (`channel.chat.message`) on behalf
of the account the service reads chat as (twitch_auth.py) - the first within
ten seconds, or Twitch closes the connection as unused.

What follows is chat (`notification`), signs of life (`session_keepalive`,
at least every KEEPALIVE_SECONDS while chat is quiet), a request to move to
another server (`session_reconnect`: the new connection keeps the
subscriptions, and the old one is closed once the new one is greeted), or
word that a subscription is over (`revocation`: the account lost the right
to read that chat, or the channel is gone). A connection that has said
nothing for longer than it promised is taken to be lost. Chat sent while
there is none is gone for good, so the service connects again at once, and
subscribes everything again - a subscription lives as long as its session.

A message is turned into a ChatMessage (chat_message.py): Twitch's own
emotes, which come as fragments of the message, stand in its text as tokens
like Kick's, with Twitch before their id ([emote:twitch.<id>:<name>]), and
the sender's colour and badges are put in the form chat_identity.py keeps.
A message of another channel's chat, shown in this one during a shared chat
session, is left out: it was written watching another stream. Chat is
handed on a batch at a time (BATCH_SECONDS), to be stored in one go.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from . import chat_identity, twitch_client
from .chat_message import ChatMessage
from .twitch_auth import AccountToken, SignInNeeded, TokenKeeper

logger = logging.getLogger("kick_clip_hunter")

EVENTSUB_URL = "wss://eventsub.wss.twitch.tv/ws"
# How often Twitch is asked to say something while chat is quiet (it allows
# 10 to 600), and how much longer than that a connection is given before it
# is taken to be lost.
KEEPALIVE_SECONDS = 30
SILENCE_GRACE_SECONDS = 10
# How long the greeting may take to come.
WELCOME_SECONDS = 15
# How often the watchlist is looked at again for channels to subscribe - one
# added since, or one whose subscription was refused before.
CHANNELS_SECONDS = 30
# How long a refused subscription is left before it is asked for again.
REFUSED_SECONDS = 600
# How long to wait before connecting again after a connection was lost or
# refused, growing with every failure in a row up to the last.
RETRY_SECONDS = (1, 5, 15, 60)
# Chat is handed on once it has waited this long, or this much has come.
BATCH_SECONDS = 0.25
BATCH_LIMIT = 500
# How many subscriptions are asked for at once.
SUBSCRIBING_AT_ONCE = 8

CHAT_MESSAGE = "channel.chat.message"
# What stands before a Twitch emote's id in a token.
EMOTE_PREFIX = "twitch."
# What an emote's id and name may be to stand in a token (see
# detector.NATIVE_EMOTE_TOKEN_PATTERN): a name ends the token at "]".
_EMOTE_ID = re.compile(r"[0-9A-Za-z_]{1,64}")
_EMOTE_NAME = re.compile(r"[^\]\s]{1,64}")
# Text a viewer typed that would read as a token - kept, but so that it does not.
_TYPED_TOKEN = "[emote:"
_HARMLESS_TOKEN = "[emote​:"


@dataclass(frozen=True)
class Channel:
    """A Twitch channel whose chat is read."""

    # The id it is kept by in the database (see platforms.py).
    broadcaster_user_id: int
    # Its name in the service, `login@twitch`.
    channel: str
    # Its id on Twitch.
    twitch_id: str


def _content(fragments: Iterable[Any]) -> tuple[str, list[dict[str, Any]]]:
    """A message's text with Twitch's emotes in it as tokens, and where each
    emote stands in it, as Kick lists its own."""
    parts: list[str] = []
    positions: dict[str, list[dict[str, int]]] = {}
    length = 0
    for fragment in fragments:
        if not isinstance(fragment, Mapping):
            continue
        text = fragment.get("text") if isinstance(fragment.get("text"), str) else ""
        emote = fragment.get("emote") if fragment.get("type") == "emote" else None
        emote_id = emote.get("id") if isinstance(emote, Mapping) else None
        if isinstance(emote_id, str) and _EMOTE_ID.fullmatch(emote_id) and _EMOTE_NAME.fullmatch(text):
            piece = f"[emote:{EMOTE_PREFIX}{emote_id}:{text}]"
            positions.setdefault(emote_id, []).append({"s": length, "e": length + len(piece) - 1})
        else:
            piece = text.replace(_TYPED_TOKEN, _HARMLESS_TOKEN)
        parts.append(piece)
        length += len(piece)
    emotes = [{"emote_id": f"{EMOTE_PREFIX}{emote_id}", "positions": where} for emote_id, where in positions.items()]
    return "".join(parts), emotes


def chat_message(event: Mapping[str, Any], channel: Channel, sent_at: str = "", received_at=None) -> ChatMessage | None:
    """What a `channel.chat.message` event says, as the service takes chat
    in - or None for one of another channel's chat, shown in this one during
    a shared chat session."""
    source = event.get("source_broadcaster_user_id")
    if source and source != event.get("broadcaster_user_id"):
        return None
    message = event.get("message") if isinstance(event.get("message"), Mapping) else {}
    fragments = message.get("fragments") or [{"type": "text", "text": message.get("text") or ""}]
    content, emotes = _content(fragments)
    badges = event.get("badges") if isinstance(event.get("badges"), list) else []
    runs = {badge.get("set_id") for badge in badges if isinstance(badge, Mapping)} & {"broadcaster", "moderator"}
    return ChatMessage(
        broadcaster_user_id=channel.broadcaster_user_id,
        channel=channel.channel,
        message_id=str(event["message_id"]),
        sender=event.get("chatter_user_name") or event.get("chatter_user_login") or "",
        content=content,
        created_at=sent_at,
        emotes=emotes,
        identity=chat_identity.from_twitch(event.get("color"), badges),
        runs_channel=bool(runs) or event.get("chatter_user_id") == event.get("broadcaster_user_id"),
        received_at=received_at,
    )


class ConnectionLost(RuntimeError):
    """The connection to Twitch went quiet, or said what it should not."""


class ChatReader:
    """Keeps the connection Twitch's chat comes over, and its subscriptions,
    for as long as it runs (`run_forever`). `channels` says whose chat is
    wanted, `take` is handed what arrives a batch at a time."""

    def __init__(
        self,
        channels: Callable[[], Iterable[Channel]],
        take: Callable[[list[ChatMessage]], Awaitable[object]],
        tokens: TokenKeeper,
        client_id: str,
        url: str | None = None,
    ) -> None:
        self._channels = channels
        self._take = take
        self._tokens = tokens
        self._client_id = client_id
        self._url = url or EVENTSUB_URL
        # For the Health page: since when a connection has been open (None
        # while there is none), when it last said anything, whose chat is
        # subscribed on it, whose was refused and why, and what is wrong
        # when something is - since when, and whether it takes an account
        # signed in again.
        self.connected_since: datetime | None = None
        self.last_heard_at: datetime | None = None
        self.subscribed: dict[str, Channel] = {}
        self.refused: dict[str, str] = {}
        self.trouble: str | None = None
        self.trouble_since: datetime | None = None
        self.needs_sign_in = False
        self._refused_at: dict[str, float] = {}
        # Every channel whose chat was wanted the last time that was asked,
        # by its Twitch id.
        self._known: dict[str, Channel] = {}
        # The connection chat is coming over right now.
        self._socket: ClientConnection

    async def run_forever(self) -> None:
        failures = 0
        while True:
            try:
                wanted = self._wanted()
                if not wanted:
                    # Nothing to read: a connection without a subscription
                    # is closed by Twitch as unused.
                    self.trouble, self.trouble_since, self.needs_sign_in = None, None, False
                    await asyncio.sleep(CHANNELS_SECONDS)
                    continue
                await self._session(wanted)
                failures = 0
            except SignInNeeded as missing:
                self._lost(str(missing), sign_in=True)
                await asyncio.sleep(CHANNELS_SECONDS)
                continue
            except (OSError, ConnectionClosed, ConnectionLost, httpx.HTTPError, TimeoutError) as error:
                self._lost(f"{type(error).__name__}: {error}" if str(error) else type(error).__name__)
                logger.warning("twitch chat: connection lost (%s) - connecting again", self.trouble)
            except Exception as error:
                # Whatever went wrong, the chat must keep coming in.
                self._lost(type(error).__name__)
                logger.exception("twitch chat: reading it failed - connecting again")
            await asyncio.sleep(RETRY_SECONDS[min(failures, len(RETRY_SECONDS) - 1)])
            failures += 1

    def _lost(self, why: str, *, sign_in: bool = False) -> None:
        self.connected_since = None
        self.subscribed.clear()
        self.trouble, self.needs_sign_in = why, sign_in
        self.trouble_since = self.trouble_since or datetime.now(timezone.utc)

    def _wanted(self) -> dict[str, Channel]:
        self._known = {channel.twitch_id: channel for channel in self._channels()}
        return dict(self._known)

    @property
    def account(self) -> str | None:
        """The name of the account chat is read as, if one is signed in."""
        token = self._tokens.account
        return token.login if token else None

    def refused_channels(self) -> list[tuple[str, str]]:
        """The channels whose chat was refused, by name, and why."""
        return [
            (self._known[twitch_id].channel if twitch_id in self._known else twitch_id, why)
            for twitch_id, why in sorted(self.refused.items())
        ]

    async def _receive(self, socket: ClientConnection, timeout: float) -> dict[str, Any] | None:
        """The next message, or None if none came in `timeout` seconds."""
        try:
            raw = await asyncio.wait_for(socket.recv(), max(timeout, 0.0))
        except TimeoutError:
            return None
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            raise ConnectionLost("Twitch sent something that is not JSON") from None
        if not isinstance(message, dict):
            raise ConnectionLost("Twitch sent something that is not a message")
        self.last_heard_at = datetime.now(timezone.utc)
        return message

    async def _connect(self, url: str) -> tuple[ClientConnection, dict[str, Any]]:
        """A new connection and its session, once it has been greeted."""
        # Twitch takes nothing from a client but pongs to its own pings.
        socket = await connect(url, ping_interval=None, proxy=None, open_timeout=WELCOME_SECONDS)
        try:
            welcome = await self._receive(socket, WELCOME_SECONDS)
            if welcome is None or (welcome.get("metadata") or {}).get("message_type") != "session_welcome":
                raise ConnectionLost("Twitch did not greet the connection")
        except BaseException:
            await socket.close()
            raise
        return socket, welcome["payload"]["session"]

    async def _session(self, wanted: dict[str, Channel]) -> None:
        url = f"{self._url}?keepalive_timeout_seconds={KEEPALIVE_SECONDS}"
        token = await self._tokens.token()
        self._socket, session = await self._connect(url)
        try:
            self.subscribed.clear()
            await self._subscribe(session["id"], wanted, token)
            if not self.subscribed:
                raise ConnectionLost("no channel's chat could be subscribed")
            self.connected_since = datetime.now(timezone.utc)
            self.trouble, self.trouble_since, self.needs_sign_in = None, None, False
            logger.info("twitch chat: reading %d channel(s) as %s", len(self.subscribed), token.login)
            await self._read(session)
        finally:
            await self._socket.close()

    def _due(self, wanted: dict[str, Channel]) -> list[Channel]:
        """The wanted channels whose chat is not subscribed yet, and was not
        refused a short while ago."""
        now = time.monotonic()
        return [
            channel
            for twitch_id, channel in wanted.items()
            if twitch_id not in self.subscribed
            and now - self._refused_at.get(twitch_id, now - REFUSED_SECONDS) >= REFUSED_SECONDS
        ]

    async def _subscribe(self, session_id: str, wanted: dict[str, Channel], token: AccountToken) -> None:
        """Subscribes the chat of the wanted channels that are due (_due)."""
        gate = asyncio.Semaphore(SUBSCRIBING_AT_ONCE)

        async def one(channel: Channel, token: AccountToken) -> None:
            async with gate:
                await self._subscribe_one(session_id, channel, token)

        for attempt in (1, 2):
            results = await asyncio.gather(
                *(one(channel, token) for channel in self._due(wanted)), return_exceptions=True
            )
            failures = [result for result in results if isinstance(result, BaseException)]
            for failure in failures:
                if not isinstance(failure, _TokenTurnedDown):
                    raise failure
            if not failures:
                return
            if attempt == 2:
                raise SignInNeeded("Twitch does not take the token of the account chat is read as.")
            # The token ran out or was taken back since it was looked at:
            # once more with a new one, for what is still missing.
            token = await self._tokens.token(renew_now=True)

    async def _subscribe_one(self, session_id: str, channel: Channel, token: AccountToken) -> None:
        try:
            await twitch_client.subscribe_chat(
                channel.twitch_id, token.user_id, session_id, self._client_id, token.access_token
            )
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            if status == 401:
                raise _TokenTurnedDown from error
            if status != 409:  # 409: subscribed on this session already
                why = f"{status} {_said(error.response)}".strip()
                self.refused[channel.twitch_id] = why
                self._refused_at[channel.twitch_id] = time.monotonic()
                logger.warning("twitch chat: could not subscribe the chat of %s (%s)", channel.channel, why)
                return
        self.subscribed[channel.twitch_id] = channel
        self.refused.pop(channel.twitch_id, None)
        self._refused_at.pop(channel.twitch_id, None)

    async def _subscribe_new(self, session_id: str) -> None:
        """Subscribes channels added to the watchlist since, or refused a
        while ago. A token that cannot be had is said, not a reason to drop
        the chat that is coming in."""
        wanted = self._wanted()
        if not self._due(wanted):
            return
        try:
            await self._subscribe(session_id, wanted, await self._tokens.token())
        except SignInNeeded as missing:
            # What is subscribed comes in still; what is not, and the next
            # connection, wait for an account.
            self.trouble, self.needs_sign_in = str(missing), True
            self.trouble_since = self.trouble_since or datetime.now(timezone.utc)
        except httpx.HTTPError as error:
            logger.warning("twitch chat: could not subscribe new channels (%s) - trying again later", error)

    async def _read(self, session: dict[str, Any]) -> None:
        keepalive = float(session.get("keepalive_timeout_seconds") or KEEPALIVE_SECONDS)
        pending: list[ChatMessage] = []
        hand_on_at: float | None = None
        look_again_at = time.monotonic() + CHANNELS_SECONDS
        heard_at = time.monotonic()
        while True:
            silent_until = heard_at + keepalive + SILENCE_GRACE_SECONDS
            next_due = min(silent_until, look_again_at, hand_on_at if hand_on_at is not None else silent_until)
            message = await self._receive(self._socket, next_due - time.monotonic())
            if message is not None:
                heard_at = time.monotonic()
                kind = (message.get("metadata") or {}).get("message_type")
                if kind == "notification":
                    taken = self._notification(message)
                    if taken is not None:
                        pending.append(taken)
                        hand_on_at = hand_on_at or time.monotonic() + BATCH_SECONDS
                elif kind == "session_reconnect":
                    session = await self._move(message["payload"]["session"]["reconnect_url"])
                    keepalive = float(session.get("keepalive_timeout_seconds") or keepalive)
                elif kind == "revocation":
                    self._revoked(message.get("payload") or {})
            elif time.monotonic() >= silent_until:
                raise ConnectionLost(f"Twitch said nothing for {keepalive + SILENCE_GRACE_SECONDS:.0f} s")
            if pending and (len(pending) >= BATCH_LIMIT or time.monotonic() >= (hand_on_at or 0)):
                batch, pending, hand_on_at = pending, [], None
                try:
                    await self._take(batch)
                except Exception:
                    # Lost, but not the chat that comes next.
                    logger.exception("twitch chat: %d message(s) could not be taken in", len(batch))
            if time.monotonic() >= look_again_at:
                look_again_at = time.monotonic() + CHANNELS_SECONDS
                await self._subscribe_new(session["id"])

    def _notification(self, message: dict[str, Any]) -> ChatMessage | None:
        metadata, payload = message.get("metadata") or {}, message.get("payload") or {}
        event = payload.get("event")
        if metadata.get("subscription_type") != CHAT_MESSAGE or not isinstance(event, Mapping):
            return None
        channel = self.subscribed.get(str(event.get("broadcaster_user_id")))
        if channel is None:
            return None
        try:
            return chat_message(
                event, channel, metadata.get("message_timestamp", ""), received_at=datetime.now(timezone.utc)
            )
        except (KeyError, TypeError, ValueError):
            logger.warning("twitch chat: a message of %s could not be read", channel.channel, exc_info=True)
            return None

    async def _move(self, url: str) -> dict[str, Any]:
        """Moves to the server Twitch asked to, keeping the subscriptions:
        the old connection is closed once the new one has been greeted."""
        moved, session = await self._connect(url)
        old, self._socket = self._socket, moved
        await old.close()
        logger.info("twitch chat: moved to another server, as Twitch asked")
        return session

    def _revoked(self, payload: Mapping[str, Any]) -> None:
        subscription = payload.get("subscription") or {}
        twitch_id = str((subscription.get("condition") or {}).get("broadcaster_user_id"))
        channel = self.subscribed.pop(twitch_id, None)
        why = str(subscription.get("status") or "revoked")
        self.refused[twitch_id] = why
        self._refused_at[twitch_id] = time.monotonic()
        logger.warning(
            "twitch chat: Twitch ended the subscription of %s (%s)", channel.channel if channel else twitch_id, why
        )


class _TokenTurnedDown(Exception):
    """Twitch did not take the account's token for a subscription."""


def _said(response: httpx.Response) -> str:
    try:
        return str(response.json().get("message", ""))
    except ValueError:
        return ""
