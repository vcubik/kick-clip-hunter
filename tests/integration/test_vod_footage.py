"""A moment's file made longer from its broadcast's VOD, with the real
ffmpeg: a VOD on a fake HLS server, cut into longer segments than the live
stream was but of the same encoding - as Kick's and Twitch's are - and the
moment's file cut from the live stream's segments."""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kick_clip_hunter import vod_footage
from kick_clip_hunter.platforms import Vod
from kick_clip_hunter.vod_footage import FootageError
from tests.support.hls import Segment
from tests.support.media import decodes_cleanly, make_fmp4_segments, make_ts_segments, probe, require_ffmpeg

pytestmark = pytest.mark.ffmpeg

START = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
# How far a cut may run on from where it was asked to end: the frame there.
TOLERANCE = 0.15
# The moment's file holds the broadcast's seconds 10 to 20.
FIRST, LAST = 10, 20


@pytest.fixture(autouse=True)
def _ffmpeg():
    require_ffmpeg()


def second(seconds: float) -> datetime:
    return START + timedelta(seconds=seconds)


def joined(paths: list[Path], into: Path, init: bytes | None = None) -> Path:
    """Segments of one encoding joined and remuxed into one file, as the
    recorder cuts a moment's file."""
    into.parent.mkdir(parents=True, exist_ok=True)
    raw = into.with_suffix(".joined")
    raw.write_bytes((init or b"") + b"".join(path.read_bytes() for path in paths))
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(raw), "-c", "copy", str(into)], check=True)
    raw.unlink()
    return into


@pytest.fixture
def moment_file(tmp_path, ts_segments) -> Path:
    """The moment's file: the live stream's one-second segments of the
    broadcast's seconds 10 to 20."""
    return joined(ts_segments[FIRST:LAST], tmp_path / "clips" / "moment_7.mp4")


def vod_timeline(ts_segments, seconds_each: int = 5, muted: set[int] = frozenset()) -> list[Segment]:
    """The broadcast's VOD: the same footage in longer segments."""
    return [
        Segment(
            b"".join(path.read_bytes() for path in ts_segments[first : first + seconds_each]),
            duration=float(seconds_each),
            muted=first // seconds_each in muted,
        )
        for first in range(0, len(ts_segments), seconds_each)
    ]


@pytest.fixture
def vod(hls, ts_segments):
    timeline = vod_timeline(ts_segments)
    return hls(timeline, published=len(timeline), window=len(timeline), stream_start=START)


def longer(moment_file: Path) -> Path:
    return moment_file.with_name("moment_7.longer.mp4")


def pictures(path: Path) -> list[tuple[float, str]]:
    return vod_footage.keyframes(path)


def holds(new: Path, old: Path, lead: float) -> bool:
    """Whether every keyframe of `old` is in `new`, `lead` seconds later."""
    there = {picture: time for time, picture in pictures(new)}
    return all(abs(there.get(picture, -99.0) - (time + lead)) < 0.01 for time, picture in pictures(old))


class TestMakingAFileLonger:
    def test_footage_before_and_after_is_added_from_the_vod(self, moment_file, vod):
        made = vod_footage.lengthen(moment_file, second(FIRST), Vod(vod.master_url), 4, 3, longer(moment_file))

        assert (made.lead, made.added_before) == (pytest.approx(4, abs=0.01), pytest.approx(4, abs=0.01))
        assert made.added_after == pytest.approx(3, abs=TOLERANCE)
        assert made.seconds == pytest.approx(4 + 10 + 3, abs=TOLERANCE)
        assert (made.short, made.muted) == (False, False)
        assert probe(made.path) == {"duration": pytest.approx(made.seconds, abs=0.05), "streams": ["audio", "video"]}
        assert decodes_cleanly(made.path)

    def test_the_old_file_is_in_the_new_one_frame_for_frame(self, moment_file, vod):
        made = vod_footage.lengthen(moment_file, second(FIRST), Vod(vod.master_url), 4, 3, longer(moment_file))

        assert holds(made.path, moment_file, made.lead)

    def test_only_footage_after_starts_the_new_file_with_the_old_ones_first_frame(self, moment_file, vod):
        made = vod_footage.lengthen(moment_file, second(FIRST), Vod(vod.master_url), 0, 6, longer(moment_file))

        assert made.lead == pytest.approx(0, abs=0.01)
        assert made.added_after == pytest.approx(6, abs=TOLERANCE)
        assert pictures(made.path)[0][1] == pictures(moment_file)[0][1]

    def test_it_begins_on_the_last_frame_a_cut_can_begin_on_before_what_was_asked(self, moment_file, vod):
        # Keyframes come every second: 2.5 s before the file asks for the
        # one 3 s before it.
        made = vod_footage.lengthen(moment_file, second(FIRST), Vod(vod.master_url), 2.5, 2, longer(moment_file))

        assert made.lead == pytest.approx(3, abs=0.01)
        assert made.short is False

    def test_the_old_file_is_left_as_it_was_and_nothing_else_behind(self, moment_file, vod):
        content = moment_file.read_bytes()

        vod_footage.lengthen(moment_file, second(FIRST), Vod(vod.master_url), 4, 3, longer(moment_file))

        assert moment_file.read_bytes() == content
        assert sorted(path.name for path in moment_file.parent.iterdir()) == ["moment_7.longer.mp4", "moment_7.mp4"]

    def test_only_the_segments_it_needs_are_fetched_once_each(self, moment_file, vod):
        vod_footage.lengthen(moment_file, second(FIRST), Vod(vod.master_url), 4, 3, longer(moment_file))

        # The VOD's segments: 5-10 before, 10-20 the file, 20-25 after.
        assert sorted(vod.segment_requests()) == [1, 2, 3, 4]


