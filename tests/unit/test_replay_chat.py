"""Replaying stored chat through the detector: replay.py, and the script
that puts it on the command line (scripts/replay_chat.py).

A replay exists to answer "what would these settings have done on a real
stream" - which is only worth anything if it behaves like the live service.
So besides its own logic, the tests here hold it to the same results the
detector gives when the very same chat is fed to it directly.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

import replay_chat
from kick_clip_hunter import db, detector, recorder, replay, tuning
from tests.support.chat import ChatSim, StoredStream, exact_crowd, people, typical_evening
from tests.support.data import add_chat, add_streamer

START = datetime(2026, 3, 1, 18, 0, 0, tzinfo=timezone.utc)
CHANNEL = "some_channel"


@pytest.fixture
def stream():
    connection = db.get_connection()
    yield StoredStream(channel=CHANNEL, now=START.timestamp(), conn=connection)
    connection.close()


def load(channel: str = CHANNEL, **kwargs):
    conn = db.get_connection()
    try:
        return replay.load_messages(conn, channel, **kwargs)
    finally:
        conn.close()


def replayed(**changed) -> list[replay.ReplayedMoment]:
    """The stored chat replayed under what is recommended, with `changed`."""
    messages, keywords = load()
    return replay.replay(messages, keywords, replace(tuning.recommended(), **changed))


def with_detector(**changed) -> detector.Settings:
    return replace(detector.recommended(), **changed)


def store_tuning(owner: int, **values) -> None:
    conn = db.get_connection()
    try:
        db.replace_tuning(conn, owner, tuning.encoded(values))
    finally:
        conn.close()


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

    def test_with_a_limit_it_is_the_newest_that_many_still_in_arrival_order(self):
        for minute in (2, 0, 3, 1):
            add_chat(CHANNEL, f"minute{minute}", "x", START + timedelta(minutes=minute))

        assert [message.sender for message in load(limit=2)[0]] == ["minute2", "minute3"]
        assert [message.sender for message in load(limit=10)[0]] == ["minute0", "minute1", "minute2", "minute3"]
        assert load(limit=0)[0] == []

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


class TestLoadingAStretchOfAChannelToldByItsId:
    """What a stream recording replays: it has the channel's name from the
    watchlist, which is not the one its chat is stored under."""

    def between(self, start: datetime, end: datetime, broadcaster_user_id: int = 1):
        conn = db.get_connection()
        try:
            return replay.load_between(conn, broadcaster_user_id, start, end)
        finally:
            conn.close()

    def test_it_is_the_channels_chat_whatever_name_it_came_under(self):
        add_chat("some_channel", "alice", "as kick spells it", START)
        add_chat("renamed_channel", "bob", "under another name", START + timedelta(seconds=1))
        add_chat("another_channel", "carol", "elsewhere", START + timedelta(seconds=2), broadcaster_user_id=2)

        messages, _keywords = self.between(START, START + timedelta(minutes=1))

        assert [(message.sender, message.content) for message in messages] == [
            ("alice", "as kick spells it"),
            ("bob", "under another name"),
        ]

    def test_only_what_arrived_in_the_stretch_in_the_order_it_arrived(self):
        for second in (30, 5, 61, -1, 12):
            add_chat(CHANNEL, f"second{second}", "x", START + timedelta(seconds=second))

        messages, _keywords = self.between(START, START + timedelta(seconds=60))

        assert [message.sender for message in messages] == ["second5", "second12", "second30"]
        assert [message.time - START.timestamp() for message in messages] == [5, 12, 30]

    def test_it_gives_what_loading_a_channel_by_name_gives(self, stream):
        typical_evening(stream)

        by_id = self.between(START - timedelta(hours=1), START + timedelta(hours=3))

        assert by_id == load()

    def test_messages_are_in_time_order_however_their_times_are_written(self):
        # As text, "…:05.000Z" sorts after "…:05.000500+00:00"; as a time it
        # is the earlier of the two.
        conn = db.get_connection()
        try:
            for message_id, received_at in (
                ("z", "2026-03-01T18:00:05.000Z"),
                ("offset", "2026-03-01T18:00:05.000500+00:00"),
            ):
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

        messages, _keywords = self.between(START, START + timedelta(minutes=1))

        assert [message.sender for message in messages] == ["z", "offset"]

    def test_emote_positions_and_the_channels_keywords_come_with_it(self):
        conn = db.get_connection()
        try:
            db.replace_channel_keywords(conn, 77, {"kekw": 3.5})
            db.insert_chat_message(
                conn,
                message_id="with-emotes",
                broadcaster_user_id=77,
                channel_slug=CHANNEL,
                sender_username="alice",
                content="[emote:1:KEKW] [emote:1:KEKW]",
                emotes_json='[{"emote_id": "1", "positions": [{"s": 0, "e": 13}, {"s": 15, "e": 28}]}]',
                created_at=START.isoformat(),
                received_at=START.isoformat(),
            )
        finally:
            conn.close()

        (message,), keywords = self.between(START, START + timedelta(minutes=1), broadcaster_user_id=77)

        assert message.emote_count == 2 and keywords == {"kekw": 3.5}

    def test_it_is_read_a_chunk_at_a_time(self, monkeypatch):
        monkeypatch.setattr(db, "CHAT_CHUNK", 3)
        for second in range(10):
            add_chat(CHANNEL, f"second{second}", "x", START + timedelta(seconds=second))

        messages, _keywords = self.between(START, START + timedelta(minutes=1))

        assert [message.sender for message in messages] == [f"second{second}" for second in range(10)]

    def test_a_stretch_with_no_chat_is_empty(self):
        assert self.between(START, START + timedelta(minutes=1)) == ([], {})


class TestHowLongChatWasGoing:
    def message(self, seconds: float) -> replay.Message:
        return replay.Message(START.timestamp() + seconds, "viewer", "x", 0)

    def test_chat_without_a_hole_in_it_ran_from_its_first_message_to_its_last(self):
        messages = [self.message(second) for second in range(0, 7201, 30)]

        assert replay.chat_hours(messages) == pytest.approx(2.0)

    def test_a_hole_the_detector_would_have_forgotten_everything_over_does_not_count(self):
        # Two one-hour streams a day apart are two hours of chat, not 26.
        hole = detector.BASELINE_WINDOW_SECONDS + 1
        day = 86400
        messages = [self.message(second) for second in range(0, 3601, 60)]
        messages += [self.message(day + second) for second in range(0, 3601, 60)]
        assert day > hole

        assert replay.chat_hours(messages) == pytest.approx(2.0)

    def test_a_lull_shorter_than_that_does(self):
        lull = detector.BASELINE_WINDOW_SECONDS
        messages = [self.message(0), self.message(lull), self.message(lull + 60)]

        assert replay.chat_hours(messages) == pytest.approx((lull + 60) / 3600)

    def test_no_chat_or_a_single_message_ran_for_no_time_at_all(self):
        assert replay.chat_hours([]) == 0.0
        assert replay.chat_hours([self.message(0)]) == 0.0


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

        brief, _sustained = replayed()

        assert brief.extension_seconds == 0
        assert brief.clip_seconds(tuning.recommended()) == (
            recorder.PRE_ROLL_SECONDS + detector.SHORT_WINDOW_SECONDS + tuning.DYNAMIC_POST_ROLL_SECONDS
        )

    def test_a_reaction_that_keeps_going_gets_a_longer_one_capped_by_the_session(self, stream):
        typical_evening(stream)
        recommended = tuning.recommended()

        brief, sustained = replayed()

        assert sustained.extension_seconds > brief.extension_seconds
        assert sustained.extension_seconds <= recommended.max_open_seconds + tuning.MOMENT_SESSION_POLL_SECONDS
        assert sustained.clip_seconds(recommended) > brief.clip_seconds(recommended)

    def test_a_shorter_cap_shortens_the_long_clips_only(self, stream):
        typical_evening(stream)

        default_brief, default_sustained = replayed()
        capped_brief, capped_sustained = replayed(max_open_seconds=6.0)

        assert capped_sustained.extension_seconds < default_sustained.extension_seconds
        assert capped_brief.extension_seconds == default_brief.extension_seconds

    def test_the_clip_is_as_long_as_the_settings_it_is_asked_about_make_it(self, stream):
        typical_evening(stream)
        longer = replace(tuning.recommended(), pre_roll_seconds=40, post_roll_seconds=25)

        brief, _sustained = replayed()

        assert brief.clip_seconds(longer) == 40 + detector.SHORT_WINDOW_SECONDS + 25

    def test_it_is_the_settings_it_is_given_that_the_detector_goes_by(self, stream):
        typical_evening(stream)
        impossible = with_detector(min_absolute_unique=999, min_reaction_unique_ceiling=999)

        assert len(replayed()) == 2
        assert replayed(detector=impossible) == []
        assert replayed(detector=with_detector(laugh_enabled=False)) == []

    def test_a_longer_cooldown_merges_what_a_shorter_one_keeps_apart(self, stream):
        typical_evening(stream)
        long = with_detector(cooldown_seconds=600)

        assert len(replayed(detector=long, quiet_seconds=5.0)) == 1

    def test_a_reaction_that_fires_again_while_its_moment_is_open_stays_one_moment(self, stream):
        # Two bursts twenty seconds apart, under a cooldown short enough for
        # the second to fire: with the first moment still open it only keeps
        # that one alive, as in the service; once it has closed, it is a
        # moment of its own.
        stream.chatter(150)
        stream.say("viewer1", "xd")
        stream.chatter(150)
        stream.burst(exact_crowd(), "xDDD")
        stream.chatter(20)
        stream.burst(people(8, "second_wave"), "xDDD")
        stream.chatter(60)
        refiring = with_detector(cooldown_seconds=15)

        assert len(replayed(detector=refiring, quiet_seconds=30.0)) == 1
        assert len(replayed(detector=refiring, quiet_seconds=5.0)) == 2

    def test_the_stored_keywords_weigh_what_the_settings_say(self, stream):
        stream.chatter(150)
        stream.say("viewer1", "Sadge")
        stream.chatter(150)
        stream.burst(people(8), "Sadge")
        messages, _keywords = load()
        keywords = detector.classify_emote_names(["Sadge"])

        # Nobody laughs at Sadge: every moment is kept, or there would be none.
        listened = replay.replay(messages, keywords, tuning.tuned({"laughing_share": 0}))
        ignored = replay.replay(
            messages, keywords, tuning.tuned({"laughing_share": 0, "ignored_emote_words": ("sadge",)})
        )

        assert [moment.reasons for moment in listened] == [("emote_mention",)]
        assert ignored == []

    # A channel that fires on fewer people laughing than it keeps a moment
    # for. As recommended the two bars are the same share of the chat, and a
    # moment fired by laughs always has enough laughing to be kept.
    @staticmethod
    def fires_early() -> detector.Settings:
        return with_detector(min_reaction_unique_fraction=detector.MIN_REACTION_UNIQUE_FRACTION / 2)

    def busier_evening(self, stream, joining: int) -> None:
        """A chat of forty, a burst just big enough to fire, and `joining`
        more people laughing half a minute later - a reaction nobody joins
        is dropped there once its minute is up."""
        settings = self.fires_early()
        assert detector._dynamic_min_reaction_unique(40, settings) < detector.laughing_needed(40, settings)
        stream.chatter(150, chatters=40)
        stream.say("viewer1", "xd")
        stream.chatter(150, chatters=40)
        stream.burst(people(detector._dynamic_min_reaction_unique(40, settings)), "xDDD")
        stream.chatter(30, chatters=40)
        stream.burst(people(joining, "joined"), "xd", spacing=3)
        stream.chatter(120, chatters=40)

    def test_a_moment_too_few_laughed_at_is_dropped_as_it_is_live(self, stream):
        self.busier_evening(stream, joining=0)

        assert replayed(detector=self.fires_early()) == []
        assert len(replayed(detector=replace(self.fires_early(), keep_laughing_fraction=0))) == 1

    def test_and_kept_once_enough_people_join_in_within_the_minute(self, stream):
        settings = self.fires_early()
        needed = detector.laughing_needed(40, settings) - detector._dynamic_min_reaction_unique(40, settings)
        self.busier_evening(stream, joining=needed)

        assert len(replayed(detector=settings)) == 1

    def evening_joined_slowly(self, stream, joining: int) -> int:
        """A chat of forty, a burst that fires, and `joining` more people
        laughing one at a time over the next minute, too far apart to fire
        again. Returns how many people laughed around the moment."""
        firing = detector._dynamic_min_reaction_unique(40)
        stream.chatter(300, chatters=40)
        stream.burst(people(firing), "xDDD")
        stream.burst(people(joining, "joined"), "xd", spacing=detector.SHORT_WINDOW_SECONDS / 2 + 1)
        stream.chatter(120, chatters=40)
        return firing + joining

    def test_a_kept_moment_says_how_many_people_laughed_around_it(self, stream):
        laughed = self.evening_joined_slowly(stream, joining=4)

        (moment,) = replayed()

        assert moment.laughing == laughed

    def test_the_same_count_is_had_afterwards_from_the_chat_around_it(self, stream):
        self.evening_joined_slowly(stream, joining=4)
        (moment,) = replayed()
        messages, keywords = load()

        assert replay.laughing_around(messages, moment.time, keywords, tuning.recommended()) == moment.laughing

    def test_a_clip_command_is_left_out_as_it_is_live(self, stream):
        # Who posted a stored message, with what badges, is not kept with it,
        # so a replay makes no clips on command - and, like the service, it
        # keeps the command away from the detector.
        typical_evening(stream)
        messages, keywords = load()
        laughs = {message.content.lower() for message in messages if detector.classify_message(message.content)[0]}

        deaf = replay.replay(messages, keywords, replace(tuning.recommended(), clip_commands=frozenset(laughs)))

        assert replay.replay(messages, keywords, tuning.recommended()) != []
        assert deaf == []

    @pytest.mark.parametrize("commands", ["record_commands", "stop_commands", "status_commands"])
    def test_so_is_one_of_the_bots(self, stream, commands):
        typical_evening(stream)
        messages, keywords = load()
        laughs = {message.content.lower() for message in messages if detector.classify_message(message.content)[0]}

        deaf = replay.replay(messages, keywords, replace(tuning.recommended(), **{commands: frozenset(laughs)}))

        assert deaf == []

    def test_replaying_twice_gives_the_same_answer(self, stream):
        # Each replay starts from a clean detector, not the previous one's state.
        typical_evening(stream)

        assert replayed() == replayed()

    def test_it_leaves_the_detector_as_it_found_it(self, stream):
        # The Settings page replays inside the running service: a channel
        # that is live must not notice, and nothing may be left behind.
        typical_evening(stream)
        live = ChatSim(channel=CHANNEL)
        live.warm_up()
        entries_before = list(detector._entries[CHANNEL])
        cooldowns_before = dict(detector._last_moment_at)

        assert len(replayed()) == 2

        assert list(detector._entries) == [CHANNEL]
        assert list(detector._entries[CHANNEL]) == entries_before
        assert detector._last_moment_at == cooldowns_before

    def test_nor_does_a_replay_that_fails_half_way(self, stream, monkeypatch):
        typical_evening(stream)
        messages, keywords = load()

        def explode(*args, **kwargs):
            raise RuntimeError("out of luck")

        monkeypatch.setattr(detector, "reaction_active", explode)
        with pytest.raises(RuntimeError, match="out of luck"):
            replay.replay(messages, keywords, tuning.recommended())

        assert dict(detector._entries) == {} and detector._last_moment_at == {}

    def test_no_chat_no_moments(self):
        assert replay.replay([], {}, tuning.recommended()) == []

    def test_each_moment_comes_with_what_the_detector_measured(self, stream):
        # What a moment is stored with, should one be made of it.
        typical_evening(stream)
        live = ChatSim(channel="live_run", now=START.timestamp())
        typical_evening(live)

        moments = replayed()

        assert len(moments) == len(live.moments) == 2
        for moment, (_time, measured) in zip(moments, live.moments, strict=True):
            spike = moment.spike
            assert (tuple(spike.reasons), spike.score) == (moment.reasons, moment.score)
            assert spike.reasons == measured.reasons
            assert (spike.message_count, spike.emote_count, spike.keyword_hits) == (
                measured.message_count,
                measured.emote_count,
                measured.keyword_hits,
            )
            # A stored time is kept to the microsecond, which shows in a rate.
            assert spike.score == pytest.approx(measured.score, rel=1e-3)
            # So does whether a message right at the baseline window's edge
            # is still held: one message more or less of a full window.
            assert spike.baseline_message_rate == pytest.approx(
                measured.baseline_message_rate,
                abs=1.01 / (detector.BASELINE_WINDOW_SECONDS - detector.SHORT_WINDOW_SECONDS),
            )
            assert spike.current_message_rate == pytest.approx(measured.current_message_rate, rel=1e-3)

    def test_it_says_how_far_it_has_got_as_it_goes(self, stream, monkeypatch):
        monkeypatch.setattr(replay, "PROGRESS_EVERY", 100)
        stream.chatter(450)
        messages, keywords = load()
        said: list[int] = []

        replay.replay(messages, keywords, tuning.recommended(), said.append)

        assert said == [0, 100, 200, 300, 400]

    def test_what_asks_how_far_can_have_it_given_up_and_nothing_of_it_stays(self, stream, monkeypatch):
        monkeypatch.setattr(replay, "PROGRESS_EVERY", 100)
        typical_evening(stream)
        messages, keywords = load()

        def enough(fed: int) -> None:
            if fed >= 200:
                raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            replay.replay(messages, keywords, tuning.recommended(), enough)

        assert dict(detector._entries) == {} and detector._last_moment_at == {}

    def test_a_moment_still_open_when_the_chat_log_ends_is_counted(self, stream):
        stream.chatter(150)
        stream.say("viewer1", "xd")
        stream.chatter(150)
        stream.burst(people(8), "xDDD")  # the log ends mid-reaction

        assert len(replayed()) == 1


class TestWhatAReplayComesTo:
    def moment(self, extension: float) -> replay.ReplayedMoment:
        return replay.ReplayedMoment(time=0.0, reasons=("laugh",), score=5.0, extension_seconds=extension)

    def test_how_many_how_often_and_how_long(self):
        recommended = tuning.recommended()
        base = recommended.pre_roll_seconds + detector.SHORT_WINDOW_SECONDS + recommended.post_roll_seconds
        moments = [self.moment(0), self.moment(0.5), self.moment(12), self.moment(40)]

        summary = replay.summarise(moments, hours=2.0, tuning=recommended)

        assert (summary.moments, summary.per_hour, summary.not_extended) == (4, 2.0, 2)
        assert summary.median_clip_seconds == pytest.approx(base + (0.5 + 12) / 2)
        assert summary.longest_clip_seconds == base + 40

    def test_no_moments_have_no_clips_to_measure(self):
        summary = replay.summarise([], hours=3.0, tuning=tuning.recommended())

        assert (summary.moments, summary.per_hour, summary.not_extended) == (0, 0.0, 0)
        assert summary.median_clip_seconds is None and summary.longest_clip_seconds is None

    def test_chat_that_ran_for_no_time_has_no_rate(self):
        assert replay.summarise([self.moment(0)], hours=0.0, tuning=tuning.recommended()).per_hour == 0.0


class TestTheSettingsInForce:
    def in_force(self, channel: str = CHANNEL) -> tuning.Tuning:
        conn = db.get_connection()
        try:
            return replay.tuning_in_force(conn, channel)
        finally:
            conn.close()

    def set_own(self, broadcaster_user_id: int, enabled: bool = True) -> None:
        conn = db.get_connection()
        try:
            db.set_streamer_own_tuning(conn, broadcaster_user_id, enabled)
        finally:
            conn.close()

    def test_with_nothing_set_they_are_what_is_recommended(self):
        assert self.in_force() == tuning.recommended()

    def test_what_was_set_for_all_channels_holds_for_any_channel(self):
        store_tuning(db.TUNING_FOR_ALL, reacting_share=0.4)

        assert self.in_force().detector.min_reaction_unique_fraction == 0.4
        assert self.in_force("never_heard_of").detector.min_reaction_unique_fraction == 0.4

    def test_a_channels_own_hold_once_it_goes_by_them(self):
        add_streamer(CHANNEL, 7)
        store_tuning(db.TUNING_FOR_ALL, reacting_share=0.4, reacting_least=5)
        store_tuning(7, reacting_share=0.6)

        assert self.in_force().detector.min_reaction_unique_fraction == 0.4

        self.set_own(7)

        assert self.in_force().detector.min_reaction_unique_fraction == 0.6
        assert self.in_force().detector.min_absolute_unique == 5
        assert self.in_force("Some_Channel").detector.min_reaction_unique_fraction == 0.6
        assert self.in_force("another_channel").detector.min_reaction_unique_fraction == 0.4

    def test_what_is_a_channels_alone_holds_whether_or_not_it_goes_by_its_own(self):
        add_streamer(CHANNEL, 7)
        store_tuning(db.TUNING_FOR_ALL, clip_commands=("!everyone",))
        store_tuning(7, clip_commands=("!clip",), reacting_share=0.6)

        assert self.in_force().clip_commands == frozenset({"!clip"})
        assert self.in_force().detector.min_reaction_unique_fraction == detector.MIN_REACTION_UNIQUE_FRACTION
        assert self.in_force("another_channel").clip_commands == frozenset()


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
        assert "h of chat" in header
        assert columns.split() == ["moments", "per", "hour", "not", "extended", "median", "clip", "longest"]
        label, moments, per_hour, not_extended, _median, _longest = current.rsplit(maxsplit=5)
        assert (label.strip(), moments, not_extended) == ("current settings", "2", "1")
        assert float(per_hour) > 0

    def test_current_settings_are_the_ones_saved_on_the_settings_page(self, stream, capsys):
        typical_evening(stream)
        store_tuning(db.TUNING_FOR_ALL, reacting_least=50, reacting_most=50)

        _code, output = self.run(capsys)

        assert output.splitlines()[2].split()[2] == "0"

    def test_an_override_adds_a_comparison_row(self, stream, capsys):
        typical_evening(stream)
        impossible = detector.MIN_REACTION_UNIQUE_CEILING * 100

        code, output = self.run(capsys, "--set", f"MIN_ABSOLUTE_UNIQUE={impossible}")

        current, overridden = output.splitlines()[2:]
        assert code == 0
        assert current.split()[2] == "2"
        assert overridden.startswith("with overrides") and overridden.split()[2] == "0"

    def test_an_override_wins_over_what_was_saved_for_that_constant(self, stream, capsys):
        typical_evening(stream)
        store_tuning(db.TUNING_FOR_ALL, reacting_least=50, reacting_most=50)

        _code, output = self.run(capsys, "--set", f"MIN_ABSOLUTE_UNIQUE={detector.MIN_ABSOLUTE_UNIQUE}")

        current, overridden = output.splitlines()[2:]
        assert (current.split()[2], overridden.split()[2]) == ("0", "2")

    def test_a_constant_that_is_not_a_setting_can_be_overridden_too(self, stream, capsys):
        # Nobody can keep a moment open against this, so none is extended.
        typical_evening(stream)

        _code, output = self.run(capsys, "--set", "MIN_SUSTAIN_UNIQUE=999")

        current, overridden = output.splitlines()[2:]
        assert (current.split()[4], overridden.split()[4]) == ("1", "2")

    def test_overrides_do_not_leak_out_of_the_comparison(self, stream, capsys):
        typical_evening(stream)
        before = detector.MIN_ABSOLUTE_UNIQUE, detector.MIN_SUSTAIN_UNIQUE

        self.run(capsys, "--set", "MIN_ABSOLUTE_UNIQUE=999", "--set", "MIN_SUSTAIN_UNIQUE=999")

        assert (detector.MIN_ABSOLUTE_UNIQUE, detector.MIN_SUSTAIN_UNIQUE) == before
        assert detector.recommended().min_absolute_unique == before[0]

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
            stored = db.get_tuning_in_force(conn)
        finally:
            conn.close()

        assert after == before
        assert stored == {}
