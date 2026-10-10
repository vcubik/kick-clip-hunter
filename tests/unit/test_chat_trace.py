"""Chat set against a clip: where a message falls on the clip's time axis,
and what is drawn from that - the trace under the clip, the spark in a
queue row, the word that says what chat said most.

Everything is built from plain lists of messages on a clock the test sets,
so each rule of the placement has a test that names it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kick_clip_hunter import chat_trace
from kick_clip_hunter.chat_trace import Timeline
from kick_clip_hunter.dashboard_view import IMPORT_REASON

# When chat saw the first frame of the clip most tests use.
CLIP_START = datetime(2026, 3, 1, 20, 0, 0, tzinfo=timezone.utc)
TIMES = chat_trace.TIMES_SIGN


def chat_at(seconds: float) -> datetime:
    """Chat's clock, `seconds` of clip time into the clip."""
    return CLIP_START + timedelta(seconds=seconds)


def message(seconds: float, content: str = "hello", sender: str = "alice") -> dict:
    return {"received_at": chat_at(seconds).isoformat(), "sender_username": sender, "content": content}


def moment_row(**overrides) -> dict:
    """A moment whose reaction ran from 20 to 30 seconds on chat's clock."""
    values = {
        "reason": "laugh",
        "window_start": chat_at(20).isoformat(),
        "window_end": chat_at(30).isoformat(),
        "clip_start": None,
        "clip_duration": None,
    }
    values.update(overrides)
    return values


def timeline_of(row: dict, **overrides) -> Timeline | None:
    # Chat is taken to run as far behind as the recorder reached back for,
    # unless a test says otherwise.
    settings = {"pre_roll": 15, "post_roll": 10, "playback_delay": 10, "chat_delay": 10}
    settings.update({"context_before": 30, "context_after": 60})
    settings.update(overrides)
    return chat_trace.clip_timeline(row, **settings)


#: A 40 second clip with 30 seconds shown before it and 60 after.
TIMELINE = Timeline(CLIP_START, 40.0, 30.0, 60.0)


class TestWhereAClipSitsAgainstChat:
    def test_a_stored_start_is_on_the_streams_clock_which_chat_runs_behind(self):
        # The clip's first frame was broadcast at 19:59:50; viewers, and so
        # chat, are taken to have seen it ten seconds later.
        row = moment_row(clip_start=(CLIP_START - timedelta(seconds=10)).isoformat(), clip_duration=38.0)

        timeline = timeline_of(row, chat_delay=10)

        assert timeline == Timeline(CLIP_START, 38.0, 30, 60)

    def test_a_shorter_delay_puts_the_same_message_later_in_the_clip(self):
        row = moment_row(clip_start=(CLIP_START - timedelta(seconds=10)).isoformat(), clip_duration=38.0)

        assumed, corrected = timeline_of(row, chat_delay=10), timeline_of(row, chat_delay=4)

        assert corrected.at(chat_at(20)) == assumed.at(chat_at(20)) + 6

    def test_with_no_delay_a_message_sits_on_the_frame_broadcast_as_it_arrived(self):
        # Which is where a streamer's own on-screen chat shows it.
        row = moment_row(clip_start=CLIP_START.isoformat(), clip_duration=38.0)

        assert timeline_of(row, chat_delay=0).at(CLIP_START + timedelta(seconds=7)) == 7

    def test_how_far_the_recorder_reached_back_does_not_move_a_clip_whose_start_is_known(self):
        row = moment_row(clip_start=CLIP_START.isoformat(), clip_duration=38.0)

        assert timeline_of(row, playback_delay=10) == timeline_of(row, playback_delay=25)

    def test_the_delay_channels_start_out_with_is_one_the_page_can_set(self):
        assert 0 <= chat_trace.CHAT_DELAY_SECONDS <= chat_trace.CHAT_DELAY_MAX_SECONDS

    def test_the_strip_runs_from_the_context_before_to_the_context_after(self):
        timeline = timeline_of(moment_row(clip_start=CLIP_START.isoformat(), clip_duration=38.0))

        assert (timeline.start, timeline.end, timeline.seconds) == (-30, 98.0, 128.0)

    def test_clip_time_counts_from_the_clips_first_frame(self):
        assert TIMELINE.at(chat_at(0)) == 0
        assert TIMELINE.at(chat_at(17.5)) == 17.5
        assert TIMELINE.at(chat_at(-12)) == -12

    def test_a_clip_whose_start_was_not_stored_is_centred_on_what_was_asked_for(self):
        # Asked for: 15s before the reaction to 10s after it, 35 seconds from
        # chat time 5. The file runs 39, so two more are assumed on each end.
        timeline = timeline_of(moment_row(clip_duration=39.0))

        assert timeline.clip_seconds == 39.0
        assert timeline.chat_start == chat_at(3)

    def test_a_clip_placed_by_its_length_follows_the_delay_too(self):
        # Its first frame is taken to have been broadcast ten seconds before
        # chat time 3 - how far the recorder reached back - and chat to have
        # seen it four seconds after that.
        timeline = timeline_of(moment_row(clip_duration=39.0), playback_delay=10, chat_delay=4)

        assert timeline.chat_start == chat_at(3 - 10 + 4)

    def test_a_clip_shorter_than_asked_for_is_centred_the_same_way(self):
        timeline = timeline_of(moment_row(clip_duration=31.0))

        assert timeline.chat_start == chat_at(7)

    def test_without_a_clip_the_window_that_would_have_been_cut_stands_in(self):
        # Chat reacted whether or not there was footage to cut.
        timeline = timeline_of(moment_row())

        assert (timeline.chat_start, timeline.clip_seconds) == (chat_at(5), 35.0)
        assert (timeline.before, timeline.after) == (30, 60)

    def test_an_imported_clip_has_its_length_but_no_place_against_chat(self):
        timeline = timeline_of(moment_row(reason=IMPORT_REASON, clip_duration=52.0))

        assert timeline == Timeline(None, 52.0)
        assert (timeline.start, timeline.end) == (0, 52.0)

    def test_an_imported_clip_of_unknown_length_has_nothing_to_draw(self):
        assert timeline_of(moment_row(reason=IMPORT_REASON)) is None


