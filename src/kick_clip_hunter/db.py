"""SQLite storage for the streamer watchlist and raw chat messages.

No retention/pruning yet - everything is kept. That's a deliberate,
temporary simplification (see roadmap) so we have real data to build the
detection heuristic against before deciding what's safe to prune.
"""

import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
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

-- Chat is looked up by when it arrived (received_at is the service's own
-- clock, the one moments are timed on), a stretch of one channel at a time.
CREATE INDEX IF NOT EXISTS idx_chat_messages_channel_received
    ON chat_messages (channel_slug, received_at);

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

-- A channel's 7TV emotes as chat types them: the exact name (7TV names are
-- case-sensitive, and a channel can rename an emote in its own set) and the
-- emote it stands for there. channel_keywords above is what the detector
-- listens for; this is what the dashboard draws. 7TV's global emotes, the
-- ones every channel has, are kept here too, under GLOBAL_EMOTES_OWNER.
CREATE TABLE IF NOT EXISTS channel_emotes (
    broadcaster_user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    emote_id TEXT NOT NULL,
    width INTEGER NOT NULL DEFAULT 0,
    height INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (broadcaster_user_id, name)
);

-- Who a chatter is in a channel: the colour of their name and their badges
-- (JSON, see chat_identity.py). One row per chatter, not per message - the
-- latest known state, rewritten only when it changes.
CREATE TABLE IF NOT EXISTS chat_identities (
    broadcaster_user_id INTEGER NOT NULL,
    username TEXT NOT NULL,
    colour TEXT,
    badges TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (broadcaster_user_id, username)
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# The broadcaster_user_id 7TV's global emotes are stored under in
# channel_emotes. No channel has it.
GLOBAL_EMOTES_OWNER = 0


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
        conn.execute(
            "UPDATE moments SET rating_int = CAST(rating AS INTEGER) WHERE rating IS NOT NULL AND rating != ''"
        )
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
    # NULL until then" story as transcript. Pure data capture for a future
    # learned classifier; nothing reads these yet.
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

    # Local AudioSet sound-event tags + embedding (see sound_events.py) -
    # broader complement to audio_events.py's narrow SenseVoice vocabulary.
    # Same "filled in asynchronously, NULL until then" story as the rest.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(moments)")}
    if "sound_events" not in columns:
        conn.execute("ALTER TABLE moments ADD COLUMN sound_events TEXT")
    if "sound_embedding" not in columns:
        conn.execute("ALTER TABLE moments ADD COLUMN sound_embedding BLOB")

    # Which stretch of the broadcast a moment's clip holds: when its first
    # frame was broadcast (the stream's program clock, like the recorder's
    # segments) and how long it runs. A clip is cut on whole segments, so
    # this differs from the window it was asked for by a few seconds - and
    # without it chat can't be lined up with the picture. NULL for clips cut
    # before this was stored; the dashboard fills in the length when it
    # first shows one, and estimates the start.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(moments)")}
    if "clip_start" not in columns:
        conn.execute("ALTER TABLE moments ADD COLUMN clip_start TEXT")
    if "clip_duration" not in columns:
        conn.execute("ALTER TABLE moments ADD COLUMN clip_duration REAL")

    # Per-channel pause switch, independent of the global watching toggle -
    # lets one noisy/offline channel be paused without touching the rest of
    # the watchlist. Defaults to on so existing rows keep behaving as before.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(streamers)")}
    if "tracking_enabled" not in columns:
        conn.execute("ALTER TABLE streamers ADD COLUMN tracking_enabled INTEGER NOT NULL DEFAULT 1")

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


def replace_channel_emotes(
    conn: sqlite3.Connection, broadcaster_user_id: int, emotes: dict[str, tuple[str, int, int]]
) -> None:
    """Stores a channel's 7TV emotes - (emote id, width, height) by the
    name chat types - in place of whatever was stored for it before."""
    conn.execute("DELETE FROM channel_emotes WHERE broadcaster_user_id = ?", (broadcaster_user_id,))
    conn.executemany(
        "INSERT INTO channel_emotes (broadcaster_user_id, name, emote_id, width, height) VALUES (?, ?, ?, ?, ?)",
        [(broadcaster_user_id, name, *emote) for name, emote in emotes.items()],
    )
    conn.commit()


def get_channel_emotes(conn: sqlite3.Connection, broadcaster_user_id: int) -> dict[str, tuple[str, int, int]]:
    rows = conn.execute(
        "SELECT name, emote_id, width, height FROM channel_emotes WHERE broadcaster_user_id = ?",
        (broadcaster_user_id,),
    ).fetchall()
    return {row[0]: (row[1], row[2], row[3]) for row in rows}


def get_chat_emotes(conn: sqlite3.Connection, broadcaster_user_id: int) -> dict[str, tuple[str, int, int]]:
    """Every 7TV emote a word in a channel's chat can stand for: 7TV's
    global ones and the channel's own. Where both have an emote of the same
    name the channel's wins, as it does for its viewers."""
    return {
        **get_channel_emotes(conn, GLOBAL_EMOTES_OWNER),
        **get_channel_emotes(conn, broadcaster_user_id),
    }


def set_chat_identity(
    conn: sqlite3.Connection, broadcaster_user_id: int, username: str, colour: str | None, badges: str
) -> None:
    """Stores a chatter's name colour and badges in a channel, in place of
    what was known of them there."""
    conn.execute(
        """
        INSERT INTO chat_identities (broadcaster_user_id, username, colour, badges) VALUES (?, ?, ?, ?)
        ON CONFLICT (broadcaster_user_id, username) DO UPDATE SET colour = excluded.colour, badges = excluded.badges
        """,
        (broadcaster_user_id, username, colour, badges),
    )
    conn.commit()


# How many names one lookup asks for at a time: under SQLite's limit on the
# parameters of a statement, also the lower one of older builds.
_IDENTITY_LOOKUP_CHUNK = 500


def get_chat_identities(
    conn: sqlite3.Connection, broadcaster_user_id: int, usernames: Iterable[str]
) -> dict[str, tuple[str | None, str]]:
    """(colour, badges) by name for those of the given chatters that are
    known in the channel."""
    names = sorted(set(usernames))
    identities: dict[str, tuple[str | None, str]] = {}
    for start in range(0, len(names), _IDENTITY_LOOKUP_CHUNK):
        chunk = names[start : start + _IDENTITY_LOOKUP_CHUNK]
        rows = conn.execute(
            f"""
            SELECT username, colour, badges FROM chat_identities
            WHERE broadcaster_user_id = ? AND username IN ({", ".join("?" * len(chunk))})
            """,
            (broadcaster_user_id, *chunk),
        ).fetchall()
        identities.update({row[0]: (row[1], row[2]) for row in rows})
    return identities


def get_streamers(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT slug, broadcaster_user_id, added_at, tracking_enabled FROM streamers ORDER BY added_at"
    ).fetchall()


def get_streamer_by_slug(conn: sqlite3.Connection, slug: str) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT broadcaster_user_id, tracking_enabled FROM streamers WHERE slug = ?", (slug,)
    ).fetchone()


