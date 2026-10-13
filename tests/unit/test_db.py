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


def minutes(count: float) -> timedelta:
    return timedelta(minutes=count)


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
    connection,
    channel: str,
    sender: str,
    content: str,
    received_at: datetime,
    message_id: str | None = None,
    broadcaster_user_id: int = 1,
):
    db.insert_chat_message(
        connection,
        message_id=message_id or f"{channel}-{sender}-{received_at.timestamp()}",
        broadcaster_user_id=broadcaster_user_id,
        channel_slug=channel,
        sender_username=sender,
        content=content,
        emotes_json="[]",
        created_at=received_at.isoformat(),
        received_at=received_at.isoformat(),
    )


class TestSchema:
    def test_a_fresh_database_gets_every_table(self, conn):
        assert tables(conn) >= {
            "streamers",
            "chat_messages",
            "moments",
            "channel_keywords",
            "channel_emotes",
            "chat_identities",
            "app_settings",
            "tuning",
            "stream_recordings",
        }

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
            "sound_events", "sound_embedding", "clip_start", "clip_duration", "title",
            "context_before", "context_after",
        }  # fmt: skip

    def test_chat_can_be_found_by_channel_and_arrival_time_without_reading_all_of_it(self, conn):
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT content FROM chat_messages"
            " WHERE channel_slug = ? AND received_at BETWEEN ? AND ?",
            ("some_channel", "a", "b"),
        ).fetchall()

        assert "idx_chat_messages_channel_received" in plan[0][-1]

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
                "clip_start", "clip_duration", "context_before", "context_after",
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

    def test_channels_watched_before_settings_of_their_own_existed_go_by_the_ones_for_all(self):
        legacy_database(
            "CREATE TABLE streamers (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "broadcaster_user_id INTEGER UNIQUE NOT NULL, slug TEXT UNIQUE NOT NULL, "
            "added_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
            "tracking_enabled INTEGER NOT NULL DEFAULT 1)",
            "INSERT INTO streamers (broadcaster_user_id, slug, tracking_enabled) VALUES (7, 'old_channel', 0)",
        )

        connection = db.get_connection()
        try:
            (row,) = db.get_streamers(connection)
            assert (row["slug"], row["tracking_enabled"], row["own_tuning"]) == ("old_channel", 0, 0)
            assert db.get_tuning_in_force(connection) == {}
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

    def test_a_new_channel_goes_by_the_settings_for_all_channels(self, conn):
        db.add_streamer(conn, 1, "some_channel")

        assert db.get_streamer_by_slug(conn, "some_channel")["own_tuning"] == 0
        assert [row["own_tuning"] for row in db.get_streamers(conn)] == [0]

    def test_a_channel_can_be_switched_to_settings_of_its_own_and_back(self, conn):
        db.add_streamer(conn, 1, "its_own")
        db.add_streamer(conn, 2, "like_the_rest")

        db.set_streamer_own_tuning(conn, 1, True)

        assert {row["slug"]: row["own_tuning"] for row in db.get_streamers(conn)} == {"its_own": 1, "like_the_rest": 0}
        db.set_streamer_own_tuning(conn, 1, False)
        assert db.get_streamer_by_slug(conn, "its_own")["own_tuning"] == 0


class TestTuning:
    """What was set on the Settings page, as it is stored: text by name,
    under the channel it was set for or under the owner that stands for all
    of them. What the text means is tuning.py's business."""

    def test_nothing_is_set_to_begin_with(self, conn):
        assert db.get_tuning(conn) == {}
        assert db.get_tuning(conn, 7) == {}
        assert db.get_tuning_in_force(conn) == {}

    def test_values_are_kept_by_owner(self, conn):
        db.replace_tuning(conn, db.TUNING_FOR_ALL, {"reacting_share": "0.2", "laugh_signal": "false"})
        db.replace_tuning(conn, 7, {"reacting_share": "0.4"})

        assert db.get_tuning(conn) == {"reacting_share": "0.2", "laugh_signal": "false"}
        assert db.get_tuning(conn, db.TUNING_FOR_ALL) == db.get_tuning(conn)
        assert db.get_tuning(conn, 7) == {"reacting_share": "0.4"}
        assert db.get_tuning(conn, 8) == {}

    def test_storing_an_owners_values_replaces_all_it_had(self, conn):
        db.replace_tuning(conn, 7, {"reacting_share": "0.4", "lull_seconds": "40"})
        db.replace_tuning(conn, 8, {"lull_seconds": "50"})

        db.replace_tuning(conn, 7, {"reacting_least": "5"})

        assert db.get_tuning(conn, 7) == {"reacting_least": "5"}
        assert db.get_tuning(conn, 8) == {"lull_seconds": "50"}
        db.replace_tuning(conn, 7, {})
        assert db.get_tuning(conn, 7) == {}

    def test_the_owner_that_stands_for_all_channels_is_no_channel(self, conn):
        # Like the owner of 7TV's global emotes: Kick's user ids start above it.
        assert db.TUNING_FOR_ALL == 0

    def test_what_is_in_force_is_what_holds_for_all_and_for_the_channels_going_by_their_own(self, conn):
        db.add_streamer(conn, 7, "its_own")
        db.add_streamer(conn, 8, "like_the_rest")
        db.set_streamer_own_tuning(conn, 7, True)
        db.replace_tuning(conn, db.TUNING_FOR_ALL, {"reacting_share": "0.2"})
        db.replace_tuning(conn, 7, {"reacting_share": "0.4"})
        db.replace_tuning(conn, 8, {"reacting_share": "0.6"})  # kept, but not gone by
        db.replace_tuning(conn, 9, {"reacting_share": "0.8"})  # a channel no longer watched

        assert db.get_tuning_in_force(conn) == {
            db.TUNING_FOR_ALL: {"reacting_share": "0.2"},
            7: {"reacting_share": "0.4"},
        }

    def test_what_is_a_channels_alone_is_in_force_for_any_watched_channel(self, conn):
        db.add_streamer(conn, 7, "its_own")
        db.add_streamer(conn, 8, "like_the_rest")
        db.set_streamer_own_tuning(conn, 7, True)
        db.replace_tuning(conn, db.TUNING_FOR_ALL, {"reacting_share": "0.2"})
        db.replace_tuning(conn, 7, {"reacting_share": "0.4", "clip_commands": '["!seven"]'})
        db.replace_tuning(conn, 8, {"reacting_share": "0.6", "clip_commands": '["!eight"]', "lull_seconds": "40"})
        db.replace_tuning(conn, 9, {"clip_commands": '["!nine"]'})  # a channel no longer watched

        assert db.get_tuning_in_force(conn, ["clip_commands", "lull_seconds"]) == {
            db.TUNING_FOR_ALL: {"reacting_share": "0.2"},
            7: {"reacting_share": "0.4", "clip_commands": '["!seven"]'},
            8: {"clip_commands": '["!eight"]', "lull_seconds": "40"},
        }

    def test_values_persist_across_connections(self, conn):
        db.replace_tuning(conn, 7, {"reacting_share": "0.4"})

        other = db.get_connection()
        try:
            assert db.get_tuning(other, 7) == {"reacting_share": "0.4"}
        finally:
            other.close()


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


