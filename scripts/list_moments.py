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
            SELECT channel_slug, detected_at, window_start, window_end,
                   message_count, baseline_rate, current_rate, score
            FROM moments
            ORDER BY detected_at DESC
            """
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("No moments detected yet.")
    for channel, detected_at, window_start, window_end, count, baseline, current, score in rows:
        print(
            f"[{channel}] {detected_at}  {count} msgs in [{window_start} .. {window_end}]  "
            f"rate {current:.2f}/s vs baseline {baseline:.2f}/s  score={score:.2f}"
        )
