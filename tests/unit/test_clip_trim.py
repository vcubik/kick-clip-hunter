"""Trimming, before anything is cut: what the file is called, what may be
asked for and where in a moment's file, or a recording's, it lies.

The cutting itself is in tests/integration/test_clip_cutting.py, the whole
of it from the page's request on in tests/integration/test_api_dashboard.py
(a moment) and tests/integration/test_api_recordings_page.py (a recording).
"""

from __future__ import annotations

import math

import pytest

from kick_clip_hunter import clip_trim
from kick_clip_hunter.clip_trim import TrimError


def row(clip: float = 40.0, before: float | None = 30.0, after: float | None = 60.0) -> dict:
    """What is stored of a moment whose file holds a clip that long with
    that much footage either side of it."""
    return {"clip_duration": clip, "context_before": before, "context_after": after}


class TestName:
    def test_the_video_is_named_after_the_clip_it_was_cut_from(self):
        assert clip_trim.video_name("moment_12.mp4") == "moment_12_trim.mp4"


class TestWhatIsKeptOfACut:
    """Where a cut video begins in its file and how long it runs, kept
    beside it so that its chat can be made later."""

    def test_it_is_named_after_the_video(self):
        assert clip_trim.record_name("moment_12_trim.mp4") == "moment_12_trim.json"

    def test_it_reads_back_as_it_was_written(self):
        assert clip_trim.read_cut_record(clip_trim.cut_record(4.0, 6.25, 10.2)) == (4.0, 6.25, 10.2)

    def test_one_kept_without_its_end_ends_where_the_video_does(self):
        assert clip_trim.read_cut_record('{"begins": 4, "duration": 6.25}') == (4.0, 6.25, 10.25)

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "not json",
            "[4, 6]",
            '{"begins": 4}',
            '{"begins": "4", "duration": 6}',
            '{"begins": -1, "duration": 6}',
            '{"begins": 4, "duration": 0}',
            '{"begins": 4, "duration": Infinity}',
            '{"begins": 4, "duration": 6, "end": 3}',
        ],
        ids=["empty", "text", "list", "no-duration", "string", "negative", "no-length", "infinite", "backwards"],
    )
    def test_anything_else_is_not_one(self, text):
        assert clip_trim.read_cut_record(text) is None


class TestFootage:
    def test_it_reaches_as_far_as_the_context_either_side_of_the_clip(self):
        assert clip_trim.footage(row()) == (-30.0, 100.0)

    def test_a_file_that_is_the_clip_alone_has_only_the_clip(self):
        assert clip_trim.footage(row(before=None, after=None)) == (0.0, 40.0)


class TestWhereInTheFile:
    def test_clip_time_is_counted_from_where_the_clip_begins_in_its_file(self):
        assert clip_trim.file_span(row(), 0, 40) == (30.0, 70.0)

    def test_a_stretch_can_reach_into_the_footage_either_side(self):
        assert clip_trim.file_span(row(), -12.5, 55.25) == (17.5, 85.25)

    def test_all_of_the_footage_is_all_of_the_file(self):
        assert clip_trim.file_span(row(), -30, 100) == (0.0, 130.0)

    def test_a_file_without_context_is_counted_from_its_first_frame(self):
        assert clip_trim.file_span(row(before=None, after=None), 2, 9.5) == (2.0, 9.5)

    def test_a_time_just_past_an_end_of_the_footage_is_taken_as_that_end(self):
        slack = clip_trim.SLACK_SECONDS

        assert clip_trim.file_span(row(), -30 - slack, 100 + slack) == (0.0, 130.0)

    @pytest.mark.parametrize(("start", "end"), [(-32, 10), (0, 102), (-200, 300)])
    def test_a_stretch_beyond_the_footage_is_refused(self, start, end):
        with pytest.raises(TrimError, match="not in the footage"):
            clip_trim.file_span(row(), start, end)

    @pytest.mark.parametrize(("start", "end"), [(5, 5), (5, 5.5), (9, 4)])
    def test_a_stretch_shorter_than_the_shortest_is_refused(self, start, end):
        with pytest.raises(TrimError, match="at least"):
            clip_trim.file_span(row(), start, end)

    def test_the_shortest_stretch_is_long_enough(self):
        assert clip_trim.file_span(row(), 5, 5 + clip_trim.MIN_SECONDS) == (35.0, 35.0 + clip_trim.MIN_SECONDS)

    @pytest.mark.parametrize("value", [None, "12", True, math.nan, math.inf, [1], {}])
    def test_what_is_not_a_number_of_seconds_is_refused(self, value):
        with pytest.raises(TrimError, match="start must be a number"):
            clip_trim.file_span(row(), value, 20)
        with pytest.raises(TrimError, match="end must be a number"):
            clip_trim.file_span(row(), 0, value)


class TestMovingChat:
    @pytest.mark.parametrize(("asked", "shift"), [(None, 0.0), (0, 0.0), (2.5, 2.5), (-7, -7.0)])
    def test_chat_is_moved_by_what_was_asked_for(self, asked, shift):
        assert clip_trim.chat_shift(asked) == shift

    def test_as_far_as_the_limit_either_way(self):
        limit = clip_trim.CHAT_SHIFT_LIMIT_SECONDS

        assert (clip_trim.chat_shift(limit), clip_trim.chat_shift(-limit)) == (limit, -limit)

    @pytest.mark.parametrize("sign", [1, -1])
    def test_and_no_further(self, sign):
        with pytest.raises(TrimError, match="at most"):
            clip_trim.chat_shift(sign * (clip_trim.CHAT_SHIFT_LIMIT_SECONDS + 0.5))

    @pytest.mark.parametrize("value", ["3", False, math.nan])
    def test_what_is_not_a_number_of_seconds_is_refused(self, value):
        with pytest.raises(TrimError, match="chat_shift must be a number"):
            clip_trim.chat_shift(value)