class TestChannelEmotes:
    def test_emotes_are_stored_by_name_with_their_id_and_size(self, conn):
        db.replace_channel_emotes(conn, 1, {"KEKW": ("ID1", 32, 32), "WideHard": ("ID2", 96, 32)})

        assert db.get_channel_emotes(conn, 1) == {"KEKW": ("ID1", 32, 32), "WideHard": ("ID2", 96, 32)}

    def test_names_differing_only_in_case_are_different_emotes(self, conn):
        # Unlike the detector's keywords: 7TV tells "KEKW" from "kekw".
        db.replace_channel_emotes(conn, 1, {"KEKW": ("ID1", 32, 32), "kekw": ("ID2", 32, 32)})

        assert db.get_channel_emotes(conn, 1) == {"KEKW": ("ID1", 32, 32), "kekw": ("ID2", 32, 32)}

    def test_replacing_drops_the_previous_set(self, conn):
        db.replace_channel_emotes(conn, 1, {"KEKW": ("ID1", 32, 32), "Sadge": ("ID2", 32, 32)})

        db.replace_channel_emotes(conn, 1, {"KEKW": ("ID3", 32, 32)})

        assert db.get_channel_emotes(conn, 1) == {"KEKW": ("ID3", 32, 32)}

    def test_each_channel_has_its_own_emotes(self, conn):
        db.replace_channel_emotes(conn, 1, {"KEKW": ("ID1", 32, 32)})
        db.replace_channel_emotes(conn, 2, {"KEKW": ("ID2", 32, 32)})

        assert db.get_channel_emotes(conn, 1) == {"KEKW": ("ID1", 32, 32)}
        assert db.get_channel_emotes(conn, 2) == {"KEKW": ("ID2", 32, 32)}
        assert db.get_channel_emotes(conn, 3) == {}

    def test_chat_can_use_the_global_emotes_and_the_channels_own(self, conn):
        db.replace_channel_emotes(conn, db.GLOBAL_EMOTES_OWNER, {"EZ": ("G1", 32, 32), "Clap": ("G2", 32, 32)})
        db.replace_channel_emotes(conn, 1, {"KEKW": ("ID1", 32, 32)})

        assert db.get_chat_emotes(conn, 1) == {"EZ": ("G1", 32, 32), "Clap": ("G2", 32, 32), "KEKW": ("ID1", 32, 32)}
        assert db.get_chat_emotes(conn, 2) == {"EZ": ("G1", 32, 32), "Clap": ("G2", 32, 32)}
        assert db.get_channel_emotes(conn, 1) == {"KEKW": ("ID1", 32, 32)}

    def test_a_channels_own_emote_wins_over_a_global_one_of_the_same_name(self, conn):
        db.replace_channel_emotes(conn, db.GLOBAL_EMOTES_OWNER, {"EZ": ("GLOBAL", 32, 32)})
        db.replace_channel_emotes(conn, 1, {"EZ": ("OWN", 64, 32)})

        assert db.get_chat_emotes(conn, 1) == {"EZ": ("OWN", 64, 32)}
        assert db.get_chat_emotes(conn, 2) == {"EZ": ("GLOBAL", 32, 32)}

    def test_a_database_from_before_emote_pictures_gains_the_table(self):
        connection = db.get_connection()
        connection.execute("DROP TABLE channel_emotes")
        connection.commit()
        connection.close()

        connection = db.get_connection()
        try:
            assert db.get_channel_emotes(connection, 1) == {}
        finally:
            connection.close()


class TestChannelsRecordedAlways:
    def test_a_channel_is_not_recorded_always_to_begin_with(self, conn):
        db.add_streamer(conn, 1, "some_channel")

        assert [row["always_record"] for row in db.get_streamers(conn)] == [0]
        assert db.get_streamer_by_slug(conn, "some_channel")["always_record"] == 0

    def test_it_is_set_for_one_channel_and_can_be_set_back(self, conn):
        db.add_streamer(conn, 1, "always")
        db.add_streamer(conn, 2, "on_request")

        db.set_streamer_always_record(conn, 1, True)

        assert {row["slug"]: row["always_record"] for row in db.get_streamers(conn)} == {"always": 1, "on_request": 0}
        assert db.get_streamer_by_slug(conn, "always")["always_record"] == 1

        db.set_streamer_always_record(conn, 1, False)

        assert db.get_streamer_by_slug(conn, "always")["always_record"] == 0

    def test_a_watchlist_from_before_there_was_such_a_thing_gets_it_switched_off(self, tmp_path, monkeypatch):
        path = tmp_path / "older.db"
        older = sqlite3.connect(path)
        older.execute(
            "CREATE TABLE streamers (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "broadcaster_user_id INTEGER UNIQUE NOT NULL, slug TEXT UNIQUE NOT NULL, "
            "added_at TEXT NOT NULL DEFAULT '2026-01-01', tracking_enabled INTEGER NOT NULL DEFAULT 1, "
            "own_tuning INTEGER NOT NULL DEFAULT 0)"
        )
        older.execute(
            "INSERT INTO streamers (broadcaster_user_id, slug, tracking_enabled) VALUES (7, 'old_channel', 0)"
        )
        older.commit()
        older.close()
        monkeypatch.setattr(db, "DB_PATH", path)

        connection = db.get_connection()
        try:
            (row,) = db.get_streamers(connection)
        finally:
            connection.close()

        assert (row["slug"], row["tracking_enabled"], row["always_record"]) == ("old_channel", 0, 0)


