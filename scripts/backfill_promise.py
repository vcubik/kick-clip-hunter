"""Give each moment stored before it was kept its promise: the chance, from
its chat, that it is rated 4 or 5 (see promise.py), by which the review
queue can be ordered "Most promising first".

The service works it out for every moment when the moment closes, from the
people who chatted in the detector's baseline window before it fired. For a
moment from before that, those people are counted from the chat stored with
it, leaving out what the channel's settings, as they are now, tell the
detector to leave out. Run scripts/backfill_laughing.py first: a moment
whose laughing was never counted is left without a promise. Imported clips
are left out - no chat set them off.

A moment with no stored chat before it gets a promise from a chat of nobody
rather than none at all: its laughing and length still say something.

Safe to run while the service runs, and again: only moments without a
promise are looked at, and the chat is read a chunk at a time.

Usage: python scripts/backfill_promise.py
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kick_clip_hunter import detector, promise, replay
from kick_clip_hunter.db import get_connection, get_moments_without_promise, update_moment_promise


def _when(text: str) -> datetime:
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def backfill(conn) -> int:
    """Works out the promise of every moment that has its laughing counted
    but no promise, and stores it. Returns how many were given one."""
    given = 0
    tunings: dict[str, replay.Tuning] = {}
    for row in get_moments_without_promise(conn):
        started = _when(row["window_start"])
        fired = started + timedelta(seconds=detector.SHORT_WINDOW_SECONDS)
        chatters = row["chatters"]
        if chatters is None:
            channel = row["channel_slug"]
            if channel not in tunings:
                tunings[channel] = replay.tuning_in_force(conn, channel)
            settings = tunings[channel].detector
            messages, _keywords = replay.load_between(
                conn, row["broadcaster_user_id"], fired - timedelta(seconds=detector.BASELINE_WINDOW_SECONDS), fired
            )
            heard = [
                (message.sender, message.time)
                for message in messages
                if not detector.is_ignored_message(message.content, settings.added_ignored_messages)
                and detector.sender_key(message.sender) not in settings.ignored_senders
            ]
            chatters = promise.chatters_before(heard, fired.timestamp())
        reaction = (_when(row["window_end"]) - started).total_seconds()
        update_moment_promise(conn, row["id"], chatters, promise.promise(row["laughing"], reaction, chatters))
        given += 1
    return given


if __name__ == "__main__":
    connection = get_connection()
    try:
        given = backfill(connection)
    finally:
        connection.close()
    print(f"gave {given} moment(s) their promise")
