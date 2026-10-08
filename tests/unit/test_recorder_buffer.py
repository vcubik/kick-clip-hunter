"""The on-disk rolling buffer: how saved segments are found again, which of
them a clip is cut from, and what gets pruned.

Nothing here downloads or decodes anything - segments are placeholder files
whose names carry the only information this layer uses.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kick_clip_hunter import recorder
from kick_clip_hunter.recorder import ChannelRecorder, RecorderError

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
CHANNEL = "some_channel"


def at(seconds: float) -> datetime:
    return BASE + timedelta(seconds=seconds)


def add_segment(
    rec: ChannelRecorder, start: datetime, duration: float = 2.0, group: str = "1-0", data: bytes = b"ts"
) -> Path:
    """Writes a segment file named the way the recorder names its downloads."""
    path = rec._base_dir / f"{int(start.timestamp() * 1000)}_{int(duration * 1000)}_{group}.ts"
    path.write_bytes(data)
    return path


def fill(rec: ChannelRecorder, start: float, end: float, duration: float = 2.0, group: str = "1-0") -> None:
    """Back-to-back segments covering [start, end) seconds after BASE."""
    offset = start
    while offset < end:
        add_segment(rec, at(offset), duration, group)
        offset += duration


@pytest.fixture
def rec() -> ChannelRecorder:
    return ChannelRecorder(CHANNEL)


def offsets(segments) -> list[float]:
    return [(segment.started_at - BASE).total_seconds() for segment in segments]


class TestStoredSegments:
    def test_a_new_recorder_gets_its_own_buffer_directory(self, rec):
        assert rec._base_dir == recorder.RECORDINGS_DIR / CHANNEL
        assert rec._base_dir.is_dir()
        assert rec._stored_segments() == []
        assert rec.is_active is False

    def test_a_segments_name_says_when_it_starts_how_long_it_is_and_its_group(self, rec):
        path = add_segment(rec, at(12.5), duration=4.167, group="1700000000-3")

        (stored,) = rec._stored_segments()

        assert stored.started_at == at(12.5)
        assert stored.duration == 4.167
        assert stored.ended_at == at(12.5) + timedelta(seconds=4.167)
        assert stored.group == "1700000000-3"
        assert stored.path == path

    def test_segments_come_back_in_broadcast_order_not_creation_order(self, rec):
        for offset in (6, 0, 4, 2):
            add_segment(rec, at(offset))

        assert offsets(rec._stored_segments()) == [0, 2, 4, 6]

    def test_files_that_are_not_finished_segments_are_ignored(self, rec):
        add_segment(rec, at(0))
        (rec._base_dir / "1767268800000_2000_1-0.ts.part").write_bytes(b"half a download")
        (rec._base_dir / "000123.ts").write_bytes(b"old recorder's numbering")
        (rec._base_dir / "notes.txt").write_text("not a segment")
        (rec._base_dir / "20260101T120000Z").mkdir()

        assert offsets(rec._stored_segments()) == [0]


class TestSegmentsOverlapping:
    def test_returns_what_lies_inside_the_range(self, rec):
        fill(rec, 0, 60)

        assert offsets(rec.segments_overlapping(at(20), at(30))) == [18, 20, 22, 24, 26, 28, 30]

    def test_segments_merely_touching_the_range_are_included(self, rec):
        # One that ends exactly where the range starts, one that starts
        # exactly where it ends: a clip is better a segment long than short.
        add_segment(rec, at(8))  # 8-10
        add_segment(rec, at(20))  # 20-22

        assert offsets(rec.segments_overlapping(at(10), at(20))) == [8, 20]

    def test_segments_clear_of_the_range_are_not(self, rec):
        add_segment(rec, at(0))  # 0-2
        add_segment(rec, at(30))  # 30-32

        assert rec.segments_overlapping(at(10), at(20)) == []

    def test_an_empty_buffer_overlaps_nothing(self, rec):
        assert rec.segments_overlapping(at(0), at(100)) == []

    def test_another_channels_buffer_is_not_searched(self, rec):
        fill(ChannelRecorder("another_channel"), 0, 20)

        assert rec.segments_overlapping(at(0), at(20)) == []


class TestBestGroup:
    def test_a_single_group_is_kept_whole(self, rec):
        fill(rec, 0, 20, group="1-0")
        segments = rec.segments_overlapping(at(4), at(12))

        assert recorder._best_group(segments, at(4), at(12)) == segments

    def test_the_group_covering_more_of_the_range_wins(self, rec):
        fill(rec, 0, 10, group="1-0")
        fill(rec, 10, 40, group="1-1")
        segments = rec.segments_overlapping(at(6), at(30))  # 4s of the first group, 20s of the second

        chosen = recorder._best_group(segments, at(6), at(30))

        assert {segment.group for segment in chosen} == {"1-1"}
        assert offsets(chosen)[0] == 10

    def test_only_the_part_inside_the_range_counts_as_coverage(self, rec):
        # A long segment that barely reaches into the range must not beat
        # shorter ones that lie fully inside it.
        add_segment(rec, at(0), duration=30, group="long")  # overlaps the range by 2s
        fill(rec, 30, 36, group="short")  # 6s inside the range
        segments = rec.segments_overlapping(at(28), at(36))

        chosen = recorder._best_group(segments, at(28), at(36))

        assert {segment.group for segment in chosen} == {"short"}


class TestClipWindow:
    def test_reaches_back_by_pre_roll_and_playback_delay_and_forward_by_post_roll(self, rec):
        fill(rec, 0, 200)
        delay = recorder.PLAYBACK_DELAY_SECONDS

        # A reaction seen in chat between 100s and 110s, with 20s lead-in
        # and 6s tail: footage from (100 - 20 - delay) to (110 + 6 - delay).
        segments = recorder._clip_segments(rec, at(100), at(110), pre_roll_seconds=20, post_roll_seconds=6)

        assert segments[0].started_at <= at(100 - 20 - delay) < segments[0].ended_at + timedelta(seconds=2)
        assert segments[-1].started_at <= at(110 + 6 - delay) <= segments[-1].ended_at

    def test_footage_is_shifted_earlier_than_chat_time(self, rec):
        # Viewers react to what they saw a few seconds ago; with no pre- or
        # post-roll the clip still ends before the chat window does.
        assert recorder.PLAYBACK_DELAY_SECONDS > 0
        fill(rec, 0, 200)

        segments = recorder._clip_segments(rec, at(100), at(110), pre_roll_seconds=0, post_roll_seconds=0)

        assert segments[-1].started_at < at(110)

    def test_no_footage_for_the_window_is_an_error(self, rec):
        fill(rec, 0, 20)

        with pytest.raises(RecorderError, match="No buffered segments"):
            recorder._clip_segments(rec, at(500), at(510), pre_roll_seconds=10, post_roll_seconds=10)

    def test_a_window_straddling_a_break_uses_the_longer_side(self, rec):
        delay = recorder.PLAYBACK_DELAY_SECONDS
        fill(rec, 0, 100, group="1-0")
        fill(rec, 100, 200, group="1-1")

        mostly_before = recorder._clip_segments(rec, at(90 + delay), at(104 + delay), 10, 0)
        mostly_after = recorder._clip_segments(rec, at(104 + delay), at(120 + delay), 10, 0)

        assert {segment.group for segment in mostly_before} == {"1-0"}
        assert {segment.group for segment in mostly_after} == {"1-1"}


class TestContextClipNames:
    def test_sit_next_to_the_clip_they_belong_to(self):
        assert recorder.context_clip_names("moment_805.mp4") == {
            "before": "moment_805_before.mp4",
            "after": "moment_805_after.mp4",
        }


class TestPruning:
    """Segment times here are relative to the real clock, like the buffer's
    retention window is."""

    def now(self, seconds_ago: float) -> datetime:
        return datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)

    def age(self, path: Path, seconds: float) -> None:
        stamp = time.time() - seconds
        os.utime(path, (stamp, stamp))

    def test_segments_older_than_the_retention_window_are_deleted(self, rec):
        retention = recorder.BUFFER_RETENTION_SECONDS
        expired = add_segment(rec, self.now(retention + 60))
        kept = add_segment(rec, self.now(retention - 60))

        rec._prune()

        assert not expired.exists()
        assert kept.exists()

    def test_a_segment_still_reaching_into_the_window_is_kept(self, rec):
        retention = recorder.BUFFER_RETENTION_SECONDS
        straddling = add_segment(rec, self.now(retention + 5), duration=30)

        rec._prune()

        assert straddling.exists()

    def test_run_directories_left_by_the_old_recorder_are_removed(self, rec):
        old_run = rec._base_dir / "20260101T120000Z"
        old_run.mkdir()
        (old_run / "000000.ts").write_bytes(b"old footage")
        self.age(old_run, recorder.BUFFER_RETENTION_SECONDS + 60)

        rec._prune()

        assert not old_run.exists()

    def test_abandoned_downloads_are_removed_but_one_in_progress_is_not(self, rec):
        abandoned = rec._base_dir / "1_2000_1-0.ts.part"
        abandoned.write_bytes(b"half")
        self.age(abandoned, recorder.BUFFER_RETENTION_SECONDS + 60)
        in_progress = rec._base_dir / "2_2000_1-0.ts.part"
        in_progress.write_bytes(b"half")

        rec._prune()

        assert not abandoned.exists()
        assert in_progress.exists()

    def test_unrelated_files_are_left_alone(self, rec):
        notes = rec._base_dir / "notes.txt"
        notes.write_text("keep me")
        self.age(notes, recorder.BUFFER_RETENTION_SECONDS + 60)

        rec._prune()

        assert notes.exists()

    def test_a_file_that_cannot_be_deleted_does_not_stop_the_rest(self, rec, monkeypatch):
        # Windows: an antivirus scan or a clip being cut can hold a segment
        # open for a moment. It is retried on the next tick.
        retention = recorder.BUFFER_RETENTION_SECONDS
        locked = add_segment(rec, self.now(retention + 120))
        deletable = add_segment(rec, self.now(retention + 60))
        real_unlink = Path.unlink

        def unlink(self, missing_ok=False):
            if self == locked:
                raise PermissionError("file is in use")
            real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", unlink)

        rec._prune()

        assert locked.exists()
        assert not deletable.exists()


class TestAdGroups:
    def test_a_group_recorded_during_an_ad_is_recognisable(self, rec):
        add_segment(rec, at(0), group="1-0")
        add_segment(rec, at(2), group="1-1" + recorder.AD_GROUP_SUFFIX)

        assert [segment.is_ad for segment in rec._stored_segments()] == [False, True]

    def test_an_ad_is_never_preferred_to_real_footage_however_long_it_is(self, rec):
        fill(rec, 0, 4, group="1-0")
        fill(rec, 4, 30, group="1-1" + recorder.AD_GROUP_SUFFIX)
        segments = rec.segments_overlapping(at(1), at(30))

        chosen = recorder._best_group(segments, at(1), at(30))

        assert {segment.group for segment in chosen} == {"1-0"}

    def test_an_ad_is_still_better_than_nothing(self, rec):
        fill(rec, 0, 10, group="1-0" + recorder.AD_GROUP_SUFFIX)
        segments = rec.segments_overlapping(at(2), at(8))

        assert recorder._best_group(segments, at(2), at(8)) == segments