class TestChatIdentities:
    MOD = '[{"text":"Moderator","type":"moderator"}]'

    def test_a_chatter_is_stored_with_their_colour_and_badges(self, conn):
        db.set_chat_identity(conn, 1, "alice", "#ff5733", self.MOD)

        assert db.get_chat_identities(conn, 1, ["alice"]) == {"alice": ("#ff5733", self.MOD)}

    def test_storing_again_replaces_what_was_known(self, conn):
        db.set_chat_identity(conn, 1, "alice", "#ff5733", self.MOD)

        db.set_chat_identity(conn, 1, "alice", None, "[]")

        assert db.get_chat_identities(conn, 1, ["alice"]) == {"alice": (None, "[]")}
        assert conn.execute("SELECT COUNT(*) FROM chat_identities").fetchone()[0] == 1

    def test_someone_is_who_they_are_in_each_channel_separately(self, conn):
        db.set_chat_identity(conn, 1, "alice", "#ff5733", self.MOD)
        db.set_chat_identity(conn, 2, "alice", "#00ff00", "[]")

        assert db.get_chat_identities(conn, 1, ["alice"]) == {"alice": ("#ff5733", self.MOD)}
        assert db.get_chat_identities(conn, 2, ["alice"]) == {"alice": ("#00ff00", "[]")}
        assert db.get_chat_identities(conn, 3, ["alice"]) == {}

    def test_only_those_asked_for_and_known_come_back(self, conn):
        db.set_chat_identity(conn, 1, "alice", "#ff5733", "[]")
        db.set_chat_identity(conn, 1, "bob", "#00ff00", "[]")

        assert db.get_chat_identities(conn, 1, ["bob", "carol", "bob"]) == {"bob": ("#00ff00", "[]")}
        assert db.get_chat_identities(conn, 1, []) == {}

    def test_more_chatters_can_be_asked_for_than_one_statement_takes(self, conn):
        names = [f"viewer{number}" for number in range(db._IDENTITY_LOOKUP_CHUNK * 2 + 7)]
        for name in names[::3]:
            db.set_chat_identity(conn, 1, name, "#ff5733", "[]")

        assert set(db.get_chat_identities(conn, 1, names)) == set(names[::3])


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

    def test_a_stretch_of_chat_comes_back_in_the_order_it_arrived(self, conn):
        store_chat(conn, "some_channel", "carol", "third", T0 + timedelta(seconds=8))
        store_chat(conn, "some_channel", "alice", "first", T0 + timedelta(seconds=1))
        store_chat(conn, "some_channel", "bob", "second", T0 + timedelta(seconds=5))

        chat = db.get_chat_between(conn, "some_channel", T0, T0 + timedelta(seconds=10))

        assert [(row["sender_username"], row["content"]) for row in chat] == [
            ("alice", "first"), ("bob", "second"), ("carol", "third"),
        ]  # fmt: skip
        assert [row["received_at"] for row in chat] == sorted(row["received_at"] for row in chat)

    def test_the_stretch_ends_where_it_was_asked_to(self, conn):
        store_chat(conn, "some_channel", "too_early", "x", T0 - timedelta(milliseconds=1))
        store_chat(conn, "some_channel", "first", "x", T0)
        store_chat(conn, "some_channel", "last", "x", T0 + timedelta(seconds=10))
        store_chat(conn, "some_channel", "too_late", "x", T0 + timedelta(seconds=10, milliseconds=1))

        chat = db.get_chat_between(conn, "some_channel", T0, T0 + timedelta(seconds=10))

        assert [row["sender_username"] for row in chat] == ["first", "last"]

    def test_the_stretch_can_be_given_in_any_time_zone(self, conn):
        store_chat(conn, "some_channel", "alice", "x", T0 + timedelta(seconds=5))
        prague = timezone(timedelta(hours=1))

        chat = db.get_chat_between(conn, "some_channel", T0.astimezone(prague), (T0 + minutes(1)).astimezone(prague))

        assert [row["sender_username"] for row in chat] == ["alice"]

    def test_messages_stored_in_the_older_timestamp_form_are_found_too(self, conn):
        # Before received_at was written by the service it was SQLite's own
        # default: milliseconds and a "Z" instead of microseconds and "+00:00".
        store_chat(conn, "some_channel", "new_form", "x", T0 + timedelta(seconds=5))
        store_chat(conn, "some_channel", "old_form", "x", T0, message_id="old")
        conn.execute("UPDATE chat_messages SET received_at = '2026-03-01T20:00:03.250Z' WHERE message_id = 'old'")
        conn.execute(
            "INSERT INTO chat_messages"
            " (message_id, broadcaster_user_id, channel_slug, sender_username, content, received_at)"
            " VALUES ('outside', 1, 'some_channel', 'old_form_outside', 'x', '2026-03-01T20:00:10.250Z')"
        )
        conn.commit()

        chat = db.get_chat_between(conn, "some_channel", T0, T0 + timedelta(seconds=10))

        assert [row["sender_username"] for row in chat] == ["old_form", "new_form"]

    def test_other_channels_are_left_out(self, conn):
        store_chat(conn, "some_channel", "alice", "here", T0)
        store_chat(conn, "another_channel", "bob", "elsewhere", T0)

        chat = db.get_chat_between(conn, "some_channel", T0, T0)

        assert [row["sender_username"] for row in chat] == ["alice"]

    def test_a_busy_stretch_is_cut_to_its_first_messages(self, conn):
        for number in range(30):
            store_chat(conn, "some_channel", f"user{number:02d}", "x", T0 + timedelta(milliseconds=100 * number))

        chat = db.get_chat_between(conn, "some_channel", T0, T0 + timedelta(seconds=10), limit=3)

        assert [row["sender_username"] for row in chat] == ["user00", "user01", "user02"]


