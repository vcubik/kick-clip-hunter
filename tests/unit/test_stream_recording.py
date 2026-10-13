"""A stream kept whole: what is written while it is recorded, how that is
made into one file afterwards, and how a second of the file is tied back to
when it was on air.

The writer and the arithmetic are tested with made-up bytes and times. The
making of the file runs real ffmpeg over real (tiny) MPEG-TS segments.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kick_clip_hunter import stream_recording
from kick_clip_hunter.recorder import AD_GROUP_SUFFIX
from kick_clip_hunter.stream_recording import Part, SaveError, StreamWriter
from tests.support.media import decodes_cleanly, make_ts_segments, probe

# Before the suite's stand-in is put in its place (see conftest.roomy_disk).
real_free_bytes = stream_recording.free_bytes

START = datetime(2026, 3, 1, 20, 0, 0, tzinfo=timezone.utc)
SEGMENT = 2.0


def at(seconds: float) -> datetime:
    return START + timedelta(seconds=seconds)


def keep(writer: StreamWriter, *segments: tuple[float, str, bytes]) -> None:
    """Hands the writer segments as (seconds after START, group, bytes)."""
    for offset, group, content in segments:
        writer.add(at(offset), SEGMENT, group, content)


def kept(folder: Path) -> dict[str, bytes]:
    """What a recording's folder holds, by file name."""
    return {path.name: path.read_bytes() for path in sorted(folder.iterdir())}


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    return tmp_path / "stream_1_parts"


@pytest.fixture
def writer(folder: Path):
    """A writer that is closed when the test is over, whatever the test did
    with it."""
    writer = StreamWriter(folder)
    yield writer
    writer.close()


def footage(segments: list[Path]) -> bytes:
    return b"".join(path.read_bytes() for path in segments)


