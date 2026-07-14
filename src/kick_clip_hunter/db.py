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

CREATE TABLE IF NOT EXISTS moments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    broadcaster_user_id INTEGER NOT NULL,
    channel_slug TEXT NOT NULL,
    detected_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    reason TEXT NOT NULL,
    score REAL NOT NULL,
    message_count INTEGER NOT NULL,
    baseline_message_rate REAL NOT NULL,
    current_message_rate REAL NOT NULL,
    emote_count INTEGER NOT NULL,
    keyword_hits INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_moments_channel_time
    ON moments (channel_slug, detected_at);

CREATE TABLE IF NOT EXISTS channel_keywords (
    broadcaster_user_id INTEGER NOT NULL,
    keyword TEXT NOT NULL,
    PRIMARY KEY (broadcaster_user_id, keyword)
);
"""


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)

    # The moments table gained columns during M3. It only ever held test
    # data, so rather than a real migration we just recreate it if it's
    # still in the old (M2) shape.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(moments)")}
    if columns and "reason" not in columns:
        conn.execute("DROP TABLE moments")

    conn.executescript(SCHEMA)
    return conn


def add_streamer(conn: sqlite3.Connection, broadcaster_user_id: int, slug: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO streamers (broadcaster_user_id, slug) VALUES (?, ?)",
        (broadcaster_user_id, slug),
    )
    conn.commit()


def replace_channel_keywords(conn: sqlite3.Connection, broadcaster_user_id: int, keywords: list[str]) -> None:
    conn.execute("DELETE FROM channel_keywords WHERE broadcaster_user_id = ?", (broadcaster_user_id,))
    conn.executemany(
        "INSERT OR IGNORE INTO channel_keywords (broadcaster_user_id, keyword) VALUES (?, ?)",
        [(broadcaster_user_id, keyword.lower()) for keyword in keywords],
    )
    conn.commit()


def get_channel_keywords(conn: sqlite3.Connection, broadcaster_user_id: int) -> set[str]:
    rows = conn.execute(
        "SELECT keyword FROM channel_keywords WHERE broadcaster_user_id = ?", (broadcaster_user_id,)
    ).fetchall()
    return {row[0] for row in rows}


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


def insert_moment(
    conn: sqlite3.Connection,
    *,
    broadcaster_user_id: int,
    channel_slug: str,
    window_start: str,
    window_end: str,
    reason: str,
    score: float,
    message_count: int,
    baseline_message_rate: float,
    current_message_rate: float,
    emote_count: int,
    keyword_hits: int,
) -> None:
    conn.execute(
        """
        INSERT INTO moments
            (broadcaster_user_id, channel_slug, window_start, window_end, reason, score,
             message_count, baseline_message_rate, current_message_rate, emote_count, keyword_hits)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            broadcaster_user_id,
            channel_slug,
            window_start,
            window_end,
            reason,
            score,
            message_count,
            baseline_message_rate,
            current_message_rate,
            emote_count,
            keyword_hits,
        ),
    )
    conn.commit()