class TestChatOfAChannelToldByItsId:
    """Kick sends a channel's chat under its own spelling of the channel's
    name, which need not be the watchlist's - the id is the same in both."""

    def test_a_stretch_comes_back_whatever_name_it_was_stored_under(self, conn):
        store_chat(conn, "some_channel", "alice", "first", T0 + timedelta(seconds=1))
        store_chat(conn, "renamed_channel", "bob", "second", T0 + timedelta(seconds=5))

        chat = db.get_channel_chat_between(conn, 1, T0, T0 + timedelta(seconds=10))

        assert [(row["sender_username"], row["content"]) for row in chat] == [("alice", "first"), ("bob", "second")]

    def test_the_stretch_ends_where_it_was_asked_to(self, conn):
        store_chat(conn, "some_channel", "too_early", "x", T0 - timedelta(milliseconds=1))
        store_chat(conn, "some_channel", "first", "x", T0)
        store_chat(conn, "some_channel", "last", "x", T0 + timedelta(seconds=10))
        store_chat(conn, "some_channel", "too_late", "x", T0 + timedelta(seconds=10, milliseconds=1))

        chat = db.get_channel_chat_between(conn, 1, T0, T0 + timedelta(seconds=10))

        assert [row["sender_username"] for row in chat] == ["first", "last"]

    def test_other_channels_are_left_out(self, conn):
        store_chat(conn, "some_channel", "alice", "here", T0)
        store_chat(conn, "another_channel", "bob", "elsewhere", T0, broadcaster_user_id=2)

        chat = db.get_channel_chat_between(conn, 1, T0, T0)

        assert [row["sender_username"] for row in chat] == ["alice"]

    def test_a_busy_stretch_is_cut_to_its_first_messages(self, conn):
        for number in range(30):
            store_chat(conn, "some_channel", f"user{number:02d}", "x", T0 + timedelta(milliseconds=100 * number))

        chat = db.get_channel_chat_between(conn, 1, T0, T0 + timedelta(seconds=10), limit=3)

        assert [row["sender_username"] for row in chat] == ["user00", "user01", "user02"]

    def test_the_stretch_is_found_through_an_index(self, conn):
        # A recording's chat is asked for every few minutes of playing it.
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT content FROM chat_messages"
            " WHERE broadcaster_user_id = 1 AND received_at BETWEEN 'a' AND 'b' ORDER BY received_at"
        ).fetchall()

        assert any("USING INDEX" in step[-1] for step in plan)

    def test_the_name_its_chat_goes_by_is_the_newest_messages(self, conn):
        store_chat(conn, "old_name", "alice", "x", T0)
        store_chat(conn, "some_channel", "bob", "x", T0 + minutes(5))
        store_chat(conn, "another_channel", "carol", "x", T0 + minutes(9), broadcaster_user_id=2)

        assert db.get_chat_name(conn, 1) == "some_channel"

    def test_a_channel_with_no_chat_has_no_such_name(self, conn):
        assert db.get_chat_name(conn, 1) is None

    def test_all_of_a_stretch_can_be_gone_through_one_message_at_a_time(self, conn):
        # For counting the chat of a stream of hours: no limit, and nothing
        # of a message but when it came, what it says and the row it is in.
        store_chat(conn, "some_channel", "too_early", "x", T0 - timedelta(milliseconds=1))
        for number in range(30):
            store_chat(conn, "some_channel", f"user{number:02d}", f"line {number}", T0 + timedelta(seconds=number))
        store_chat(conn, "renamed_channel", "bob", "under another name", T0 + timedelta(seconds=5, milliseconds=500))
        store_chat(conn, "another_channel", "carol", "elsewhere", T0 + timedelta(seconds=6), broadcaster_user_id=2)

        chat = db.iter_channel_chat_between(conn, 1, T0, T0 + timedelta(seconds=29))

        first = next(chat)
        assert (first["received_at"], first["content"]) == (T0.isoformat(), "line 0")
        assert set(first.keys()) == {"received_at", "content", "rowid"}
        rest = [row["content"] for row in chat]
        assert len(rest) == 30 and rest[5] == "under another name" and rest[-1] == "line 29"

    @pytest.mark.parametrize("chunk", [1, 3, 5, 10, 11])
    def test_it_is_gone_through_a_chunk_at_a_time_and_every_message_once(self, conn, monkeypatch, chunk):
        monkeypatch.setattr(db, "CHAT_CHUNK", chunk)
        for second in range(10):
            store_chat(conn, "some_channel", "alice", f"second {second}", T0 + timedelta(seconds=second))

        chat = db.iter_channel_chat_between(conn, 1, T0, T0 + timedelta(seconds=9))

        assert [row["content"] for row in chat] == [f"second {second}" for second in range(10)]

    def test_messages_that_arrived_at_the_same_instant_are_not_lost_between_two_chunks(self, conn, monkeypatch):
        monkeypatch.setattr(db, "CHAT_CHUNK", 2)
        for number in range(5):
            store_chat(conn, "some_channel", f"user{number}", f"line {number}", T0, message_id=f"m-{number}")
        store_chat(conn, "some_channel", "alice", "later", T0 + timedelta(seconds=1))

        chat = db.iter_channel_chat_between(conn, 1, T0, T0 + timedelta(seconds=1))

        assert [row["content"] for row in chat] == [*(f"line {number}" for number in range(5)), "later"]

    def test_no_statement_is_left_open_while_it_is_gone_through(self, conn, monkeypatch):
        # One left open would keep the database locked against the message
        # that arrives meanwhile.
        monkeypatch.setattr(db, "CHAT_CHUNK", 2)
        for second in range(5):
            store_chat(conn, "some_channel", "alice", f"second {second}", T0 + timedelta(seconds=second))
        writer = db.get_connection()
        writer.execute("PRAGMA busy_timeout = 0")

        try:
            for number, _row in enumerate(db.iter_channel_chat_between(conn, 1, T0, T0 + minutes(1))):
                store_chat(writer, "some_channel", "bob", "meanwhile", T0 + minutes(5) + timedelta(seconds=number))
        finally:
            writer.close()

        assert len(db.get_channel_chat_between(conn, 1, T0 + minutes(5), T0 + minutes(6))) == 5

    def test_a_message_that_arrives_meanwhile_moves_none_of_the_others(self, conn, monkeypatch):
        # Each chunk goes on from the last message of the one before, not
        # from how many were handed out - which a new message would shift.
        monkeypatch.setattr(db, "CHAT_CHUNK", 2)
        for second in range(0, 10, 2):
            store_chat(conn, "some_channel", "alice", f"second {second}", T0 + timedelta(seconds=second))

        handed_out = []
        for row in db.iter_channel_chat_between(conn, 1, T0, T0 + minutes(1)):
            handed_out.append(row["content"])
            if row["content"] == "second 2":
                store_chat(conn, "some_channel", "bob", "behind", T0 + timedelta(seconds=1))
                store_chat(conn, "some_channel", "bob", "ahead", T0 + timedelta(seconds=7))

        assert handed_out == ["second 0", "second 2", "second 4", "second 6", "ahead", "second 8"]

    def test_going_through_it_is_done_through_the_same_index_and_without_sorting(self, conn):
        asked = []
        conn.set_trace_callback(asked.append)
        list(db.iter_channel_chat_between(conn, 1, T0, T0 + minutes(1)))
        conn.set_trace_callback(None)
        (statement,) = asked

        plan = conn.execute(f"EXPLAIN QUERY PLAN {statement}").fetchall()

        assert any("USING INDEX idx_chat_messages_broadcaster_received" in step[-1] for step in plan)
        assert not any("TEMP B-TREE" in step[-1] for step in plan)


class TestMomentsOfAChannelToldByItsId:
    """When a channel's moments were, for what has the channel's name from
    the watchlist - a recording, on whose trace they are marked."""

    def window(self, conn, start: float, end: float, **overrides) -> int:
        return store_moment(
            conn,
            window_start=(T0 + timedelta(seconds=start)).isoformat(),
            window_end=(T0 + timedelta(seconds=end)).isoformat(),
            **overrides,
        )

    def between(self, conn, start: float, end: float, broadcaster_user_id: int = 1) -> list[int]:
        rows = db.get_channel_moment_windows(
            conn, broadcaster_user_id, T0 + timedelta(seconds=start), T0 + timedelta(seconds=end)
        )
        return [row["id"] for row in rows]

    def test_the_moments_whose_reaction_ran_in_a_stretch_come_back_oldest_first(self, conn):
        late = self.window(conn, 300, 312)
        early = self.window(conn, 100, 110)
        self.window(conn, 900, 910)

        assert self.between(conn, 0, 600) == [early, late]

    def test_each_says_when_its_reaction_ran(self, conn):
        self.window(conn, 100, 110)

        (row,) = db.get_channel_moment_windows(conn, 1, T0, T0 + minutes(10))

        assert (row["window_start"], row["window_end"]) == (
            (T0 + timedelta(seconds=100)).isoformat(),
            (T0 + timedelta(seconds=110)).isoformat(),
        )

    def test_one_that_only_reaches_into_the_stretch_counts(self, conn):
        over_the_start = self.window(conn, 95, 105)
        over_the_end = self.window(conn, 195, 205)
        self.window(conn, 80, 99)
        self.window(conn, 201, 210)

        assert self.between(conn, 100, 200) == [over_the_start, over_the_end]

    def test_whatever_name_the_channel_had_and_no_other_channels(self, conn):
        renamed = self.window(conn, 100, 110, channel_slug="renamed_channel")
        self.window(conn, 100, 110, broadcaster_user_id=2, channel_slug="another_channel")

        assert self.between(conn, 0, 600) == [renamed]
        assert self.between(conn, 0, 600, broadcaster_user_id=3) == []


class TestTheChannelsThatHaveChat:
    def test_every_channel_with_chat_is_named_once(self, conn):
        for owner in (7, 3, 7, 12, 3, 7):
            store_chat(conn, f"channel_{owner}", "alice", "x", T0 + minutes(owner), broadcaster_user_id=owner)
            store_chat(conn, f"channel_{owner}", "bob", "x", T0 + minutes(owner), broadcaster_user_id=owner)

        assert db.get_chat_channel_ids(conn) == [3, 7, 12]

    def test_no_chat_means_no_channels(self, conn):
        assert db.get_chat_channel_ids(conn) == []

    def test_a_channels_chat_reaches_from_its_oldest_message_to_its_newest(self, conn):
        for minute in (5, 1, 9):
            store_chat(conn, "some_channel", "alice", "x", T0 + minutes(minute))
        store_chat(conn, "another_channel", "bob", "x", T0 + minutes(30), broadcaster_user_id=2)

        assert db.get_chat_span(conn, 1) == ((T0 + minutes(1)).isoformat(), (T0 + minutes(9)).isoformat())

    def test_a_channel_without_chat_reaches_nowhere(self, conn):
        assert db.get_chat_span(conn, 1) is None


