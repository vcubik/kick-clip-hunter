"""Cutting clips out of the rolling buffer with the real ffmpeg.

The buffer is filled with genuine MPEG-TS segments (a synthetic test pattern
with a tone, see tests/support/media.py), one second each, so every assertion
about a clip's length or playability is made on a file ffmpeg really produced.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kick_clip_hunter import recorder
from kick_clip_hunter.recorder import ChannelRecorder, RecorderError
from tests.support.hls import Segment
from tests.support.media import FFMPEG, decodes_cleanly, probe
from tests.support.waiting import wait_until

pytestmark = pytest.mark.ffmpeg

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
DELAY = recorder.PLAYBACK_DELAY_SECONDS
# Container overhead and frame boundaries make a remuxed clip's reported
# duration differ from the sum of its segments by a fraction of a second.
TOLERANCE = 0.35


def at(seconds: float) -> datetime:
    return BASE + timedelta(seconds=seconds)


def chat_time(seconds: float) -> datetime:
    """The chat-clock time at which viewers see footage from `seconds`."""
    return at(seconds + DELAY)


def cut(*args, **kwargs) -> Path:
    """Cuts a clip and hands back just the file, for the tests that are
    about what is in it."""
    return recorder.extract_clip(*args, **kwargs).path


def buffer_segments(rec: ChannelRecorder, ts_segments: list[Path], indexes, group: str = "1-0") -> None:
    """Puts one-second segment `i` of the source footage at second `i` of
    the buffer's timeline."""
    for index in indexes:
        name = f"{int(at(index).timestamp() * 1000)}_1000_{group}.ts"
        (rec._base_dir / name).write_bytes(ts_segments[index].read_bytes())


@pytest.fixture(autouse=True)
def the_clip_alone(monkeypatch):
    """Most of these tests are about the clip itself, so its file is cut
    without the footage either side of it. That has tests of its own
    (TestContext), which turn it back on."""
    monkeypatch.setattr(recorder, "CONTEXT_BEFORE_SECONDS", 0)
    monkeypatch.setattr(recorder, "CONTEXT_AFTER_SECONDS", 0)


def first_frame(path: Path) -> bytes:
    """A fingerprint of a video's first picture."""
    # fmt: off
    result = subprocess.run(
        [FFMPEG, "-v", "error", "-i", str(path), "-frames:v", "1", "-an", "-f", "md5", "-"],
        check=True, capture_output=True,
    )
    # fmt: on
    return result.stdout.strip()


@pytest.fixture
def rec(ts_segments) -> ChannelRecorder:
    """A recorder whose buffer holds seconds 0-39 of one unbroken stream."""
    channel_recorder = ChannelRecorder("some_channel")
    buffer_segments(channel_recorder, ts_segments, range(40))
    return channel_recorder


