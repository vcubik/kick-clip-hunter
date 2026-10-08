"""Puts rows into the test database through the service's own storage code,
with defaults for everything a given test doesn't care about."""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

from kick_clip_hunter import db

_ids = itertools.count(1)

# A fixed "now" for stored timestamps, so tests can talk about moments a
# known distance apart without racing the real clock.
T0 = datetime(2026, 3, 1, 20, 0, 0, tzinfo=timezone.utc)


def minutes(count: float) -> timedelta:
    return timedelta(minutes=count)


def add_streamer(slug: str, broadcaster_user_id: int, *, tracking: bool = True) -> None:
    conn = db.get_connection()
    try:
        db.add_streamer(conn, broadcaster_user_id, slug)
        if not tracking:
            db.set_streamer_tracking(conn, broadcaster_user_id, False)
    finally:
        conn.close()


def add_chat(channel: str, sender: str, content: str, received_at: datetime, broadcaster_user_id: int = 1) -> None:
    conn = db.get_connection()
    try:
        db.insert_chat_message(
            conn,
            message_id=f"message-{next(_ids)}",
            broadcaster_user_id=broadcaster_user_id,
            channel_slug=channel,
            sender_username=sender,
            content=content,
            emotes_json="[]",
            created_at=received_at.isoformat(),
            received_at=received_at.isoformat(),
        )
    finally:
        conn.close()


def add_moment(
    channel: str = "some_channel",
    *,
    detected_at: datetime = T0,
    reaction_seconds: float = 10.0,
    reason: str = "laugh",
    score: float = 7.5,
    broadcaster_user_id: int = 1,
    stream_elapsed_seconds: int | None = 3600,
    **columns: object,
) -> int:
    """Stores a moment detected at `detected_at`, whose window is the
    `reaction_seconds` before it. Any other column of the `moments` table
    (clip_path, rating, notes, transcript, ...) can be given by name."""
    conn = db.get_connection()
    try:
        moment_id = db.insert_moment(
            conn,
            broadcaster_user_id=broadcaster_user_id,
            channel_slug=channel,
            window_start=(detected_at - timedelta(seconds=reaction_seconds)).isoformat(),
            window_end=detected_at.isoformat(),
            reason=reason,
            score=score,
            message_count=12,
            baseline_message_rate=0.5,
            current_message_rate=1.2,
            emote_count=3,
            keyword_hits=8,
            stream_elapsed_seconds=stream_elapsed_seconds,
        )
        columns["detected_at"] = detected_at.isoformat()
        assignments = ", ".join(f"{name} = ?" for name in columns)
        conn.execute(f"UPDATE moments SET {assignments} WHERE id = ?", (*columns.values(), moment_id))
        conn.commit()
        return moment_id
    finally:
        conn.close()


def moment(moment_id: int) -> dict:
    conn = db.get_connection()
    try:
        cursor = conn.execute("SELECT * FROM moments WHERE id = ?", (moment_id,))
        names = [description[0] for description in cursor.description]
        return dict(zip(names, cursor.fetchone(), strict=True))
    finally:
        conn.close()