class TestSessionsOfChat:
    """Chat with no silence longer than the gap in it is one session."""

    def sessions(self, conn, **overrides):
        asked = {"gap_seconds": 600, "min_messages": 1, "min_seconds": 0, "limit": 10, "newest": 1000}
        return [dict(row) for row in db.get_chat_sessions(conn, 1, **{**asked, **overrides})]

    def chat(self, conn, *at_minutes: float, broadcaster_user_id: int = 1) -> None:
        for minute in at_minutes:
            store_chat(
                conn, "some_channel", "alice", "x", T0 + minutes(minute), broadcaster_user_id=broadcaster_user_id
            )

    def test_a_silence_longer_than_the_gap_ends_a_session(self, conn):
        self.chat(conn, 0, 4, 9, 30, 31, 100)

        assert self.sessions(conn) == [
            {"first": (T0 + minutes(100)).isoformat(), "last": (T0 + minutes(100)).isoformat(), "messages": 1},
            {"first": (T0 + minutes(30)).isoformat(), "last": (T0 + minutes(31)).isoformat(), "messages": 2},
            {"first": T0.isoformat(), "last": (T0 + minutes(9)).isoformat(), "messages": 3},
        ]

    def test_a_shorter_silence_does_not(self, conn):
        self.chat(conn, 0, 9.9, 20.5)

        assert [session["messages"] for session in self.sessions(conn)] == [1, 2]

    def test_chat_stored_in_the_older_way_and_in_todays_is_one_chat(self, conn):
        # Rows from before arrival times were written by the service itself
        # end in "Z" and carry milliseconds.
        conn.execute(
            "INSERT INTO chat_messages (message_id, broadcaster_user_id, channel_slug, received_at)"
            " VALUES ('old', 1, 'some_channel', '2026-03-01T19:58:00.000Z')"
        )
        conn.commit()
        self.chat(conn, 0)

        assert [session["messages"] for session in self.sessions(conn)] == [2]

    def test_a_session_of_too_few_messages_or_too_short_is_left_out(self, conn):
        self.chat(conn, 0, 1, 2, 3, 40, 41, 80, 85, 89, 120)

        assert [session["messages"] for session in self.sessions(conn, min_messages=2)] == [3, 2, 4]
        assert [session["messages"] for session in self.sessions(conn, min_seconds=170)] == [3, 4]
        assert [session["messages"] for session in self.sessions(conn, min_messages=4, min_seconds=170)] == [4]

    def test_only_so_many_sessions_are_returned_the_newest_ones(self, conn):
        self.chat(conn, 0, 30, 60, 90)

        assert [session["first"] for session in self.sessions(conn, limit=2)] == [
            (T0 + minutes(90)).isoformat(),
            (T0 + minutes(60)).isoformat(),
        ]

    def test_only_the_newest_messages_are_looked_at(self, conn):
        self.chat(conn, 0, 1, 2, 30, 31, 32)

        assert [session["messages"] for session in self.sessions(conn, newest=4)] == [3, 1]

    def test_another_channels_chat_is_no_part_of_them(self, conn):
        self.chat(conn, 0, 30)
        self.chat(conn, 10, 20, broadcaster_user_id=2)

        assert [session["messages"] for session in self.sessions(conn)] == [1, 1]

    def test_a_channel_without_chat_has_none(self, conn):
        assert self.sessions(conn) == []


class TestAStretchOfAChannelsChat:
    """What the Analyze page reads: a channel's chat from one time up to,
    but not at, another - all of it, one chatter's, or what has a text in
    it."""

    END = T0 + minutes(10)

    def said(self, rows) -> list[str]:
        return [row["content"] for row in rows]

    def searched(self, conn, **asked) -> list[str]:
        return self.said(db.search_channel_chat(conn, 1, T0, self.END, **asked))

    def test_it_holds_its_start_and_not_its_end(self, conn):
        store_chat(conn, "some_channel", "alice", "too early", T0 - timedelta(milliseconds=1))
        store_chat(conn, "some_channel", "alice", "first", T0)
        store_chat(conn, "some_channel", "alice", "last", self.END - timedelta(milliseconds=1))
        store_chat(conn, "some_channel", "alice", "too late", self.END)

        assert self.searched(conn, newest_first=False) == ["first", "last"]
        assert db.count_channel_chat(conn, 1, T0, self.END) == 2
        assert self.said(db.iter_channel_chat(conn, 1, T0, self.END, limit=10)) == ["last", "first"]

    def test_it_is_one_channels_whatever_name_its_chat_came_under(self, conn):
        store_chat(conn, "some_channel", "alice", "here", T0)
        store_chat(conn, "renamed_channel", "alice", "here too", T0 + minutes(1))
        store_chat(conn, "another_channel", "alice", "elsewhere", T0 + minutes(2), broadcaster_user_id=2)

        assert self.searched(conn, newest_first=False) == ["here", "here too"]
        assert db.count_channel_chat(conn, 1, T0, self.END) == 2

    def test_it_comes_newest_first_unless_asked_otherwise(self, conn):
        for minute in (3, 1, 2):
            store_chat(conn, "some_channel", "alice", f"minute {minute}", T0 + minutes(minute))

        assert self.searched(conn) == ["minute 3", "minute 2", "minute 1"]
        assert self.searched(conn, newest_first=False) == ["minute 1", "minute 2", "minute 3"]

    def test_it_is_read_a_page_at_a_time(self, conn):
        for minute in range(7):
            store_chat(conn, "some_channel", "alice", f"minute {minute}", T0 + minutes(minute))

        assert self.searched(conn, limit=3) == ["minute 6", "minute 5", "minute 4"]
        assert self.searched(conn, limit=3, offset=3) == ["minute 3", "minute 2", "minute 1"]
        assert self.searched(conn, limit=3, offset=6) == ["minute 0"]
        assert self.searched(conn, limit=3, offset=9) == []

    def test_one_chatters_messages_are_found_however_the_name_is_capitalised(self, conn):
        store_chat(conn, "some_channel", "Marmot", "mine", T0)
        store_chat(conn, "some_channel", "Marmot2", "not mine", T0 + minutes(1))
        store_chat(conn, "some_channel", "alice", "nor this", T0 + minutes(2))

        assert self.searched(conn, sender="marmot") == ["mine"]
        assert db.count_channel_chat(conn, 1, T0, self.END, sender="MARMOT") == 1

    def test_a_text_is_found_however_it_is_capitalised_in_any_alphabet(self, conn):
        store_chat(conn, "some_channel", "alice", "ČAU všem", T0)
        store_chat(conn, "some_channel", "bob", "no čau teda", T0 + minutes(1))
        store_chat(conn, "some_channel", "carol", "cau", T0 + minutes(2))

        assert self.searched(conn, text="Čau", newest_first=False) == ["ČAU všem", "no čau teda"]
        assert db.count_channel_chat(conn, 1, T0, self.END, text="čau") == 2

    def test_a_text_is_looked_for_as_it_is_and_not_as_a_pattern(self, conn):
        store_chat(conn, "some_channel", "alice", "100% sure", T0)
        store_chat(conn, "some_channel", "bob", "1000 sure", T0 + minutes(1))
        store_chat(conn, "some_channel", "carol", "a_b", T0 + minutes(2))
        store_chat(conn, "some_channel", "dave", "axb", T0 + minutes(3))

        assert self.searched(conn, text="100%") == ["100% sure"]
        assert self.searched(conn, text="a_b") == ["a_b"]

    def test_a_chatter_and_a_text_narrow_it_together(self, conn):
        store_chat(conn, "some_channel", "alice", "gg wp", T0)
        store_chat(conn, "some_channel", "alice", "ahoj", T0 + minutes(1))
        store_chat(conn, "some_channel", "bob", "gg", T0 + minutes(2))

        assert self.searched(conn, sender="alice", text="gg") == ["gg wp"]
        assert self.said(db.iter_channel_chat(conn, 1, T0, self.END, limit=10, sender="alice", text="GG")) == ["gg wp"]

    def test_a_message_comes_with_what_tells_it_from_every_other(self, conn):
        store_chat(conn, "some_channel", "alice", "hello", T0, message_id="m-1")

        (row,) = db.search_channel_chat(conn, 1, T0, self.END)

        assert dict(row) == {
            "message_id": "m-1",
            "received_at": T0.isoformat(),
            "sender_username": "alice",
            "content": "hello",
        }

    def test_all_of_it_is_handed_out_once_a_chunk_at_a_time(self, conn, monkeypatch):
        monkeypatch.setattr(db, "CHAT_CHUNK", 3)
        for second in range(10):
            store_chat(conn, "some_channel", "alice", f"second {second}", T0 + timedelta(seconds=second))

        handed_out = self.said(db.iter_channel_chat(conn, 1, T0, self.END, limit=100))

        assert handed_out == [f"second {second}" for second in reversed(range(10))]

    @pytest.mark.parametrize("limit", [0, 1, 3, 4, 6, 7])
    def test_no_more_than_was_asked_for_is_handed_out(self, conn, monkeypatch, limit):
        monkeypatch.setattr(db, "CHAT_CHUNK", 3)
        for second in range(10):
            store_chat(conn, "some_channel", "alice", f"second {second}", T0 + timedelta(seconds=second))

        handed_out = self.said(db.iter_channel_chat(conn, 1, T0, self.END, limit=limit))

        assert handed_out == [f"second {second}" for second in reversed(range(10))][:limit]

    def test_no_statement_is_left_open_while_its_rows_are_worked_through(self, conn, monkeypatch):
        # One left open would keep the database locked against the message
        # that arrives meanwhile.
        monkeypatch.setattr(db, "CHAT_CHUNK", 2)
        for second in range(5):
            store_chat(conn, "some_channel", "alice", f"second {second}", T0 + timedelta(seconds=second))
        writer = db.get_connection()
        writer.execute("PRAGMA busy_timeout = 0")

        try:
            for number, _row in enumerate(db.iter_channel_chat(conn, 1, T0, self.END, limit=100)):
                store_chat(writer, "some_channel", "bob", "meanwhile", self.END + timedelta(seconds=number))
        finally:
            writer.close()

        assert db.count_channel_chat(conn, 1, T0, self.END + minutes(1), sender="bob") == 5

    def test_the_stretch_is_found_through_an_index(self, conn):
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT content FROM chat_messages"
            " WHERE broadcaster_user_id = 1 AND received_at >= 'a' AND received_at < 'b' ORDER BY received_at DESC"
        ).fetchall()

        assert any("USING INDEX" in step[-1] for step in plan)
        assert not any("TEMP B-TREE" in step[-1] for step in plan)


