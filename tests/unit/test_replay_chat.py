"""scripts/replay_chat.py: replaying stored chat through the detector.

The script exists to answer "what would this tuning change have done on a
real stream" - which is only worth anything if a replay behaves like the
live service. So besides its own logic, the tests here hold it to the same
results the detector gives when the very same chat is fed to it directly.
"""

from __future__ import annotations

import itertools
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

import replay_chat
from kick_clip_hunter import db, detector, main, recorder
from tests.support.chat import ChatSim, people
from tests.support.data import add_chat

START = datetime(2026, 3, 1, 18, 0, 0, tzinfo=timezone.utc)
CHANNEL = "some_channel"


@dataclass
class StoredStream(ChatSim):
    """A `ChatSim` whose messages go into the database, stamped with the
    simulated clock, instead of into the detector - what the service leaves
    behind after a stream. The same script can therefore be run live
    (`ChatSim`) and stored-then-replayed (`StoredStream`) and compared."""

    conn: sqlite3.Connection | None = None
    _ids: itertools.count = field(default_factory=itertools.count)

    def say(self, sender: str, content: str) -> None:
        received_at = datetime.fromtimestamp(self.now, timezone.utc).isoformat()
        db.insert_chat_message(
            self.conn,
            message_id=f"stored-{next(self._ids)}",
            broadcaster_user_id=1,
            channel_slug=self.channel,
            sender_username=sender,
            content=content,
            emotes_json="[]",
            created_at=received_at,
            received_at=received_at,
        )


@pytest.fixture
def stream():
    connection = db.get_connection()
    yield StoredStream(channel=CHANNEL, now=START.timestamp(), conn=connection)
    connection.close()


def exact_crowd() -> list[str]:
    """Just enough people to fire a moment on a ten-chatter channel - so the
    reaction stops at the trigger and the moment isn't extended."""
    return people(detector._dynamic_min_reaction_unique(10))


def typical_evening(chat: ChatSim) -> None:
    """Five minutes of chat with one stray laugh in it, then two real
    reactions three minutes apart: the first is over as soon as it fires,
    the second keeps drawing in new people."""
    chat.chatter(150)
    chat.say("viewer1", "xd")
    chat.chatter(150)
    chat.burst(exact_crowd(), "xDDD")
    chat.chatter(180)
    chat.burst(people(8, "second_wave"), "xDDD")
    chat.wait(3)
    chat.burst(people(8, "still_laughing"), "xDDD")
    chat.chatter(60)


def load(channel: str = CHANNEL, **kwargs):
    conn = db.get_connection()
    try:
        return replay_chat.load_messages(conn, channel, **kwargs)
    finally:
        conn.close()


def replayed(**session) -> list[replay_chat.ReplayedMoment]:
    messages, keywords = load()
    return replay_chat.replay(messages, keywords, replay_chat.SessionSettings(**session))


class TestSettingsStayInStep:
    def test_the_session_timing_is_the_services(self):
        # replay_chat can't import main.py, so it carries a copy.
        defaults = replay_chat.SessionSettings()

        assert defaults.poll_seconds == main.MOMENT_SESSION_POLL_SECONDS
        assert defaults.quiet_seconds == main.MOMENT_SESSION_QUIET_SECONDS
        assert defaults.max_seconds == main.MOMENT_SESSION_MAX_SECONDS
        assert defaults.post_roll_seconds == main.DYNAMIC_POST_ROLL_SECONDS