def get_streamer_tracking_enabled(conn: sqlite3.Connection, broadcaster_user_id: int) -> bool:
    row = conn.execute(
        "SELECT tracking_enabled FROM streamers WHERE broadcaster_user_id = ?", (broadcaster_user_id,)
    ).fetchone()
    return bool(row[0]) if row else True


def set_streamer_tracking(conn: sqlite3.Connection, broadcaster_user_id: int, enabled: bool) -> None:
    conn.execute(
        "UPDATE streamers SET tracking_enabled = ? WHERE broadcaster_user_id = ?",
        (1 if enabled else 0, broadcaster_user_id),
    )
    conn.commit()


# What the dashboard and the scripts read of a moment.
_MOMENT_COLUMNS = """
    id, broadcaster_user_id, channel_slug, detected_at, window_start, window_end, reason, score,
    message_count, baseline_message_rate, current_message_rate,
    emote_count, keyword_hits, stream_elapsed_seconds, clip_path, clip_start, clip_duration,
    rating, notes, transcript, audio_events, sound_events, stream_type, moment_type
"""


def _moment_filter(channel_slug: str | None, unrated: bool, min_rating: int | None) -> tuple[str, list]:
    """The WHERE clause (empty when nothing is filtered) and its parameters
    for the ways the dashboard narrows the list of moments down."""
    conditions: list[str] = []
    params: list = []
    if channel_slug:
        conditions.append("channel_slug = ?")
        params.append(channel_slug)
    if unrated:
        conditions.append("rating IS NULL")
    if min_rating is not None:
        conditions.append("rating >= ?")
        params.append(min_rating)
    return ("WHERE " + " AND ".join(conditions) if conditions else ""), params