class TestFindingTheFileInTheVod:
    """The clocks are seconds apart; the pictures say where the file is."""

    @pytest.mark.parametrize("off", [2.5, -3.0, 0.4])
    def test_a_clock_seconds_off_still_finds_it_exactly(self, moment_file, vod, off):
        made = vod_footage.lengthen(moment_file, second(FIRST + off), Vod(vod.master_url), 4, 3, longer(moment_file))

        assert made.lead == pytest.approx(4, abs=0.01)
        assert holds(made.path, moment_file, made.lead)

    def test_a_vod_without_times_of_its_own_is_counted_on_from_when_its_broadcast_began(
        self, moment_file, hls, ts_segments
    ):
        timeline = vod_timeline(ts_segments)
        server = hls(timeline, published=len(timeline), window=len(timeline), program_date_time=False)

        # As Twitch says when a past broadcast began: some seconds off.
        vod = Vod(server.master_url, started_at=START - timedelta(seconds=3))
        made = vod_footage.lengthen(moment_file, second(FIRST), vod, 4, 3, longer(moment_file))

        assert made.lead == pytest.approx(4, abs=0.01)
        assert holds(made.path, moment_file, made.lead)

    def test_footage_of_another_picture_is_not_taken(self, moment_file, hls, tmp_path):
        other = make_ts_segments(tmp_path / "other", count=40, seconds_each=1.0, size="128x96")
        timeline = vod_timeline(other)
        server = hls(timeline, published=len(timeline), window=len(timeline), stream_start=START)

        with pytest.raises(FootageError, match="not found in the VOD by its pictures"):
            vod_footage.lengthen(moment_file, second(FIRST), Vod(server.master_url), 4, 3, longer(moment_file))
        assert not longer(moment_file).exists()

    def test_a_picture_that_never_changes_cannot_be_found_by_it(self, tmp_path, vod):
        still = tmp_path / "clips" / "moment_7.mp4"
        still.parent.mkdir(parents=True)
        # fmt: off
        subprocess.run(
            [
                "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=gray:size=160x120:rate=10:duration=4",
                "-c:v", "libx264", "-g", "10", "-pix_fmt", "yuv420p", str(still),
            ],
            check=True,
        )
        # fmt: on

        with pytest.raises(FootageError, match="does not change"):
            vod_footage.lengthen(still, second(FIRST), Vod(vod.master_url), 4, 3, longer(still))


class TestAYouTubeStreamWoundBack:
    """The live playlist lists only its newest segments; the older ones are
    had by their numbers in a segment's address."""

    @pytest.fixture
    def live(self, hls, ts_segments):
        timeline = [Segment(path.read_bytes(), duration=1.0) for path in ts_segments]
        return hls(
            timeline,
            published=len(timeline),
            window=3,
            stream_start=START,
            numbered=True,
            program_date_time_once=True,
        )

    def test_footage_long_gone_from_the_playlist_is_had_by_its_number(self, moment_file, live):
        made = vod_footage.lengthen(
            moment_file, second(FIRST), Vod(live.master_url, numbered=True), 4, 3, longer(moment_file)
        )

        assert made.lead == pytest.approx(4, abs=0.01)
        assert made.seconds == pytest.approx(17, abs=TOLERANCE)
        assert holds(made.path, moment_file, made.lead)
        assert set(range(6, 23)) <= set(live.segment_requests())

    def test_nothing_newer_than_the_newest_listed_is_asked_for(self, moment_file, hls, ts_segments):
        timeline = [Segment(path.read_bytes(), duration=1.0) for path in ts_segments]
        live = hls(timeline, published=22, window=3, stream_start=START, numbered=True, program_date_time_once=True)

        made = vod_footage.lengthen(
            moment_file, second(FIRST), Vod(live.master_url, numbered=True), 0, 10, longer(moment_file)
        )

        assert made.added_after == pytest.approx(2, abs=TOLERANCE)
        assert made.short is True
        assert max(live.segment_requests()) == 21


