"""SQLite storage for the streamer watchlist and raw chat messages.

No retention/pruning yet - everything is kept. That's a deliberate,
temporary simplification (see roadmap) so we have real data to build the
detection heuristic against before deciding what's safe to prune.
"""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "kick_clip_hunter.db"

# Manual categorization tags set from the dashboard while reviewing a clip -
# (value, label) pairs so the display text can differ from the stored value
# without a migration. Free-form TEXT columns, not a DB-level enum, so this
# list is the only place the vocabulary is enforced (see the /stream_type
# and /moment_type routes in main.py) - extending it later is just adding a
# tuple here, no schema change needed.
STREAM_TYPES = [
    ("irl", "IRL"),
    ("gaming", "Gaming"),
    ("webcam", "Webcam/PC"),
    ("reaction", "Reaction"),
]

MOMENT_TYPES = [
    ("funny", "Funny"),
    ("fail", "Fail"),
    ("rage", "Rage"),
    ("good_play", "Good play"),
    ("hype", "Hype"),
    ("music", "Music"),
    ("wholesome", "Wholesome"),
    ("boring", "Boring"),
    ("awkward", "Awkward"),
    ("other", "Other"),
]

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

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
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

    # Started out as a binary accept/reject "feedback" column, replaced before
    # it ever shipped with a 1-5 "rating" scale (some clips are funny but
    # unlikely to go viral - that's a real middle ground, not just yes/no).
    # Renamed in place rather than adding a second column since the old one
    # never held real data.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(moments)")}
    if "rating" not in columns:
        if "feedback" in columns:
            conn.execute("ALTER TABLE moments RENAME COLUMN feedback TO rating")
        else:
            conn.execute("ALTER TABLE moments ADD COLUMN rating INTEGER")

    # The feedback->rating rename kept the old column's TEXT affinity, so
    # ratings were stored (and compared) as strings - "1" != 1, which silently
    # broke the dashboard's active-button check after a reload. Rebuild the
    # column with INTEGER affinity, converting the existing string values. The
    # UPDATE opens an implicit transaction, so this whole block needs an
    # explicit commit; the drop-if-exists makes it recover from a half-applied
    # run rather than tripping over a leftover rating_int column.
    types = {row[1]: (row[2] or "").upper() for row in conn.execute("PRAGMA table_info(moments)")}
    if types.get("rating") != "INTEGER":
        if "rating_int" in types:
            conn.execute("ALTER TABLE moments DROP COLUMN rating_int")
        conn.execute("ALTER TABLE moments ADD COLUMN rating_int INTEGER")
        conn.execute("UPDATE moments SET rating_int = CAST(rating AS INTEGER) WHERE rating IS NOT NULL AND rating != ''")
        conn.execute("ALTER TABLE moments DROP COLUMN rating")
        conn.execute("ALTER TABLE moments RENAME COLUMN rating_int TO rating")
        conn.commit()

    # Free-text notes the reviewer writes about what's good/bad in a moment -
    # the qualitative counterpart to the numeric rating, read back later to
    # inform detector weight tuning.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(moments)")}
    if "notes" not in columns:
        conn.execute("ALTER TABLE moments ADD COLUMN notes TEXT")

    # Local speech-to-text of the cut clip (see transcriber.py) - filled in
    # asynchronously after the clip exists, so it's NULL for a while even on
    # a moment that will eventually have one.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(moments)")}
    if "transcript" not in columns:
        conn.execute("ALTER TABLE moments ADD COLUMN transcript TEXT")

    # Local audio event/emotion tags (see audio_events.py) and video frame
    # embedding (see frame_encoder.py) - same "filled in asynchronously,
    # NULL until then" story as transcript. Pure data capture for the future
    # learned-classifier path (docs/moment-judge-design.md); nothing reads
    # these yet.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(moments)")}
    if "audio_events" not in columns:
        conn.execute("ALTER TABLE moments ADD COLUMN audio_events TEXT")
    if "frame_embedding" not in columns:
        conn.execute("ALTER TABLE moments ADD COLUMN frame_embedding BLOB")

    # Manual categorization tags, set from the dashboard while reviewing a
    # clip (see STREAM_TYPES/MOMENT_TYPES above) - human-labeled context
    # distinct from rating (how good) and notes (free text).
    columns = {row[1] for row in conn.execute("PRAGMA table_info(moments)")}
    if "stream_type" not in columns:
        conn.execute("ALTER TABLE moments ADD COLUMN stream_type TEXT")
    if "moment_type" not in columns:
        conn.execute("ALTER TABLE moments ADD COLUMN moment_type TEXT")

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
    conn: sqlite3.Connection, limit: int = 50, offset: int = 0, channel_slug: str | None = None
) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    where = "WHERE channel_slug = ?" if channel_slug else ""
    params = (channel_slug, limit, offset) if channel_slug else (limit, offset)
    return conn.execute(
        f"""
        SELECT id, channel_slug, detected_at, window_start, window_end, reason, score,
               message_count, baseline_message_rate, current_message_rate,
               emote_count, keyword_hits, stream_elapsed_seconds, clip_path, rating, notes,
               transcript, audio_events, stream_type, moment_type
        FROM moments
        {where}
        ORDER BY detected_at DESC
        LIMIT ? OFFSET ?
        """,
        params,
    ).fetchall()