class TestMessageCounts:
    def test_each_message_is_counted_in_the_second_of_clip_time_it_arrived_in(self):
        messages = [message(0.0), message(0.9), message(1.0), message(39.99)]

        everything, _laughing = chat_trace.message_counts(messages, TIMELINE)

        # The strip starts 30 seconds before the clip, so clip second 0 is its 30th.
        assert len(everything) == 130
        assert (everything[30], everything[31], everything[69]) == (2, 1, 1)
        assert sum(everything) == 4

    def test_context_on_either_side_is_counted_too(self):
        messages = [message(-30.0), message(-0.5), message(40.0), message(99.9)]

        everything, _laughing = chat_trace.message_counts(messages, TIMELINE)

        assert (everything[0], everything[29], everything[70], everything[129]) == (1, 1, 1, 1)

    def test_what_lies_outside_the_strip_is_left_out(self):
        messages = [message(-30.5), message(100.0), message(500)]

        assert sum(chat_trace.message_counts(messages, TIMELINE)[0]) == 0

    def test_laughing_messages_are_counted_a_second_time_on_their_own(self):
        messages = [message(5, "what"), message(5, "xDDD"), message(5, "[emote:1:KEKW]"), message(6, "OMEGALUL")]

        everything, laughing = chat_trace.message_counts(messages, TIMELINE, ["omegalul"])

        assert (everything[35], everything[36]) == (3, 1)
        assert (laughing[35], laughing[36]) == (2, 1)

    def test_a_channels_emote_only_counts_as_laughing_on_that_channel(self):
        messages = [message(6, "OMEGALUL")]

        assert sum(chat_trace.message_counts(messages, TIMELINE)[1]) == 0

    def test_a_message_without_text_is_still_a_message(self):
        messages = [{"received_at": chat_at(2).isoformat(), "sender_username": None, "content": None}]

        everything, laughing = chat_trace.message_counts(messages, TIMELINE)

        assert (everything[32], laughing[32]) == (1, 0)

    def test_older_timestamps_ending_in_z_are_read_the_same(self):
        messages = [{"received_at": "2026-03-01T20:00:02.500Z", "sender_username": "a", "content": "x"}]

        assert chat_trace.message_counts(messages, TIMELINE)[0][32] == 1