class TestExtractClip:
    def test_produces_a_playable_clip_with_video_and_sound(self, rec):
        clip = cut(rec, chat_time(20), chat_time(25), "moment_1.mp4", pre_roll_seconds=5, post_roll_seconds=5)

        assert clip == recorder.CLIPS_DIR / "some_channel" / "moment_1.mp4"
        assert probe(clip)["streams"] == ["audio", "video"]
        assert decodes_cleanly(clip)

    def test_says_which_stretch_of_the_broadcast_the_clip_holds(self, rec):
        # Footage 15s..30s is asked for. The clip is made of whole segments,
        # so what it holds starts and ends on a segment's edge around that.
        clip = recorder.extract_clip(
            rec, chat_time(20), chat_time(25), "moment_1.mp4", pre_roll_seconds=5, post_roll_seconds=5
        )

        assert at(14) <= clip.started_at <= at(15)
        assert at(30) <= clip.ended_at <= at(31)
        assert clip.duration == (clip.ended_at - clip.started_at).total_seconds()
        assert probe(clip.path)["duration"] == pytest.approx(clip.duration, abs=TOLERANCE)

    def test_covers_pre_roll_reaction_and_post_roll(self, rec):
        # Footage 15s..30s is asked for; whole segments touching that range
        # are used, so one extra second on either side at most.
        clip = cut(rec, chat_time(20), chat_time(25), "moment_1.mp4", pre_roll_seconds=5, post_roll_seconds=5)

        duration = probe(clip)["duration"]
        assert 15 - TOLERANCE <= duration <= 17 + TOLERANCE

    def test_a_longer_reaction_gives_a_proportionally_longer_clip(self, rec):
        short = cut(rec, chat_time(20), chat_time(22), "short.mp4", 5, 5)
        longer = cut(rec, chat_time(20), chat_time(30), "longer.mp4", 5, 5)

        assert probe(longer)["duration"] - probe(short)["duration"] == pytest.approx(8, abs=2 * TOLERANCE)

    def test_pre_and_post_roll_default_to_the_modules_constants(self, rec):
        with_defaults = cut(rec, chat_time(20), chat_time(25), "defaults.mp4")
        explicit = cut(
            rec, chat_time(20), chat_time(25), "explicit.mp4", recorder.PRE_ROLL_SECONDS, recorder.POST_ROLL_SECONDS
        )

        assert probe(with_defaults)["duration"] == pytest.approx(probe(explicit)["duration"], abs=TOLERANCE)

    def test_leaves_no_working_files_behind(self, rec):
        cut(rec, chat_time(20), chat_time(25), "moment_1.mp4", 5, 5)

        assert sorted(path.name for path in (recorder.CLIPS_DIR / "some_channel").iterdir()) == ["moment_1.mp4"]

    def test_the_buffer_itself_is_not_consumed(self, rec):
        before = sorted(path.name for path in rec._base_dir.iterdir())

        cut(rec, chat_time(20), chat_time(25), "moment_1.mp4", 5, 5)

        assert sorted(path.name for path in rec._base_dir.iterdir()) == before

    def test_a_window_partly_outside_the_buffer_yields_what_there_is(self, rec):
        # The buffer ends at second 40; the request runs to second 60.
        clip = cut(rec, chat_time(35), chat_time(50), "tail.mp4", pre_roll_seconds=5, post_roll_seconds=10)

        assert probe(clip)["duration"] == pytest.approx(11, abs=1 + TOLERANCE)
        assert decodes_cleanly(clip)

    def test_no_footage_at_all_is_an_error_and_writes_nothing(self, rec):
        with pytest.raises(RecorderError):
            cut(rec, chat_time(500), chat_time(505), "nothing.mp4", 5, 5)

        assert not (recorder.CLIPS_DIR / "some_channel" / "nothing.mp4").exists()

    def test_unreadable_footage_fails_loudly_and_cleans_up(self, tmp_path):
        broken = ChannelRecorder("broken_channel")
        for second in range(10):
            (broken._base_dir / f"{int(at(second).timestamp() * 1000)}_1000_1-0.ts").write_bytes(b"not mpeg-ts at all")

        with pytest.raises(subprocess.CalledProcessError):
            cut(broken, chat_time(4), chat_time(6), "broken.mp4", 2, 2)

        leftovers = [path.name for path in (recorder.CLIPS_DIR / "broken_channel").iterdir() if path.suffix == ".ts"]
        assert leftovers == []


class TestClipAcrossABreak:
    """Footage on either side of a break (an ad, a stream restart) may have a
    different layout, so a clip is never stitched across one."""

    @pytest.fixture
    def rec(self, ts_segments) -> ChannelRecorder:
        channel_recorder = ChannelRecorder("some_channel")
        buffer_segments(channel_recorder, ts_segments, range(0, 20), group="1-0")
        buffer_segments(channel_recorder, ts_segments, range(20, 40), group="1-1")
        return channel_recorder

    def test_a_clip_mostly_before_the_break_stops_at_it(self, rec):
        # Footage 12s..24s: eight seconds before the break, four after.
        clip = cut(rec, chat_time(12), chat_time(24), "before.mp4", 0, 0)

        assert probe(clip)["duration"] == pytest.approx(9, abs=TOLERANCE)  # seconds 11-20
        assert decodes_cleanly(clip)

    def test_a_clip_mostly_after_the_break_starts_at_it(self, rec):
        # Footage 17s..30s: three seconds before the break, ten after.
        clip = cut(rec, chat_time(17), chat_time(30), "after.mp4", 0, 0)

        assert probe(clip)["duration"] == pytest.approx(11, abs=TOLERANCE)  # seconds 20-31
        assert decodes_cleanly(clip)


