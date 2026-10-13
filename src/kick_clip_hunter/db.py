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

-- The same by the channel's id, for what knows a channel by its name on the
-- watchlist: chat is stored under the name Kick sends it with, which need
-- not be spelled the same.
CREATE INDEX IF NOT EXISTS idx_chat_messages_broadcaster_received
    ON chat_messages (broadcaster_user_id, received_at);

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

-- What was set on the dashboard's Settings page (see tuning.py), by the
-- setting's name, as JSON: under TUNING_FOR_ALL the values for every
-- channel, under a channel's id the ones it has of its own. A setting
-- nobody gave a value has no row.
CREATE TABLE IF NOT EXISTS tuning (
    broadcaster_user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (broadcaster_user_id, name)
);

-- Streams kept whole (see stream_recording.py), one row from the moment a
-- recording is asked for. While it runs the row says little: how it stands
-- (`state`) and when it was asked for. What it holds is filled in once its
-- file has been made: where the file is (relative to the streams folder),
-- how long and how big it is, when its first and last frames were
-- broadcast, and its parts (JSON: when each began and how long it runs, in
-- the order they are in the file) - which is what places chat against the
-- picture. `ended_by` is why it ended. Ids are never used twice: a
-- recording's folder and file are named for it.
CREATE TABLE IF NOT EXISTS stream_recordings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    broadcaster_user_id INTEGER NOT NULL,
    channel_slug TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    state TEXT NOT NULL,
    ended_by TEXT,
    file_path TEXT,
    started_at TEXT,
    ended_at TEXT,
    duration REAL,
    size_bytes INTEGER,
    parts TEXT
);
"""


# The broadcaster_user_id 7TV's global emotes are stored under in
# channel_emotes. No channel has it.
GLOBAL_EMOTES_OWNER = 0

# The same for the settings that hold for every channel, in tuning.
TUNING_FOR_ALL = 0


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

    # A name the reviewer gives a moment, shown in the dashboard's queue in
    # place of what chat said most. NULL until one is given.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(moments)")}
    if "title" not in columns:
        conn.execute("ALTER TABLE moments ADD COLUMN title TEXT")

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

    # A moment's file holds the clip and the footage either side of it: how
    # many seconds of it come before the clip's first frame, and how many
    # after its last. clip_start and clip_duration are still the clip's own,
    # so the file runs for context_before + clip_duration + context_after.
    # NULL where the file is the clip alone - an imported clip, or one from
    # when context was kept in files of its own next to the clip (see
    # scripts/join_context_clips.py).
    if "context_before" not in columns:
        conn.execute("ALTER TABLE moments ADD COLUMN context_before REAL")
    if "context_after" not in columns:
        conn.execute("ALTER TABLE moments ADD COLUMN context_after REAL")

    # Per-channel pause switch, independent of the global watching toggle -
    # lets one noisy/offline channel be paused without touching the rest of
    # the watchlist. Defaults to on so existing rows keep behaving as before.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(streamers)")}
    if "tracking_enabled" not in columns:
        conn.execute("ALTER TABLE streamers ADD COLUMN tracking_enabled INTEGER NOT NULL DEFAULT 1")

    # Whether a channel goes by settings of its own where it has them (see
    # tuning.py) or by the ones for all channels. Off to begin with, and
    # switching it off again leaves the channel's values where they are.
    if "own_tuning" not in columns:
        conn.execute("ALTER TABLE streamers ADD COLUMN own_tuning INTEGER NOT NULL DEFAULT 0")

    # Whether every stream of the channel is kept whole, from when it goes
    # live, without anyone starting the recording (see stream_recording.py).
    # Off to begin with: a recording is big.
    if "always_record" not in columns:
        conn.execute("ALTER TABLE streamers ADD COLUMN always_record INTEGER NOT NULL DEFAULT 0")

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
        "SELECT slug, broadcaster_user_id, added_at, tracking_enabled, own_tuning, always_record"
        " FROM streamers ORDER BY added_at"
    ).fetchall()


def get_streamer_by_slug(conn: sqlite3.Connection, slug: str) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT broadcaster_user_id, tracking_enabled, own_tuning, always_record FROM streamers WHERE slug = ?",
        (slug,),
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


def set_streamer_always_record(conn: sqlite3.Connection, broadcaster_user_id: int, enabled: bool) -> None:
    conn.execute(
        "UPDATE streamers SET always_record = ? WHERE broadcaster_user_id = ?",
        (1 if enabled else 0, broadcaster_user_id),
    )
    conn.commit()


def set_streamer_own_tuning(conn: sqlite3.Connection, broadcaster_user_id: int, enabled: bool) -> None:
    conn.execute(
        "UPDATE streamers SET own_tuning = ? WHERE broadcaster_user_id = ?",
        (1 if enabled else 0, broadcaster_user_id),
    )
    conn.commit()


def get_tuning(conn: sqlite3.Connection, owner: int = TUNING_FOR_ALL) -> dict[str, str]:
    """The settings that were given a value, as stored (JSON by name): the
    ones for every channel, or with a channel's id that channel's own -
    whether or not it goes by them right now."""
    rows = conn.execute("SELECT name, value FROM tuning WHERE broadcaster_user_id = ?", (owner,)).fetchall()
    return {row[0]: row[1] for row in rows}


def get_tuning_in_force(conn: sqlite3.Connection) -> dict[int, dict[str, str]]:
    """Everything stored that the service is to go by, by owner: the
    settings for every channel (TUNING_FOR_ALL) and, by channel id, those of
    the channels that go by their own."""
    rows = conn.execute(
        """
        SELECT tuning.broadcaster_user_id, tuning.name, tuning.value FROM tuning
        LEFT JOIN streamers ON streamers.broadcaster_user_id = tuning.broadcaster_user_id
        WHERE tuning.broadcaster_user_id = ? OR streamers.own_tuning = 1
        """,
        (TUNING_FOR_ALL,),
    ).fetchall()
    in_force: dict[int, dict[str, str]] = {}
    for owner, name, value in rows:
        in_force.setdefault(owner, {})[name] = value
    return in_force


def replace_tuning(conn: sqlite3.Connection, owner: int, values: dict[str, str]) -> None:
    """Stores an owner's settings in place of whatever it had: a setting
    that is not among them goes back to having no value of its own."""
    conn.execute("DELETE FROM tuning WHERE broadcaster_user_id = ?", (owner,))
    conn.executemany(
        "INSERT INTO tuning (broadcaster_user_id, name, value) VALUES (?, ?, ?)",
        [(owner, name, value) for name, value in values.items()],
    )
    conn.commit()


# What the dashboard and the scripts read of a moment.
_MOMENT_COLUMNS = """
    id, broadcaster_user_id, channel_slug, detected_at, window_start, window_end, reason, score,
    message_count, baseline_message_rate, current_message_rate,
    emote_count, keyword_hits, stream_elapsed_seconds, clip_path, clip_start, clip_duration,
    context_before, context_after,
    rating, notes, title, transcript, audio_events, sound_events, stream_type, moment_type
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
        SELECT id, channel_slug, clip_path, clip_duration, context_before, context_after,
               transcript, audio_events, frame_embedding, sound_events, sound_embedding
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


