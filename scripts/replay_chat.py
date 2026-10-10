"""Replay stored chat through the detector, to see what a tuning change would
have done on a real stream before trying it on a live one.

Usage:
    python scripts/replay_chat.py <channel_slug> [--since ISO] [--until ISO]
                                  [--set NAME=VALUE ...] [--quiet S] [--max S]

Every chat message the service receives is stored with its arrival time, and
the detector takes its clock as an argument - so a past stream can be fed
through the current code exactly as it arrived, in a second or two per hour
of chat. The output is how many moments would have fired and how long their
clips would have been:

    some_channel: 18367 messages over 8.2 h (2026-10-07 13:06 .. 21:17 UTC)
                         moments  per hour  not extended  median clip  longest
    current settings          37       4.5            20          35s      59s
    with overrides            16       2.0            13          35s      47s

`--set` overrides any constant of detector.py for the second row, e.g.
`--set MIN_REACTION_UNIQUE_FRACTION=0.15 --set COOLDOWN_SECONDS=90`;
`--quiet` / `--max` do the same for the moment session's two limits.

What this can and cannot tell you: it reproduces *when* moments fire (on the
day it was first used it matched the live run 102 to 103), so it answers "how
many" and "how long". It says nothing about whether the moments that remain
are the good ones - that needs ratings.

Read-only: nothing is written to the database.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kick_clip_hunter import detector, recorder
from kick_clip_hunter.db import get_channel_keywords, get_connection

REPLAY_CHANNEL = "replay"


@dataclass(frozen=True)
class SessionSettings:
    """The moment session's timing, mirrored from main.py (which can't be
    imported here without starting half the service). A test keeps the two
    in step."""

    poll_seconds: float = 3.0
    quiet_seconds: float = 30.0
    max_seconds: float = 60.0
    post_roll_seconds: float = 10.0


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

    def clip_seconds(self, session: SessionSettings) -> float:
        """Length of the clip this moment would get: pre-roll, the detection
        window, however long the reaction kept going, post-roll."""
        return (
            recorder.PRE_ROLL_SECONDS
            + detector.SHORT_WINDOW_SECONDS
            + self.extension_seconds
            + session.post_roll_seconds
        )


def _timestamp(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def load_messages(
    conn, channel_slug: str, since: str | None = None, until: str | None = None
) -> tuple[list[Message], dict[str, float]]:
    """A channel's stored chat in arrival order, and its emote keywords."""
    rows = conn.execute(
        "SELECT broadcaster_user_id, sender_username, content, emotes, received_at FROM chat_messages "
        "WHERE lower(channel_slug) = lower(?)",
        (channel_slug,),
    ).fetchall()
    earliest = _timestamp(since) if since else float("-inf")
    latest = _timestamp(until) if until else float("inf")

    messages = []
    broadcaster_user_id = None
    for user_id, sender, content, emotes, received_at in rows:
        time = _timestamp(received_at)
        if earliest <= time < latest:
            broadcaster_user_id = user_id
            positions = sum(len(emote.get("positions", [])) for emote in json.loads(emotes or "[]"))
            messages.append(Message(time, sender or "", content or "", positions))
    messages.sort(key=lambda message: message.time)

    keywords = get_channel_keywords(conn, broadcaster_user_id) if broadcaster_user_id is not None else {}
    return messages, keywords


def replay(messages: list[Message], keywords: dict[str, float], session: SessionSettings) -> list[ReplayedMoment]:
    """Feeds `messages` through the detector and the moment-session rule,
    with the clock taken from the messages themselves."""
    detector._entries.pop(REPLAY_CHANNEL, None)
    detector._last_moment_at.pop(REPLAY_CHANNEL, None)

    moments: list[ReplayedMoment] = []
    open_session: dict | None = None

    def run_session_until(time: float) -> None:
        """The session polls on its own schedule, between messages too."""
        nonlocal open_session
        while open_session is not None and open_session["next_poll"] <= time:
            now = open_session["next_poll"]
            if detector.reaction_active(REPLAY_CHANNEL, now=now, since=open_session["fired_at"]):
                open_session["last_active"] = now
                open_session["window_end"] = now
            timed_out = now - open_session["fired_at"] >= session.max_seconds
            gone_quiet = now - open_session["last_active"] >= session.quiet_seconds
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
                open_session["next_poll"] += session.poll_seconds

    for message in messages:
        run_session_until(message.time)
        laugh_weight, mention_weight = detector.classify_message(message.content, keywords)
        spike = detector.record_message(
            REPLAY_CHANNEL,
            sender=message.sender,
            content=message.content,
            emote_count=message.emote_count,
            emote_weight=detector.classify_native_emotes(message.content),
            laugh_weight=laugh_weight,
            mention_weight=mention_weight,
            now=message.time,
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
                "next_poll": message.time + session.poll_seconds,
            }

    if messages:
        run_session_until(messages[-1].time + session.max_seconds + session.poll_seconds)
    return moments


