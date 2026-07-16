"""SQLite storage for the streamer watchlist and raw chat messages.

No retention/pruning yet - everything is kept. That's a deliberate,
temporary simplification (see roadmap) so we have real data to build the
detection heuristic against before deciding what's safe to prune.
"""

import sqlite3
from datetime import datetime, timedelta
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
    keyword_hits INTEGER NOT NULL,
    stream_elapsed_seconds INTEGER
);

CREATE INDEX IF NOT EXISTS idx_moments_channel_time
    ON moments (channel_slug, detected_at);

CREATE TABLE IF NOT EXISTS channel_keywords (
    broadcaster_user_id INTEGER NOT NULL,
    keyword TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (broadcaster_user_id, keyword)
);
"""


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)

    # The moments table gained columns during M3. It only ever held test
    # data at that point, so that migration just recreated the table.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(moments)")}
    if columns and "reason" not in columns:
        conn.execute("DROP TABLE moments")

    conn.executescript(SCHEMA)

    # stream_elapsed_seconds was added later, after real moments had already
    # been captured - add it in place instead of dropping the table.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(moments)")}
    if "stream_elapsed_seconds" not in columns:
        conn.execute("ALTER TABLE moments ADD COLUMN stream_elapsed_seconds INTEGER")

    columns = {row[1] for row in conn.execute("PRAGMA table_info(channel_keywords)")}
    if "weight" not in columns:
        conn.execute("ALTER TABLE channel_keywords ADD COLUMN weight REAL NOT NULL DEFAULT 1.0")

    columns = {row[1] for row in conn.execute("PRAGMA table_info(moments)")}
    if "clip_path" not in columns:
        conn.execute("ALTER TABLE moments ADD COLUMN clip_path TEXT")

    columns = {row[1] for row in conn.execute("PRAGMA table_info(moments)")}
    if "feedback" not in columns:
        conn.execute("ALTER TABLE moments ADD COLUMN feedback TEXT")

    return conn


def add_streamer(conn: sqlite3.Connection, broadcaster_user_id: int, slug: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO streamers (broadcaster_user_id, slug) VALUES (?, ?)",
        (broadcaster_user_id, slug),
    )
    conn.commit()


def replace_channel_keywords(
    conn: sqlite3.Connection, broadcaster_user_id: int, keyword_weights: dict[str, float]
) -> None:
    conn.execute("DELETE FROM channel_keywords WHERE broadcaster_user_id = ?", (broadcaster_user_id,))
    conn.executemany(
        "INSERT OR IGNORE INTO channel_keywords (broadcaster_user_id, keyword, weight) VALUES (?, ?, ?)",
        [(broadcaster_user_id, keyword.lower(), weight) for keyword, weight in keyword_weights.items()],
    )
    conn.commit()


def get_channel_keywords(conn: sqlite3.Connection, broadcaster_user_id: int) -> dict[str, float]:
    rows = conn.execute(
        "SELECT keyword, weight FROM channel_keywords WHERE broadcaster_user_id = ?", (broadcaster_user_id,)
    ).fetchall()
    return {row[0]: row[1] for row in rows}


def get_streamers(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT slug, broadcaster_user_id, added_at FROM streamers ORDER BY added_at").fetchall()


def get_recent_moments(
    conn: sqlite3.Connection, limit: int = 50, channel_slug: str | None = None
) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    where = "WHERE channel_slug = ?" if channel_slug else ""
    params = (channel_slug, limit) if channel_slug else (limit,)
    return conn.execute(
        f"""
        SELECT id, channel_slug, detected_at, window_start, window_end, reason, score,
               message_count, baseline_message_rate, current_message_rate,
               emote_count, keyword_hits, stream_elapsed_seconds, clip_path, feedback
        FROM moments
        {where}
        ORDER BY detected_at DESC
        LIMIT ?
        """,
        params,
    ).fetchall()


def get_moment_channels(conn: sqlite3.Connection) -> list[str]:
    """Distinct channels that have at least one moment, for the dashboard filter -
    covers channels no longer on the watchlist too, so their past moments stay filterable.
    """
    conn.row_factory = sqlite3.Row
    return [row[0] for row in conn.execute("SELECT DISTINCT channel_slug FROM moments ORDER BY channel_slug")]


SNIPPET_PADDING_SECONDS = 2


def get_chat_snippet(
    conn: sqlite3.Connection, channel_slug: str, window_start: str, window_end: str, limit: int = 20
) -> list[sqlite3.Row]:
    """Chat messages inside a moment's detection window, with a small padding
    buffer on both ends. window_start/window_end are reconstructed from
    wall-clock time after the fact, while the detector's own window is based
    on each message's arrival order - webhook delivery jitter between
    messages means a message that was genuinely part of the burst can end up
    with a received_at a few hundred ms outside the stored boundary.
    """
    conn.row_factory = sqlite3.Row
    padded_start = (datetime.fromisoformat(window_start) - timedelta(seconds=SNIPPET_PADDING_SECONDS)).isoformat()
    padded_end = (datetime.fromisoformat(window_end) + timedelta(seconds=SNIPPET_PADDING_SECONDS)).isoformat()
    return conn.execute(
        """
        SELECT sender_username, content
        FROM chat_messages
        WHERE channel_slug = ? AND received_at BETWEEN ? AND ?
        ORDER BY received_at
        LIMIT ?
        """,
        (channel_slug, padded_start, padded_end, limit),
    ).fetchall()


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
    received_at: str,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO chat_messages
            (message_id, broadcaster_user_id, channel_slug, sender_username, content, emotes, created_at, received_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_id,
            broadcaster_user_id,
            channel_slug,
            sender_username,
            content,
            emotes_json,
            created_at,
            received_at,
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
    stream_elapsed_seconds: int | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO moments
            (broadcaster_user_id, channel_slug, window_start, window_end, reason, score,
             message_count, baseline_message_rate, current_message_rate, emote_count, keyword_hits,
             stream_elapsed_seconds)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            stream_elapsed_seconds,
        ),
    )
    conn.commit()
    return cursor.lastrowid


def update_moment_clip_path(conn: sqlite3.Connection, moment_id: int, clip_path: str) -> None:
    conn.execute("UPDATE moments SET clip_path = ? WHERE id = ?", (clip_path, moment_id))
    conn.commit()


def update_moment_feedback(conn: sqlite3.Connection, moment_id: int, feedback: str | None) -> None:
    conn.execute("UPDATE moments SET feedback = ? WHERE id = ?", (feedback, moment_id))
    conn.commit()