class TestTrace:
    WINDOW = (chat_at(20), chat_at(30))

    def trace(self, everything=None, laughing=None, timeline=TIMELINE, usual=0.5, window=WINDOW) -> dict:
        size = 130 if timeline is TIMELINE else int(timeline.seconds)
        return chat_trace.trace(timeline, everything or [0] * size, laughing or [0] * size, usual=usual, window=window)

    def counts(self, **at_seconds: int) -> list[int]:
        """A quiet strip with the given counts at the given clip seconds ("s25=9")."""
        values = [0] * 130
        for name, count in at_seconds.items():
            values[30 + int(name[1:])] = count
        return values

    def test_says_what_stretch_of_clip_time_it_covers(self):
        strip = self.trace()

        assert (strip["start"], strip["end"], strip["clip_seconds"], strip["width"]) == ("-30", "100", "40", "130")

    def test_the_height_stands_for_at_least_a_quiet_chats_dozen(self):
        assert self.trace(self.counts(s25=3))["top"] == chat_trace.TRACE_MIN_TOP

    def test_and_grows_with_the_peak_in_steps_of_the_grid(self):
        assert self.trace(self.counts(s25=13))["top"] == 14
        assert self.trace(self.counts(s25=23))["top"] == 25
        assert self.trace(self.counts(s25=61))["top"] == 70

    def test_the_line_of_all_messages_runs_through_every_second(self):
        strip = self.trace(self.counts(s0=3, s1=12))

        points = strip["all"].removeprefix("M").split(" L")
        assert len(points) == 130
        # Second 0 of the clip is drawn at the middle of the strip's 30th second.
        assert (points[0], points[30], points[31]) == ("0.5,12", "30.5,9", "31.5,0")

    def test_the_laughing_line_is_only_drawn_where_someone_laughed(self):
        laughing = self.counts(s10=2, s11=5, s12=1, s25=4)

        strip = self.trace(self.counts(s10=3, s11=6, s12=2, s25=6), laughing)

        # Two strokes, each one second out on either side of the laughing.
        assert strip["laugh"] == "M39.5,12 L40.5,10 L41.5,7 L42.5,11 L43.5,12 M54.5,12 L55.5,8 L56.5,12"
        assert strip["wash"] == (
            "M39.5,12 L39.5,12 L40.5,10 L41.5,7 L42.5,11 L43.5,12 L43.5,12 Z "
            "M54.5,12 L54.5,12 L55.5,8 L56.5,12 L56.5,12 Z"
        )

    def test_with_no_laughing_there_is_no_laughing_line(self):
        strip = self.trace(self.counts(s10=3))

        assert (strip["laugh"], strip["wash"]) == ("", "")

    def test_laughing_at_the_very_edges_of_the_strip_stays_inside_it(self):
        laughing = [0] * 130
        laughing[0] = laughing[129] = 2

        strip = self.trace(laughing, laughing)

        assert strip["laugh"] == "M0.5,10 L1.5,12 M128.5,12 L129.5,10"

    def test_the_grid_has_a_heavy_line_every_ten_seconds_of_clip_time(self):
        strip = self.trace()

        heavy = strip["grid_strong"].split("M")[1:]
        # Clip times -20, -10, 0 ... 90: counted from the clip's start, so
        # they fall on round clip times, and none on the strip's own edges.
        assert heavy == [f"{x},0V12" for x in range(10, 130, 10)]
        assert "M2,0V12" in strip["grid"] and "M10,0V12" not in strip["grid"]

    def test_the_grid_has_a_fine_line_every_couple_of_messages_a_second(self):
        assert [f"M0,{level}H130" in self.trace()["grid"] for level in (2, 4, 10, 12, 1)] == [
            True,
            True,
            True,
            False,
            False,
        ]
        # A tall chart gets fewer, wider-spaced lines rather than a wall of them.
        tall = self.trace(self.counts(s25=61))["grid"]
        assert "M0,10H130" in tall and "M0,60H130" in tall and "M0,2H130" not in tall

    def test_the_context_either_side_of_the_clip_is_marked_off(self):
        strip = self.trace()

        assert strip["context"] == [{"left": "0", "width": "23.077"}, {"left": "53.846", "width": "46.154"}]
        assert strip["clip_left"] == "23.077"

    def test_the_usual_pace_is_a_height_on_the_same_scale(self):
        strip = self.trace(usual=3.0)

        assert (strip["usual"], strip["usual_height"]) == ("3", "25")

    def test_the_moment_is_a_band_with_a_note_beside_it(self):
        strip = self.trace(self.counts(s22=4, s25=9, s31=20))

        band = strip["moment"]
        # Clip seconds 20 to 30 of a strip that starts at -30 and is 130 long.
        assert (band["left"], band["width"]) == ("38.462", "7.692")
        # The peak is the moment's own, not the strip's.
        assert band["note"] == "the moment: 10 s, peaking at 9/s"
        assert band["note_left"] == "46.154" and "note_right" not in band

    def test_the_note_moves_to_the_other_side_near_the_strips_right_end(self):
        late = (chat_at(70), chat_at(80))

        band = self.trace(window=late)["moment"]

        assert band["note_right"] == "23.077" and "note_left" not in band

    def test_a_moment_with_no_chat_in_it_is_noted_without_a_peak(self):
        assert self.trace()["moment"]["note"] == "the moment: 10 s"

    def test_a_moment_reaching_past_the_strip_is_cut_at_its_edge(self):
        band = self.trace(window=(chat_at(90), chat_at(200)))["moment"]

        assert (band["left"], band["width"]) == ("92.308", "7.692")

    def test_times_are_written_under_it(self):
        axis = [(label["words"], label["left"], label["align"]) for label in self.trace()["axis"]]

        assert axis == [
            ("30 s before", "0", "start"),
            ("0:00", "23.077", "start"),
            ("0:10", "30.769", "middle"),
            ("0:20", "38.462", "middle"),
            ("0:30", "46.154", "middle"),
            ("0:40", "53.846", "start"),
            ("60 s after", "100", "end"),
        ]

    def test_a_step_that_would_crowd_the_clips_end_is_left_out(self):
        timeline = Timeline(CLIP_START, 38.0, 30.0, 60.0)

        words = [label["words"] for label in self.trace(timeline=timeline)["axis"]]

        assert words == ["30 s before", "0:00", "0:10", "0:20", "0:30", "0:38", "60 s after"]
        assert [label["words"] for label in self.trace(timeline=Timeline(CLIP_START, 33.0, 30.0, 60.0))["axis"]][
            3:5
        ] == ["0:20", "0:33"]

    def test_a_long_strip_is_labelled_in_wider_steps(self):
        timeline = Timeline(CLIP_START, 95.0, 30.0, 60.0)

        words = [label["words"] for label in self.trace(timeline=timeline)["axis"]]

        assert words == ["30 s before", "0:00", "0:20", "0:40", "1:00", "1:20", "1:35", "60 s after"]

    def test_it_is_described_in_a_sentence(self):
        strip = self.trace(self.counts(s25=9))

        assert strip["label"] == (
            "Chat messages a second from 30 seconds before the clip to 60 seconds after it. "
            "The usual pace is 0.5 a second; it peaks at 9 a second during the 10 second moment."
        )

    def test_the_counts_come_along_for_reading_one_second_off(self):
        strip = self.trace(self.counts(s0=3), self.counts(s0=1))

        assert strip["all_counts"].split(",")[29:32] == ["0", "3", "0"]
        assert strip["laugh_counts"].split(",")[30] == "1"

    def test_an_imported_clip_gets_bare_paper_over_just_the_clip(self):
        strip = chat_trace.trace(Timeline(None, 52.0), [], [], usual=0.0, window=None)

        assert strip["has_chat"] is False
        assert (strip["start"], strip["end"], strip["context"], strip["moment"]) == ("0", "52", [], None)
        assert [label["words"] for label in strip["axis"]] == ["0:00", "0:10", "0:20", "0:30", "0:40", "0:52"]
        assert strip["axis"][-1]["align"] == "end"
        assert strip["label"] == "No chat was measured for this clip."
        assert "all" not in strip and "usual" not in strip


