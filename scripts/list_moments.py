"""CLI to list detected moments, most recent first.

Usage: python scripts/list_moments.py
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kick_clip_hunter.db import get_connection


def _to_local(iso_timestamp: str) -> str:
    """Moments are stored in UTC; display them in the system's local time zone."""
    return datetime.fromisoformat(iso_timestamp).astimezone().strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT channel_slug, detected_at, window_start, window_end, reason, score,
                   message_count, baseline_message_rate, current_message_rate,
                   emote_count, keyword_hits, stream_elapsed_seconds
            FROM moments
            ORDER BY detected_at DESC
            """
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("No moments detected yet.")
    for (
        channel,
        detected_at,
        window_start,
        window_end,
        reason,
        score,
        message_count,
        baseline_rate,
        current_rate,
        emote_count,
        keyword_hits,
        stream_elapsed_seconds,
    ) in rows:
        stream_time = str(timedelta(seconds=stream_elapsed_seconds)) if stream_elapsed_seconds is not None else "unknown"
        print(
            f"[{channel}] {_to_local(detected_at)}  stream_time={stream_time}  reason={reason}  score={score:.2f}  "
            f"window=[{_to_local(window_start)} .. {_to_local(window_end)}]  "
            f"msgs={message_count} ({current_rate:.2f}/s vs baseline {baseline_rate:.2f}/s)  "
            f"emotes={emote_count}  keyword_hits={keyword_hits}"
        )