def parse_override(text: str) -> tuple[str, float]:
    name, separator, value = text.partition("=")
    if not separator or not name.isupper() or not hasattr(detector, name):
        raise argparse.ArgumentTypeError(f"{text!r} is not NAME=VALUE for a constant of detector.py")
    current = getattr(detector, name)
    if isinstance(current, bool) or not isinstance(current, (int, float)):
        raise argparse.ArgumentTypeError(f"{name} is not a number and can't be overridden from here")
    try:
        return name, type(current)(float(value))
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number") from None


def summary_row(label: str, moments: list[ReplayedMoment], hours: float, session: SessionSettings) -> str:
    clips = [moment.clip_seconds(session) for moment in moments]
    not_extended = sum(1 for moment in moments if moment.extension_seconds < 1)
    median = f"{statistics.median(clips):.0f}s" if clips else "-"
    longest = f"{max(clips):.0f}s" if clips else "-"
    per_hour = len(moments) / hours if hours else 0.0
    return f"{label:<20}{len(moments):>8}{per_hour:>10.1f}{not_extended:>14}{median:>13}{longest:>9}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("channel_slug")
    parser.add_argument("--since", help="only chat received at or after this ISO time (UTC unless it has an offset)")
    parser.add_argument("--until", help="only chat received before this ISO time")
    parser.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        type=parse_override,
        metavar="NAME=VALUE",
        help="override a detector.py constant for the comparison row (repeatable)",
    )
    defaults = SessionSettings()
    parser.add_argument(
        "--quiet", type=float, help=f"session quiet timeout for the comparison row (now {defaults.quiet_seconds:g}s)"
    )
    parser.add_argument(
        "--max", type=float, help=f"session hard cap for the comparison row (now {defaults.max_seconds:g}s)"
    )
    args = parser.parse_args(argv)

    conn = get_connection()
    try:
        messages, keywords = load_messages(conn, args.channel_slug, args.since, args.until)
    finally:
        conn.close()
    if not messages:
        print(f"no stored chat for {args.channel_slug!r} in that range")
        return 1

    first, last = messages[0].time, messages[-1].time
    hours = (last - first) / 3600

    def clock(time: float, pattern: str) -> str:
        return datetime.fromtimestamp(time, timezone.utc).strftime(pattern)

    print(
        f"{args.channel_slug}: {len(messages)} messages over {hours:.1f} h "
        f"({clock(first, '%Y-%m-%d %H:%M')} .. {clock(last, '%H:%M')} UTC)"
    )
    print(f"{'':<20}{'moments':>8}{'per hour':>10}{'not extended':>14}{'median clip':>13}{'longest':>9}")
    print(summary_row("current settings", replay(messages, keywords, defaults), hours, defaults))

    if args.overrides or args.quiet is not None or args.max is not None:
        overridden = SessionSettings(
            quiet_seconds=args.quiet if args.quiet is not None else defaults.quiet_seconds,
            max_seconds=args.max if args.max is not None else defaults.max_seconds,
        )
        originals = {name: getattr(detector, name) for name, _value in args.overrides}
        try:
            for name, value in args.overrides:
                setattr(detector, name, value)
            print(summary_row("with overrides", replay(messages, keywords, overridden), hours, overridden))
        finally:
            for name, value in originals.items():
                setattr(detector, name, value)
    return 0


if __name__ == "__main__":
    sys.exit(main())
