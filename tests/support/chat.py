"""Drives the detector with scripted chat on a controllable clock.

The detector takes an explicit `now`, so no test has to wait for real time to
pass: `ChatSim` keeps its own clock and feeds messages the same way the
webhook handler does (same classification calls, same arguments).
"""

from __future__ import annotations

import itertools
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from kick_clip_hunter import db, detector
from kick_clip_hunter.detector import Spike


def native_emote(name: str, emote_id: int = 1) -> str:
    """The token Kick embeds in message content for one of its own emotes."""
    return f"[emote:{emote_id}:{name}]"


@dataclass
class ChatSim:
    channel: str = "test_channel"
    # An arbitrary reading of a monotonic clock; nothing depends on its value.
    now: float = 100_000.0
    # 7TV emote names of the channel -> mention weight, as stored per channel.
    keywords: dict[str, float] = field(default_factory=dict)
    # The channel's own detector settings; None for the recommended ones.
    settings: detector.Settings | None = None
    # Every moment fired so far, as (clock time, spike).
    moments: list[tuple[float, Spike]] = field(default_factory=list)
    _message_number: int = 0

    def say(self, sender: str, content: str) -> Spike | None:
        """One chat message at the current time, classified like the webhook does."""
        weights = detector.keyword_weights(self.keywords, self.settings) if self.settings else self.keywords
        laugh_weight, mention_weight = detector.classify_message(content, weights, self.settings)
        spike = detector.record_message(
            self.channel,
            sender=sender,
            content=content,
            emote_count=len(detector.NATIVE_EMOTE_TOKEN_PATTERN.findall(content)),
            emote_weight=detector.classify_native_emotes(content, self.settings),
            laugh_weight=laugh_weight,
            mention_weight=mention_weight,
            now=self.now,
            settings=self.settings,
        )
        if spike is not None:
            self.moments.append((self.now, spike))
        return spike

    def wait(self, seconds: float) -> None:
        self.now += seconds

    def chatter(self, seconds: float, chatters: int = 10, interval: float = 1.0, prefix: str = "viewer") -> None:
        """Ordinary conversation: `chatters` people taking turns, one message
        every `interval` seconds, every message different."""
        elapsed = 0.0
        while elapsed < seconds:
            self._message_number += 1
            sender = f"{prefix}{self._message_number % chatters}"
            self.say(sender, f"just chatting, message {self._message_number}")
            self.wait(interval)
            elapsed += interval

    def warm_up(self, chatters: int = 10, interval: float = 1.0) -> None:
        """Enough ordinary chatter for the detector to trust its baseline."""
        self.chatter(detector.BASELINE_WINDOW_SECONDS, chatters=chatters, interval=interval)

    def burst(self, senders: Iterable[str], content: str, spacing: float = 0.2) -> Spike | None:
        """The same reaction from several people in quick succession.

        Returns the moment it fired, if any (the detector fires at most once
        per cooldown, so there is never more than one).
        """
        fired = None
        for sender in senders:
            fired = self.say(sender, content) or fired
            self.wait(spacing)
        return fired

    def reactions_needed(self, content: str, limit: int = 60, prefix: str = "fan", spacing: float = 0.1) -> int | None:
        """Has one new person after another send `content` and returns how
        many it took for a moment to fire - None if `limit` people weren't
        enough. They all fit inside one short window."""
        assert limit * spacing < detector.SHORT_WINDOW_SECONDS
        for count, sender in enumerate(people(limit, prefix), start=1):
            if self.say(sender, content) is not None:
                return count
            self.wait(spacing)
        return None

    def reaction_active(self, since: float | None = None) -> bool:
        return detector.reaction_active(self.channel, now=self.now, since=since, settings=self.settings)

    @property
    def moment_times(self) -> list[float]:
        return [time for time, _ in self.moments]


def people(count: int, prefix: str = "fan") -> list[str]:
    """`count` distinct usernames that never collide with `ChatSim.chatter`'s."""
    return [f"{prefix}{number}" for number in range(count)]


@dataclass
class StoredStream(ChatSim):
    """A `ChatSim` whose messages go into the database, stamped with the
    simulated clock, instead of into the detector - what the service leaves
    behind after a stream. The same script can therefore be run live
    (`ChatSim`) and stored-then-replayed (`StoredStream`) and compared.

    `now` is taken as seconds since the epoch, and `conn` is the connection
    the messages are written through."""

    conn: sqlite3.Connection | None = None
    broadcaster_user_id: int = 1
    _ids: itertools.count = field(default_factory=itertools.count)

    def say(self, sender: str, content: str) -> None:
        received_at = datetime.fromtimestamp(self.now, timezone.utc).isoformat()
        db.insert_chat_message(
            self.conn,
            message_id=f"stored-{self.channel}-{next(self._ids)}",
            broadcaster_user_id=self.broadcaster_user_id,
            channel_slug=self.channel,
            sender_username=sender,
            content=content,
            emotes_json="[]",
            created_at=received_at,
            received_at=received_at,
        )


def exact_crowd() -> list[str]:
    """Just enough people to fire a moment on a ten-chatter channel - so the
    reaction stops at the trigger and the moment isn't extended."""
    return people(detector._dynamic_min_reaction_unique(10))


def typical_evening(chat: ChatSim) -> None:
    """Five minutes of chat with one stray laugh in it, then two real
    reactions three minutes apart: the first is over as soon as it fires,
    the second keeps drawing in new people."""
    chat.chatter(150)
    chat.say("viewer1", "xd")
    chat.chatter(150)
    chat.burst(exact_crowd(), "xDDD")
    chat.chatter(180)
    chat.burst(people(8, "second_wave"), "xDDD")
    chat.wait(3)
    chat.burst(people(8, "still_laughing"), "xDDD")
    chat.chatter(60)