def get_channel_chat_between(
    conn: sqlite3.Connection, broadcaster_user_id: int, start: datetime, end: datetime, limit: int = 2000
) -> list[sqlite3.Row]:
    """As get_chat_between, for a channel told by its id rather than by the
    name its chat came under. That name is Kick's own spelling (all in small
    letters), so it is not what to look chat up by for something that has
    the channel's name from the watchlist - a stream recording."""
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT received_at, sender_username, content
        FROM chat_messages
        WHERE broadcaster_user_id = ? AND received_at BETWEEN ? AND ?
        ORDER BY received_at
        LIMIT ?
        """,
        (
            broadcaster_user_id,
            start.astimezone(timezone.utc).isoformat(),
            end.astimezone(timezone.utc).isoformat(),
            limit,
        ),
    ).fetchall()


def get_chat_name(conn: sqlite3.Connection, broadcaster_user_id: int) -> str | None:
    """The name a channel's chat is stored under - the one its newest
    message came with - or None if none is stored. It is also the name its
    moments have, and what is kept by channel name is kept under."""
    row = conn.execute(
        "SELECT channel_slug FROM chat_messages WHERE broadcaster_user_id = ? ORDER BY received_at DESC LIMIT 1",
        (broadcaster_user_id,),
    ).fetchone()
    return row[0] if row else None


# -- a channel's stored chat as a whole (the Analyze page, see chat_stats.py) --
#
# A stretch of chat is given as when it starts and when it ends, and holds
# what arrived from the start up to, but not at, the end - so two stretches
# that follow each other share no message. The bounds are compared as text,
# as in get_chat_between.

# How many rows of a channel's chat one statement reads at a time.
CHAT_CHUNK = 20_000


def _folded(text: str | None) -> str:
    return (text or "").casefold()


def _chat_filter(
    conn: sqlite3.Connection,
    broadcaster_user_id: int,
    start: datetime,
    end: datetime,
    sender: str | None = None,
    text: str | None = None,
) -> tuple[str, list]:
    """The WHERE clause, and its parameters, for a stretch of a channel's
    chat - narrowed to what one chatter wrote (`sender`, in whatever case)
    and to the messages that have `text` in them, however that is capitalised
    in either."""
    conditions = ["broadcaster_user_id = ?", "received_at >= ?", "received_at < ?"]
    params: list = [broadcaster_user_id, start.astimezone(timezone.utc).isoformat()]
    params.append(end.astimezone(timezone.utc).isoformat())
    if sender:
        conditions.append("sender_username = ? COLLATE NOCASE")
        params.append(sender)
    if text:
        # SQLite's own LIKE folds the case of A to Z and nothing else, which
        # is not enough for a chat that is not written in English.
        conn.create_function("folded", 1, _folded, deterministic=True)
        conditions.append("instr(folded(content), ?) > 0")
        params.append(text.casefold())
    return "WHERE " + " AND ".join(conditions), params


def get_chat_channel_ids(conn: sqlite3.Connection) -> list[int]:
    """The id of every channel that has chat stored, on the watchlist or not.

    Asked on every load of a page, so it must not read all of the chat to
    tell a handful of channels apart: it steps through the index from one
    channel's first entry to the next one's."""
    rows = conn.execute(
        """
        WITH RECURSIVE channel(id) AS (
            SELECT MIN(broadcaster_user_id) FROM chat_messages
            UNION ALL
            SELECT (SELECT MIN(broadcaster_user_id) FROM chat_messages WHERE broadcaster_user_id > channel.id)
            FROM channel WHERE channel.id IS NOT NULL
        )
        SELECT id FROM channel WHERE id IS NOT NULL
        """
    ).fetchall()
    return [row[0] for row in rows]


def get_chat_span(conn: sqlite3.Connection, broadcaster_user_id: int) -> tuple[str, str] | None:
    """When a channel's oldest and newest stored messages arrived, or None
    if it has none."""
    ends = [
        conn.execute(
            f"SELECT received_at FROM chat_messages WHERE broadcaster_user_id = ? ORDER BY received_at {order} LIMIT 1",
            (broadcaster_user_id,),
        ).fetchone()
        for order in ("ASC", "DESC")
    ]
    return (ends[0][0], ends[1][0]) if ends[0] else None


def get_chat_sessions(
    conn: sqlite3.Connection,
    broadcaster_user_id: int,
    *,
    gap_seconds: float,
    min_messages: int,
    min_seconds: float,
    limit: int,
    newest: int,
) -> list[sqlite3.Row]:
    """A channel's chat as the sessions it came in, the newest first: when
    the first and the last message of each arrived, and how many there were.
    A session is chat with no silence longer than `gap_seconds` in it; one
    of fewer than `min_messages`, or one that is over within `min_seconds`,
    is left out, and at most `limit` are returned. Only the `newest` that
    many messages are looked at, so that the cost does not grow with
    everything that was ever stored."""
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        WITH timed AS (
            SELECT received_at, julianday(received_at) AS day FROM (
                SELECT received_at FROM chat_messages
                WHERE broadcaster_user_id = ? ORDER BY received_at DESC LIMIT ?
            )
        ), stepped AS (
            SELECT received_at, day - LAG(day) OVER (ORDER BY received_at) AS silence FROM timed
        ), numbered AS (
            SELECT received_at, SUM(silence IS NOT NULL AND silence > ?) OVER (ORDER BY received_at) AS session
            FROM stepped
        )
        SELECT MIN(received_at) AS first, MAX(received_at) AS last, COUNT(*) AS messages
        FROM numbered GROUP BY session
        HAVING COUNT(*) >= ? AND julianday(MAX(received_at)) - julianday(MIN(received_at)) >= ?
        ORDER BY session DESC LIMIT ?
        """,
        (broadcaster_user_id, newest, gap_seconds / 86400, min_messages, min_seconds / 86400, limit),
    ).fetchall()


