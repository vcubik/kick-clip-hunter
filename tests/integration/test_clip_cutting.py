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
from tests.support.media import decodes_cleanly, probe
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


class TestContextClips:
    @pytest.fixture(autouse=True)
    def short_context(self, monkeypatch):
        # Production keeps 30s before and 60s after; the test buffer is 40s.
        monkeypatch.setattr(recorder, "CONTEXT_BEFORE_SECONDS", 6)
        monkeypatch.setattr(recorder, "CONTEXT_AFTER_SECONDS", 8)

    def cut(self, rec, start: float, end: float, name: str = "moment_7.mp4"):
        clip = cut(rec, chat_time(start), chat_time(end), name, 0, 0)
        context = recorder.extract_context_clips(rec, chat_time(start), chat_time(end), name, 0, 0)
        return clip, context

    def test_saves_the_lead_up_and_the_aftermath_next_to_the_clip(self, rec):
        clip, context = self.cut(rec, 15, 22)

        assert context == {
            "before": clip.with_name("moment_7_before.mp4"),
            "after": clip.with_name("moment_7_after.mp4"),
        }
        assert probe(context["before"])["duration"] == pytest.approx(6, abs=TOLERANCE)
        assert probe(context["after"])["duration"] == pytest.approx(8, abs=TOLERANCE)
        assert decodes_cleanly(context["before"]) and decodes_cleanly(context["after"])

    def test_before_clip_and_after_are_one_continuous_stretch(self, rec):
        # Nothing repeated, nothing skipped: clip covers seconds 14-23, so
        # the three files together span 8s..31s.
        clip, context = self.cut(rec, 15, 22)

        total = sum(probe(path)["duration"] for path in (context["before"], clip, context["after"]))
        assert total == pytest.approx(23, abs=3 * TOLERANCE)

    def test_a_clip_at_the_start_of_the_buffer_has_no_lead_up(self, rec):
        _clip, context = self.cut(rec, 0, 5)

        assert list(context) == ["after"]

    def test_a_clip_at_the_end_of_the_buffer_has_no_aftermath(self, rec):
        _clip, context = self.cut(rec, 34, 39)

        assert list(context) == ["before"]

    def test_context_is_cut_from_the_same_arguments_as_the_clip(self, rec):
        # Pre- and post-roll move the clip's edges, and the context with them.
        clip = cut(rec, chat_time(18), chat_time(20), "moment_9.mp4", 4, 4)
        context = recorder.extract_context_clips(rec, chat_time(18), chat_time(20), "moment_9.mp4", 4, 4)

        total = sum(probe(path)["duration"] for path in (context["before"], clip, context["after"]))
        # clip: seconds 13-25 (12s), before: 7-13, after: 25-33.
        assert total == pytest.approx(26, abs=3 * TOLERANCE)

    def test_without_the_clips_own_footage_there_is_no_context_either(self, rec):
        with pytest.raises(RecorderError):
            recorder.extract_context_clips(rec, chat_time(500), chat_time(505), "moment_7.mp4", 0, 0)


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

    def test_the_lead_up_to_a_moment_right_after_an_ad_comes_from_the_vod(
        self, recording, live, vod, start, monkeypatch
    ):
        monkeypatch.setattr(recorder, "CONTEXT_BEFORE_SECONDS", 6)
        monkeypatch.setattr(recorder, "CONTEXT_AFTER_SECONDS", 4)
        rec = self.recorded(recording, live, vod)

        def chat(seconds: float) -> datetime:
            return start + timedelta(seconds=seconds + DELAY)

        # The clip is live footage 26s..34s; the six seconds before it are
        # almost all ad in the live recording.
        context = recorder.extract_context_clips(rec, chat(27), chat(33), "moment_4.mp4", 0, 0)

        assert vod.segment_requests() == [3, 4, 5]
        assert probe(context["before"])["duration"] == pytest.approx(5, abs=TOLERANCE)  # VOD segment 20-25
        assert decodes_cleanly(context["before"])

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