class TestContext:
    """A moment's file holds its clip and the footage either side of it."""

    @pytest.fixture(autouse=True)
    def short_context(self, monkeypatch):
        # Production keeps 30s before and 60s after; the test buffer is 40s.
        monkeypatch.setattr(recorder, "CONTEXT_BEFORE_SECONDS", 6)
        monkeypatch.setattr(recorder, "CONTEXT_AFTER_SECONDS", 8)

    def cut(self, rec, start: float, end: float, name: str = "moment_7.mp4", roll: int = 0) -> recorder.CutClip:
        return recorder.extract_clip(rec, chat_time(start), chat_time(end), name, roll, roll)

    def test_the_lead_up_and_the_aftermath_are_in_the_clips_file(self, rec):
        # The clip is seconds 14-23, so the file runs from 8s to 31s.
        clip = self.cut(rec, 15, 22)

        assert (clip.started_at, clip.ended_at) == (at(14), at(23))
        assert (clip.context_before, clip.context_after) == (6, 8)
        assert probe(clip.path)["duration"] == pytest.approx(6 + 9 + 8, abs=TOLERANCE)
        assert probe(clip.path)["streams"] == ["audio", "video"]
        assert decodes_cleanly(clip.path)

    def test_a_moment_has_one_file(self, rec):
        clip = self.cut(rec, 15, 22)

        assert [path.name for path in clip.path.parent.iterdir()] == ["moment_7.mp4"]

    def test_the_clip_is_the_same_stretch_with_context_around_it_or_without(self, rec, monkeypatch):
        around = self.cut(rec, 15, 22)
        monkeypatch.setattr(recorder, "CONTEXT_BEFORE_SECONDS", 0)
        monkeypatch.setattr(recorder, "CONTEXT_AFTER_SECONDS", 0)

        alone = self.cut(rec, 15, 22, "alone.mp4")

        assert (around.started_at, around.ended_at) == (alone.started_at, alone.ended_at)
        assert (alone.context_before, alone.context_after) == (0, 0)
        assert probe(alone.path)["duration"] == pytest.approx(alone.duration, abs=TOLERANCE)

    def test_a_clip_at_the_start_of_the_buffer_has_no_lead_up(self, rec):
        clip = self.cut(rec, 0, 5)

        assert (clip.context_before, clip.context_after) == (0, 8)
        assert probe(clip.path)["duration"] == pytest.approx(clip.duration + 8, abs=TOLERANCE)

    def test_a_clip_at_the_end_of_the_buffer_has_no_aftermath(self, rec):
        clip = self.cut(rec, 34, 39)

        assert (clip.context_before, clip.context_after) == (6, 0)
        assert probe(clip.path)["duration"] == pytest.approx(6 + clip.duration, abs=TOLERANCE)

    def test_a_buffer_that_runs_short_gives_the_context_there_is(self, rec):
        # The clip is seconds 2-34 of a buffer that holds 0-40.
        clip = self.cut(rec, 3, 33)

        assert (clip.context_before, clip.context_after) == (2, 6)

    def test_pre_and_post_roll_move_the_clips_edges_and_the_context_with_them(self, rec):
        clip = self.cut(rec, 18, 20, "moment_9.mp4", roll=4)

        # clip: seconds 13-25, before it: 7-13, after it: 25-33.
        assert (clip.started_at, clip.ended_at) == (at(13), at(25))
        assert probe(clip.path)["duration"] == pytest.approx(26, abs=TOLERANCE)

    def test_without_the_clips_own_footage_there_is_no_file(self, rec):
        with pytest.raises(RecorderError):
            self.cut(rec, 500, 505)

        assert not (recorder.CLIPS_DIR / "some_channel" / "moment_7.mp4").exists()


class TestContextAtABreak:
    """Everything in a file comes from one unbroken run of the stream: the
    one the clip itself is best cut from."""

    @pytest.fixture(autouse=True)
    def short_context(self, monkeypatch):
        monkeypatch.setattr(recorder, "CONTEXT_BEFORE_SECONDS", 6)
        monkeypatch.setattr(recorder, "CONTEXT_AFTER_SECONDS", 8)

    @pytest.fixture
    def rec(self, ts_segments) -> ChannelRecorder:
        channel_recorder = ChannelRecorder("some_channel")
        buffer_segments(channel_recorder, ts_segments, range(0, 20), group="1-0")
        buffer_segments(channel_recorder, ts_segments, range(20, 40), group="1-1")
        return channel_recorder

    def test_context_beyond_the_break_is_left_out(self, rec):
        # The clip is seconds 21-29; one second separates it from the break.
        clip = recorder.extract_clip(rec, chat_time(22), chat_time(28), "moment_7.mp4", 0, 0)

        assert (clip.started_at, clip.ended_at) == (at(21), at(29))
        assert (clip.context_before, clip.context_after) == (1, 8)
        assert probe(clip.path)["duration"] == pytest.approx(1 + 8 + 8, abs=TOLERANCE)
        assert decodes_cleanly(clip.path)

    def test_the_clip_keeps_to_its_longer_side_whatever_context_that_costs(self, rec):
        # Footage 16s..23s: four seconds before the break, three after it.
        # The side after the break has all its context, the side before
        # none of what follows - and is still the one the clip is cut from.
        clip = recorder.extract_clip(rec, chat_time(16), chat_time(23), "moment_7.mp4", 0, 0)

        assert (clip.started_at, clip.ended_at) == (at(15), at(20))
        assert (clip.context_before, clip.context_after) == (6, 0)

    def test_two_sides_that_hold_as_much_of_the_clip_are_told_apart_by_their_context(self, rec):
        # Footage 17s..23s: three seconds on either side of the break. The
        # side after it has eight seconds of context to the other's six.
        clip = recorder.extract_clip(rec, chat_time(17), chat_time(23), "moment_7.mp4", 0, 0)

        assert clip.started_at == at(20)
        assert (clip.context_before, clip.context_after) == (0, 8)


