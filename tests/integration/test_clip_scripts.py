"""The command-line helpers that work on clip files, against real files cut
by the real ffmpeg: filling in a clip's analysis results, and giving older
moments one video file each.

What these scripts do to the database alone is in tests/unit/test_scripts.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import backfill_taste
import join_context_clips
from kick_clip_hunter import audio_events, frame_encoder, recorder, sound_events, transcriber
from kick_clip_hunter.recorder import ChannelRecorder
from tests.support.data import T0, add_moment, minutes, moment
from tests.support.media import decodes_cleanly, probe

pytestmark = pytest.mark.ffmpeg

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
CHANNEL = "some_channel"
TOLERANCE = 0.35


def at(seconds: float) -> datetime:
    return BASE + timedelta(seconds=seconds)


@pytest.fixture
def rec(ts_segments) -> ChannelRecorder:
    """A recorder whose buffer holds seconds 0-39 of one unbroken stream."""
    channel_recorder = ChannelRecorder(CHANNEL)
    for index in range(40):
        name = f"{int(at(index).timestamp() * 1000)}_1000_1-0.ts"
        (channel_recorder._base_dir / name).write_bytes(ts_segments[index].read_bytes())
    return channel_recorder


def files() -> list[str]:
    return sorted(path.name for path in (recorder.CLIPS_DIR / CHANNEL).iterdir())


class TestBackfillOnAFileWithContext:
    def test_the_steps_are_given_the_clip_not_everything_in_its_file(self, rec, monkeypatch):
        monkeypatch.setattr(recorder, "CONTEXT_BEFORE_SECONDS", 6)
        monkeypatch.setattr(recorder, "CONTEXT_AFTER_SECONDS", 8)
        delay = timedelta(seconds=recorder.PLAYBACK_DELAY_SECONDS)
        clip = recorder.extract_clip(rec, at(15) + delay, at(22) + delay, "moment_1.mp4", 0, 0)
        moment_id = add_moment(
            clip_path=f"{CHANNEL}/moment_1.mp4",
            clip_duration=clip.duration,
            context_before=clip.context_before,
            context_after=clip.context_after,
        )
        given: dict[str, float] = {}

        def step(name, result):
            def run(clip_path):
                given[name] = probe(clip_path)["duration"]
                return result

            return run

        monkeypatch.setattr(transcriber, "transcribe_clip", step("transcript", "hello"))
        monkeypatch.setattr(audio_events, "detect_audio_events", step("audio", "en, HAPPY"))
        monkeypatch.setattr(frame_encoder, "encode_clip", step("frames", b"\x01\x02"))
        monkeypatch.setattr(sound_events, "tag_sound_events", step("sound", ("Laughter:0.80", b"\x03\x04")))

        backfill_taste.main(limit=None)

        assert given == {name: pytest.approx(clip.duration, abs=TOLERANCE) for name in given}
        assert sorted(given) == ["audio", "frames", "sound", "transcript"]
        assert moment(moment_id)["transcript"] == "hello"
        # The file itself is as it was, and nothing was left next to it.
        assert probe(clip.path)["duration"] == pytest.approx(6 + clip.duration + 8, abs=TOLERANCE)
        assert files() == ["moment_1.mp4"]


class TestJoiningOlderMoments:
    """scripts/join_context_clips.py: moments stored as a clip with context
    files next to it become moments with one file."""

    CLIP_START = "2026-01-01T12:00:14+00:00"

    def older_moment(self, rec, number: int = 1, sides=("before", "after"), **columns) -> int:
        """A clip of seconds 14-23 with 8-14 and 23-31 in context files, and
        the moment it belongs to as it was stored then."""
        name = f"moment_{number}.mp4"
        segments = {segment.started_at: segment for segment in rec._stored_segments()}
        names = recorder.context_clip_names(name)
        spans = {"before": range(8, 14), "after": range(23, 31)}
        for side in sides:
            recorder._write_clip(rec, [segments[at(second)] for second in spans[side]], names[side])
        recorder._write_clip(rec, [segments[at(second)] for second in range(14, 23)], name)
        stored = {"clip_start": self.CLIP_START, "clip_duration": 9.0, **columns}
        return add_moment(CHANNEL, detected_at=T0 + minutes(number), clip_path=f"{CHANNEL}/{name}", **stored)

    def context(self, moment_id: int) -> tuple:
        row = moment(moment_id)
        return row["clip_duration"], row["context_before"], row["context_after"]

    def test_a_moment_gets_one_file_and_where_its_clip_lies_in_it(self, rec, capsys):
        moment_id = self.older_moment(rec)

        join_context_clips.main()

        assert files() == ["moment_1.mp4"]
        clip = recorder.CLIPS_DIR / CHANNEL / "moment_1.mp4"
        assert probe(clip)["duration"] == pytest.approx(23, abs=TOLERANCE)
        assert decodes_cleanly(clip)
        assert self.context(moment_id) == pytest.approx((9.0, 6, 8), abs=TOLERANCE)
        # Where the clip starts on the stream's clock has not moved.
        assert moment(moment_id)["clip_start"] == self.CLIP_START
        output = capsys.readouterr().out
        assert f"moment {moment_id}: one file now, 6 s before the clip and 8 s after it" in output
        assert "1 moment(s) joined" in output

    def test_the_clips_stored_length_is_kept_and_a_missing_one_is_measured(self, rec):
        stored = self.older_moment(rec, 1, clip_duration=9.25)
        unknown = self.older_moment(rec, 2, clip_start=None, clip_duration=None)

        join_context_clips.main()

        assert self.context(stored)[0] == 9.25
        assert self.context(unknown)[0] == pytest.approx(9, abs=TOLERANCE)

    def test_a_dry_run_only_says_what_it_would_do(self, rec, capsys):
        moment_id = self.older_moment(rec)

        join_context_clips.main(dry_run=True)

        assert files() == ["moment_1.mp4", "moment_1_after.mp4", "moment_1_before.mp4"]
        assert self.context(moment_id) == (9.0, None, None)
        output = capsys.readouterr().out
        assert f"moment {moment_id}: would join moment_1.mp4 with moment_1_before.mp4, moment_1_after.mp4" in output
        assert "1 moment(s) to join" in output

    def test_running_it_again_changes_nothing(self, rec, capsys):
        moment_id = self.older_moment(rec)
        join_context_clips.main()
        after_first = self.context(moment_id), (recorder.CLIPS_DIR / CHANNEL / "moment_1.mp4").read_bytes()
        capsys.readouterr()

        join_context_clips.main()

        assert (self.context(moment_id), (recorder.CLIPS_DIR / CHANNEL / "moment_1.mp4").read_bytes()) == after_first
        assert capsys.readouterr().out.strip() == "0 moment(s) joined"

    def test_moments_that_have_nothing_to_join_are_left_alone(self, rec, capsys):
        alone = self.older_moment(rec, 1, sides=())
        add_moment(CHANNEL, detected_at=T0)  # no clip at all
        add_moment(CHANNEL, detected_at=T0, clip_path=f"{CHANNEL}/gone.mp4")  # its file is not there
        content = (recorder.CLIPS_DIR / CHANNEL / "moment_1.mp4").read_bytes()

        join_context_clips.main()

        assert self.context(alone) == (9.0, None, None)
        assert (recorder.CLIPS_DIR / CHANNEL / "moment_1.mp4").read_bytes() == content
        assert capsys.readouterr().out.strip() == "0 moment(s) joined"

    def test_a_moment_that_cannot_be_joined_is_reported_and_the_rest_still_are(self, rec, capsys):
        broken = self.older_moment(rec, 1)
        fine = self.older_moment(rec, 2)
        Path(recorder.CLIPS_DIR / CHANNEL / "moment_1_after.mp4").write_bytes(b"not really video")

        join_context_clips.main()

        assert self.context(broken) == (9.0, None, None)
        assert self.context(fine) == pytest.approx((9.0, 6, 8), abs=TOLERANCE)
        assert files() == ["moment_1.mp4", "moment_1_after.mp4", "moment_1_before.mp4", "moment_2.mp4"]
        output = capsys.readouterr().out
        assert f"moment {broken}: could not be measured, files left as they were" in output
        assert "1 moment(s) joined" in output

    def test_a_join_that_fails_is_reported_too(self, rec, capsys, monkeypatch):
        moment_id = self.older_moment(rec)
        monkeypatch.setattr(recorder, "JOIN_TOLERANCE_SECONDS", -1.0)  # nothing is close enough

        join_context_clips.main()

        assert self.context(moment_id) == (9.0, None, None)
        assert files() == ["moment_1.mp4", "moment_1_after.mp4", "moment_1_before.mp4"]
        assert f"moment {moment_id}: joining failed, files left as they were" in capsys.readouterr().out

    def test_only_one_side_of_context_is_joined_as_that(self, rec):
        moment_id = self.older_moment(rec, sides=("after",))

        join_context_clips.main()

        assert files() == ["moment_1.mp4"]
        assert self.context(moment_id) == pytest.approx((9.0, 0, 8), abs=TOLERANCE)
