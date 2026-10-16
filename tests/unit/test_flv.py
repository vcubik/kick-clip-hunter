"""A live FLV stream read as it arrives and cut into pieces a recorder can
keep (flv.py): its tags whole however the bytes come, what TikTok's
onMetaData says, pieces from keyframe to keyframe on the stream's own clock,
breaks that start a new run - and the MPEG-TS copies of the pieces numbered
on as one stream."""

from __future__ import annotations

import struct

import pytest

from kick_clip_hunter import flv
from kick_clip_hunter.flv import AUDIO, SCRIPT, VIDEO, Cutter, FlvError, FlvReader, Tag, TsCounters
from tests.support.tiktok import amf0, flv_file

# What a picture's tag begins with in legacy FLV: frame type and codec, then
# the packet type (0 a sequence header, 1 a frame) and the composition time.
AVC_HEADER = b"\x17\x00\x00\x00\x00" + b"avc-config"
HEVC_HEADER = b"\x1c\x00\x00\x00\x00" + b"hevc-config"
AAC_HEADER = b"\xaf\x00" + b"\x12\x10"


def keyframe(time: int, codec: int = 7) -> Tag:
    return Tag(VIDEO, time, bytes([0x10 | codec, 1, 0, 0, 0]) + b"key-picture")


def picture(time: int, codec: int = 7) -> Tag:
    return Tag(VIDEO, time, bytes([0x20 | codec, 1, 0, 0, 0]) + b"picture")


def sound(time: int) -> Tag:
    return Tag(AUDIO, time, b"\xaf\x01" + b"sound")


def metadata(**properties: str | float) -> Tag:
    return Tag(SCRIPT, 0, amf0("onMetaData", properties))


def stream(*, seconds: int, keyframe_every: int = 2000, start: int = 0, frame_ms: int = 100) -> list[Tag]:
    """Tags of a stream: sequence headers, then a picture and a stretch of
    sound every `frame_ms`, a keyframe every `keyframe_every`."""
    tags = [Tag(VIDEO, 0, AVC_HEADER), Tag(AUDIO, 0, AAC_HEADER)]
    for at in range(start, start + seconds * 1000, frame_ms):
        tags.append(keyframe(at) if (at - start) % keyframe_every == 0 else picture(at))
        tags.append(sound(at))
    return tags


def cut(tags: list[Tag], received_ms: int = 1_800_000_000_000, cutter: Cutter | None = None) -> list[flv.Piece]:
    cutter = cutter or Cutter()
    return [piece for tag in tags for piece in cutter.add(tag, received_ms)]


class TestReadingTags:
    def test_tags_come_whole_however_the_bytes_arrive(self):
        tags = [metadata(start_time="1792063475583"), *stream(seconds=1)]
        content = flv_file(tags)

        for size in (1, 7, 1000, len(content)):
            reader = FlvReader()
            read = [tag for at in range(0, len(content), size) for tag in reader.feed(content[at : at + size])]
            assert read == tags

    def test_the_file_header_says_whether_there_is_sound_and_a_picture(self):
        reader = FlvReader()
        list(reader.feed(flv_file([keyframe(0)], flags=flv.HAS_VIDEO)))

        assert reader.flags == flv.HAS_VIDEO

    def test_a_large_tag_late_in_a_stream_is_read_whole(self):
        # Its time takes the byte that extends the other three.
        tag = Tag(VIDEO, 0x12345678, b"\x17\x01\x00\x00\x00" + bytes(70_000))

        assert list(FlvReader().feed(flv_file([tag]))) == [tag]

    def test_what_is_not_an_flv_stream_is_said_to_be_so(self):
        with pytest.raises(FlvError, match="does not begin"):
            list(FlvReader().feed(b"<html>Access Denied</html>"))

    def test_a_header_too_short_to_be_one_is_said_to_be_so(self):
        with pytest.raises(FlvError, match="can't be 3 bytes"):
            list(FlvReader().feed(b"FLV\x01\x05" + struct.pack(">I", 3) + b"\0\0\0\0"))

    def test_tags_of_no_kind_read_here_are_passed_over(self):
        content = flv_file([Tag(15, 0, b"what"), keyframe(0)])

        assert list(FlvReader().feed(content)) == [keyframe(0)]