class TestTheClipWithoutItsContext:
    """What looks at a clip - the analysis steps - is given the clip, not
    the minute and a half its file holds."""

    @pytest.fixture
    def clip(self, rec, monkeypatch) -> recorder.CutClip:
        monkeypatch.setattr(recorder, "CONTEXT_BEFORE_SECONDS", 6)
        monkeypatch.setattr(recorder, "CONTEXT_AFTER_SECONDS", 8)
        return recorder.extract_clip(rec, chat_time(15), chat_time(22), "moment_7.mp4", 0, 0)

    @pytest.fixture
    def alone(self, rec, monkeypatch) -> recorder.CutClip:
        """The same clip cut with nothing around it, to compare with."""
        monkeypatch.setattr(recorder, "CONTEXT_BEFORE_SECONDS", 0)
        monkeypatch.setattr(recorder, "CONTEXT_AFTER_SECONDS", 0)
        return recorder.extract_clip(rec, chat_time(15), chat_time(22), "alone.mp4", 0, 0)

    def test_the_clip_can_be_cut_back_out_of_its_file(self, clip, alone):
        with recorder.clip_part(clip.path, clip.context_before, clip.duration) as part:
            assert part != clip.path and part.name == clip.path.name
            assert probe(part)["streams"] == ["audio", "video"]
            assert probe(part)["duration"] == pytest.approx(clip.duration, abs=TOLERANCE)
            assert decodes_cleanly(part)
            # It starts on the clip's first picture, not the file's.
            assert first_frame(part) == first_frame(alone.path) != first_frame(clip.path)

    def test_a_file_that_cannot_be_asked_where_its_frames_are_is_cut_where_it_was_told(self, tmp_path):
        assert recorder._keyframe_near(tmp_path / "not_there.mp4", 6.0) == 6.0

    def test_the_cut_out_part_is_gone_once_it_has_been_used(self, clip):
        with recorder.clip_part(clip.path, clip.context_before, clip.duration) as part:
            assert part.exists()

        assert not part.exists() and not part.parent.exists()
        assert [path.name for path in clip.path.parent.iterdir()] == ["moment_7.mp4"]

    def test_and_also_when_using_it_failed(self, clip):
        with pytest.raises(RuntimeError), recorder.clip_part(clip.path, clip.context_before, clip.duration) as part:
            raise RuntimeError("the model fell over")

        assert not part.exists()

    def test_a_file_with_context_has_its_clip_cut_out(self, clip, alone):
        with recorder.clip_alone(clip.path, clip.context_before, clip.duration, clip.context_after) as given:
            assert given != clip.path
            assert probe(given)["duration"] == pytest.approx(clip.duration, abs=TOLERANCE)
            assert first_frame(given) == first_frame(alone.path)

    def test_context_on_one_side_only_is_context_too(self, clip, alone):
        with recorder.clip_alone(clip.path, 0.0, clip.context_before + clip.duration, clip.context_after) as given:
            assert given != clip.path
            assert probe(given)["duration"] == pytest.approx(6 + clip.duration, abs=TOLERANCE)

    @pytest.mark.parametrize(
        "extent",
        [(None, None, None), (None, 9.0, None), (0.0, 9.0, 0.0), (6.0, None, 8.0)],
        ids=["nothing-known", "length-only", "no-context", "length-unknown"],
    )
    def test_a_file_that_is_the_clip_alone_is_used_as_it_is(self, alone, extent):
        with recorder.clip_alone(alone.path, *extent) as given:
            assert given == alone.path

        assert alone.path.exists()


