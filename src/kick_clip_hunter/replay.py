"""Replaying stored chat through the detector, to see what a set of settings
would have done on a real stream before a live one is run on them.

Every chat message the service receives is stored with its arrival time, and
the detector takes its clock as an argument - so a past stream can be fed
through the current code exactly as it arrived, in a few seconds per hour of
chat. What comes out is when moments would have fired and how long each
would have been held open, which is enough to say how many there would have
been and how long their clips.

What this can and cannot tell: it reproduces *when* moments fire (on the day
it was first used it matched the live run 102 to 103), so it answers "how
many" and "how long". It says nothing about whether the moments that remain
are the good ones - that needs ratings.

Used by scripts/replay_chat.py and by the Settings page's trial. Nothing is
written to the database, and the detector's state for the channels that are
live is not touched: a replay runs under a name of its own.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone

from . import detector
from .db import TUNING_FOR_ALL, get_channel_keywords, get_tuning_in_force
from .tuning import MOMENT_SESSION_POLL_SECONDS, Tuning, stored, tuned

# Each replay is a channel of its own to the detector. No channel on Kick can
# be called this: a name there has no space in it.
_runs = itertools.count(1)


@dataclass(frozen=True)
class Message:
    time: float  # arrival, seconds since the epoch
    sender: str
    content: str
    emote_count: int


@dataclass(frozen=True)
class ReplayedMoment:
    time: float
    reasons: tuple[str, ...]
    score: float
    extension_seconds: float

    def clip_seconds(self, tuning: Tuning) -> float:
        """Length of the clip this moment would get: pre-roll, the detection
        window, however long the reaction kept going, post-roll."""
        return (
            tuning.pre_roll_seconds
            + detector.SHORT_WINDOW_SECONDS
            + self.extension_seconds
            + tuning.post_roll_seconds
        )


@dataclass(frozen=True)
class Summary:
    """What a replay comes to. The clip lengths are None when there was no
    moment to have one."""

    moments: int
    per_hour: float
    not_extended: int
    median_clip_seconds: float | None
    longest_clip_seconds: float | None


def _timestamp(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def load_messages(
    conn: sqlite3.Connection,
    channel_slug: str,
    since: str | None = None,
    until: str | None = None,
    limit: int | None = None,
) -> tuple[list[Message], dict[str, float]]:
    """A channel's stored chat in arrival order, and its emote keywords.
    With a `limit`, the newest that many messages of it."""
    rows = conn.execute(
        "SELECT broadcaster_user_id, sender_username, content, emotes, received_at FROM chat_messages "
        "WHERE lower(channel_slug) = lower(?)",
        (channel_slug,),
    ).fetchall()
    earliest = _timestamp(since) if since else float("-inf")
    latest = _timestamp(until) if until else float("inf")

    timed = []
    for user_id, sender, content, emotes, received_at in rows:
        time = _timestamp(received_at)
        if earliest <= time < latest:
            timed.append((time, user_id, sender, content, emotes))
    timed.sort(key=lambda row: row[0])
    if limit is not None:
        timed = timed[-limit:] if limit > 0 else []

    messages = []
    broadcaster_user_id = None
    for time, user_id, sender, content, emotes in timed:
        broadcaster_user_id = user_id
        positions = sum(len(emote.get("positions", [])) for emote in json.loads(emotes or "[]"))
        messages.append(Message(time, sender or "", content or "", positions))

    keywords = get_channel_keywords(conn, broadcaster_user_id) if broadcaster_user_id is not None else {}
    return messages, keywords


def tuning_in_force(conn: sqlite3.Connection, channel_slug: str) -> Tuning:
    """The settings the service goes by for a channel right now: the ones
    for all channels, and the channel's own if it is on the watchlist and
    goes by them."""
    row = conn.execute(
        "SELECT broadcaster_user_id FROM streamers WHERE lower(slug) = lower(?)", (channel_slug,)
    ).fetchone()
    texts = get_tuning_in_force(conn)
    own = texts.get(row[0], {}) if row else {}
    return tuned(stored(texts.get(TUNING_FOR_ALL, {})), stored(own))


def chat_hours(messages: list[Message]) -> float:
    """How long chat was going in `messages`, in hours. Stored chat has
    holes in it - the channel was offline, the service was - and a hole the
    detector would have forgotten everything over is not time a moment
    could have fired in."""
    seconds = 0.0
    for earlier, later in itertools.pairwise(messages):
        gap = later.time - earlier.time
        if gap <= detector.BASELINE_WINDOW_SECONDS:
            seconds += gap
    return seconds / 3600


def replay(messages: list[Message], keywords: dict[str, float], tuning: Tuning) -> list[ReplayedMoment]:
    """Feeds `messages` through the detector and the moment-session rule
    under `tuning`, with the clock taken from the messages themselves.
    `keywords` are the channel's emote keywords as they are stored."""
    channel = f"replay {next(_runs)}"
    settings = tuning.detector
    weights = detector.keyword_weights(keywords, settings)
    poll = MOMENT_SESSION_POLL_SECONDS

    moments: list[ReplayedMoment] = []
    open_session: dict | None = None

    def run_session_until(time: float) -> None:
        """The session polls on its own schedule, between messages too."""
        nonlocal open_session
        while open_session is not None and open_session["next_poll"] <= time:
            now = open_session["next_poll"]
            if detector.reaction_active(channel, now=now, since=open_session["fired_at"], settings=settings):
                open_session["last_active"] = now
                open_session["window_end"] = now
            timed_out = now - open_session["fired_at"] >= tuning.max_open_seconds
            gone_quiet = now - open_session["last_active"] >= tuning.quiet_seconds
            if timed_out or gone_quiet:
                spike = open_session["spike"]
                moments.append(
                    ReplayedMoment(
                        time=open_session["fired_at"],
                        reasons=tuple(spike.reasons),
                        score=spike.score,
                        extension_seconds=open_session["window_end"] - open_session["fired_at"],
                    )
                )
                open_session = None
            else:
                open_session["next_poll"] += poll

    try:
        for message in messages:
            run_session_until(message.time)
            laugh_weight, mention_weight = detector.classify_message(message.content, weights, settings)
            spike = detector.record_message(
                channel,
                sender=message.sender,
                content=message.content,
                emote_count=message.emote_count,
                emote_weight=detector.classify_native_emotes(message.content, settings),
                laugh_weight=laugh_weight,
                mention_weight=mention_weight,
                now=message.time,
                settings=settings,
            )
            if spike is None:
                continue
            if open_session is not None:
                # A re-fire while a moment is open keeps it alive, like the service does.
                open_session["last_active"] = message.time
            else:
                open_session = {
                    "spike": spike,
                    "fired_at": message.time,
                    "last_active": message.time,
                    "window_end": message.time,
                    "next_poll": message.time + poll,
                }

        if messages:
            run_session_until(messages[-1].time + tuning.max_open_seconds + poll)
    finally:
        detector._entries.pop(channel, None)
        detector._last_moment_at.pop(channel, None)
    return moments


def summarise(moments: list[ReplayedMoment], hours: float, tuning: Tuning) -> Summary:
    clips = [moment.clip_seconds(tuning) for moment in moments]
    return Summary(
        moments=len(moments),
        per_hour=len(moments) / hours if hours else 0.0,
        not_extended=sum(1 for moment in moments if moment.extension_seconds < 1),
        median_clip_seconds=statistics.median(clips) if clips else None,
        longest_clip_seconds=max(clips) if clips else None,
    )
