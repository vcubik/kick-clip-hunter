"""SQLite storage: the schema and its in-place upgrades, then each query.

There is no migration framework - `get_connection()` brings whatever
database it finds up to date with idempotent ALTERs. The upgrade tests build
databases the way older versions of the code left them and check that opening
them today loses nothing.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from kick_clip_hunter import db

T0 = datetime(2026, 3, 1, 20, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    connection = db.get_connection()
    yield connection
    connection.close()


def columns(connection: sqlite3.Connection, table: str) -> dict[str, str]:
    """Column name -> declared type."""
    return {row[1]: row[2].upper() for row in connection.execute(f"PRAGMA table_info({table})")}


def tables(connection: sqlite3.Connection) -> set[str]:
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def legacy_database(*statements: str) -> None:
    """Creates the database file by hand, as an older version left it."""
    db.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db.DB_PATH)
    for statement in statements:
        connection.execute(statement)
    connection.commit()
    connection.close()


def store_moment(connection, channel: str = "some_channel", **overrides) -> int:
    values = {
        "broadcaster_user_id": 1,
        "channel_slug": channel,
        "window_start": (T0 - timedelta(seconds=10)).isoformat(),
        "window_end": T0.isoformat(),
        "reason": "laugh",
        "score": 7.5,
        "message_count": 12,
        "baseline_message_rate": 0.5,
        "current_message_rate": 1.2,
        "emote_count": 3,
        "keyword_hits": 8,
    }
    values.update(overrides)
    return db.insert_moment(connection, **values)


def store_chat(
    connection, channel: str, sender: str, content: str, received_at: datetime, message_id: str | None = None
):
    db.insert_chat_message(
        connection,
        message_id=message_id or f"{channel}-{sender}-{received_at.timestamp()}",
        broadcaster_user_id=1,
        channel_slug=channel,
        sender_username=sender,
        content=content,
        emotes_json="[]",
        created_at=received_at.isoformat(),
        received_at=received_at.isoformat(),
    )


class TestSchema:
    def test_a_fresh_database_gets_every_table(self, conn):
        assert tables(conn) >= {"streamers", "chat_messages", "moments", "channel_keywords", "app_settings"}

    def test_the_database_file_and_its_directory_are_created_on_first_use(self):
        assert not db.DB_PATH.parent.exists()

        db.get_connection().close()

        assert db.DB_PATH.is_file()

    def test_moments_have_every_column_the_application_reads(self, conn):
        assert set(columns(conn, "moments")) == {
            "id", "broadcaster_user_id", "channel_slug", "detected_at", "window_start", "window_end",
            "reason", "score", "message_count", "baseline_message_rate", "current_message_rate",
            "emote_count", "keyword_hits", "stream_elapsed_seconds", "clip_path", "rating", "notes",
            "transcript", "audio_events", "frame_embedding", "stream_type", "moment_type",
            "sound_events", "sound_embedding",
        }  # fmt: skip

    def test_ratings_are_integers(self, conn):
        # "3" != 3 once broke the dashboard's active-button check.
        assert columns(conn, "moments")["rating"] == "INTEGER"

    def test_opening_the_database_again_changes_nothing(self, conn):
        db.add_streamer(conn, 1, "some_channel")
        moment_id = store_moment(conn)
        db.update_moment_rating(conn, moment_id, 4)
        before = (columns(conn, "moments"), columns(conn, "streamers"))

        for _ in range(3):
            again = db.get_connection()
            again.close()

        assert (columns(conn, "moments"), columns(conn, "streamers")) == before
        assert conn.execute("SELECT rating FROM moments").fetchone()[0] == 4
        assert conn.execute("SELECT slug FROM streamers").fetchone()[0] == "some_channel"


class TestUpgradingOlderDatabases:
    OLD_MOMENTS = """
        CREATE TABLE moments (
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
            {extra}
        )
    """
    OLD_MOMENT_ROW = (
        "INSERT INTO moments (broadcaster_user_id, channel_slug, window_start, window_end, reason, score, "
        "message_count, baseline_message_rate, current_message_rate, emote_count, keyword_hits {columns}) "
        "VALUES (1, 'some_channel', '2026-01-01T12:00:00+00:00', '2026-01-01T12:00:10+00:00', 'laugh', 5.0, "
        "9, 0.5, 0.9, 0, 4 {values})"
    )

    def test_a_database_from_before_clips_and_review_gains_the_new_columns(self):
        legacy_database(
            self.OLD_MOMENTS.format(extra=""),
            self.OLD_MOMENT_ROW.format(columns="", values=""),
        )

        connection = db.get_connection()
        try:
            added_since = {
                "stream_elapsed_seconds", "clip_path", "rating", "notes", "transcript", "audio_events",
                "frame_embedding", "stream_type", "moment_type", "sound_events", "sound_embedding",
            }  # fmt: skip
            assert added_since <= set(columns(connection, "moments"))
            row = connection.execute("SELECT channel_slug, reason, score, clip_path, rating FROM moments").fetchone()
            assert tuple(row) == ("some_channel", "laugh", 5.0, None, None)
        finally:
            connection.close()

    def test_the_short_lived_feedback_column_becomes_the_rating(self):
        legacy_database(
            self.OLD_MOMENTS.format(extra=", feedback TEXT"),
            self.OLD_MOMENT_ROW.format(columns=", feedback", values=", '4'"),
        )

        connection = db.get_connection()
        try:
            assert "feedback" not in columns(connection, "moments")
            assert columns(connection, "moments")["rating"] == "INTEGER"
            rating, kind = connection.execute("SELECT rating, typeof(rating) FROM moments").fetchone()
            assert (rating, kind) == (4, "integer")
        finally:
            connection.close()

    def test_ratings_once_stored_as_text_become_numbers(self):
        legacy_database(
            self.OLD_MOMENTS.format(extra=", rating TEXT"),
            self.OLD_MOMENT_ROW.format(columns=", rating", values=", '5'"),
            self.OLD_MOMENT_ROW.format(columns=", rating", values=", ''"),
            self.OLD_MOMENT_ROW.format(columns=", rating", values=", NULL"),
        )

        connection = db.get_connection()
        try:
            stored = connection.execute("SELECT rating, typeof(rating) FROM moments ORDER BY id").fetchall()
            assert stored == [(5, "integer"), (None, "null"), (None, "null")]
        finally:
            connection.close()

    def test_a_half_finished_rating_conversion_is_completed(self):
        # A crash between adding the temporary column and dropping the old one.
        legacy_database(
            self.OLD_MOMENTS.format(extra=", rating TEXT, rating_int INTEGER"),
            self.OLD_MOMENT_ROW.format(columns=", rating", values=", '2'"),
        )

        connection = db.get_connection()
        try:
            assert "rating_int" not in columns(connection, "moments")
            assert connection.execute("SELECT rating, typeof(rating) FROM moments").fetchone() == (2, "integer")
        finally:
            connection.close()

    def test_the_pre_detection_moments_table_is_replaced(self):
        # Before the heuristic existed the table had no `reason` and only
        # ever held throwaway rows.
        legacy_database(
            "CREATE TABLE moments (id INTEGER PRIMARY KEY, channel_slug TEXT, detected_at TEXT)",
            "INSERT INTO moments (channel_slug, detected_at) VALUES ('some_channel', '2026-01-01')",
        )

        connection = db.get_connection()
        try:
            assert "reason" in columns(connection, "moments")
            assert connection.execute("SELECT COUNT(*) FROM moments").fetchone()[0] == 0
        finally:
            connection.close()

    def test_keywords_stored_without_a_weight_get_the_neutral_one(self):
        legacy_database(
            "CREATE TABLE channel_keywords (broadcaster_user_id INTEGER NOT NULL, keyword TEXT NOT NULL, "
            "PRIMARY KEY (broadcaster_user_id, keyword))",
            "INSERT INTO channel_keywords VALUES (1, 'kekw')",
        )

        connection = db.get_connection()
        try:
            assert db.get_channel_keywords(connection, 1) == {"kekw": 1.0}
        finally:
            connection.close()

    def test_channels_watched_before_the_tracking_switch_existed_stay_tracked(self):
        legacy_database(
            "CREATE TABLE streamers (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "broadcaster_user_id INTEGER UNIQUE NOT NULL, "
            "slug TEXT UNIQUE NOT NULL, added_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))",
            "INSERT INTO streamers (broadcaster_user_id, slug) VALUES (7, 'old_channel')",
        )

        connection = db.get_connection()
        try:
            assert db.get_streamer_tracking_enabled(connection, 7) is True
            assert [row["slug"] for row in db.get_streamers(connection)] == ["old_channel"]
        finally:
            connection.close()


class TestStreamers:
    def test_added_channels_are_listed_in_the_order_they_were_added(self, conn):
        for user_id, slug in [(3, "third_id_first_added"), (1, "first_id_second_added"), (2, "added_last")]:
            db.add_streamer(conn, user_id, slug)
            conn.execute(
                "UPDATE streamers SET added_at = ? WHERE slug = ?", (f"2026-01-0{len(db.get_streamers(conn))}", slug)
            )
            conn.commit()

        assert [row["slug"] for row in db.get_streamers(conn)] == [
            "third_id_first_added",
            "first_id_second_added",
            "added_last",
        ]

    def test_adding_the_same_channel_twice_keeps_one_row(self, conn):
        db.add_streamer(conn, 1, "some_channel")
        db.add_streamer(conn, 1, "some_channel")

        assert len(db.get_streamers(conn)) == 1

    def test_lookup_by_slug(self, conn):
        db.add_streamer(conn, 42, "some_channel")

        row = db.get_streamer_by_slug(conn, "some_channel")

        assert (row["broadcaster_user_id"], row["tracking_enabled"]) == (42, 1)
        assert db.get_streamer_by_slug(conn, "nobody") is None

    def test_a_new_channel_is_tracked(self, conn):
        db.add_streamer(conn, 1, "some_channel")

        assert db.get_streamer_tracking_enabled(conn, 1) is True

    def test_tracking_can_be_paused_per_channel(self, conn):
        db.add_streamer(conn, 1, "paused")
        db.add_streamer(conn, 2, "tracked")

        db.set_streamer_tracking(conn, 1, False)

        assert db.get_streamer_tracking_enabled(conn, 1) is False
        assert db.get_streamer_tracking_enabled(conn, 2) is True
        db.set_streamer_tracking(conn, 1, True)
        assert db.get_streamer_tracking_enabled(conn, 1) is True

    def test_a_broadcaster_that_is_not_on_the_watchlist_counts_as_tracked(self, conn):
        # Messages for an unknown channel are processed, not silently dropped.
        assert db.get_streamer_tracking_enabled(conn, 999) is True


class TestChannelKeywords:
    def test_keywords_are_stored_lowercased_with_their_weights(self, conn):
        db.replace_channel_keywords(conn, 1, {"KEKW": 3.5, "Sadge": 1.0})

        assert db.get_channel_keywords(conn, 1) == {"kekw": 3.5, "sadge": 1.0}

    def test_replacing_drops_the_previous_set(self, conn):
        db.replace_channel_keywords(conn, 1, {"kekw": 3.5, "sadge": 1.0})

        db.replace_channel_keywords(conn, 1, {"omegalul": 3.5})

        assert db.get_channel_keywords(conn, 1) == {"omegalul": 3.5}

    def test_each_channel_has_its_own_keywords(self, conn):
        db.replace_channel_keywords(conn, 1, {"kekw": 3.5})
        db.replace_channel_keywords(conn, 2, {"sadge": 1.0})

        assert db.get_channel_keywords(conn, 1) == {"kekw": 3.5}
        assert db.get_channel_keywords(conn, 2) == {"sadge": 1.0}
        assert db.get_channel_keywords(conn, 3) == {}

    def test_names_differing_only_in_case_collapse_into_one(self, conn):
        db.replace_channel_keywords(conn, 1, {"KEKW": 3.5, "kekw": 3.5})

        assert db.get_channel_keywords(conn, 1) == {"kekw": 3.5}


class TestChatMessages:
    def test_storing_reports_whether_the_message_was_new(self, conn):
        def store(message_id: str) -> bool:
            return db.insert_chat_message(
                conn,
                message_id=message_id,
                broadcaster_user_id=1,
                channel_slug="some_channel",
                sender_username="alice",
                content="hello",
                emotes_json="[]",
                created_at=T0.isoformat(),
                received_at=T0.isoformat(),
            )

        assert store("m-1") is True
        assert store("m-1") is False
        assert store("m-2") is True

    def test_a_message_is_stored_once_however_often_it_is_delivered(self, conn):
        store_chat(conn, "some_channel", "alice", "first delivery", T0, message_id="m-1")
        store_chat(conn, "some_channel", "alice", "retried delivery", T0, message_id="m-1")

        assert conn.execute("SELECT content FROM chat_messages").fetchall() == [("first delivery",)]

    def test_the_snippet_is_the_chat_inside_the_window_in_order(self, conn):
        store_chat(conn, "some_channel", "carol", "third", T0 + timedelta(seconds=8))
        store_chat(conn, "some_channel", "alice", "first", T0 + timedelta(seconds=1))
        store_chat(conn, "some_channel", "bob", "second", T0 + timedelta(seconds=5))

        snippet = db.get_chat_snippet(conn, "some_channel", T0.isoformat(), (T0 + timedelta(seconds=10)).isoformat())

        assert [(row["sender_username"], row["content"]) for row in snippet] == [
            ("alice", "first"), ("bob", "second"), ("carol", "third"),
        ]  # fmt: skip

    def test_the_window_is_padded_a_little_on_both_sides(self, conn):
        # Webhook delivery jitter puts genuine burst messages just outside.
        padding = db.SNIPPET_PADDING_SECONDS
        assert padding >= 1
        store_chat(conn, "some_channel", "too_early", "x", T0 - timedelta(seconds=padding + 1))
        store_chat(conn, "some_channel", "just_before", "x", T0 - timedelta(seconds=padding / 2))
        store_chat(conn, "some_channel", "just_after", "x", T0 + timedelta(seconds=10 + padding / 2))
        store_chat(conn, "some_channel", "too_late", "x", T0 + timedelta(seconds=10 + padding + 1))

        snippet = db.get_chat_snippet(conn, "some_channel", T0.isoformat(), (T0 + timedelta(seconds=10)).isoformat())

        assert [row["sender_username"] for row in snippet] == ["just_before", "just_after"]

    def test_other_channels_are_left_out(self, conn):
        store_chat(conn, "some_channel", "alice", "here", T0)
        store_chat(conn, "another_channel", "bob", "elsewhere", T0)

        snippet = db.get_chat_snippet(conn, "some_channel", T0.isoformat(), T0.isoformat())

        assert [row["sender_username"] for row in snippet] == ["alice"]

    def test_a_busy_window_is_cut_to_the_first_messages(self, conn):
        for number in range(30):
            store_chat(conn, "some_channel", f"user{number:02d}", "x", T0 + timedelta(milliseconds=100 * number))

        snippet = db.get_chat_snippet(conn, "some_channel", T0.isoformat(), (T0 + timedelta(seconds=10)).isoformat())
        short = db.get_chat_snippet(
            conn, "some_channel", T0.isoformat(), (T0 + timedelta(seconds=10)).isoformat(), limit=3
        )

        assert len(snippet) == 20
        assert [row["sender_username"] for row in short] == ["user00", "user01", "user02"]


class TestMoments:
    def test_inserting_returns_consecutive_ids(self, conn):
        assert [store_moment(conn), store_moment(conn), store_moment(conn)] == [1, 2, 3]

    def test_a_new_moment_has_no_review_data_yet(self, conn):
        store_moment(conn)

        (row,) = db.get_recent_moments(conn)

        assert row["reason"] == "laugh" and row["score"] == 7.5
        for column in (
            "clip_path",
            "rating",
            "notes",
            "transcript",
            "audio_events",
            "sound_events",
            "stream_type",
            "moment_type",
        ):
            assert row[column] is None

    def test_recent_moments_come_newest_first(self, conn):
        for minute, reason in [(1, "first"), (3, "third"), (2, "second")]:
            moment_id = store_moment(conn, reason=reason)
            conn.execute("UPDATE moments SET detected_at = ? WHERE id = ?", (f"2026-01-01T12:0{minute}:00Z", moment_id))
        conn.commit()

        assert [row["reason"] for row in db.get_recent_moments(conn)] == ["third", "second", "first"]

    def test_paging_and_channel_filter(self, conn):
        for number in range(6):
            moment_id = store_moment(conn, channel="channel_a" if number % 2 else "channel_b", reason=f"m{number}")
            conn.execute("UPDATE moments SET detected_at = ? WHERE id = ?", (f"2026-01-01T12:00:0{number}Z", moment_id))
        conn.commit()

        assert [row["reason"] for row in db.get_recent_moments(conn, limit=2)] == ["m5", "m4"]
        assert [row["reason"] for row in db.get_recent_moments(conn, limit=2, offset=2)] == ["m3", "m2"]
        assert [row["reason"] for row in db.get_recent_moments(conn, channel_slug="channel_a")] == ["m5", "m3", "m1"]
        assert [row["reason"] for row in db.get_recent_moments(conn, limit=1, offset=1, channel_slug="channel_b")] == [
            "m2"
        ]

    def test_counts(self, conn):
        store_moment(conn, channel="channel_a")
        with_clip = store_moment(conn, channel="channel_a")
        store_moment(conn, channel="channel_b")
        db.update_moment_clip_path(conn, with_clip, "channel_a/moment_2.mp4")

        assert db.count_moments(conn) == 3
        assert db.count_moments(conn, channel_slug="channel_a") == 2
        assert db.count_moments(conn, channel_slug="nobody") == 0
        assert db.count_moments_with_clip(conn) == 1

    def test_channels_with_moments_are_listed_alphabetically_without_duplicates(self, conn):
        for channel in ["zeta", "alpha", "zeta", "midway"]:
            store_moment(conn, channel=channel)

        assert db.get_moment_channels(conn) == ["alpha", "midway", "zeta"]

    @pytest.mark.parametrize(
        ("update", "column", "value"),
        [
            (db.update_moment_clip_path, "clip_path", "some_channel/moment_1.mp4"),
            (db.update_moment_transcript, "transcript", "to je konec"),
            (db.update_moment_audio_events, "audio_events", "cs, HAPPY"),
            (db.update_moment_frame_embedding, "frame_embedding", b"\x00\x01\x02"),
            (db.update_moment_sound_events, "sound_events", "Laughter:0.80"),
            (db.update_moment_sound_embedding, "sound_embedding", b"\x09\x08"),
            (db.update_moment_stream_type, "stream_type", "gaming"),
            (db.update_moment_type, "moment_type", "fail"),
            (db.update_moment_rating, "rating", 4),
            (db.update_moment_notes, "notes", "cut starts too late"),
            (db.update_moment_window_end, "window_end", "2026-01-01T12:00:30+00:00"),
        ],
    )
    def test_each_update_touches_its_own_column_of_its_own_moment(self, conn, update, column, value):
        target, bystander = store_moment(conn), store_moment(conn)
        before = dict(
            zip(
                columns(conn, "moments"),
                conn.execute("SELECT * FROM moments WHERE id = ?", (bystander,)).fetchone(),
                strict=True,
            )
        )

        update(conn, target, value)

        assert conn.execute(f"SELECT {column} FROM moments WHERE id = ?", (target,)).fetchone()[0] == value
        after = dict(
            zip(
                columns(conn, "moments"),
                conn.execute("SELECT * FROM moments WHERE id = ?", (bystander,)).fetchone(),
                strict=True,
            )
        )
        assert after == before

    @pytest.mark.parametrize(
        "update", [db.update_moment_rating, db.update_moment_notes, db.update_moment_stream_type, db.update_moment_type]
    )
    def test_review_fields_can_be_cleared_again(self, conn, update):
        moment_id = store_moment(conn)
        db.update_moment_rating(conn, moment_id, 3)
        db.update_moment_notes(conn, moment_id, "note")
        db.update_moment_stream_type(conn, moment_id, "irl")
        db.update_moment_type(conn, moment_id, "funny")

        update(conn, moment_id, None)

        row = db.get_recent_moments(conn)[0]
        cleared = [name for name in ("rating", "notes", "stream_type", "moment_type") if row[name] is None]
        assert len(cleared) == 1

    def test_backfill_candidates_are_clips_missing_any_analysis_result(self, conn):
        no_clip = store_moment(conn)
        nothing_yet = store_moment(conn)
        complete = store_moment(conn)
        missing_one = store_moment(conn)
        for moment_id in (nothing_yet, complete, missing_one):
            db.update_moment_clip_path(conn, moment_id, f"some_channel/moment_{moment_id}.mp4")
        for moment_id in (complete, missing_one):
            db.update_moment_transcript(conn, moment_id, "text")
            db.update_moment_audio_events(conn, moment_id, "tags")
            db.update_moment_frame_embedding(conn, moment_id, b"\x00")
            db.update_moment_sound_events(conn, moment_id, "tags")
        db.update_moment_sound_embedding(conn, complete, b"\x00")

        candidates = [row["id"] for row in db.get_moments_missing_taste_data(conn)]

        assert candidates == [nothing_yet, missing_one]
        assert no_clip not in candidates

    def test_an_empty_transcript_counts_as_done(self, conn):
        # "" means "analysed, nothing was said" - not "still to do".
        moment_id = store_moment(conn)
        db.update_moment_clip_path(conn, moment_id, "some_channel/moment_1.mp4")
        db.update_moment_transcript(conn, moment_id, "")
        db.update_moment_audio_events(conn, moment_id, "")
        db.update_moment_frame_embedding(conn, moment_id, b"\x00")
        db.update_moment_sound_events(conn, moment_id, "")
        db.update_moment_sound_embedding(conn, moment_id, b"\x00")

        assert db.get_moments_missing_taste_data(conn) == []


class TestFlags:
    def test_a_flag_never_set_reads_as_its_default(self, conn):
        assert db.get_flag(conn, "watching_enabled", default=True) is True
        assert db.get_flag(conn, "watching_enabled", default=False) is False

    def test_a_stored_flag_wins_over_the_default(self, conn):
        db.set_flag(conn, "watching_enabled", False)
        assert db.get_flag(conn, "watching_enabled", default=True) is False

        db.set_flag(conn, "watching_enabled", True)
        assert db.get_flag(conn, "watching_enabled", default=False) is True

    def test_flags_are_independent_and_persist_across_connections(self, conn):
        db.set_flag(conn, "transcript_enabled", True)
        db.set_flag(conn, "frames_enabled", False)

        other = db.get_connection()
        try:
            assert db.get_flag(other, "transcript_enabled", default=False) is True
            assert db.get_flag(other, "frames_enabled", default=True) is False
            assert db.get_flag(other, "never_set", default=True) is True
        finally:
            other.close()

    def test_setting_a_flag_twice_keeps_one_row(self, conn):
        db.set_flag(conn, "watching_enabled", True)
        db.set_flag(conn, "watching_enabled", False)

        assert conn.execute("SELECT key, value FROM app_settings").fetchall() == [("watching_enabled", "0")]