class TestWhatIsBehindChat:
    @pytest.mark.parametrize("asked", [None, ""])
    def test_nothing_unless_a_colour_was_asked_for(self, asked):
        assert clip_trim.chat_background(asked) is None

    def test_a_colour_is_kept_as_it_is_written_in_a_stylesheet(self):
        assert clip_trim.chat_background("#1A2b3C") == "#1a2b3c"

    @pytest.mark.parametrize(
        "value", ["red", "#fff", "#12345g", "#1234567", "1a2b3c", "#1a2b3c; color: red", " #1a2b3c", 0x1A2B3C, True]
    )
    def test_anything_else_is_refused(self, value):
        with pytest.raises(TrimError, match="chat_background must be a colour"):
            clip_trim.chat_background(value)

    def test_what_the_dialog_starts_out_with_is_such_a_colour(self):
        assert clip_trim.chat_background(clip_trim.DEFAULT_CHAT_BACKGROUND) == clip_trim.DEFAULT_CHAT_BACKGROUND


class TestAStretchOfAnyFootage:
    """What a moment's stretch is checked by, on footage that is simply
    there from one second to another - a recording's file."""

    def test_it_is_the_stretch_that_was_asked_for(self):
        assert clip_trim.stretch(0.0, 7200.0, 5025.5, 5070) == (5025.5, 5070.0)

    def test_a_time_just_past_an_end_of_the_footage_is_taken_as_that_end(self):
        slack = clip_trim.SLACK_SECONDS

        assert clip_trim.stretch(0.0, 7200.0, -slack, 7200 + slack) == (0.0, 7200.0)

    @pytest.mark.parametrize(("start", "end"), [(-2, 10), (7000, 7202), (9000, 9100)])
    def test_one_beyond_the_footage_is_refused(self, start, end):
        with pytest.raises(TrimError, match="not in the footage"):
            clip_trim.stretch(0.0, 7200.0, start, end)

    @pytest.mark.parametrize(("start", "end"), [(60, 60), (60, 60.5), (90, 30)])
    def test_one_shorter_than_the_shortest_is_refused(self, start, end):
        with pytest.raises(TrimError, match="at least"):
            clip_trim.stretch(0.0, 7200.0, start, end)

    def test_what_is_not_a_number_of_seconds_is_refused(self):
        with pytest.raises(TrimError, match="start must be a number"):
            clip_trim.stretch(0.0, 7200.0, "1:23:45", 5070)


class TestChatGoingWithAStretch:
    def test_a_stretch_as_long_as_the_longest_can_have_its_chat_as_a_video(self):
        clip_trim.with_chat(clip_trim.CHAT_VIDEO_MAX_SECONDS)

    def test_a_longer_one_is_refused_and_told_how_long_it_may_be(self):
        with pytest.raises(TrimError) as refused:
            clip_trim.with_chat(25.5 * 60)

        assert str(refused.value).endswith("a stretch of up to 10 minutes, and this one is 25.5 minutes long")

    def test_the_lines_a_chat_video_starts_out_with_are_looked_for_far_enough_back(self):
        assert clip_trim.CHAT_VIDEO_LEAD_SECONDS >= 60


class TestTheCutOfARecording:
    """A recording's cut is named for the stretch it holds: there is more
    than one to be had of a stream of hours."""

    RECORDING = "2026-03-01_20-00_some_channel_7.mp4"

    def test_it_is_named_after_the_recording_and_the_stretch(self):
        name = clip_trim.recording_video_name(self.RECORDING, 5025.4, 5070.6)

        assert name == "2026-03-01_20-00_some_channel_7_trim_1h23m45s-1h24m31s.mp4"

    def test_a_stretch_in_the_first_hour_is_written_the_same_way(self):
        assert clip_trim.recording_video_name(self.RECORDING, 0, 59.5) == (
            "2026-03-01_20-00_some_channel_7_trim_0h00m00s-0h01m00s.mp4"
        )

    @pytest.mark.parametrize(("start", "end"), [(0, 30), (5025, 5071), (36000, 43199), (359999, 360000)])
    def test_the_name_says_which_stretch_it_holds(self, start, end):
        name = clip_trim.recording_video_name(self.RECORDING, start, end)

        assert clip_trim.recording_stretch(self.RECORDING, name) == (start, end)

    @pytest.mark.parametrize(
        "name",
        [
            RECORDING,
            # Its chat video, and a cut that is not finished yet.
            "2026-03-01_20-00_some_channel_7_trim_1h23m45s-1h24m31s_chat.mov",
            "2026-03-01_20-00_some_channel_7_trim_1h23m45s-1h24m31s.part.mp4",
            # Another recording's, whose name only begins the same.
            "2026-03-01_20-00_some_channel_71_trim_0h00m00s-0h00m30s.mp4",
            "2026-03-01_20-00_some_channel_7_trim.mp4",
            "2026-03-01_20-00_some_channel_7_trim_1h2m3s-1h2m9s.mp4",
            "notes.txt",
        ],
    )
    def test_any_other_file_is_not_a_cut_of_the_recording(self, name):
        assert clip_trim.recording_stretch(self.RECORDING, name) is None

    def test_a_recording_whose_name_has_signs_in_it_is_matched_as_it_is_written(self):
        recording = "a file (1) [x].mp4"
        name = clip_trim.recording_video_name(recording, 10, 20)

        assert clip_trim.recording_stretch(recording, name) == (10, 20)
        assert clip_trim.recording_stretch("a file (2) [x].mp4", name) is None