class TestJoiningOlderContextClips:
    """Moments used to keep the footage before and after a clip in files of
    their own next to it; joined, they are what is cut today."""

    def older_moment(self, rec, sides=("before", "after"), name: str = "moment_7.mp4") -> Path:
        """A clip of seconds 14-23 with 8-14 and 23-31 in context files."""
        segments = {segment.started_at: segment for segment in rec._stored_segments()}
        names = recorder.context_clip_names(name)
        spans = {"before": range(8, 14), "after": range(23, 31)}
        for side in sides:
            recorder._write_clip(rec, [segments[at(second)] for second in spans[side]], names[side])
        return recorder._write_clip(rec, [segments[at(second)] for second in range(14, 23)], name)

    def files(self, clip: Path) -> list[str]:
        return sorted(path.name for path in clip.parent.iterdir())

    def test_the_three_files_become_one_in_the_clips_place(self, rec):
        clip = self.older_moment(rec)
        assert self.files(clip) == ["moment_7.mp4", "moment_7_after.mp4", "moment_7_before.mp4"]

        before, length, after = recorder.join_context_clips(clip)

        assert self.files(clip) == ["moment_7.mp4"]
        assert (before, length, after) == pytest.approx((6, 9, 8), abs=TOLERANCE)
        assert probe(clip)["duration"] == pytest.approx(23, abs=TOLERANCE)
        assert probe(clip)["streams"] == ["audio", "video"]
        assert decodes_cleanly(clip)

    def test_the_joined_file_is_what_would_be_cut_today(self, rec, monkeypatch):
        clip = self.older_moment(rec)
        before, length, _after = recorder.join_context_clips(clip)
        monkeypatch.setattr(recorder, "CONTEXT_BEFORE_SECONDS", 6)
        monkeypatch.setattr(recorder, "CONTEXT_AFTER_SECONDS", 8)
        today = recorder.extract_clip(rec, chat_time(15), chat_time(22), "today.mp4", 0, 0)

        assert probe(clip)["duration"] == pytest.approx(probe(today.path)["duration"], abs=TOLERANCE)
        assert first_frame(clip) == first_frame(today.path)
        # And the clip is found in it where the join says it is.
        with recorder.clip_part(clip, before, length) as old, recorder.clip_part(today.path, 6, 9) as new:
            assert first_frame(old) == first_frame(new)

    @pytest.mark.parametrize(("sides", "expected"), [(("before",), (6, 9, 0)), (("after",), (0, 9, 8))])
    def test_one_context_file_is_joined_on_its_own(self, rec, sides, expected):
        clip = self.older_moment(rec, sides)

        assert recorder.join_context_clips(clip) == pytest.approx(expected, abs=TOLERANCE)
        assert self.files(clip) == ["moment_7.mp4"]
        assert probe(clip)["duration"] == pytest.approx(sum(expected), abs=TOLERANCE)

    def test_a_clip_without_context_files_is_left_alone(self, rec):
        clip = self.older_moment(rec, sides=())
        content = clip.read_bytes()

        assert recorder.join_context_clips(clip) is None
        assert clip.read_bytes() == content

    def test_joining_twice_does_nothing_more(self, rec):
        clip = self.older_moment(rec)
        recorder.join_context_clips(clip)
        content = clip.read_bytes()

        assert recorder.join_context_clips(clip) is None
        assert clip.read_bytes() == content

    def test_a_context_file_that_cannot_be_read_leaves_everything_as_it_was(self, rec):
        clip = self.older_moment(rec)
        clip.with_name("moment_7_after.mp4").write_bytes(b"not really video")
        content = clip.read_bytes()

        assert recorder.join_context_clips(clip) is None
        assert self.files(clip) == ["moment_7.mp4", "moment_7_after.mp4", "moment_7_before.mp4"]
        assert clip.read_bytes() == content

    def test_context_files_without_their_clip_are_left_alone(self, rec):
        clip = self.older_moment(rec)
        clip.unlink()

        assert recorder.join_context_clips(clip) is None
        assert self.files(clip) == ["moment_7_after.mp4", "moment_7_before.mp4"]

    def test_a_join_that_comes_out_the_wrong_length_is_not_kept(self, rec, monkeypatch):
        clip = self.older_moment(rec)
        content = clip.read_bytes()
        monkeypatch.setattr(recorder, "JOIN_TOLERANCE_SECONDS", -1.0)  # nothing is close enough

        with pytest.raises(RecorderError, match=r"joining moment_7\.mp4"):
            recorder.join_context_clips(clip)

        assert self.files(clip) == ["moment_7.mp4", "moment_7_after.mp4", "moment_7_before.mp4"]
        assert clip.read_bytes() == content