class TestTheChatAroundAMessage:
    def chat(self, conn, count: int = 9) -> None:
        for number in range(count):
            store_chat(conn, "some_channel", "alice", f"line {number}", T0 + minutes(number), message_id=f"m-{number}")

    def around(self, conn, message_id: str, before: int = 2, after: int = 2) -> list[str] | None:
        rows = db.get_chat_around(conn, 1, message_id, before=before, after=after)
        return None if rows is None else [row["content"] for row in rows]

    def test_it_comes_with_what_was_written_before_and_after_in_order(self, conn):
        self.chat(conn)

        assert self.around(conn, "m-4") == ["line 2", "line 3", "line 4", "line 5", "line 6"]

    def test_there_is_less_of_it_at_either_end_of_the_chat(self, conn):
        self.chat(conn)

        assert self.around(conn, "m-0") == ["line 0", "line 1", "line 2"]
        assert self.around(conn, "m-8") == ["line 6", "line 7", "line 8"]

    def test_another_channels_chat_is_no_part_of_it(self, conn):
        self.chat(conn, 3)
        store_chat(conn, "another_channel", "bob", "elsewhere", T0 + timedelta(seconds=90), broadcaster_user_id=2)

        assert self.around(conn, "m-1") == ["line 0", "line 1", "line 2"]

    def test_a_message_the_channel_does_not_have_has_nothing_around_it(self, conn):
        self.chat(conn, 3)
        store_chat(conn, "another_channel", "bob", "x", T0, message_id="theirs", broadcaster_user_id=2)

        assert self.around(conn, "no-such-message") is None
        assert self.around(conn, "theirs") is None


class TestWhoWroteInAStretchOfChat:
    END = T0 + minutes(10)

    def test_everyone_is_counted_the_most_active_first(self, conn):
        for minute, sender in enumerate(["bob", "alice", "carol", "alice", "bob", "alice"]):
            store_chat(conn, "some_channel", sender, "x", T0 + minutes(minute))
        store_chat(conn, "some_channel", "late", "x", self.END)
        store_chat(conn, "another_channel", "elsewhere", "x", T0, broadcaster_user_id=2)

        assert db.get_chatter_counts(conn, 1, T0, self.END) == [("alice", 3), ("bob", 2), ("carol", 1)]

    def test_those_as_active_as_each_other_come_in_the_order_of_their_names(self, conn):
        for minute, sender in enumerate(["zed", "alice", "mia"]):
            store_chat(conn, "some_channel", sender, "x", T0 + minutes(minute))

        assert [name for name, _count in db.get_chatter_counts(conn, 1, T0, self.END)] == ["alice", "mia", "zed"]

    def test_who_had_written_before_is_told_from_everything_stored_before(self, conn):
        store_chat(conn, "some_channel", "regular", "x", T0 - timedelta(days=30))
        store_chat(conn, "some_channel", "gone", "x", T0 - timedelta(days=30))
        store_chat(conn, "some_channel", "regular", "x", T0)
        store_chat(conn, "some_channel", "regular", "x", T0 + minutes(1))
        store_chat(conn, "some_channel", "newcomer", "x", T0 + minutes(2))
        store_chat(conn, "another_channel", "newcomer", "x", T0 - timedelta(days=30), broadcaster_user_id=2)

        assert db.count_returning_chatters(conn, 1, T0, self.END) == 1

    def test_with_nothing_stored_from_before_nobody_can_be_called_new(self, conn):
        store_chat(conn, "some_channel", "alice", "x", T0)

        assert db.count_returning_chatters(conn, 1, T0, self.END) is None

    def test_a_chatter_was_first_seen_when_their_oldest_message_arrived(self, conn):
        store_chat(conn, "some_channel", "Marmot", "x", T0 + minutes(5))
        store_chat(conn, "some_channel", "Marmot", "x", T0 + minutes(1))
        store_chat(conn, "another_channel", "Marmot", "x", T0 - minutes(9), broadcaster_user_id=2)

        assert db.get_first_message_time(conn, 1, "marmot") == (T0 + minutes(1)).isoformat()
        assert db.get_first_message_time(conn, 1, "nobody") is None