class TestWhatATagIs:
    @pytest.mark.parametrize(
        ("tag", "keyframe_", "header", "frame", "codec"),
        [
            (Tag(VIDEO, 0, AVC_HEADER), True, True, False, "h264"),
            (keyframe(0), True, False, True, "h264"),
            (picture(0), False, False, True, "h264"),
            (Tag(VIDEO, 0, HEVC_HEADER), True, True, False, "h265"),
            (keyframe(0, codec=12), True, False, True, "h265"),
            # The end of a sequence is no frame.
            (Tag(VIDEO, 0, b"\x17\x02\x00\x00\x00"), True, False, False, "h264"),
            # Enhanced RTMP: frame type and packet type in the first byte,
            # the codec by its four characters.
            (Tag(VIDEO, 0, b"\x90hvc1" + b"config"), True, True, False, "h265"),
            (Tag(VIDEO, 0, b"\x91hvc1" + b"\x00\x00\x00picture"), True, False, True, "h265"),
            (Tag(VIDEO, 0, b"\xa3av01" + b"picture"), False, False, True, "av1"),
            (Tag(VIDEO, 0, b"\x92hvc1"), True, False, False, "h265"),
            (Tag(VIDEO, 0, b"\x94hvc1" + b"metadata"), True, False, False, "h265"),
            (Tag(AUDIO, 0, AAC_HEADER), False, True, False, None),
            (sound(0), False, False, True, None),
            (metadata(), False, False, False, None),
        ],
    )
    def test_its_first_bytes_say_what_it_carries(self, tag, keyframe_, header, frame, codec):
        assert (tag.is_keyframe, tag.is_sequence_header, tag.is_frame, tag.codec) == (keyframe_, header, frame, codec)

    def test_a_tag_written_out_is_read_back_the_same_at_any_time(self):
        for time in (0, 1, 0xFFFFFF, 0x1000000, 0xFFFFFFFF):
            tag = Tag(AUDIO, time, b"\xaf\x01sound")
            assert list(FlvReader().feed(flv_file([tag]))) == [tag]

    def test_a_time_out_of_range_is_written_as_the_nearest_there_can_be(self):
        (read,) = FlvReader().feed(flv_file([]) + sound(0).encoded(-5))

        assert read.time == 0


class TestWhatTheStreamSaysOfItself:
    def test_tiktoks_start_time_is_read_from_its_digits(self):
        said = flv.metadata(metadata(start_time="1792063475583", interval="2"))

        assert flv.start_time(said) == 1792063475583
        assert said["interval"] == "2"

    def test_a_start_time_written_as_a_number_is_read_too(self):
        assert flv.start_time(flv.metadata(metadata(start_time=1792063475583.0))) == 1792063475583

    @pytest.mark.parametrize("value", ["", "soon", "-1", "0", 0.0, -5.0])
    def test_a_start_time_that_is_not_one_says_nothing(self, value):
        assert flv.start_time(flv.metadata(metadata(start_time=value))) is None

    def test_a_stream_without_one_says_nothing(self):
        assert flv.start_time(flv.metadata(metadata(duration=0.0))) is None

    def test_every_kind_of_value_is_read(self):
        data = (
            b"\x02\x00\x0aonMetaData\x03"
            + b"\x00\x01a\x01\x01"  # a boolean
            + b"\x00\x01b\x05"  # null
            + b"\x00\x01c\x0a\x00\x00\x00\x02\x00"
            + struct.pack(">d", 1.5)
            + b"\x02\x00\x01x"  # an array
            + b"\x00\x01d\x0b"
            + struct.pack(">d", 7.0)
            + b"\x00\x00"  # a date
            + b"\x00\x01e\x0c\x00\x00\x00\x02hi"  # a long string
            + b"\x00\x01f\x03\x00\x01g\x06\x00\x00\x09"  # an object, with undefined in it
            + b"\x00\x00\x09"
        )

        assert flv.metadata(Tag(SCRIPT, 0, data)) == {
            "a": True,
            "b": None,
            "c": [1.5, "x"],
            "d": 7.0,
            "e": "hi",
            "f": {"g": None},
        }

    @pytest.mark.parametrize(
        "data",
        [
            amf0("onCuePoint", {"start_time": "1"}),
            b"\x02\x00\x0aonMetaData\x02\x00\x01x",
            b"\x02\x00\x0aonMetaData\x08\x00\x00\x00\x01\x00\x01a\x0d",
            b"\x02\x00\x0aonMetaData\x08\x00\x00\x00\x01\x00\x01a\x00\x00",
        ],
    )
    def test_what_is_not_onmetadata_or_cannot_be_read_says_nothing(self, data):
        assert flv.metadata(Tag(SCRIPT, 0, data)) == {}

    def test_a_tag_that_is_not_data_says_nothing(self):
        assert flv.metadata(sound(0)) == {}


