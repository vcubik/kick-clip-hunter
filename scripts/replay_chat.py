"""Replay stored chat through the detector, to see what a tuning change would
have done on a real stream before trying it on a live one.

Usage:
    python scripts/replay_chat.py <channel_slug> [--since ISO] [--until ISO]
                                  [--set NAME=VALUE ...] [--quiet S] [--max S]

Every chat message the service receives is stored with its arrival time, and
the detector takes its clock as an argument - so a past stream can be fed
through the current code exactly as it arrived, in a few seconds per hour of
chat. The output is how many moments would have fired and how long their
clips would have been:

    some_channel: 18367 messages over 8.2 h of chat (2026-10-07 13:06 .. 21:17 UTC)
                         moments  per hour  not extended  median clip  longest
    current settings          37       4.5            20          35s      59s
    with overrides            16       2.0            13          35s      47s

"Current settings" are the ones the service goes by for the channel: what
was set on the dashboard's Settings page, and the constants for everything
else. `--set` overrides any constant of detector.py for the second row,
whatever the Settings page has it at, e.g.
`--set MIN_REACTION_UNIQUE_FRACTION=0.15 --set COOLDOWN_SECONDS=90`;
`--quiet` / `--max` do the same for the moment session's two limits. The
Settings page runs the same replay on the values in its form.

What this can and cannot tell you: it reproduces *when* moments fire (on the
day it was first used it matched the live run 102 to 103), so it answers "how
many" and "how long". It says nothing about whether the moments that remain
are the good ones - that needs ratings.

Read-only: nothing is written to the database.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kick_clip_hunter import detector
from kick_clip_hunter.db import get_connection
from kick_clip_hunter.replay import (
    ReplayedMoment,
    Summary,
    chat_hours,
    load_messages,
    replay,
    summarise,
    tuning_in_force,
)
from kick_clip_hunter.tuning import Tuning

# The detector's settings by the name of the constant each one is unless it
# was set (see detector.Settings).
_SETTINGS_FIELDS = {field.name.upper(): field.name for field in dataclasses.fields(detector.Settings)}


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


def summary_row(label: str, summary: Summary) -> str:
    def seconds(value: float | None) -> str:
        return "-" if value is None else f"{value:.0f}s"

    return (
        f"{label:<20}{summary.moments:>8}{summary.per_hour:>10.1f}{summary.not_extended:>14}"
        f"{seconds(summary.median_clip_seconds):>13}{seconds(summary.longest_clip_seconds):>9}"
    )


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
    parser.add_argument("--quiet", type=float, help="session quiet timeout for the comparison row")
    parser.add_argument("--max", type=float, help="session hard cap for the comparison row")
    args = parser.parse_args(argv)

    conn = get_connection()
    try:
        messages, keywords = load_messages(conn, args.channel_slug, args.since, args.until)
        current = tuning_in_force(conn, args.channel_slug)
    finally:
        conn.close()
    if not messages:
        print(f"no stored chat for {args.channel_slug!r} in that range")
        return 1

    first, last = messages[0].time, messages[-1].time
    hours = chat_hours(messages)

    def clock(time: float, pattern: str) -> str:
        return datetime.fromtimestamp(time, timezone.utc).strftime(pattern)

    def row(label: str, tuning: Tuning) -> str:
        moments: list[ReplayedMoment] = replay(messages, keywords, tuning)
        return summary_row(label, summarise(moments, hours, tuning))

    print(
        f"{args.channel_slug}: {len(messages)} messages over {hours:.1f} h of chat "
        f"({clock(first, '%Y-%m-%d %H:%M')} .. {clock(last, '%H:%M')} UTC)"
    )
    print(f"{'':<20}{'moments':>8}{'per hour':>10}{'not extended':>14}{'median clip':>13}{'longest':>9}")
    print(row("current settings", current))

    if args.overrides or args.quiet is not None or args.max is not None:
        # A constant that is one of the detector's settings is overridden
        # there, over whatever the channel has it set to; any other one is
        # the same for every channel, and is changed where it is kept.
        fields = {_SETTINGS_FIELDS[name]: value for name, value in args.overrides if name in _SETTINGS_FIELDS}
        constants = {name: value for name, value in args.overrides if name not in _SETTINGS_FIELDS}
        overridden = dataclasses.replace(
            current,
            detector=dataclasses.replace(current.detector, **fields),
            quiet_seconds=args.quiet if args.quiet is not None else current.quiet_seconds,
            max_open_seconds=args.max if args.max is not None else current.max_open_seconds,
        )
        originals = {name: getattr(detector, name) for name in constants}
        try:
            for name, value in constants.items():
                setattr(detector, name, value)
            print(row("with overrides", overridden))
        finally:
            for name, value in originals.items():
                setattr(detector, name, value)
    return 0


if __name__ == "__main__":
    sys.exit(main())
