"""SQLite storage for the streamer watchlist and raw chat messages.

No retention/pruning yet - everything is kept. That's a deliberate,
temporary simplification (see roadmap) so we have real data to build the
detection heuristic against before deciding what's safe to prune.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "kick_clip_hunter.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS streamers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    broadcaster_user_id INTEGER UNIQUE NOT NULL,
    slug TEXT UNIQUE NOT NULL,
    added_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS chat_messages (
    message_id TEXT PRIMARY KEY,
    broadcaster_user_id INTEGER NOT NULL,
    channel_slug TEXT NOT NULL,
    sender_username TEXT,
    content TEXT,
    emotes TEXT,
    created_at TEXT,
    received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_channel_time
    ON chat_messages (channel_slug, created_at);
"""


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    return conn


def add_streamer(conn: sqlite3.Connection, broadcaster_user_id: int, slug: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO streamers (broadcaster_user_id, slug) VALUES (?, ?)",
        (broadcaster_user_id, slug),
    )
    conn.commit()


def insert_chat_message(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    broadcaster_user_id: int,
    channel_slug: str,
    sender_username: str,
    content: str,
    emotes_json: str,
    created_at: str,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO chat_messages
            (message_id, broadcaster_user_id, channel_slug, sender_username, content, emotes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_id,
            broadcaster_user_id,
            channel_slug,
            sender_username,
            content,
            emotes_json,
            created_at,
        ),
    )
    conn.commit()