class TestChatReplay:
    WINDOW = (chat_at(20), chat_at(30))

    def test_every_line_carries_the_clip_time_it_belongs_to(self):
        messages = [message(-12.34, "before"), message(0, "start"), message(17.56, "during")]

        lines = chat_trace.chat_replay(messages, TIMELINE, self.WINDOW)

        assert [line["at"] for line in lines] == ["-12.3", "0", "17.6"]

    def test_lines_sent_during_the_moment_are_marked(self):
        padding = chat_trace.MOMENT_PADDING_SECONDS
        messages = [
            message(20 - padding - 0.5, "too early"),
            message(20 - padding / 2, "just before"),
            message(25, "in it"),
            message(30 + padding / 2, "just after"),
            message(30 + padding + 0.5, "too late"),
        ]

        lines = chat_trace.chat_replay(messages, TIMELINE, self.WINDOW)

        assert [line["in_moment"] for line in lines] == [False, True, True, True, False]

    def test_a_line_has_who_said_it_their_colour_and_what_they_said(self):
        (line,) = chat_trace.chat_replay([message(5, "no way [emote:7:KEKW]", "bob")], TIMELINE, self.WINDOW)

        assert (line["nick"], line["colour"]) == ("bob", chat_trace.nick_colour("bob"))
        assert [part.get("text") or part["emote"] for part in line["parts"]] == ["no way ", "KEKW"]

    def test_the_channels_7tv_emotes_are_emotes_in_a_line_too(self):
        emotes = {"KEKW": ("ID1", 32, 32)}

        (line,) = chat_trace.chat_replay([message(5, "no way KEKW", "bob")], TIMELINE, self.WINDOW, emotes)

        assert [part.get("text") or part["emote"] for part in line["parts"]] == ["no way ", "KEKW"]
        assert line["parts"][1]["image"] == "https://cdn.7tv.app/emote/ID1/2x.webp"

    def test_a_known_chatter_has_their_own_colour_and_badges(self):
        identities = {"bob": ("#9ad8ff", '[{"text":"Moderator","type":"moderator"}]')}
        messages = [message(5, "hello", "bob"), message(6, "hi", "carol")]

        known, unknown = chat_trace.chat_replay(messages, TIMELINE, self.WINDOW, None, identities)

        assert known["own_colour"] == "#9ad8ff"
        assert known["badges"] == [{"icon": "moderator", "words": "Moderator"}]
        assert (unknown["own_colour"], unknown["badges"]) == (None, [])
        # The colour worked out from the name is still there to fall back on.
        assert unknown["colour"] == chat_trace.nick_colour("carol")

    def test_a_colour_too_dark_for_the_page_is_drawn_lighter(self):
        identities = {"bob": ("#00008b", "[]")}

        (line,) = chat_trace.chat_replay([message(5, "hello", "bob")], TIMELINE, self.WINDOW, None, identities)

        assert line["own_colour"] == chat_trace.chat_identity.readable("#00008b") != "#00008b"

    def test_a_known_chatter_without_a_colour_keeps_the_worked_out_one(self):
        identities = {"bob": (None, '[{"text":"VIP","type":"vip"}]')}

        (line,) = chat_trace.chat_replay([message(5, "hello", "bob")], TIMELINE, self.WINDOW, None, identities)

        assert line["own_colour"] is None
        assert line["badges"] == [{"icon": "vip", "words": "VIP"}]

    def test_a_message_from_nobody_with_nothing_in_it_still_makes_a_line(self):
        nothing = {"received_at": chat_at(1).isoformat(), "sender_username": None, "content": None}

        (line,) = chat_trace.chat_replay([nothing], TIMELINE, None)

        assert (line["nick"], line["parts"], line["in_moment"]) == ("", [], False)


