"""Puts rows into the test database through the service's own storage code,
with defaults for everything a given test doesn't care about."""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

from kick_clip_hunter import db, stream_recording

_ids = itertools.count(1)

# A fixed "now" for stored timestamps, so tests can talk about moments a
# known distance apart without racing the real clock.
T0 = datetime(2026, 3, 1, 20, 0, 0, tzinfo=timezone.utc)


def minutes(count: float) -> timedelta:
    return timedelta(minutes=count)


def add_streamer(slug: str, broadcaster_user_id: int, *, tracking: bool = True, chat_only: bool = False) -> None:
    """A Kick channel on the watchlist, tracked for clips - or for nothing,
    or with `chat_only` for its chat alone."""
    conn = db.get_connection()
    try:
        db.add_streamer(conn, broadcaster_user_id, slug)
        if not tracking or chat_only:
            db.set_streamer_tracking(conn, broadcaster_user_id, False, chat_only=chat_only)
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


def add_stream_recording(
    channel: str = "some_channel",
    *,
    broadcaster_user_id: int = 1,
    started_at: datetime = T0,
    seconds: float = 600.0,
    parts: list[tuple[datetime, float]] | None = None,
    state: str = stream_recording.READY,
    file: bool = True,
    **columns: object,
) -> int:
    """Stores a stream recording: `seconds` of a channel's stream from
    `started_at` on, in one part unless `parts` says otherwise (when each
    began and how long it runs).

    One that is ready is stored as the service stores it once its file has
    been made, with a file of made-up bytes where the video would be unless
    `file` is False; in any other state it is the bare row a recording
    starts out as. Any column of `stream_recordings` can be given by name.
    """
    kept = [stream_recording.Part(began, length) for began, length in parts or [(started_at, seconds)]]
    conn = db.get_connection()
    try:
        recording_id = db.insert_stream_recording(
            conn,
            broadcaster_user_id=broadcaster_user_id,
            channel_slug=channel,
            requested_at=started_at.isoformat(),
            state=state,
        )
        if state == stream_recording.READY:
            name = stream_recording.file_name(channel, recording_id, kept[0].started_at.astimezone())
            path = stream_recording.channel_folder(channel) / name
            if file:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"not really a video")
            db.finish_stream_recording(
                conn,
                recording_id,
                state=state,
                file_path=path.relative_to(stream_recording.STREAMS_DIR).as_posix(),
                started_at=kept[0].started_at.isoformat(),
                ended_at=kept[-1].ended_at.isoformat(),
                duration=stream_recording.length(kept),
                size_bytes=3 * 1024**3,
                parts=stream_recording.encoded(kept),
            )
        if columns:
            assignments = ", ".join(f"{name} = ?" for name in columns)
            conn.execute(f"UPDATE stream_recordings SET {assignments} WHERE id = ?", (*columns.values(), recording_id))
            conn.commit()
        return recording_id
    finally:
        conn.close()


def stream_recordings() -> list[dict]:
    """Every stored stream recording, the oldest first."""
    conn = db.get_connection()
    try:
        return [dict(row) for row in reversed(db.get_stream_recordings(conn))]
    finally:
        conn.close()
