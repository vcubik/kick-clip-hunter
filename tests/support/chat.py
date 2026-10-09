"""Drives the detector with scripted chat on a controllable clock.

The detector takes an explicit `now`, so no test has to wait for real time to
pass: `ChatSim` keeps its own clock and feeds messages the same way the
webhook handler does (same classification calls, same arguments).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from kick_clip_hunter import detector
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
    # Every moment fired so far, as (clock time, spike).
    moments: list[tuple[float, Spike]] = field(default_factory=list)
    _message_number: int = 0

    def say(self, sender: str, content: str) -> Spike | None:
        """One chat message at the current time, classified like the webhook does."""
        laugh_weight, mention_weight = detector.classify_message(content, self.keywords)
        spike = detector.record_message(
            self.channel,
            sender=sender,
            content=content,
            emote_count=len(detector.NATIVE_EMOTE_TOKEN_PATTERN.findall(content)),
            emote_weight=detector.classify_native_emotes(content),
            laugh_weight=laugh_weight,
            mention_weight=mention_weight,
            now=self.now,
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
        return detector.reaction_active(self.channel, now=self.now, since=since)

    @property
    def moment_times(self) -> list[float]:
        return [time for time, _ in self.moments]


def people(count: int, prefix: str = "fan") -> list[str]:
    """`count` distinct usernames that never collide with `ChatSim.chatter`'s."""
    return [f"{prefix}{number}" for number in range(count)]