def get_recent_moments(
    conn: sqlite3.Connection,
    limit: int = 50,
    offset: int = 0,
    channel_slug: str | None = None,
    *,
    unrated: bool = False,
    min_rating: int | None = None,
) -> list[sqlite3.Row]:
    """Moments newest first, optionally only one channel's, only the ones
    not rated yet, or only those rated `min_rating` or higher."""
    conn.row_factory = sqlite3.Row
    where, params = _moment_filter(channel_slug, unrated, min_rating)
    return conn.execute(
        f"""
        SELECT {_MOMENT_COLUMNS}
        FROM moments
        {where}
        ORDER BY detected_at DESC, id DESC
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    ).fetchall()


def get_moment(conn: sqlite3.Connection, moment_id: int) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    return conn.execute(f"SELECT {_MOMENT_COLUMNS} FROM moments WHERE id = ?", (moment_id,)).fetchone()


def count_moments(
    conn: sqlite3.Connection,
    channel_slug: str | None = None,
    *,
    unrated: bool = False,
    min_rating: int | None = None,
) -> int:
    where, params = _moment_filter(channel_slug, unrated, min_rating)
    return conn.execute(f"SELECT COUNT(*) FROM moments {where}", params).fetchone()[0]


def count_moments_with_clip(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM moments WHERE clip_path IS NOT NULL").fetchone()[0]


def get_moments_missing_taste_data(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Moments with a clip but missing transcript/audio_events/frame_embedding/
    sound_events/sound_embedding - e.g. imported via import_clip.py/
    import_clips_dir.py, which don't go through the live pipeline's
    background tasks. See scripts/backfill_taste.py.
    """
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT id, channel_slug, clip_path, transcript, audio_events, frame_embedding,
               sound_events, sound_embedding
        FROM moments
        WHERE clip_path IS NOT NULL
          AND (transcript IS NULL OR audio_events IS NULL OR frame_embedding IS NULL
               OR sound_events IS NULL OR sound_embedding IS NULL)
        ORDER BY id
        """
    ).fetchall()


def get_moment_channels(conn: sqlite3.Connection) -> list[str]:
    """Distinct channels that have at least one moment, for the dashboard filter -
    covers channels no longer on the watchlist too, so their past moments stay filterable.
    """
    conn.row_factory = sqlite3.Row
    return [row[0] for row in conn.execute("SELECT DISTINCT channel_slug FROM moments ORDER BY channel_slug")]


def get_chat_between(
    conn: sqlite3.Connection, channel_slug: str, start: datetime, end: datetime, limit: int = 2000
) -> list[sqlite3.Row]:
    """One channel's chat that arrived from `start` up to and including
    `end`, oldest first - at most `limit` messages, the earliest ones.

    The bounds are compared as text, which works because every stored
    received_at is a UTC ISO timestamp; older rows end in "Z" and carry
    milliseconds rather than "+00:00" and microseconds, which can only
    misplace a message by less than a millisecond.
    """
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT received_at, sender_username, content
        FROM chat_messages
        WHERE channel_slug = ? AND received_at BETWEEN ? AND ?
        ORDER BY received_at
        LIMIT ?
        """,
        (channel_slug, start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat(), limit),
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
) -> bool:
    """Stores a chat message. Returns False if a message with this id was
    already stored (a redelivered webhook), in which case nothing changes."""
    cursor = conn.execute(
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
    return cursor.rowcount == 1


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


def update_moment_clip_path(
    conn: sqlite3.Connection,
    moment_id: int,
    clip_path: str,
    clip_start: str | None = None,
    clip_duration: float | None = None,
) -> None:
    """Records a moment's clip and, where it is known, the stretch of the
    broadcast it holds (see the clip_start/clip_duration columns)."""
    conn.execute(
        "UPDATE moments SET clip_path = ?, clip_start = ?, clip_duration = ? WHERE id = ?",
        (clip_path, clip_start, clip_duration, moment_id),
    )
    conn.commit()


def update_moment_clip_duration(conn: sqlite3.Connection, moment_id: int, clip_duration: float) -> None:
    conn.execute("UPDATE moments SET clip_duration = ? WHERE id = ?", (clip_duration, moment_id))
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


def update_moment_sound_events(conn: sqlite3.Connection, moment_id: int, sound_events: str) -> None:
    conn.execute("UPDATE moments SET sound_events = ? WHERE id = ?", (sound_events, moment_id))
    conn.commit()


def update_moment_sound_embedding(conn: sqlite3.Connection, moment_id: int, sound_embedding: bytes) -> None:
    conn.execute("UPDATE moments SET sound_embedding = ? WHERE id = ?", (sound_embedding, moment_id))
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


def _chat_delay_key(channel_slug: str) -> str:
    return f"chat_delay:{channel_slug}"


def get_chat_delay(conn: sqlite3.Connection, channel_slug: str) -> int | None:
    """How many seconds behind its broadcast a channel's chat is taken to
    run when it is shown against a clip - or None if that was never set for
    the channel. Kept by channel name rather than with the watchlist, so it
    outlasts the channel being on it, as its moments do."""
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (_chat_delay_key(channel_slug),)).fetchone()
    return int(row[0]) if row else None


def set_chat_delay(conn: sqlite3.Connection, channel_slug: str, seconds: int) -> None:
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (_chat_delay_key(channel_slug), str(seconds)),
    )
    conn.commit()
