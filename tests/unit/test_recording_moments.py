"""Moments found in a recording after the fact: where a replayed moment
lies in the recording's file (recording_moments.py).

The replaying itself is tests/unit/test_replay_chat.py's, the cutting
tests/integration/test_clip_cutting.py's, and the two put together behind
the Recordings page tests/integration/test_api_recording_moments.py's. Here
it is the arithmetic between them, on made-up moments and parts.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from kick_clip_hunter import detector, recording_moments, tuning
from kick_clip_hunter.replay import ReplayedMoment
from kick_clip_hunter.stream_recording import Part

T0 = datetime(2026, 3, 1, 20, 0, 0, tzinfo=timezone.utc)
WINDOW = detector.SHORT_WINDOW_SECONDS
SPIKE = detector.Spike(
    reasons=["laugh"],
    score=6.5,
    message_count=14,
    baseline_message_rate=0.4,
    current_message_rate=1.4,
    emote_count=2,
    keyword_hits=9,
)


def framed(**changed) -> tuning.Tuning:
    """Settings whose clips are easy to do sums on: 15 s before the
    reaction and 10 s after it, cut 10 s earlier than chat reacted, with 30 s
    of context before and 60 s after."""
    framing = {
        "pre_roll_seconds": 15.0,
        "post_roll_seconds": 10.0,
        "playback_delay_seconds": 10.0,
        "context_before_seconds": 30.0,
        "context_after_seconds": 60.0,
    }
    return replace(tuning.recommended(), **{**framing, **changed})


def fired(second: float, extension: float = 0.0, spike: detector.Spike | None = SPIKE) -> ReplayedMoment:
    """A replayed moment that fired `second` seconds after T0 on chat's
    clock."""
    time = (T0 + timedelta(seconds=second)).timestamp()
    return ReplayedMoment(time=time, reasons=("laugh",), score=6.5, extension_seconds=extension, spike=spike)


def one_part(seconds: float = 3600.0) -> list[Part]:
    return [Part(T0, seconds)]


def found_in(parts: list[Part], *moments: ReplayedMoment, **changed) -> list[recording_moments.Found]:
    return recording_moments.find(moments, parts, framed(**changed))[0]


class TestWhichChatIsReplayed:
    def test_it_reaches_the_detectors_baseline_past_either_end_of_the_recording(self):
        parts = [Part(T0, 600.0), Part(T0 + timedelta(seconds=900), 300.0)]
        around = timedelta(seconds=detector.BASELINE_WINDOW_SECONDS)

        assert recording_moments.chat_span(parts) == (T0 - around, T0 + timedelta(seconds=1200) + around)


class TestWhereAMomentLiesInTheFile:
    def test_its_clip_is_framed_as_the_recorder_frames_one(self):
        # Chat reacted from 590 s to 600 s. The clip reaches 15 s further back
        # and 10 s further on, and all of it is 10 s earlier on the stream's
        # clock - which, in a recording that began at T0, is the file's.
        (found,) = found_in(one_part(), fired(600))

        assert found.clip == (pytest.approx(565), pytest.approx(600))
        assert found.clip_seconds == pytest.approx(35)
        assert found.clip_started_at == T0 + timedelta(seconds=565)

    def test_its_file_has_the_context_either_side_of_the_clip(self):
        (found,) = found_in(one_part(), fired(600))

        assert found.footage == (pytest.approx(535), pytest.approx(660))

    def test_a_reaction_that_went_on_makes_the_clip_that_much_longer(self):
        (found,) = found_in(one_part(), fired(600, extension=24))

        assert found.clip == (pytest.approx(565), pytest.approx(624))

    def test_it_is_stored_with_when_its_reaction_ran_and_what_the_detector_measured(self):
        (found,) = found_in(one_part(), fired(600, extension=24))

        assert found.window_start == T0 + timedelta(seconds=600 - WINDOW)
        assert found.window_end == T0 + timedelta(seconds=624)
        assert found.fired_at == T0 + timedelta(seconds=600)
        assert found.spike is SPIKE

    def test_the_channels_own_framing_is_what_frames_it(self):
        (found,) = found_in(
            one_part(),
            fired(600),
            pre_roll_seconds=5.0,
            post_roll_seconds=20.0,
            playback_delay_seconds=0.0,
            context_before_seconds=10.0,
            context_after_seconds=0.0,
        )

        assert found.clip == (pytest.approx(585), pytest.approx(620))
        assert found.footage == (pytest.approx(575), pytest.approx(620))

    def test_a_recording_that_began_mid_stream_counts_from_its_own_first_frame(self):
        began = T0 + timedelta(seconds=500)

        (found,) = found_in([Part(began, 600.0)], fired(600))

        assert found.clip == (pytest.approx(65), pytest.approx(100))
        assert found.clip_started_at == T0 + timedelta(seconds=565)

    def test_moments_come_in_the_order_they_fired_each_with_a_key_of_its_own(self):
        found = found_in(one_part(), fired(600), fired(900), fired(1500.25))

        assert [one.clip[0] for one in found] == sorted(one.clip[0] for one in found)
        assert len({one.key for one in found}) == 3
        assert found[2].key == round((T0 + timedelta(seconds=1500.25)).timestamp() * 1000)

    def test_the_same_moment_has_the_same_key_every_time(self):
        assert found_in(one_part(), fired(600))[0].key == found_in(one_part(), fired(600))[0].key


class TestTheEndsOfTheRecording:
    def test_context_stops_where_the_footage_does(self):
        early, late = found_in(one_part(700.0), fired(60), fired(680))

        # Their clips are whole; there is just less around them.
        assert early.clip == (pytest.approx(25), pytest.approx(60)) and early.footage[0] == 0.0
        assert late.clip == (pytest.approx(645), pytest.approx(680)) and late.footage[1] == 700.0

    def test_a_clip_the_recording_has_most_of_is_cut_as_far_as_the_footage_goes(self):
        # The clip would be 5 s to 40 s before the first frame to 30 s after it.
        (found,) = found_in(one_part(), fired(30))

        assert found.clip == (0.0, pytest.approx(30))
        assert found.clip_started_at == T0

    def test_one_it_has_too_little_of_is_not_found_but_counted(self):
        # 10 s of 35: the recording began in the middle of what chat laughed at.
        found, missing = recording_moments.find([fired(10)], one_part(), framed())

        assert found == [] and missing == 1

    def test_the_same_at_its_last_frame(self):
        found, missing = recording_moments.find([fired(3625)], one_part(3600.0), framed())

        assert found == [] and missing == 1

    def test_one_that_lies_outside_the_recording_altogether_is_not_even_counted(self):
        # The chat replayed reaches five minutes past either end.
        before, after = fired(-200), fired(3600 + 200)

        assert recording_moments.find([before, after], one_part(3600.0), framed()) == ([], 0)

    def test_what_decides_is_the_share_of_the_clip_the_recording_has(self):
        share = recording_moments.MIN_CLIP_SHARE
        # A clip of 35 s that the first frame cuts into at `into` seconds.
        enough = fired(35 - 35 * share + 1)
        too_little = fired(35 - 35 * share - 1)

        found, missing = recording_moments.find([too_little, enough], one_part(), framed())

        assert len(found) == 1 and missing == 1


class TestBreaksInTheStream:
    # Ten minutes, then - five minutes later - twenty more: second 600 of
    # the file was on air 900 s after the stream began.
    PARTS = (Part(T0, 600.0), Part(T0 + timedelta(seconds=900), 1200.0))

    def test_a_moment_after_a_break_is_where_its_part_is_in_the_file(self):
        (found,) = found_in(self.PARTS, fired(1500))

        # 1465 s on the stream's clock is 565 s into the second part.
        assert found.clip == (pytest.approx(600 + 565), pytest.approx(600 + 600))
        assert found.clip_started_at == T0 + timedelta(seconds=1465)

    def test_nothing_is_cut_across_a_break(self):
        # One clip begins 5 s after the stream came back; the other would
        # have run on for 10 s after it went.
        (after,) = found_in(self.PARTS, fired(900 + 40))
        (before,) = found_in(self.PARTS, fired(610))

        # Its context reaches back to the break, and no further.
        assert after.clip == (pytest.approx(605), pytest.approx(640)) and after.footage[0] == 600.0
        # The other ends where its part does, with nothing after it.
        assert before.clip == (pytest.approx(575), 600.0)
        assert before.footage == (pytest.approx(545), 600.0)

    def test_a_clip_is_kept_to_the_part_that_holds_more_of_it(self):
        # From 20 s before the break to 15 s after it.
        parts = [Part(T0, 600.0), Part(T0 + timedelta(seconds=600.5), 1200.0)]

        (found,) = found_in(parts, fired(615))

        assert found.clip == (pytest.approx(580), 600.0)
        assert found.footage[1] == 600.0

    def test_a_moment_from_while_nothing_was_recorded_is_counted_as_missing(self):
        found, missing = recording_moments.find([fired(750)], self.PARTS, framed())

        assert found == [] and missing == 1

    def test_a_recording_with_no_footage_has_no_moments(self):
        assert recording_moments.find([fired(600)], [], framed()) == ([], 0)


class TestWhatIsLeftOut:
    def test_a_moment_the_replay_did_not_measure_is_not_one_to_store(self):
        assert recording_moments.find([fired(600, spike=None)], one_part(), framed()) == ([], 0)


class TestWhatTheCutFileHolds:
    def found(self) -> recording_moments.Found:
        (found,) = found_in(one_part(), fired(600))
        return found  # the clip from 565 to 600, footage from 535 to 660

    def test_cut_exactly_as_asked_it_is_the_context_the_clip_and_the_context(self):
        assert recording_moments.extent(self.found(), 535.0, 125.0) == (
            pytest.approx(30),
            pytest.approx(35),
            pytest.approx(60),
        )

    def test_a_cut_that_begins_earlier_has_that_much_more_before_the_clip(self):
        # It began on the last frame a cut can begin on, 1.6 s before.
        before, clip, after = recording_moments.extent(self.found(), 533.4, 126.6)

        assert (before, clip, after) == (pytest.approx(31.6), pytest.approx(35), pytest.approx(60))

    def test_a_cut_that_came_out_short_has_less_after_the_clip_or_less_of_it(self):
        assert recording_moments.extent(self.found(), 535.0, 80.0)[1:] == (pytest.approx(35), pytest.approx(15))
        assert recording_moments.extent(self.found(), 535.0, 50.0)[1:] == (pytest.approx(20), 0.0)

    def test_the_three_always_add_up_to_the_file(self):
        for begins, duration in ((535.0, 125.0), (533.4, 126.6), (535.0, 50.0)):
            assert sum(recording_moments.extent(self.found(), begins, duration)) == pytest.approx(duration)


class TestWhichMomentItIsAlready:
    def found(self) -> recording_moments.Found:
        (found,) = found_in(one_part(), fired(600, extension=20))
        return found  # reacting from 590 to 620

    def window(self, moment_id: int, start: float, end: float) -> dict:
        return {
            "id": moment_id,
            "window_start": (T0 + timedelta(seconds=start)).isoformat(),
            "window_end": (T0 + timedelta(seconds=end)).isoformat(),
        }

    @pytest.mark.parametrize(
        ("start", "end"),
        [(590, 620), (570, 595), (615, 660), (600, 605), (500, 700)],
        ids=["the-same", "ends-in-it", "begins-in-it", "inside-it", "around-it"],
    )
    def test_a_stored_moment_whose_reaction_ran_while_its_own_did_is_it(self, start, end):
        assert recording_moments.moment_of(self.found(), [self.window(7, start, end)]) == 7

    @pytest.mark.parametrize(("start", "end"), [(540, 580), (630, 660)], ids=["before", "after"])
    def test_one_from_another_time_is_not(self, start, end):
        assert recording_moments.moment_of(self.found(), [self.window(7, start, end)]) is None

    def test_with_no_stored_moments_it_is_none(self):
        assert recording_moments.moment_of(self.found(), []) is None

    def test_of_several_it_is_the_first_that_matches(self):
        windows = [self.window(3, 100, 110), self.window(5, 595, 600), self.window(9, 610, 640)]

        assert recording_moments.moment_of(self.found(), windows) == 5

    def test_times_stored_in_another_zone_are_the_same_times(self):
        elsewhere = timezone(timedelta(hours=2))
        window = {
            "id": 4,
            "window_start": (T0 + timedelta(seconds=600)).astimezone(elsewhere).isoformat(),
            "window_end": (T0 + timedelta(seconds=605)).astimezone(elsewhere).isoformat(),
        }

        assert recording_moments.moment_of(self.found(), [window]) == 4