class TestSparks:
    WINDOW_START = chat_at(20)

    def test_a_spark_starts_a_little_before_the_moment_does(self):
        start, end = chat_trace.spark_span(self.WINDOW_START)

        assert start == self.WINDOW_START - timedelta(seconds=chat_trace.SPARK_LEAD_SECONDS)
        assert (end - start).total_seconds() == chat_trace.SPARK_STEPS * chat_trace.SPARK_STEP_SECONDS

    def test_messages_are_counted_in_steps_of_a_few_seconds(self):
        lead, step = chat_trace.SPARK_LEAD_SECONDS, chat_trace.SPARK_STEP_SECONDS
        messages = [
            message(20 - lead, "first step"),
            message(20 - lead + step - 0.1, "still the first"),
            message(20, "xDDD"),  # the step the moment's window starts in
            message(20 + 0.5, "ha"),
            message(20 - lead - 0.1, "before the spark"),
            message(20 - lead + chat_trace.SPARK_STEPS * step, "after it"),
        ]

        everything, laughing = chat_trace.spark_counts(messages, self.WINDOW_START)

        assert len(everything) == chat_trace.SPARK_STEPS
        assert (everything[0], everything[lead // step], sum(everything)) == (2, 2, 4)
        assert (laughing[lead // step], sum(laughing)) == (1, 1)

    def test_every_spark_on_a_page_is_drawn_to_the_biggest_one(self):
        assert chat_trace.spark_top([[1, 30, 2], [5, 5, 5]]) == 30

    def test_but_a_page_of_quiet_moments_is_not_blown_up(self):
        assert chat_trace.spark_top([[1, 2, 1], [0, 0, 0]]) == chat_trace.SPARK_MIN_TOP
        assert chat_trace.spark_top([]) == chat_trace.SPARK_MIN_TOP

    def test_the_lines_stay_a_unit_clear_of_the_wells_edges(self):
        everything = [0, 8] + [0] * 14

        paths = chat_trace.spark_paths(everything, [0] * 16, top=8)

        points = paths["all"].removeprefix("M").split(" L")
        # Nothing sits a unit above the floor, the tallest a unit below the top.
        assert (points[0], points[1]) == ("0.5,17", "1.5,1")
        assert len(points) == chat_trace.SPARK_STEPS
        assert paths["laugh"] == ""

    def test_the_laughing_line_shares_the_scale_and_the_floor(self):
        paths = chat_trace.spark_paths([0, 4, 8, 4] + [0] * 12, [0, 0, 4, 0] + [0] * 12, top=8)

        assert paths["laugh"] == "M1.5,17 L2.5,9 L3.5,17"


class TestWhatChatSaidMost:
    def said(self, *contents: str, emotes=()) -> str | None:
        return chat_trace.reaction_words([message(0, content) for content in contents], emotes)

    def test_an_emote_posted_by_many_wins(self):
        assert (
            self.said("[emote:1:KEKW]", "[emote:1:KEKW][emote:1:KEKW]", "no way", "[emote:1:KEKW]") == f"KEKW {TIMES}3"
        )

    def test_stacking_it_in_one_message_counts_once(self):
        assert self.said("[emote:1:KEKW][emote:1:KEKW][emote:1:KEKW]", "hm") is None

    def test_every_form_of_the_typed_laugh_is_the_same_word(self):
        assert self.said("xd", "XDDDD", "to je konec xDD", "xD!!") == f"xD {TIMES}4"

    def test_a_channels_own_emote_counts_inside_a_sentence_too(self):
        said = self.said("KEKW", "on spadl KEKW", "kekw kekw", "kdo to byl", emotes=["kekw"])

        assert said == f"KEKW {TIMES}3"

    def test_it_is_written_the_way_most_of_chat_wrote_it(self):
        assert self.said("pog", "POG", "POG", "POG POG") == f"POG {TIMES}4"

    def test_a_reaction_sent_on_its_own_counts_whatever_it_is(self):
        assert self.said("W", "W W W", "W", "what a play") == f"W {TIMES}3"
        assert self.said("???", "???", "?") == f"??? {TIMES}2"

    def test_ordinary_words_inside_sentences_never_win(self):
        assert self.said("to je konec", "to snad ne", "to bylo dobry", "to to to") is None

    def test_something_said_only_once_is_not_worth_naming(self):
        assert self.said("KEKW", "hm", "ok", emotes=["kekw"]) is None

    def test_silence_has_nothing_to_name(self):
        assert self.said() is None
        assert chat_trace.reaction_words([{"received_at": chat_at(0).isoformat(), "content": None}]) is None

    def test_the_most_common_of_several_reactions_is_the_one_named(self):
        said = self.said("xD", "[emote:1:KEKW]", "xDDD", "[emote:1:KEKW]", "xd", "W")

        assert said == f"xD {TIMES}3"


class TestAQueueRowsActivity:
    WINDOW = (chat_at(20), chat_at(30))

    def test_gives_the_spark_and_what_was_said_while_the_moment_lasted(self):
        messages = [
            message(10, "W"),
            message(11, "W"),
            message(12, "W"),  # before the moment: in the spark, not in the word
            message(21, "KEKW"),
            message(22, "KEKW"),
            message(40, "W"),
        ]

        activity = chat_trace.queue_activity(messages, self.WINDOW, ["kekw", "sadge"])

        assert sum(activity["all"]) == 6
        assert sum(activity["laugh"]) == 2
        assert activity["said"] == f"KEKW {TIMES}2"

    def test_a_quiet_moment_has_a_flat_spark_and_no_word(self):
        activity = chat_trace.queue_activity([], self.WINDOW)

        assert activity == {"all": [0] * chat_trace.SPARK_STEPS, "laugh": [0] * chat_trace.SPARK_STEPS, "said": None}


def test_the_grid_step_keeps_the_lines_to_a_handful():
    for peak in (1, 12, 16, 17, 40, 41, 80, 200, 400, 5000):
        step = chat_trace._grid_step(peak)
        assert peak / step <= 8 or step == 100, (peak, step)


@pytest.mark.parametrize(("seconds", "words"), [(0, "0:00"), (9.9, "0:09"), (38, "0:38"), (95.5, "1:35"), (-3, "0:00")])
def test_clip_time_is_written_as_minutes_and_seconds(seconds, words):
    assert chat_trace._clock(seconds) == words