def count_channel_chat(
    conn: sqlite3.Connection,
    broadcaster_user_id: int,
    start: datetime,
    end: datetime,
    *,
    sender: str | None = None,
    text: str | None = None,
) -> int:
    """How many messages a stretch of a channel's chat holds (see
    _chat_filter)."""
    where, params = _chat_filter(conn, broadcaster_user_id, start, end, sender, text)
    return conn.execute(f"SELECT COUNT(*) FROM chat_messages {where}", params).fetchone()[0]


def iter_channel_chat(
    conn: sqlite3.Connection,
    broadcaster_user_id: int,
    start: datetime,
    end: datetime,
    *,
    limit: int,
    sender: str | None = None,
    text: str | None = None,
) -> Iterable[sqlite3.Row]:
    """A stretch of a channel's chat (see _chat_filter), the newest message
    first and at most `limit` of them.

    It is read a chunk at a time, each with a statement that is over before
    its rows are handed out: a statement left open while its rows are worked
    through keeps the database locked against the webhook's writes for as
    long as that takes. The stretch must not reach into the future, or a
    message arriving meanwhile would move the ones behind it."""
    conn.row_factory = sqlite3.Row
    where, params = _chat_filter(conn, broadcaster_user_id, start, end, sender, text)
    handed_out = 0
    while handed_out < limit:
        rows = conn.execute(
            f"""
            SELECT message_id, received_at, sender_username, content FROM chat_messages {where}
            ORDER BY received_at DESC LIMIT ? OFFSET ?
            """,
            (*params, min(CHAT_CHUNK, limit - handed_out), handed_out),
        ).fetchall()
        yield from rows
        handed_out += len(rows)
        if len(rows) < CHAT_CHUNK:
            return


