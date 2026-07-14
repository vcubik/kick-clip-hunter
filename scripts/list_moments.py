"""CLI to list detected moments, most recent first.

Usage: python scripts/list_moments.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kick_clip_hunter.db import get_connection

if __name__ == "__main__":
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT channel_slug, detected_at, window_start, window_end, reason, score,
                   message_count, baseline_message_rate, current_message_rate,
                   emote_count, keyword_hits
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
    ) in rows:
        print(
            f"[{channel}] {detected_at}  reason={reason}  score={score:.2f}  "
            f"window=[{window_start} .. {window_end}]  "
            f"msgs={message_count} ({current_rate:.2f}/s vs baseline {baseline_rate:.2f}/s)  "
            f"emotes={emote_count}  keyword_hits={keyword_hits}"
        )
