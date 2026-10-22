"""Count how many people laughed around each moment stored before the count
was kept, from the chat stored with it.

The service counts it for every moment when the moment closes (see
detector.laughing_people) and the review queue can be ordered by it. A
moment from before that has no count, and is listed after every moment that
has one; this gives it the count the service would have given it, under the
channel's settings as they are now. Imported clips are left out - no chat
set them off - and so is a moment with no stored chat around it at all,
which keeps no count rather than a count of nobody.

Safe to run while the service runs, and again: only moments without a count
are looked at, and the chat is read a chunk at a time.

Usage: python scripts/backfill_laughing.py
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kick_clip_hunter import detector, replay
from kick_clip_hunter.db import get_connection, get_moments_without_laughing, update_moment_laughing


def backfill(conn) -> tuple[int, int]:
    """Counts what laughed around every moment without a count and stores
    it. Returns how many were counted and how many had no chat to count."""
    counted = skipped = 0
    tunings: dict[str, replay.Tuning] = {}
    for row in get_moments_without_laughing(conn):
        started = datetime.fromisoformat(row["window_start"].replace("Z", "+00:00"))
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        fired = started + timedelta(seconds=detector.SHORT_WINDOW_SECONDS)
        messages, keywords = replay.load_between(
            conn,
            row["broadcaster_user_id"],
            fired - timedelta(seconds=detector.SHORT_WINDOW_SECONDS),
            fired + timedelta(seconds=detector.KEEP_LAUGHING_SECONDS),
        )
        if not messages:
            skipped += 1
            continue
        channel = row["channel_slug"]
        if channel not in tunings:
            tunings[channel] = replay.tuning_in_force(conn, channel)
        laughing = replay.laughing_around(messages, fired.timestamp(), keywords, tunings[channel])
        update_moment_laughing(conn, row["id"], laughing)
        counted += 1
    return counted, skipped


if __name__ == "__main__":
    connection = get_connection()
    try:
        counted, skipped = backfill(connection)
    finally:
        connection.close()
    print(f"counted {counted} moment(s); {skipped} had no stored chat around them")