def search_channel_chat(
    conn: sqlite3.Connection,
    broadcaster_user_id: int,
    start: datetime,
    end: datetime,
    *,
    sender: str | None = None,
    text: str | None = None,
    newest_first: bool = True,
    limit: int = 100,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """One page of what a search of a stretch of a channel's chat finds (see
    _chat_filter; count_channel_chat says how many that is in all)."""
    conn.row_factory = sqlite3.Row
    where, params = _chat_filter(conn, broadcaster_user_id, start, end, sender, text)
    return conn.execute(
        f"""
        SELECT message_id, received_at, sender_username, content FROM chat_messages {where}
        ORDER BY received_at {"DESC" if newest_first else "ASC"} LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    ).fetchall()


def get_chat_around(
    conn: sqlite3.Connection, broadcaster_user_id: int, message_id: str, *, before: int, after: int
) -> list[sqlite3.Row] | None:
    """A message of a channel's chat with what was written around it, in the
    order it arrived: up to `before` messages ahead of it and `after`
    behind. None if the channel has no such message."""
    conn.row_factory = sqlite3.Row
    found = conn.execute(
        "SELECT received_at FROM chat_messages WHERE message_id = ? AND broadcaster_user_id = ?",
        (message_id, broadcaster_user_id),
    ).fetchone()
    if found is None:
        return None
    columns = "SELECT message_id, received_at, sender_username, content FROM chat_messages"
    earlier = conn.execute(
        f"{columns} WHERE broadcaster_user_id = ? AND received_at < ? ORDER BY received_at DESC LIMIT ?",
        (broadcaster_user_id, found["received_at"], before),
    ).fetchall()
    # The message itself is the first of these, unless another arrived in
    # the very same instant.
    later = conn.execute(
        f"{columns} WHERE broadcaster_user_id = ? AND received_at >= ? ORDER BY received_at LIMIT ?",
        (broadcaster_user_id, found["received_at"], after + 1),
    ).fetchall()
    return [*reversed(earlier), *later]


def get_chatter_counts(
    conn: sqlite3.Connection, broadcaster_user_id: int, start: datetime, end: datetime
) -> list[tuple[str, int]]:
    """Everyone who wrote in a stretch of a channel's chat and how many
    messages each, the most active first."""
    where, params = _chat_filter(conn, broadcaster_user_id, start, end)
    rows = conn.execute(
        f"""
        SELECT sender_username, COUNT(*) FROM chat_messages {where}
        GROUP BY sender_username ORDER BY COUNT(*) DESC, sender_username
        """,
        params,
    ).fetchall()
    return [(row[0] or "", row[1]) for row in rows]


def count_returning_chatters(
    conn: sqlite3.Connection, broadcaster_user_id: int, start: datetime, end: datetime
) -> int | None:
    """How many of those who wrote in a stretch of a channel's chat had
    written there before it - or None if no chat is stored from before it,
    which leaves nothing to tell a new chatter by."""
    where, params = _chat_filter(conn, broadcaster_user_id, start, end)
    before = (broadcaster_user_id, start.astimezone(timezone.utc).isoformat())
    earlier = "FROM chat_messages WHERE broadcaster_user_id = ? AND received_at < ?"
    if conn.execute(f"SELECT 1 {earlier} LIMIT 1", before).fetchone() is None:
        return None
    return conn.execute(
        f"""
        SELECT COUNT(*) FROM (
            SELECT sender_username FROM chat_messages {where}
            INTERSECT
            SELECT sender_username {earlier}
        )
        """,
        (*params, *before),
    ).fetchone()[0]


def get_first_message_time(conn: sqlite3.Connection, broadcaster_user_id: int, sender: str) -> str | None:
    """When the oldest stored message of a chatter in a channel arrived."""
    return conn.execute(
        """
        SELECT MIN(received_at) FROM chat_messages
        WHERE broadcaster_user_id = ? AND sender_username = ? COLLATE NOCASE
        """,
        (broadcaster_user_id, sender),
    ).fetchone()[0]


def get_moment_times(
    conn: sqlite3.Connection, broadcaster_user_id: int, start: datetime, end: datetime, *, not_for: str
) -> list[str]:
    """When each moment of a channel in a stretch of time was detected, the
    oldest first. Moments stored for the reason `not_for` are left out: a
    clip imported from elsewhere is a moment in name only, "detected" when
    it was imported."""
    rows = conn.execute(
        """
        SELECT detected_at FROM moments
        WHERE broadcaster_user_id = ? AND detected_at >= ? AND detected_at < ? AND reason != ?
        ORDER BY detected_at
        """,
        (
            broadcaster_user_id,
            start.astimezone(timezone.utc).isoformat(),
            end.astimezone(timezone.utc).isoformat(),
            not_for,
        ),
    ).fetchall()
    return [row[0] for row in rows]


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


def get_moments_with_clip_alone(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Moments whose clip file is recorded as holding the clip and nothing
    else - among them the ones cut when the footage either side of a clip
    was kept in files of its own. See scripts/join_context_clips.py."""
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT id, channel_slug, clip_path, clip_duration
        FROM moments
        WHERE clip_path IS NOT NULL AND context_before IS NULL AND context_after IS NULL
        ORDER BY id
        """
    ).fetchall()


def update_moment_clip_path(
    conn: sqlite3.Connection,
    moment_id: int,
    clip_path: str,
    clip_start: str | None = None,
    clip_duration: float | None = None,
    context_before: float | None = None,
    context_after: float | None = None,
) -> None:
    """Records a moment's clip and, where it is known, the stretch of the
    broadcast it holds (see the clip_start/clip_duration columns) and how
    much footage its file has on either side of it (context_before/_after)."""
    conn.execute(
        """
        UPDATE moments
        SET clip_path = ?, clip_start = ?, clip_duration = ?, context_before = ?, context_after = ?
        WHERE id = ?
        """,
        (clip_path, clip_start, clip_duration, context_before, context_after, moment_id),
    )
    conn.commit()


def update_moment_context(
    conn: sqlite3.Connection, moment_id: int, clip_duration: float, context_before: float, context_after: float
) -> None:
    """Records that a moment's file now also holds the footage either side
    of its clip - what joining its context clips into it makes true."""
    conn.execute(
        "UPDATE moments SET clip_duration = ?, context_before = ?, context_after = ? WHERE id = ?",
        (clip_duration, context_before, context_after, moment_id),
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


def update_moment_title(conn: sqlite3.Connection, moment_id: int, title: str | None) -> None:
    conn.execute("UPDATE moments SET title = ? WHERE id = ?", (title, moment_id))
    conn.commit()


def update_moment_notes(conn: sqlite3.Connection, moment_id: int, notes: str | None) -> None:
    conn.execute("UPDATE moments SET notes = ? WHERE id = ?", (notes, moment_id))
    conn.commit()


def insert_stream_recording(
    conn: sqlite3.Connection, *, broadcaster_user_id: int, channel_slug: str, requested_at: str, state: str
) -> int:
    """Stores a stream recording that was just asked for (see the
    stream_recordings table) and returns its id."""
    cursor = conn.execute(
        """
        INSERT INTO stream_recordings (broadcaster_user_id, channel_slug, requested_at, state)
        VALUES (?, ?, ?, ?)
        """,
        (broadcaster_user_id, channel_slug, requested_at, state),
    )
    conn.commit()
    return cursor.lastrowid


def get_stream_recording(conn: sqlite3.Connection, recording_id: int) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM stream_recordings WHERE id = ?", (recording_id,)).fetchone()


def get_stream_recordings(conn: sqlite3.Connection, states: Iterable[str] | None = None) -> list[sqlite3.Row]:
    """Stream recordings, the newest first - all of them, or the ones that
    stand in one of the given states."""
    conn.row_factory = sqlite3.Row
    states = None if states is None else list(states)
    where = "" if states is None else f"WHERE state IN ({', '.join('?' * len(states))})"
    return conn.execute(f"SELECT * FROM stream_recordings {where} ORDER BY id DESC", states or []).fetchall()


def count_stream_recordings(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM stream_recordings").fetchone()[0]


def set_stream_recording_state(
    conn: sqlite3.Connection, recording_id: int, state: str, ended_by: str | None = None
) -> None:
    """Records how a stream recording stands now and, where that is given,
    why it ended - which otherwise stays as it was."""
    conn.execute(
        "UPDATE stream_recordings SET state = ?, ended_by = COALESCE(?, ended_by) WHERE id = ?",
        (state, ended_by, recording_id),
    )
    conn.commit()


def finish_stream_recording(
    conn: sqlite3.Connection,
    recording_id: int,
    *,
    state: str,
    file_path: str,
    started_at: str,
    ended_at: str,
    duration: float,
    size_bytes: int,
    parts: str,
) -> None:
    """Records the file a stream recording was made into and what it holds."""
    conn.execute(
        """
        UPDATE stream_recordings
        SET state = ?, file_path = ?, started_at = ?, ended_at = ?, duration = ?, size_bytes = ?, parts = ?
        WHERE id = ?
        """,
        (state, file_path, started_at, ended_at, duration, size_bytes, parts, recording_id),
    )
    conn.commit()


def delete_stream_recording(conn: sqlite3.Connection, recording_id: int) -> None:
    conn.execute("DELETE FROM stream_recordings WHERE id = ?", (recording_id,))
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