def part_of(folder: Path, offset: float, segments: list[Path]) -> Path:
    """Puts segments into a recording's folder as one part that began
    `offset` seconds after START."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / stream_recording.part_name(at(offset))
    path.write_bytes(footage(segments))
    return path


class TestWhereItIsKept:
    def test_a_recording_has_a_folder_of_its_own_under_its_channel(self):
        assert stream_recording.parts_folder("some_channel", 12) == (
            stream_recording.STREAMS_DIR / "some_channel" / "stream_12_parts"
        )

    def test_its_file_is_named_for_when_it_began_and_whose_stream_it_is(self):
        name = stream_recording.file_name("some_channel", 12, datetime(2026, 3, 1, 21, 4, 59))

        assert name == "2026-03-01_21-04_some_channel_12.mp4"

    def test_files_of_one_channel_sort_in_the_order_they_were_recorded(self):
        names = [
            stream_recording.file_name("a", 3, datetime(2026, 3, 2, 9, 0)),
            stream_recording.file_name("a", 1, datetime(2026, 2, 28, 23, 0)),
            stream_recording.file_name("a", 2, datetime(2026, 3, 1, 21, 4)),
        ]

        assert sorted(names) == [names[1], names[2], names[0]]

    @pytest.mark.parametrize("slug", ["../../etc", "a/b", "a\\b", "con:1", "<b>x</b>", ""])
    def test_a_channel_name_can_not_put_anything_outside_the_folder(self, slug):
        folder = stream_recording.parts_folder(slug, 1)
        name = stream_recording.file_name(slug, 1, datetime(2026, 3, 1, 21, 4))

        assert folder.parent.parent == stream_recording.STREAMS_DIR
        assert not set(folder.parent.name) & set('./\\:<>"')
        assert not set(name.removesuffix(".mp4")) & set('./\\:<>"')

    def test_how_much_room_the_disk_has_is_asked_where_recordings_are_kept(self):
        assert real_free_bytes() > 0
        assert stream_recording.STREAMS_DIR.is_dir()


class TestWhatIsWritten:
    def test_segments_that_follow_each_other_are_one_part(self, folder):
        writer = StreamWriter(folder)

        keep(writer, (0, "7-0", b"one;"), (2, "7-0", b"two;"), (4, "7-0", b"three;"))
        writer.close()

        assert kept(folder) == {stream_recording.part_name(START): b"one;two;three;"}

    def test_a_part_is_named_for_when_its_first_frame_was_broadcast(self):
        assert stream_recording.part_name(START) == f"part_{int(START.timestamp() * 1000)}.ts"

    def test_to_the_millisecond_whatever_the_millisecond(self):
        # The stream's clock is told in milliseconds, and a part's name is
        # where its start is read back from.
        first = int(START.timestamp()) * 1000

        names = [stream_recording.part_name(START + timedelta(milliseconds=ms)) for ms in range(1000)]

        assert names == [f"part_{first + ms}.ts" for ms in range(1000)]

    def test_a_break_in_the_stream_starts_the_next_part(self, folder):
        writer = StreamWriter(folder)

        keep(writer, (0, "7-0", b"one;"), (2, "7-0", b"two;"), (10, "7-1", b"later;"), (12, "7-1", b"last;"))
        writer.close()

        assert kept(folder) == {
            stream_recording.part_name(at(0)): b"one;two;",
            stream_recording.part_name(at(10)): b"later;last;",
        }

    def test_an_ad_is_left_out_and_what_follows_it_is_a_new_part(self, folder):
        writer = StreamWriter(folder)
        ad = f"7-1{AD_GROUP_SUFFIX}"

        keep(writer, (0, "7-0", b"one;"), (2, ad, b"ad;"), (4, ad, b"ad;"), (6, "7-2", b"back;"))
        writer.close()

        assert kept(folder) == {
            stream_recording.part_name(at(0)): b"one;",
            stream_recording.part_name(at(6)): b"back;",
        }
        assert writer.seconds == 2 * SEGMENT

    def test_what_is_already_kept_is_not_kept_again(self, folder):
        # A recorder that starts over lists the last few segments again,
        # under a group of its own.
        writer = StreamWriter(folder)

        keep(writer, (0, "7-0", b"one;"), (2, "7-0", b"two;"), (4, "7-0", b"three;"))
        keep(writer, (2, "8-0", b"two;"), (4, "8-0", b"three;"), (6, "8-0", b"four;"))
        writer.close()

        assert kept(folder) == {
            stream_recording.part_name(at(0)): b"one;two;three;",
            stream_recording.part_name(at(6)): b"four;",
        }
        assert writer.seconds == 4 * SEGMENT

    def test_it_counts_what_it_has(self, writer):
        assert (writer.seconds, writer.size, writer.started_at) == (0.0, 0, None)

        keep(writer, (0, "7-0", b"12345"), (2, "7-0", b"678"))

        assert (writer.seconds, writer.size, writer.started_at) == (2 * SEGMENT, 8, START)

    def test_it_knows_how_long_ago_it_was_last_given_anything(self, writer):
        assert writer.idle_seconds() is None

        keep(writer, (0, "7-0", b"one;"))

        assert 0 <= writer.idle_seconds() < 5

    def test_an_ad_does_not_count_as_footage_arriving(self, writer):
        keep(writer, (0, f"7-0{AD_GROUP_SUFFIX}", b"ad;"))

        assert writer.idle_seconds() is None and writer.seconds == 0

    def test_what_is_written_is_on_disk_before_the_writer_is_closed(self, writer, folder):
        # A recording the service dies under must not lose what it had.
        keep(writer, (0, "7-0", b"one;"), (2, "7-0", b"two;"))

        assert (folder / stream_recording.part_name(START)).read_bytes() == b"one;two;"

    def test_nothing_is_kept_once_it_is_closed(self, folder):
        writer = StreamWriter(folder)
        keep(writer, (0, "7-0", b"one;"))

        writer.close()
        writer.close()
        keep(writer, (2, "7-0", b"two;"))

        assert kept(folder) == {stream_recording.part_name(START): b"one;"}
        assert writer.seconds == SEGMENT

    def test_a_disk_that_can_not_be_written_to_ends_the_keeping_without_an_error(self, writer, folder, caplog):
        # The folder is gone from under it: opening a part fails.
        folder.rmdir()

        keep(writer, (0, "7-0", b"one;"), (2, "7-0", b"two;"))

        assert writer.failed is True
        assert (writer.seconds, writer.size) == (0.0, 0)
        assert sum("could not write" in record.getMessage() for record in caplog.records) == 1


class TestWhatAFolderHolds:
    def test_when_the_first_frame_kept_was_broadcast(self, folder):
        writer = StreamWriter(folder)
        keep(writer, (30, "7-0", b"one;"), (40, "7-1", b"later;"))
        writer.close()

        assert stream_recording.kept_from(folder) == at(30)
        assert stream_recording.kept_bytes(folder) == len(b"one;later;")

    def test_nothing_where_nothing_was_kept(self, folder, tmp_path):
        StreamWriter(folder).close()

        assert stream_recording.kept_from(folder) is None
        assert stream_recording.kept_from(tmp_path / "never_made") is None
        assert stream_recording.kept_bytes(tmp_path / "never_made") == 0

    def test_a_part_that_was_opened_and_never_written_to_is_not_one(self, folder):
        folder.mkdir()
        (folder / stream_recording.part_name(at(0))).write_bytes(b"")
        (folder / stream_recording.part_name(at(8))).write_bytes(b"footage")
        (folder / "notes.txt").write_text("not a part", encoding="utf-8")

        assert stream_recording.kept_from(folder) == at(8)


class TestGoingOnAfterARestart:
    """A writer given a folder that holds footage already - a recording the
    service was restarted under - goes on from what there is."""

    @pytest.mark.ffmpeg
    def test_what_the_folder_holds_counts(self, folder, ts_segments):
        part_of(folder, 0, ts_segments[:6])
        part_of(folder, 30, ts_segments[10:14])

        writer = StreamWriter(folder)

        assert writer.started_at == START
        assert writer.seconds == pytest.approx(10, abs=0.4)
        assert writer.size == sum(path.stat().st_size for path in [*ts_segments[:6], *ts_segments[10:14]])
        writer.close()

    @pytest.mark.ffmpeg
    def test_what_follows_is_added_as_further_parts(self, folder, ts_segments):
        first = part_of(folder, 0, ts_segments[:6])
        before = first.read_bytes()
        writer = StreamWriter(folder)

        keep(writer, (60, "9-0", b"later;"), (62, "9-0", b"last;"))
        writer.close()

        assert kept(folder) == {
            stream_recording.part_name(at(0)): before,
            stream_recording.part_name(at(60)): b"later;last;",
        }
        assert writer.seconds == pytest.approx(6 + 2 * SEGMENT, abs=0.3)

    @pytest.mark.ffmpeg
    def test_footage_it_has_from_before_is_not_kept_again(self, folder, ts_segments):
        part_of(folder, 0, ts_segments[:6])
        writer = StreamWriter(folder)

        keep(writer, (2, "9-0", b"had it;"), (3.5, "9-0", b"had it;"), (6, "9-0", b"new;"))
        writer.close()

        assert kept(folder)[stream_recording.part_name(at(6))] == b"new;"
        assert len(kept(folder)) == 2

    @pytest.mark.ffmpeg
    def test_the_time_without_footage_is_counted_from_now(self, folder, ts_segments):
        # Not from never: a stream that does not come back still ends it.
        part_of(folder, 0, ts_segments[:3])

        writer = StreamWriter(folder)

        assert 0 <= writer.idle_seconds() < 5
        writer.close()

    def test_footage_that_can_not_be_measured_still_counts_for_its_size(self, folder):
        folder.mkdir()
        (folder / stream_recording.part_name(START)).write_bytes(b"not a video at all")

        writer = StreamWriter(folder)

        assert (writer.started_at, writer.seconds, writer.size) == (START, 0.0, 18)
        assert writer.idle_seconds() is not None
        writer.close()

    @pytest.mark.ffmpeg
    def test_how_long_each_part_in_a_folder_runs(self, folder, ts_segments):
        part_of(folder, 0, ts_segments[:6])
        part_of(folder, 30, ts_segments[10:14])

        lengths = stream_recording.kept_lengths(folder)

        assert lengths == {at(0): pytest.approx(6, abs=0.3), at(30): pytest.approx(4, abs=0.3)}

    def test_when_a_folder_was_last_added_to(self, folder, tmp_path):
        folder.mkdir()
        older = folder / stream_recording.part_name(at(0))
        newer = folder / stream_recording.part_name(at(30))
        older.write_bytes(b"one")
        newer.write_bytes(b"two")
        os.utime(older, (1_700_000_000, 1_700_000_000))
        os.utime(newer, (1_700_000_500, 1_700_000_500))

        assert stream_recording.last_written(folder) == datetime.fromtimestamp(1_700_000_500, tz=timezone.utc)
        assert stream_recording.last_written(tmp_path / "never_made") is None


class TestWhenASecondOfTheFileWasOnAir:
    # Two parts with a minute of the stream missing between them.
    PARTS = (Part(at(0), 100.0), Part(at(160), 50.0))

    def test_parts_are_stored_and_read_back(self):
        assert stream_recording.decoded(stream_recording.encoded(self.PARTS)) == list(self.PARTS)

    @pytest.mark.parametrize("stored", [None, "", "not json", '{"a": 1}', "[[1, 2, 3]]", '[["yesterday", 5]]'])
    def test_parts_that_can_not_be_read_are_no_parts(self, stored):
        assert stream_recording.decoded(stored) == []

    def test_the_file_runs_for_as_long_as_its_parts_do(self):
        assert stream_recording.length(self.PARTS) == 150.0
        assert stream_recording.length(()) == 0.0

    def test_what_fell_between_the_parts_is_missing(self):
        assert stream_recording.missing_seconds(self.PARTS) == 60.0
        assert stream_recording.missing_seconds(self.PARTS[:1]) == 0.0

    def test_parts_that_overlap_are_missing_nothing(self):
        assert stream_recording.missing_seconds((Part(at(0), 100.0), Part(at(99.5), 50.0))) == 0.0

    @pytest.mark.parametrize(
        ("second", "broadcast"),
        [(0, 0), (40.5, 40.5), (99.9, 99.9), (100, 160), (120, 180), (150, 210), (-5, 0), (400, 210)],
    )
    def test_a_second_of_the_file_is_when_its_part_had_it_on_air(self, second, broadcast):
        assert stream_recording.broadcast_time(self.PARTS, second) == at(broadcast)

    @pytest.mark.parametrize(
        ("broadcast", "second"),
        [(0, 0), (40.5, 40.5), (160, 100), (180, 120), (209, 149)],
    )
    def test_and_the_other_way_round(self, broadcast, second):
        assert stream_recording.file_second(self.PARTS, at(broadcast)) == second

    @pytest.mark.parametrize(
        ("broadcast", "second"),
        [(-30, 0), (100, 100), (130, 100), (159.9, 100), (210, 150), (900, 150)],
    )
    def test_a_time_the_file_has_no_footage_of_is_the_next_frame_there_is(self, broadcast, second):
        # Before it began, in the break between its parts, after it ended.
        assert stream_recording.file_second(self.PARTS, at(broadcast)) == second

    def test_one_part_is_simply_counted_from_its_start(self):
        parts = (Part(at(0), 3600.0),)

        assert stream_recording.broadcast_time(parts, 1800) == at(1800)
        assert stream_recording.file_second(parts, at(1800)) == 1800


@pytest.mark.ffmpeg
class TestMakingTheFile:
    @pytest.fixture
    def output(self, tmp_path: Path) -> Path:
        return tmp_path / "streams" / "some_channel" / "recording.mp4"

    def test_one_part_becomes_the_file(self, folder, output, ts_segments):
        part_of(folder, 0, ts_segments[:12])

        parts = stream_recording.save(folder, output)

        assert [part.started_at for part in parts] == [START]
        assert parts[0].seconds == pytest.approx(12, abs=0.3)
        assert probe(output) == {"duration": pytest.approx(12, abs=0.3), "streams": ["audio", "video"]}
        assert decodes_cleanly(output)

    def test_the_footage_stays_in_the_folder_until_the_caller_removes_it(self, folder, output, ts_segments):
        # Until the parts are stored with the recording, the folder is what
        # the file can be made from again.
        part = part_of(folder, 0, ts_segments[:6])

        stream_recording.save(folder, output)
        output.unlink()
        again = stream_recording.save(folder, output)

        assert part.exists()
        assert again[0].seconds == pytest.approx(6, abs=0.3) and output.exists()

    def test_several_parts_are_joined_in_the_order_they_were_on_air(self, folder, output, ts_segments):
        # Written out of order, as nothing says a folder lists them in order.
        part_of(folder, 30, ts_segments[20:40])
        part_of(folder, 0, ts_segments[:15])

        parts = stream_recording.save(folder, output)

        assert [part.started_at for part in parts] == [at(0), at(30)]
        assert [part.seconds for part in parts] == [pytest.approx(15, abs=0.3), pytest.approx(20, abs=0.3)]
        # The fifteen seconds that fell between them are not in the file.
        assert probe(output)["duration"] == pytest.approx(35, abs=0.4)
        assert decodes_cleanly(output)
        assert stream_recording.missing_seconds(parts) == pytest.approx(15, abs=0.3)

    def test_each_part_is_a_video_of_its_own_once_the_file_is_made(self, folder, output, ts_segments):
        # Never both the footage and the video of a part for long: a long
        # recording would need its room three times over.
        part_of(folder, 0, ts_segments[:5])
        part_of(folder, 30, ts_segments[20:25])

        stream_recording.save(folder, output)

        assert sorted(path.suffix for path in folder.iterdir()) == [".mp4", ".mp4"]

    def test_a_save_that_was_cut_short_can_be_done_again(self, folder, output, ts_segments):
        # The first part's video was made, the second's was not, and the
        # service stopped there.
        part_of(folder, 0, ts_segments[:5])
        stream_recording.save(folder, folder / "part_done.mp4")
        first = part_of(folder, 0, ts_segments[:5])
        (folder / "part_done.mp4").replace(first.with_suffix(".mp4"))
        first.unlink()
        part_of(folder, 30, ts_segments[20:25])

        parts = stream_recording.save(folder, output)

        assert [part.started_at for part in parts] == [at(0), at(30)]
        assert probe(output)["duration"] == pytest.approx(10, abs=0.4)

    def test_nothing_kept_means_no_file(self, folder, output):
        StreamWriter(folder).close()

        assert stream_recording.save(folder, output) == []
        assert not output.exists()

    def test_a_folder_that_is_not_there_means_no_file_either(self, tmp_path, output):
        assert stream_recording.save(tmp_path / "never_made", output) == []

    def test_footage_cut_off_mid_segment_is_still_saved(self, folder, output, ts_segments):
        # What a recording looks like that the service died under.
        whole = footage(ts_segments[:8])
        folder.mkdir()
        (folder / stream_recording.part_name(START)).write_bytes(whole[: len(whole) - 3000])

        parts = stream_recording.save(folder, output)

        assert parts[0].seconds == pytest.approx(8, abs=1.2)

    def test_a_part_no_video_can_be_made_of_is_left_out_of_the_rest(self, folder, output, ts_segments, caplog):
        part_of(folder, 0, ts_segments[:5])
        folder.joinpath(stream_recording.part_name(at(10))).write_bytes(b"not a video at all" * 100)
        part_of(folder, 30, ts_segments[20:25])

        parts = stream_recording.save(folder, output)

        assert [part.started_at for part in parts] == [at(0), at(30)]
        assert any("leaving" in record.getMessage() for record in caplog.records)
        assert decodes_cleanly(output)

    def test_when_no_video_can_be_made_at_all_it_says_why_and_keeps_what_there_is(self, folder, output):
        folder.mkdir()
        part = folder / stream_recording.part_name(START)
        part.write_bytes(b"not a video at all" * 100)

        with pytest.raises(SaveError, match="ffmpeg could not make a video"):
            stream_recording.save(folder, output)

        assert part.exists()
        assert not output.exists() and list(output.parent.iterdir()) == []

    def test_several_parts_none_of_which_is_a_video_say_so_too(self, folder, output):
        folder.mkdir()
        for offset in (0, 30):
            (folder / stream_recording.part_name(at(offset))).write_bytes(b"not a video at all" * 100)

        with pytest.raises(SaveError, match="no video could be made of anything"):
            stream_recording.save(folder, output)

        assert len(list(folder.glob("*.ts"))) == 2 and not output.exists()

    def test_parts_of_different_pictures_are_not_forced_into_one_file(self, folder, output, ts_segments, tmp_path):
        # A streamer who came back with another picture size. Joined without
        # re-encoding, that plays wrong from the seam on - so the parts stay
        # as they are, each a video that plays.
        other = make_ts_segments(tmp_path / "other", count=4, size="320x240")
        part_of(folder, 0, ts_segments[:5])
        part_of(folder, 30, other)

        with pytest.raises(SaveError, match="changed its picture or sound format"):
            stream_recording.save(folder, output)

        assert not output.exists()
        videos = sorted(folder.glob("*.mp4"))
        assert len(videos) == 2 and all(decodes_cleanly(video) for video in videos)
