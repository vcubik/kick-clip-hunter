"""CLI to list streamers currently on the watchlist.

Usage: python scripts/list_watchlist.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kick_clip_hunter.db import get_connection

if __name__ == "__main__":
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT slug, broadcaster_user_id, added_at FROM streamers ORDER BY added_at"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("Watchlist is empty.")
    for slug, broadcaster_user_id, added_at in rows:
        print(f"{slug}\tbroadcaster_user_id={broadcaster_user_id}\tadded_at={added_at}")
