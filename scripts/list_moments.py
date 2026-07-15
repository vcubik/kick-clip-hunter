"""CLI to list detected moments, most recent first.

Usage: python scripts/list_moments.py
"""

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kick_clip_hunter.db import get_connection, get_recent_moments
from kick_clip_hunter.timeutil import to_local

if __name__ == "__main__":
    conn = get_connection()
    try:
        rows = get_recent_moments(conn, limit=1000)
    finally:
        conn.close()

    if not rows:
        print("No moments detected yet.")
    for row in rows:
        stream_time = (
            str(timedelta(seconds=row["stream_elapsed_seconds"]))
            if row["stream_elapsed_seconds"] is not None
            else "unknown"
        )
        print(
            f"[{row['channel_slug']}] {to_local(row['detected_at'])}  stream_time={stream_time}  "
            f"reason={row['reason']}  score={row['score']:.2f}  "
            f"window=[{to_local(row['window_start'])} .. {to_local(row['window_end'])}]  "
            f"msgs={row['message_count']} ({row['current_message_rate']:.2f}/s vs baseline {row['baseline_message_rate']:.2f}/s)  "
            f"emotes={row['emote_count']}  keyword_hits={row['keyword_hits']}"
        )