class TestLoadingStoredChat:
    def test_messages_come_back_in_arrival_order_whatever_order_they_were_stored_in(self):
        add_chat(CHANNEL, "second", "b", START + timedelta(seconds=5))
        add_chat(CHANNEL, "first", "a", START)

        messages, _keywords = load()

        assert [(message.sender, message.content) for message in messages] == [("first", "a"), ("second", "b")]
        assert messages[1].time - messages[0].time == 5

    def test_only_the_asked_for_channel_is_loaded_whatever_its_case(self):
        add_chat("Some_Channel", "alice", "here", START)
        add_chat("another_channel", "bob", "elsewhere", START)

        messages, _keywords = load("some_channel")

        assert [message.sender for message in messages] == ["alice"]

    def test_the_time_range_includes_its_start_and_excludes_its_end(self):
        for minute in range(4):
            add_chat(CHANNEL, f"minute{minute}", "x", START + timedelta(minutes=minute))

        messages, _keywords = load(since="2026-03-01T18:01:00", until="2026-03-01T18:03:00+00:00")

        assert [message.sender for message in messages] == ["minute1", "minute2"]

    def test_both_timestamp_spellings_found_in_real_databases_are_understood(self):
        # Rows written by SQLite's default carry a "Z", rows written by the
        # service an explicit offset; both are in the same table.
        conn = db.get_connection()
        try:
            for message_id, received_at in (("z", "2026-03-01T18:00:05.000Z"), ("offset", "2026-03-01T18:00:00+00:00")):
                db.insert_chat_message(
                    conn,
                    message_id=message_id,
                    broadcaster_user_id=1,
                    channel_slug=CHANNEL,
                    sender_username=message_id,
                    content="x",
                    emotes_json="[]",
                    created_at=received_at,
                    received_at=received_at,
                )
        finally:
            conn.close()

        messages, _keywords = load()

        assert [message.sender for message in messages] == ["offset", "z"]
        assert messages[1].time - messages[0].time == 5

    def test_the_channels_emote_keywords_come_with_it(self):
        conn = db.get_connection()
        try:
            db.replace_channel_keywords(conn, 77, {"kekw": 3.5})
        finally:
            conn.close()
        add_chat(CHANNEL, "alice", "KEKW", START, broadcaster_user_id=77)

        _messages, keywords = load()

        assert keywords == {"kekw": 3.5}

    def test_emote_positions_are_counted_from_the_stored_payload(self):
        conn = db.get_connection()
        try:
            db.insert_chat_message(
                conn,
                message_id="with-emotes",
                broadcaster_user_id=1,
                channel_slug=CHANNEL,
                sender_username="alice",
                content="[emote:1:KEKW] [emote:1:KEKW] [emote:2:Sadge]",
                emotes_json='[{"emote_id": "1", "positions": [{"s": 0, "e": 13}, {"s": 15, "e": 28}]}, '
                '{"emote_id": "2", "positions": [{"s": 30, "e": 44}]}]',
                created_at=START.isoformat(),
                received_at=START.isoformat(),
            )
        finally:
            conn.close()

        (message,), _keywords = load()

        assert message.emote_count == 3

    def test_a_channel_with_no_stored_chat_is_empty(self):
        assert load("nobody") == ([], {})


class TestReplay:
    def test_finds_the_moments_of_a_stored_stream(self, stream):
        typical_evening(stream)

        moments = replayed()

        assert len(moments) == 2
        assert all("laugh" in moment.reasons for moment in moments)
        # Five minutes of warm-up, then the first reaction; the second one
        # three minutes after it.
        first, second = (moment.time - START.timestamp() for moment in moments)
        assert 300 <= first <= 302
        assert 480 <= second <= 485

    def test_fires_at_exactly_the_instants_the_live_detector_would(self, stream):
        # The same script, once stored and replayed, once fed straight in.
        typical_evening(stream)
        live = ChatSim(now=START.timestamp())
        typical_evening(live)
        assert len(live.moments) == 2

        moments = replayed()

        assert [moment.time for moment in moments] == pytest.approx(live.moment_times, abs=1e-3)
        assert [list(moment.reasons) for moment in moments] == [spike.reasons for _time, spike in live.moments]
        assert [moment.score for moment in moments] == pytest.approx([spike.score for _time, spike in live.moments])

    def test_a_reaction_that_stops_at_the_trigger_gets_a_clip_of_the_base_length(self, stream):
        typical_evening(stream)
        session = replay_chat.SessionSettings()

        brief, _sustained = replayed()

        assert brief.extension_seconds == 0
        assert brief.clip_seconds(session) == (
            recorder.PRE_ROLL_SECONDS + detector.SHORT_WINDOW_SECONDS + main.DYNAMIC_POST_ROLL_SECONDS
        )

    def test_a_reaction_that_keeps_going_gets_a_longer_one_capped_by_the_session(self, stream):
        typical_evening(stream)
        session = replay_chat.SessionSettings()

        brief, sustained = replayed()

        assert sustained.extension_seconds > brief.extension_seconds
        assert sustained.extension_seconds <= session.max_seconds + session.poll_seconds
        assert sustained.clip_seconds(session) > brief.clip_seconds(session)

    def test_a_shorter_cap_shortens_the_long_clips_only(self, stream):
        typical_evening(stream)

        default_brief, default_sustained = replayed()
        capped_brief, capped_sustained = replayed(max_seconds=6.0)

        assert capped_sustained.extension_seconds < default_sustained.extension_seconds
        assert capped_brief.extension_seconds == default_brief.extension_seconds

    def test_replaying_twice_gives_the_same_answer(self, stream):
        # Each replay starts from a clean detector, not the previous one's state.
        typical_evening(stream)

        assert replayed() == replayed()

    def test_no_chat_no_moments(self):
        assert replay_chat.replay([], {}, replay_chat.SessionSettings()) == []

    def test_a_moment_still_open_when_the_chat_log_ends_is_counted(self, stream):
        stream.chatter(150)
        stream.say("viewer1", "xd")
        stream.chatter(150)
        stream.burst(people(8), "xDDD")  # the log ends mid-reaction

        assert len(replayed()) == 1