class TestAdReplacedFromTheVod:
    """A stitched ad replaces ten seconds of the live stream; the broadcast's
    own recording (served by a second fake stream, in five-second segments on
    the same clock) still has what was really on."""

    AD = range(15, 25)

    @pytest.fixture
    def start(self) -> datetime:
        # Recent, so that starting the recording doesn't prune the buffer.
        # On a whole second, so segment boundaries survive the buffer's
        # millisecond file names exactly.
        return datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=60)

    @pytest.fixture
    def live(self, hls, ts_segments, start):
        def source(name: str) -> tuple[str, ...]:
            return (
                f'#EXT-X-DATERANGE:ID="{name}",CLASS="{recorder.STREAM_SOURCE_CLASS}",'
                f'{recorder.STREAM_SOURCE_ATTRIBUTE}="{name}"',
            )

        timeline = [Segment(path.read_bytes(), duration=1.0) for path in ts_segments]
        for index in self.AD:
            # The ad's own footage: the first seconds of the test pattern.
            timeline[index] = Segment(ts_segments[index - self.AD.start].read_bytes(), duration=1.0, title="creative")
        timeline[self.AD.start].discontinuity = True
        timeline[self.AD.start].tags = source("creative")
        timeline[self.AD.stop].discontinuity = True
        timeline[self.AD.stop].tags = source(recorder.LIVE_STREAM_SOURCE)
        return hls(timeline, published=len(timeline), window=len(timeline), stream_start=start)

    @pytest.fixture
    def vod_timeline(self, ts_segments) -> list[Segment]:
        return [
            Segment(b"".join(path.read_bytes() for path in ts_segments[first : first + 5]), duration=5.0)
            for first in range(0, 40, 5)
        ]

    @pytest.fixture
    def vod(self, hls, vod_timeline, start):
        return hls(vod_timeline, published=len(vod_timeline), window=len(vod_timeline), stream_start=start)

    @pytest.fixture(autouse=True)
    def fast(self, monkeypatch):
        monkeypatch.setattr(recorder, "INITIAL_SEGMENTS", 40)
        monkeypatch.setattr(recorder, "VOD_POLL_SECONDS", 0.01)

    def recorded(self, recording, live, vod=None) -> ChannelRecorder:
        rec = recording.of(live, vod=vod)
        rec.tick()
        wait_until(lambda: len(rec._stored_segments()) == 40, "the whole stream being recorded")
        return rec

    def clip(self, rec, start, first: float, last: float, name: str = "moment_3.mp4") -> Path:
        def chat(seconds: float) -> datetime:
            return start + timedelta(seconds=seconds + DELAY)

        return cut(rec, chat(first), chat(last), name, 0, 0)

    def test_a_clip_through_an_ad_is_cut_from_the_vod_instead(self, recording, live, vod, start):
        rec = self.recorded(recording, live, vod)

        clip = self.clip(rec, start, 12, 28)

        # The VOD segments covering 12s..28s: 10-15, 15-20, 20-25, 25-30.
        assert probe(clip)["duration"] == pytest.approx(20, abs=TOLERANCE)
        assert decodes_cleanly(clip)

    def test_only_the_vod_segments_the_window_needs_are_downloaded(self, recording, live, vod, start):
        rec = self.recorded(recording, live, vod)

        self.clip(rec, start, 12, 28)

        assert vod.segment_requests() == [2, 3, 4, 5]

    def test_vod_segments_already_in_the_buffer_are_not_downloaded_again(self, recording, live, vod, start):
        rec = self.recorded(recording, live, vod)

        self.clip(rec, start, 12, 28)
        self.clip(rec, start, 12, 28, "again.mp4")

        assert vod.segment_requests() == [2, 3, 4, 5]

    def test_a_clip_clear_of_the_ad_never_touches_the_vod(self, recording, live, vod, start):
        rec = self.recorded(recording, live, vod)

        clip = self.clip(rec, start, 2, 8)

        assert vod.requests == []
        assert probe(clip)["duration"] == pytest.approx(8, abs=TOLERANCE)  # seconds 1-9, live

    def test_context_that_runs_into_an_ad_takes_the_whole_file_to_the_vod(
        self, recording, live, vod, start, monkeypatch
    ):
        monkeypatch.setattr(recorder, "CONTEXT_BEFORE_SECONDS", 6)
        monkeypatch.setattr(recorder, "CONTEXT_AFTER_SECONDS", 4)
        rec = self.recorded(recording, live, vod)

        def chat(seconds: float) -> datetime:
            return start + timedelta(seconds=seconds + DELAY)

        # The clip is live footage 26s..34s, clear of the ad - but the six
        # seconds before it are almost all ad in the live recording. The VOD
        # holds the clip just as well, and what was really on before it too.
        clip = recorder.extract_clip(rec, chat(27), chat(33), "moment_4.mp4", 0, 0)

        assert vod.segment_requests() == [4, 5, 6, 7]
        # VOD segments: 20-25 before the clip, 25-35 the clip, 35-40 after it.
        assert (clip.started_at, clip.ended_at) == (start + timedelta(seconds=25), start + timedelta(seconds=35))
        assert (clip.context_before, clip.context_after) == (5, 5)
        assert probe(clip.path)["duration"] == pytest.approx(20, abs=TOLERANCE)
        assert decodes_cleanly(clip.path)

    def test_without_a_vod_the_context_stops_at_the_ad(self, recording, live, start, monkeypatch):
        monkeypatch.setattr(recorder, "CONTEXT_BEFORE_SECONDS", 6)
        monkeypatch.setattr(recorder, "CONTEXT_AFTER_SECONDS", 4)
        rec = self.recorded(recording, live)

        def chat(seconds: float) -> datetime:
            return start + timedelta(seconds=seconds + DELAY)

        clip = recorder.extract_clip(rec, chat(27), chat(33), "moment_4.mp4", 0, 0)

        # Live footage 26s..34s, with the one second between it and the ad.
        assert (clip.started_at, clip.ended_at) == (start + timedelta(seconds=26), start + timedelta(seconds=34))
        assert (clip.context_before, clip.context_after) == (1, 4)
        assert probe(clip.path)["duration"] == pytest.approx(1 + 8 + 4, abs=TOLERANCE)

    def test_waits_for_the_vod_to_catch_up_with_the_end_of_the_window(self, recording, live, hls, vod_timeline, start):
        vod = hls(vod_timeline, published=5, window=len(vod_timeline), stream_start=start)  # reaches 25s
        rec = self.recorded(recording, live, vod)

        def catch_up() -> None:
            wait_until(lambda: vod.requests.count("/variant/high.m3u8") >= 2, "the VOD playlist being re-read")
            vod.publish_all()

        publisher = threading.Thread(target=catch_up)
        publisher.start()
        clip = self.clip(rec, start, 12, 28)
        publisher.join()

        assert probe(clip)["duration"] == pytest.approx(20, abs=TOLERANCE)

    def test_a_vod_that_never_catches_up_is_used_as_far_as_it_goes(
        self, recording, live, hls, vod_timeline, start, monkeypatch
    ):
        monkeypatch.setattr(recorder, "VOD_WAIT_SECONDS", 0)
        vod = hls(vod_timeline, published=5, window=len(vod_timeline), stream_start=start)  # reaches 25s
        rec = self.recorded(recording, live, vod)

        clip = self.clip(rec, start, 12, 28)

        assert probe(clip)["duration"] == pytest.approx(15, abs=TOLERANCE)  # VOD 10s..25s

    def test_a_channel_without_a_vod_still_gets_a_clip(self, recording, live, start):
        rec = self.recorded(recording, live)

        clip = self.clip(rec, start, 12, 28)

        # No way round the ad: the longer stretch of real footage either
        # side of it (25s..29s) is all there is.
        assert probe(clip)["duration"] == pytest.approx(4, abs=TOLERANCE)
        assert decodes_cleanly(clip)

    def test_a_vod_that_cannot_be_fetched_still_leaves_a_clip(self, recording, live, vod, start, caplog):
        vod.fail_next["/master.m3u8"] = [503]
        rec = self.recorded(recording, live, vod)

        with caplog.at_level(logging.WARNING, logger="kick_clip_hunter"):
            clip = self.clip(rec, start, 12, 28)

        assert probe(clip)["duration"] == pytest.approx(4, abs=TOLERANCE)
        assert any("VOD backfill failed" in record.getMessage() for record in caplog.records)