class TestPieces:
    def test_a_piece_runs_from_a_keyframe_to_the_first_keyframe_long_enough_after_it(self):
        pieces = cut([metadata(start_time="1000000"), *stream(seconds=13, keyframe_every=3000)])

        # Keyframes every 3 s; a piece takes them until it has 4 s: 0-6, 6-12.
        assert [(piece.started_at, piece.duration) for piece in pieces] == [(1000000, 6000), (1006000, 6000)]
        assert {piece.run for piece in pieces} == {0}
        assert {piece.codec for piece in pieces} == {"h264"}

    def test_a_piece_is_an_flv_of_its_own_with_the_sequence_headers_in_front(self):
        tags = stream(seconds=9)
        (first, second) = cut(tags)
        read = list(FlvReader().feed(second.flv))

        assert read[0] == Tag(VIDEO, 4000, AVC_HEADER) and read[1] == Tag(AUDIO, 4000, AAC_HEADER)
        assert read[2].is_keyframe
        # Its frames are those of 4 s up to 8 s, counted from the run's first.
        assert [tag.time for tag in read[2:] if tag.kind == VIDEO] == list(range(4000, 8000, 100))
        assert len(list(FlvReader().feed(first.flv))) == 2 + 2 * 40

    def test_times_in_a_piece_are_counted_from_the_runs_first_frame(self):
        (piece, _) = cut([metadata(start_time="1792063475583"), *stream(seconds=9, start=25_960_272)])

        assert piece.started_at == 1792063475583 + 25_960_272
        assert min(tag.time for tag in FlvReader().feed(piece.flv) if tag.is_frame) == 0

    def test_without_a_start_time_the_first_picture_is_taken_to_be_live_when_it_arrives(self):
        (piece, _) = cut(stream(seconds=9, start=50_000), received_ms=1_800_000_123_000)

        assert piece.started_at == 1_800_000_123_000

    def test_what_comes_before_the_first_keyframe_is_in_no_piece(self):
        tags = [Tag(VIDEO, 0, AVC_HEADER), sound(0), picture(100), *stream(seconds=9, start=200)[1:]]

        (piece, _) = cut(tags, received_ms=1_800_000_000_000)

        # The clock was set by the first frame (the sound at 0), the piece
        # begins on the keyframe at 200.
        assert piece.started_at == 1_800_000_000_200
        frames = [tag for tag in FlvReader().feed(piece.flv) if tag.is_frame]
        assert frames[0].is_keyframe and frames[0].time == 0

    def test_what_follows_the_last_keyframe_makes_no_piece_until_the_next_one_comes(self):
        cutter = Cutter()
        # Keyframes at 0, 2, 4 and 6 s: a piece 0-4, the rest waits.
        assert [piece.duration for piece in cut(stream(seconds=7), cutter=cutter)] == [4000]

        (piece,) = cutter.add(keyframe(8000), 0)

        assert (piece.started_at - 1_800_000_000_000, piece.duration) == (4000, 4000)

    def test_a_new_sequence_header_that_changes_nothing_starts_no_run(self):
        tags = stream(seconds=9)
        tags.insert(60, Tag(VIDEO, 0, AVC_HEADER))

        assert {piece.run for piece in cut(tags)} == {0}

    def test_a_picture_encoded_otherwise_from_some_point_on_starts_a_new_run(self):
        tags = stream(seconds=9)
        changed = stream(seconds=9, start=9000)
        changed[0] = Tag(VIDEO, 0, AVC_HEADER + b"-1080p")

        pieces = cut([*tags, *changed])

        # What came after the last keyframe before the change is left out.
        assert [(piece.run, piece.started_at % 100_000) for piece in pieces] == [
            (0, 0),
            (0, 4000),
            (1, 9000),
            (1, 13000),
        ]
        # The pieces of the new run carry the new header, and their times
        # are counted from the new run's first frame.
        read = list(FlvReader().feed(pieces[2].flv))
        assert read[0].data == AVC_HEADER + b"-1080p" and read[2].time == 0

    @pytest.mark.parametrize("jump", [-3000, flv.MAX_STEP_MS + 100])
    def test_times_that_jump_start_a_new_run_on_a_clock_of_its_own(self, jump):
        before = stream(seconds=9, start=10_000)
        after = [tag for tag in stream(seconds=9, start=10_000 + 9000 + jump) if tag.is_frame]
        cutter = Cutter()
        cut([metadata(start_time="1000000"), *before], received_ms=5_000_000, cutter=cutter)

        pieces = cut(after, received_ms=7_000_000, cutter=cutter)

        # The jump is no longer on the clock onMetaData set: the first
        # picture after it is taken to be live when it arrives.
        assert pieces[0].run == 1
        assert pieces[0].started_at == 7_000_000

    def test_sound_running_a_little_behind_the_picture_is_no_jump(self):
        tags = [Tag(VIDEO, 0, AVC_HEADER), Tag(AUDIO, 0, AAC_HEADER)]
        for at in range(1000, 10_000, 100):
            tags += [keyframe(at) if at % 2000 == 0 else picture(at), sound(at - 60)]

        assert {piece.run for piece in cut(tags)} == {0}

    def test_an_hevc_stream_says_so(self):
        tags = [Tag(VIDEO, 0, HEVC_HEADER), *(keyframe(at, codec=12) for at in range(0, 9000, 2000))]

        assert {piece.codec for piece in cut(tags)} == {"h265"}