class TestCommandLine:
    def run(self, capsys, *arguments: str) -> tuple[int, str]:
        code = replay_chat.main([CHANNEL, *arguments])
        return code, capsys.readouterr().out

    def test_prints_how_many_moments_and_how_long_their_clips(self, stream, capsys):
        typical_evening(stream)

        code, output = self.run(capsys)

        header, columns, current = output.splitlines()
        assert code == 0
        assert header.startswith(f"{CHANNEL}: ") and "2026-03-01 18:00" in header
        assert columns.split() == ["moments", "per", "hour", "not", "extended", "median", "clip", "longest"]
        label, moments, per_hour, not_extended, _median, _longest = current.rsplit(maxsplit=5)
        assert (label.strip(), moments, not_extended) == ("current settings", "2", "1")
        assert float(per_hour) > 0

    def test_an_override_adds_a_comparison_row(self, stream, capsys):
        typical_evening(stream)
        impossible = detector.MIN_REACTION_UNIQUE_CEILING * 100

        code, output = self.run(capsys, "--set", f"MIN_ABSOLUTE_UNIQUE={impossible}")

        current, overridden = output.splitlines()[2:]
        assert code == 0
        assert current.split()[2] == "2"
        assert overridden.startswith("with overrides") and overridden.split()[2] == "0"

    def test_overrides_do_not_leak_out_of_the_comparison(self, stream, capsys):
        typical_evening(stream)
        before = detector.MIN_ABSOLUTE_UNIQUE

        self.run(capsys, "--set", "MIN_ABSOLUTE_UNIQUE=999")

        assert detector.MIN_ABSOLUTE_UNIQUE == before

    def test_session_limits_can_be_overridden_too(self, stream, capsys):
        typical_evening(stream)

        _code, output = self.run(capsys, "--max", "6")

        current, overridden = output.splitlines()[2:]
        longest = [int(row.split()[-1].rstrip("s")) for row in (current, overridden)]
        assert longest[1] < longest[0]

    @pytest.mark.parametrize(
        "override",
        ["NOT_A_CONSTANT=1", "MIN_ABSOLUTE_UNIQUE", "MIN_ABSOLUTE_UNIQUE=lots", "record_message=1", "VOTE_MESSAGES=1"],
    )
    def test_a_bad_override_is_refused_before_anything_runs(self, capsys, override):
        with pytest.raises(SystemExit) as stopped:
            replay_chat.main([CHANNEL, "--set", override])

        assert stopped.value.code == 2

    def test_an_integer_constant_stays_an_integer(self):
        assert replay_chat.parse_override("MIN_ABSOLUTE_UNIQUE=5") == ("MIN_ABSOLUTE_UNIQUE", 5)
        assert isinstance(replay_chat.parse_override("MIN_ABSOLUTE_UNIQUE=5")[1], int)
        assert replay_chat.parse_override("SUSTAIN_FRACTION=0.5") == ("SUSTAIN_FRACTION", 0.5)

    def test_a_channel_without_stored_chat_exits_with_an_error(self, capsys):
        code, output = self.run(capsys)

        assert code == 1
        assert "no stored chat" in output

    def test_nothing_is_written_to_the_database(self, stream, capsys):
        typical_evening(stream)
        conn = db.get_connection()
        try:
            before = conn.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0], db.count_moments(conn)
            self.run(capsys, "--set", "COOLDOWN_SECONDS=30")
            after = conn.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0], db.count_moments(conn)
        finally:
            conn.close()

        assert after == before