def count_moments(conn: sqlite3.Connection, channel_slug: str | None = None) -> int:
    where = "WHERE channel_slug = ?" if channel_slug else ""
    params = (channel_slug,) if channel_slug else ()
    return conn.execute(f"SELECT COUNT(*) FROM moments {where}", params).fetchone()[0]


def get_moments_missing_taste_data(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Moments with a clip but missing transcript/audio_events/frame_embedding -
    e.g. imported via import_clip.py/import_clips_dir.py, which don't go
    through the live pipeline's background tasks. See scripts/backfill_taste.py.
    """
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT id, channel_slug, clip_path, transcript, audio_events, frame_embedding
        FROM moments
        WHERE clip_path IS NOT NULL
          AND (transcript IS NULL OR audio_events IS NULL OR frame_embedding IS NULL)
        ORDER BY id
        """
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


def update_moment_transcript(conn: sqlite3.Connection, moment_id: int, transcript: str) -> None:
    conn.execute("UPDATE moments SET transcript = ? WHERE id = ?", (transcript, moment_id))
    conn.commit()


def update_moment_audio_events(conn: sqlite3.Connection, moment_id: int, audio_events: str) -> None:
    conn.execute("UPDATE moments SET audio_events = ? WHERE id = ?", (audio_events, moment_id))
    conn.commit()


def update_moment_frame_embedding(conn: sqlite3.Connection, moment_id: int, frame_embedding: bytes) -> None:
    conn.execute("UPDATE moments SET frame_embedding = ? WHERE id = ?", (frame_embedding, moment_id))
    conn.commit()


def update_moment_stream_type(conn: sqlite3.Connection, moment_id: int, stream_type: str | None) -> None:
    conn.execute("UPDATE moments SET stream_type = ? WHERE id = ?", (stream_type, moment_id))
    conn.commit()


def update_moment_type(conn: sqlite3.Connection, moment_id: int, moment_type: str | None) -> None:
    conn.execute("UPDATE moments SET moment_type = ? WHERE id = ?", (moment_type, moment_id))
    conn.commit()


def update_moment_rating(conn: sqlite3.Connection, moment_id: int, rating: int | None) -> None:
    conn.execute("UPDATE moments SET rating = ? WHERE id = ?", (rating, moment_id))
    conn.commit()


def update_moment_window_end(conn: sqlite3.Connection, moment_id: int, window_end: str) -> None:
    conn.execute("UPDATE moments SET window_end = ? WHERE id = ?", (window_end, moment_id))
    conn.commit()


def update_moment_notes(conn: sqlite3.Connection, moment_id: int, notes: str | None) -> None:
    conn.execute("UPDATE moments SET notes = ? WHERE id = ?", (notes, moment_id))
    conn.commit()


def get_flag(conn: sqlite3.Connection, key: str, default: bool = True) -> bool:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    return row[0] == "1"


def set_flag(conn: sqlite3.Connection, key: str, value: bool) -> None:
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, "1" if value else "0"),
    )
    conn.commit()