def ts_packet(pid: int, counter: int, payload: bool = True) -> bytes:
    flags = (0x10 if payload else 0x20) | counter
    return bytes([0x47, pid >> 8, pid & 0xFF, flags]) + bytes(184)


def counters_of(ts: bytes) -> list[tuple[int, int]]:
    return [(((ts[at + 1] & 0x1F) << 8) | ts[at + 2], ts[at + 3] & 0x0F) for at in range(0, len(ts), 188)]


class TestJoiningCopiesIntoOneStream:
    def test_each_streams_packets_are_numbered_on_from_the_copy_before(self):
        counters = TsCounters()
        first = counters.carry_on(ts_packet(0x100, 0) + ts_packet(0x101, 0) + ts_packet(0x100, 1))
        second = counters.carry_on(ts_packet(0x100, 0) + ts_packet(0x101, 0))

        assert counters_of(first) == [(0x100, 0), (0x101, 0), (0x100, 1)]
        assert counters_of(second) == [(0x100, 2), (0x101, 1)]

    def test_the_numbering_goes_round_after_fifteen(self):
        counters = TsCounters()
        for _ in range(3):
            last = counters.carry_on(b"".join(ts_packet(0x100, n) for n in range(7)))

        assert counters_of(last)[-1] == (0x100, (3 * 7 - 1) % 16)

    def test_a_packet_with_no_payload_keeps_the_count_it_follows(self):
        joined = TsCounters().carry_on(ts_packet(0x100, 0) + ts_packet(0x100, 9, payload=False) + ts_packet(0x100, 1))

        assert counters_of(joined) == [(0x100, 0), (0x100, 0), (0x100, 1)]

    def test_the_rest_of_a_packet_is_left_as_it_was(self):
        packet = bytearray(ts_packet(0x100, 5))
        packet[3] |= 0xC0  # scrambling bits, which are not the counter's
        packet[100] = 0xAB

        joined = TsCounters().carry_on(bytes(packet))

        assert joined[:3] == packet[:3] and joined[3] == 0xC0 | 0x10 | 0 and joined[4:] == packet[4:]

    @pytest.mark.parametrize("ts", [bytes(100), b"\x48" + bytes(187)])
    def test_what_is_not_mpeg_ts_is_said_to_be_so(self, ts):
        with pytest.raises(ValueError, match="MPEG-TS"):
            TsCounters().carry_on(ts)