class TestWhenMomentsWereDetected:
    def detected(self, conn, at: str, **overrides) -> None:
        """Stores a moment that was detected at `at`, as the column has it."""
        conn.execute("UPDATE moments SET detected_at = ? WHERE id = ?", (at, store_moment(conn, **overrides)))
        conn.commit()

    def times(self, conn) -> list[str]:
        return db.get_moment_times(conn, 1, T0, T0 + minutes(10), not_for="manual_import")

    def test_a_stretch_of_time_has_the_moments_detected_in_it_oldest_first(self, conn):
        for minute in (7, -1, 3, 10):
            self.detected(conn, (T0 + minutes(minute)).isoformat())
        self.detected(conn, (T0 + minutes(5)).isoformat(), broadcaster_user_id=2)

        assert self.times(conn) == [(T0 + minutes(3)).isoformat(), (T0 + minutes(7)).isoformat()]

    def test_a_moment_timed_by_the_database_is_found_too(self, conn):
        # Left to the database, a time is written with a "Z" and milliseconds.
        self.detected(conn, "2026-03-01T20:04:00.000Z")

        assert self.times(conn) == ["2026-03-01T20:04:00.000Z"]

    def test_an_imported_clip_is_not_a_moment_that_was_detected(self, conn):
        self.detected(conn, (T0 + minutes(1)).isoformat(), reason="manual_import")
        self.detected(conn, (T0 + minutes(2)).isoformat(), reason="laugh")

        assert self.times(conn) == [(T0 + minutes(2)).isoformat()]


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

    def test_moments_detected_at_the_same_instant_come_in_a_fixed_order(self, conn):
        # Paging through a list must not show one of them twice and skip the
        # other; the later one stored counts as the newer.
        for reason in ["first", "second", "third"]:
            moment_id = store_moment(conn, reason=reason)
            conn.execute("UPDATE moments SET detected_at = ? WHERE id = ?", ("2026-01-01T12:00:00Z", moment_id))
        conn.commit()

        assert [row["reason"] for row in db.get_recent_moments(conn)] == ["third", "second", "first"]
        pages = [db.get_recent_moments(conn, limit=1, offset=offset)[0]["reason"] for offset in range(3)]
        assert pages == ["third", "second", "first"]

    def rated(self, conn, ratings: dict[str, int | None]) -> None:
        """One moment per entry, named by its reason, in the order given."""
        for second, (reason, rating) in enumerate(ratings.items()):
            channel = "channel_b" if reason.startswith("b_") else "channel_a"
            moment_id = store_moment(conn, channel=channel, reason=reason)
            conn.execute("UPDATE moments SET detected_at = ? WHERE id = ?", (f"2026-01-01T12:00:0{second}Z", moment_id))
            db.update_moment_rating(conn, moment_id, rating)

    def test_the_list_can_be_narrowed_to_what_is_not_rated_yet(self, conn):
        self.rated(conn, {"unrated": None, "poor": 1, "good": 5, "b_unrated": None})

        assert [row["reason"] for row in db.get_recent_moments(conn, unrated=True)] == ["b_unrated", "unrated"]
        assert [row["reason"] for row in db.get_recent_moments(conn, channel_slug="channel_a", unrated=True)] == [
            "unrated"
        ]

    def test_or_to_what_was_rated_at_least_so_high(self, conn):
        self.rated(conn, {"unrated": None, "one": 1, "three": 3, "four": 4, "b_five": 5})

        assert [row["reason"] for row in db.get_recent_moments(conn, min_rating=4)] == ["b_five", "four"]
        assert [row["reason"] for row in db.get_recent_moments(conn, min_rating=1)] == [
            "b_five",
            "four",
            "three",
            "one",
        ]
        assert [row["reason"] for row in db.get_recent_moments(conn, channel_slug="channel_a", min_rating=4)] == [
            "four"
        ]

    def test_a_narrowed_list_is_paged_like_the_full_one(self, conn):
        self.rated(conn, {"a": None, "rated": 2, "b": None, "c": None})

        assert [row["reason"] for row in db.get_recent_moments(conn, limit=2, unrated=True)] == ["c", "b"]
        assert [row["reason"] for row in db.get_recent_moments(conn, limit=2, offset=2, unrated=True)] == ["a"]

    def test_counts_follow_the_same_narrowing(self, conn):
        self.rated(conn, {"unrated": None, "one": 1, "four": 4, "b_five": 5, "b_unrated": None})

        assert db.count_moments(conn) == 5
        assert db.count_moments(conn, unrated=True) == 2
        assert db.count_moments(conn, min_rating=4) == 2
        assert db.count_moments(conn, channel_slug="channel_a", unrated=True) == 1
        assert db.count_moments(conn, channel_slug="channel_a", min_rating=4) == 1
        assert db.count_moments(conn, channel_slug="nobody", unrated=True) == 0

    def test_one_moment_can_be_fetched_by_its_id(self, conn):
        store_moment(conn, reason="first")
        second = store_moment(conn, channel="channel_b", reason="second")
        db.update_moment_rating(conn, second, 4)

        row = db.get_moment(conn, second)

        assert (row["id"], row["channel_slug"], row["reason"], row["rating"]) == (second, "channel_b", "second", 4)
        # The same columns the list hands out, so the two can be shown alike.
        assert row.keys() == db.get_recent_moments(conn)[0].keys()

    def test_a_moment_that_does_not_exist_is_none(self, conn):
        assert db.get_moment(conn, 999) is None

    def test_a_clip_is_recorded_with_the_stretch_of_the_broadcast_it_holds(self, conn):
        moment_id = store_moment(conn)

        db.update_moment_clip_path(
            conn, moment_id, "some_channel/moment_1.mp4", clip_start="2026-03-01T19:59:35+00:00", clip_duration=38.5
        )

        row = db.get_moment(conn, moment_id)
        assert (row["clip_path"], row["clip_start"], row["clip_duration"]) == (
            "some_channel/moment_1.mp4",
            "2026-03-01T19:59:35+00:00",
            38.5,
        )

    def test_and_with_how_much_footage_its_file_has_either_side_of_it(self, conn):
        moment_id = store_moment(conn)

        db.update_moment_clip_path(
            conn,
            moment_id,
            "some_channel/moment_1.mp4",
            clip_start="2026-03-01T19:59:35+00:00",
            clip_duration=38.5,
            context_before=31.5,
            context_after=58.25,
        )

        row = db.get_moment(conn, moment_id)
        assert (row["clip_duration"], row["context_before"], row["context_after"]) == (38.5, 31.5, 58.25)

    def test_a_file_that_is_the_clip_alone_has_no_context_recorded(self, conn):
        moment_id = store_moment(conn)

        db.update_moment_clip_path(conn, moment_id, "some_channel/moment_1.mp4", clip_duration=38.5)

        row = db.get_moment(conn, moment_id)
        assert (row["context_before"], row["context_after"]) == (None, None)

    def test_joined_context_is_recorded_without_touching_where_the_clip_starts(self, conn):
        moment_id = store_moment(conn)
        db.update_moment_clip_path(
            conn, moment_id, "some_channel/moment_1.mp4", clip_start="2026-03-01T19:59:35+00:00", clip_duration=38.5
        )

        db.update_moment_context(conn, moment_id, 38.5, 29.2, 58.3)

        row = db.get_moment(conn, moment_id)
        assert (row["clip_path"], row["clip_start"]) == ("some_channel/moment_1.mp4", "2026-03-01T19:59:35+00:00")
        assert (row["clip_duration"], row["context_before"], row["context_after"]) == (38.5, 29.2, 58.3)

    def test_the_moments_whose_file_is_the_clip_alone_can_be_listed(self, conn):
        without_clip = store_moment(conn)
        alone = store_moment(conn)
        with_context = store_moment(conn)
        joined = store_moment(conn)
        db.update_moment_clip_path(conn, alone, "some_channel/moment_2.mp4", clip_duration=38.5)
        db.update_moment_clip_path(
            conn, with_context, "some_channel/moment_3.mp4", clip_duration=38.5, context_before=30.0, context_after=0.0
        )
        db.update_moment_clip_path(conn, joined, "some_channel/moment_4.mp4")
        db.update_moment_context(conn, joined, 40.0, 0.0, 58.0)

        rows = db.get_moments_with_clip_alone(conn)

        assert without_clip not in [row["id"] for row in rows]
        assert [(row["id"], row["clip_path"], row["clip_duration"]) for row in rows] == [
            (alone, "some_channel/moment_2.mp4", 38.5)
        ]

    def test_a_clip_of_unknown_extent_can_be_given_its_length_later(self, conn):
        moment_id = store_moment(conn)
        db.update_moment_clip_path(conn, moment_id, "some_channel/moment_1.mp4")

        row = db.get_moment(conn, moment_id)
        assert (row["clip_start"], row["clip_duration"]) == (None, None)

        db.update_moment_clip_duration(conn, moment_id, 41.25)

        row = db.get_moment(conn, moment_id)
        assert (row["clip_path"], row["clip_start"], row["clip_duration"]) == ("some_channel/moment_1.mp4", None, 41.25)

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

    def test_a_channels_chat_delay_is_unset_until_it_is_set(self, conn):
        assert db.get_chat_delay(conn, "channel_a") is None

        db.set_chat_delay(conn, "channel_a", 7)
        db.set_chat_delay(conn, "channel_b", 0)

        assert (db.get_chat_delay(conn, "channel_a"), db.get_chat_delay(conn, "channel_b")) == (7, 0)
        assert db.get_chat_delay(conn, "channel_c") is None

    def test_setting_a_channels_chat_delay_again_replaces_it(self, conn):
        db.set_chat_delay(conn, "channel_a", 7)
        db.set_chat_delay(conn, "channel_a", 3)

        assert db.get_chat_delay(conn, "channel_a") == 3

    def test_setting_a_flag_twice_keeps_one_row(self, conn):
        db.set_flag(conn, "watching_enabled", True)
        db.set_flag(conn, "watching_enabled", False)

        assert conn.execute("SELECT key, value FROM app_settings").fetchall() == [("watching_enabled", "0")]