class TestMeasuringAClip:
    """Clips cut before their length was stored are measured by the dashboard."""

    def test_gives_the_length_of_a_clip_in_seconds(self, rec):
        clip = recorder.extract_clip(rec, chat_time(20), chat_time(25), "moment_1.mp4", 5, 5)

        assert recorder.probe_duration(clip.path) == pytest.approx(clip.duration, abs=TOLERANCE)

    def test_a_file_that_is_not_a_video_has_no_length(self, tmp_path):
        path = tmp_path / "moment_1.mp4"
        path.write_bytes(b"not really video")

        assert recorder.probe_duration(path) is None

    def test_neither_has_a_file_that_is_not_there(self, tmp_path):
        assert recorder.probe_duration(tmp_path / "gone.mp4") is None


class TestTrimming:
    """A stretch of a moment's file cut out as a file of its own, for an
    editor: where it starts and ends is theirs to say."""

    @pytest.fixture
    def clip(self, rec, monkeypatch) -> recorder.CutClip:
        """A file of 6 s before a clip, the clip, and 8 s after it, with a
        frame a cut can begin on every second."""
        monkeypatch.setattr(recorder, "CONTEXT_BEFORE_SECONDS", 6)
        monkeypatch.setattr(recorder, "CONTEXT_AFTER_SECONDS", 8)
        return recorder.extract_clip(rec, chat_time(15), chat_time(22), "moment_7.mp4", 0, 0)

    @pytest.fixture
    def alone(self, rec, monkeypatch) -> recorder.CutClip:
        """The same clip cut with nothing around it: it begins on the
        picture that is 6 s into the file with context."""
        monkeypatch.setattr(recorder, "CONTEXT_BEFORE_SECONDS", 0)
        monkeypatch.setattr(recorder, "CONTEXT_AFTER_SECONDS", 0)
        return recorder.extract_clip(rec, chat_time(15), chat_time(22), "alone.mp4", 0, 0)

    def trimmed(self, clip) -> Path:
        return clip.path.with_name("moment_7_trim.mp4")

    def test_a_stretch_is_cut_out_as_a_playable_file(self, clip):
        output = self.trimmed(clip)

        begins, duration = recorder.trim_clip(clip.path, 3.0, 12.0, output)

        assert begins == pytest.approx(3, abs=0.1)
        assert duration == pytest.approx(9, abs=TOLERANCE)
        assert probe(output) == {"duration": pytest.approx(duration, abs=0.01), "streams": ["audio", "video"]}
        assert decodes_cleanly(output)

    def test_it_begins_on_the_last_frame_a_cut_can_begin_on_before_the_start(self, clip, alone):
        output = self.trimmed(clip)

        # Asked to start 0.6 s into the clip; the frame before that is the
        # clip's first.
        begins, duration = recorder.trim_clip(clip.path, 6.6, 10.0, output)

        assert begins == pytest.approx(6, abs=0.1)
        assert duration == pytest.approx(4, abs=TOLERANCE)
        assert first_frame(output) == first_frame(alone.path) != first_frame(clip.path)

    def test_a_start_a_hair_before_such_a_frame_begins_on_it_not_a_second_earlier(self, clip, alone):
        output = self.trimmed(clip)

        begins, _duration = recorder.trim_clip(clip.path, 6.0 - recorder.KEYFRAME_SLACK_SECONDS / 2, 10.0, output)

        assert begins == pytest.approx(6, abs=0.1)
        assert first_frame(output) == first_frame(alone.path)

    def test_from_the_very_start_of_the_file_it_begins_with_the_file(self, clip):
        output = self.trimmed(clip)

        begins, duration = recorder.trim_clip(clip.path, 0.0, 5.0, output)

        assert begins == pytest.approx(0, abs=0.1)
        assert duration == pytest.approx(5, abs=TOLERANCE)
        assert first_frame(output) == first_frame(clip.path)

    def test_it_ends_where_it_was_asked_to(self, clip):
        _begins, short = recorder.trim_clip(clip.path, 4.0, 7.3, self.trimmed(clip))
        _begins, longer = recorder.trim_clip(clip.path, 4.0, 9.8, self.trimmed(clip))

        assert short == pytest.approx(3.3, abs=TOLERANCE)
        assert longer == pytest.approx(5.8, abs=TOLERANCE)

    def test_the_file_it_was_cut_from_is_as_it_was_and_nothing_else_is_left(self, clip):
        content = clip.path.read_bytes()

        recorder.trim_clip(clip.path, 3.0, 12.0, self.trimmed(clip))

        assert clip.path.read_bytes() == content
        assert sorted(path.name for path in clip.path.parent.iterdir()) == ["moment_7.mp4", "moment_7_trim.mp4"]

    def test_its_folder_is_made_if_it_is_not_there(self, clip, tmp_path):
        output = tmp_path / "taken" / "away" / "cut.mp4"

        recorder.trim_clip(clip.path, 3.0, 6.0, output)

        assert decodes_cleanly(output)

    def test_a_file_that_cannot_be_cut_is_an_error_and_leaves_nothing(self, tmp_path):
        broken = tmp_path / "moment_1.mp4"
        broken.write_bytes(b"not really video")

        with pytest.raises(subprocess.CalledProcessError):
            recorder.trim_clip(broken, 1.0, 5.0, tmp_path / "moment_1_trim.mp4")

        assert [path.name for path in tmp_path.iterdir()] == ["moment_1.mp4"]

    def test_a_stretch_the_file_does_not_reach_is_an_error_and_leaves_nothing(self, clip):
        with pytest.raises(RuntimeError, match=r"moment_7\.mp4 is \d+\.\d s long"):
            recorder.trim_clip(clip.path, 500.0, 510.0, self.trimmed(clip))

        assert [path.name for path in clip.path.parent.iterdir()] == ["moment_7.mp4"]

    def test_a_failed_cut_keeps_the_one_there_was(self, clip):
        output = self.trimmed(clip)
        recorder.trim_clip(clip.path, 3.0, 12.0, output)
        content = output.read_bytes()

        with pytest.raises(RuntimeError):
            recorder.trim_clip(clip.path, 500.0, 510.0, output)

        assert output.read_bytes() == content

    def test_a_file_that_cannot_be_asked_where_its_frames_are_is_cut_where_it_was_told(self, tmp_path):
        assert recorder._keyframe_before(tmp_path / "not_there.mp4", 6.0) == 6.0
