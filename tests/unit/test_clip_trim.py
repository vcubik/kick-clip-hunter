"""Trimming, before anything is cut: what the file is called, what may be
asked for and where in a moment's file it lies.

The cutting itself is in tests/integration/test_clip_cutting.py, the whole
of it from the page's request on in tests/integration/test_api_dashboard.py.
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