class TestWhatIsNotThere:
    def test_a_vod_that_does_not_reach_as_far_yet_gives_what_it_has_and_says_so(self, moment_file, hls, ts_segments):
        timeline = vod_timeline(ts_segments)
        server = hls(timeline, published=5, window=len(timeline), stream_start=START)  # reaches 25 s

        made = vod_footage.lengthen(moment_file, second(FIRST), Vod(server.master_url), 2, 10, longer(moment_file))

        assert made.added_after == pytest.approx(5, abs=TOLERANCE)
        assert made.short is True

    def test_a_vod_with_nothing_more_is_an_error_and_leaves_nothing(self, moment_file, hls, ts_segments):
        timeline = vod_timeline(ts_segments)
        server = hls(timeline, published=4, window=len(timeline), stream_start=START)  # reaches 20 s

        with pytest.raises(FootageError, match="nothing of the stream there"):
            vod_footage.lengthen(moment_file, second(FIRST), Vod(server.master_url), 0, 5, longer(moment_file))
        assert sorted(path.name for path in moment_file.parent.iterdir()) == ["moment_7.mp4"]

    def test_a_vod_that_cannot_be_fetched_is_said(self, moment_file, vod):
        vod.fail_next["/master.m3u8"] = [503, 503]

        with pytest.raises(FootageError, match=r"could not be fetched \(503\)"):
            vod_footage.lengthen(moment_file, second(FIRST), Vod(vod.master_url), 4, 3, longer(moment_file))

    def test_a_vod_silenced_where_the_clip_is_is_not_taken(self, moment_file, hls, ts_segments):
        # Twitch silenced 10-15 s: the clip would lose its sound.
        timeline = vod_timeline(ts_segments, muted={2})
        server = hls(timeline, published=len(timeline), window=len(timeline), stream_start=START)

        with pytest.raises(FootageError, match="silenced the sound of this stretch"):
            vod_footage.lengthen(moment_file, second(FIRST), Vod(server.master_url), 4, 3, longer(moment_file))

    def test_silenced_footage_only_among_what_is_added_is_taken_and_said(self, moment_file, hls, ts_segments):
        timeline = vod_timeline(ts_segments, muted={1})  # 5-10 s
        server = hls(timeline, published=len(timeline), window=len(timeline), stream_start=START)

        made = vod_footage.lengthen(moment_file, second(FIRST), Vod(server.master_url), 4, 3, longer(moment_file))

        assert made.muted is True


class TestFragmentedMp4:
    """Twitch's VOD of a stream that is on is fragmented MP4: segments that
    play only after an initialization segment."""

    def test_its_fragments_are_joined_after_their_initialization_segment(self, tmp_path, hls):
        init, pieces = make_fmp4_segments(tmp_path / "vod", count=8, seconds_each=5.0)
        moment_file = joined(pieces[2:4], tmp_path / "clips" / "moment_7.mp4", init=init)
        timeline = [Segment(piece.read_bytes(), duration=5.0, title="") for piece in pieces]
        server = hls(timeline, published=len(timeline), window=len(timeline), stream_start=START, init=init)

        made = vod_footage.lengthen(moment_file, second(FIRST), Vod(server.master_url), 5, 5, longer(moment_file))

        assert made.lead == pytest.approx(5, abs=0.01)
        assert made.seconds == pytest.approx(20, abs=TOLERANCE)
        assert holds(made.path, moment_file, made.lead)
        assert server.requests.count("/segment/init.mp4") == 1


class TestTellingHowFarItHasGot:
    def test_each_step_is_told_and_how_much_has_been_fetched(self, moment_file, vod):
        told = []

        vod_footage.lengthen(
            moment_file, second(FIRST), Vod(vod.master_url), 4, 3, longer(moment_file), lambda *step: told.append(step)
        )

        assert told[0] == (vod_footage.FINDING, None)
        fetched = [done for what, done in told if what == vod_footage.FETCHING]
        assert fetched[0] == 0.0 and fetched[-1] == 1.0 and fetched == sorted(fetched)
        assert told[-1] == (vod_footage.JOINING, None)

    def test_one_who_gives_up_gives_it_up_and_nothing_is_left_behind(self, moment_file, vod):
        class GivenUp(Exception):
            pass

        def give_up(what, done):
            if what == vod_footage.FETCHING:
                raise GivenUp

        with pytest.raises(GivenUp):
            vod_footage.lengthen(moment_file, second(FIRST), Vod(vod.master_url), 4, 3, longer(moment_file), give_up)
        assert sorted(path.name for path in moment_file.parent.iterdir()) == ["moment_7.mp4"]