class TestStreamRecordings:
    """Streams kept whole (stream_recording.py): one row from the moment a
    recording is asked for, filled in once its file has been made."""

    def start(self, conn, channel: str = "some_channel", when: datetime = T0, state: str = "recording") -> int:
        return db.insert_stream_recording(
            conn, broadcaster_user_id=41, channel_slug=channel, requested_at=when.isoformat(), state=state
        )

    def finish(self, conn, recording_id: int) -> None:
        db.finish_stream_recording(
            conn,
            recording_id,
            state="ready",
            file_path="some_channel/2026-03-01_21-00_some_channel_1.mp4",
            started_at=(T0 + minutes(1)).isoformat(),
            ended_at=(T0 + minutes(61)).isoformat(),
            duration=3600.5,
            size_bytes=5_000_000_000,
            parts='[["2026-03-01T20:01:00+00:00", 3600.5]]',
        )

    def test_one_that_was_just_asked_for_says_little(self, conn):
        recording_id = self.start(conn)

        row = db.get_stream_recording(conn, recording_id)

        assert dict(row) == {
            "id": recording_id,
            "broadcaster_user_id": 41,
            "channel_slug": "some_channel",
            "requested_at": T0.isoformat(),
            "state": "recording",
            "ended_by": None,
            "file_path": None,
            "started_at": None,
            "ended_at": None,
            "duration": None,
            "size_bytes": None,
            "parts": None,
        }

    def test_what_it_holds_is_filled_in_when_its_file_has_been_made(self, conn):
        recording_id = self.start(conn)
        db.set_stream_recording_state(conn, recording_id, "saving", "stopped")

        self.finish(conn, recording_id)

        row = db.get_stream_recording(conn, recording_id)
        assert (row["state"], row["ended_by"]) == ("ready", "stopped")
        assert row["file_path"] == "some_channel/2026-03-01_21-00_some_channel_1.mp4"
        assert (row["duration"], row["size_bytes"]) == (3600.5, 5_000_000_000)
        assert row["parts"] == '[["2026-03-01T20:01:00+00:00", 3600.5]]'

    def test_a_size_past_four_gigabytes_is_stored_whole(self, conn):
        recording_id = self.start(conn)
        self.finish(conn, recording_id)
        conn.execute("UPDATE stream_recordings SET size_bytes = ?", (60 * 1024**3,))

        assert db.get_stream_recording(conn, recording_id)["size_bytes"] == 60 * 1024**3

    def test_why_it_ended_stays_unless_it_is_given_again(self, conn):
        recording_id = self.start(conn)
        db.set_stream_recording_state(conn, recording_id, "saving", "stream_ended")

        db.set_stream_recording_state(conn, recording_id, "failed")
        assert db.get_stream_recording(conn, recording_id)["ended_by"] == "stream_ended"

        db.set_stream_recording_state(conn, recording_id, "saving", "interrupted")
        assert db.get_stream_recording(conn, recording_id)["ended_by"] == "interrupted"

    def test_they_are_listed_newest_first(self, conn):
        first = self.start(conn, "channel_a")
        second = self.start(conn, "channel_b", T0 + minutes(5))
        third = self.start(conn, "channel_a", T0 + minutes(9))

        assert [row["id"] for row in db.get_stream_recordings(conn)] == [third, second, first]
        assert db.count_stream_recordings(conn) == 3

    def test_or_only_the_ones_that_stand_a_certain_way(self, conn):
        running = self.start(conn)
        saving = self.start(conn, state="saving")
        ready = self.start(conn)
        self.finish(conn, ready)

        assert [row["id"] for row in db.get_stream_recordings(conn, ("recording", "saving"))] == [saving, running]
        assert [row["id"] for row in db.get_stream_recordings(conn, ["ready"])] == [ready]
        assert db.get_stream_recordings(conn, []) == []

    def test_one_that_is_removed_is_gone_and_its_number_is_not_used_again(self, conn):
        # A recording's folder and file are named for its number.
        first = self.start(conn)
        second = self.start(conn)

        db.delete_stream_recording(conn, second)
        third = self.start(conn)

        assert db.get_stream_recording(conn, second) is None
        assert third > second > first
        assert db.count_stream_recordings(conn) == 2

    def test_an_unknown_one_is_none(self, conn):
        assert db.get_stream_recording(conn, 999) is None

    def test_a_database_from_before_recordings_gets_the_table(self):
        legacy_database("CREATE TABLE streamers (id INTEGER PRIMARY KEY, broadcaster_user_id INTEGER, slug TEXT)")

        connection = db.get_connection()
        try:
            assert "stream_recordings" in tables(connection)
            assert db.get_stream_recordings(connection) == []
        finally:
            connection.close()
