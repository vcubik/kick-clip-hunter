"""A live FLV stream, read as it arrives and cut into pieces a recorder can
keep - for TikTok, whose streams can only be had as FLV (see
tiktok_stream.py).

An FLV file is a short header and then tags, one after another: a picture,
a stretch of sound, or data for the player (onMetaData). A live stream is
one download that does not end: no playlist, nothing to fetch twice. So
`FlvReader` takes its bytes in as they come, in pieces of any size, and
hands back whole tags.

A recorder keeps a stream as segments of a few seconds, each of which plays
on its own and can be joined onto the one before byte for byte (see
recorder.py). FLV tags are neither: a tag is meaningless without the
stream's sequence headers - what the picture and the sound are encoded with,
sent once at the start. So `Cutter` gathers the tags from one whole picture
(a keyframe) up to the first keyframe at least PIECE_MS later, and makes a
small FLV file of each such stretch, with the sequence headers in front of
its tags (`Piece`). ffmpeg then copies each one into MPEG-TS, the form every
other segment is kept in, and `TsCounters` makes the copies read as one
stream once they are joined.

Times. A tag's time is in milliseconds from when the stream began, which
TikTok's onMetaData says in `start_time` (epoch milliseconds) - together the
two are when a picture was broadcast, the program clock the recorder names
its segments by. Without that, the first picture is taken to be live when
it arrives. The times are counted from zero again in the pieces of a run
(see below), so that each piece's file carries times that go on from the
piece before's, and stay small however long the stream has been on.

A sequence header that changes, or a time that goes back or jumps forward,
ends a *run* of pieces (`Piece.run`): what is on either side of such a break
can't be joined straight onto each other. The few seconds since the last
keyframe before a break - and before the end of the stream - make no piece:
a piece is only cut once the next one's keyframe says where it ends.

Pure: bytes come in, tags and pieces come out.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass

SIGNATURE = b"FLV"
HEADER_SIZE = 9
# What a tag carries: sound, a picture, or data for the player.
AUDIO = 8
VIDEO = 9
SCRIPT = 18
_TAG_HEADER = 11
_TAG_TYPE_MASK = 0x1F
# The size of the tag before, which follows the file header and every tag.
_PREVIOUS_SIZE = 4
# The file header's flags: the stream has sound, a picture.
HAS_AUDIO = 0x04
HAS_VIDEO = 0x01

# A picture that is whole in itself, which a piece can start on.
KEYFRAME = 1
# Legacy FLV numbers its video codecs: 7 is H.264, and 12 HEVC as Chinese
# CDNs - TikTok's among them - send it, outside the original spec.
_VIDEO_CODECS = {7: "h264", 12: "h265"}
# Enhanced RTMP marks a video tag whose header names its codec by a
# four-character code instead, and numbers its packets on its own.
_EX_HEADER = 0x80
_EX_CODECS = {b"avc1": "h264", b"hvc1": "h265", b"av01": "av1", b"vp09": "vp9"}
_EX_SEQUENCE_START = 0
_EX_SEQUENCE_END = 2
_EX_METADATA = 4
_AAC = 10
_SEQUENCE_HEADER = 0
_END_OF_SEQUENCE = 2

# A piece runs from a keyframe to the first keyframe at least this long
# after it.
PIECE_MS = 4000
# A tag whose time goes forward by more than this from the last of its kind
# is not where the stream was.
MAX_STEP_MS = 5000


class FlvError(ValueError):
    """What came in is not an FLV stream."""


@dataclass(frozen=True)
class Tag:
    """One tag of the stream, as it came: its kind, when it was broadcast
    (milliseconds since the stream began) and its data."""

    kind: int
    time: int
    data: bytes

    @property
    def _enhanced(self) -> bool:
        return self.kind == VIDEO and bool(self.data) and bool(self.data[0] & _EX_HEADER)

    @property
    def is_keyframe(self) -> bool:
        if self.kind != VIDEO or not self.data:
            return False
        first = self.data[0]
        return (first >> 4) & (0x07 if self._enhanced else 0x0F) == KEYFRAME

    @property
    def codec(self) -> str | None:
        """What a picture is encoded with, as ffprobe names it - None for
        sound, data, and a codec not known here."""
        if self.kind != VIDEO or not self.data:
            return None
        if self._enhanced:
            return _EX_CODECS.get(self.data[1:5])
        return _VIDEO_CODECS.get(self.data[0] & 0x0F)

    @property
    def is_sequence_header(self) -> bool:
        """Whether it says what the picture or the sound is encoded with,
        rather than being a frame of it."""
        if len(self.data) < 2:
            return False
        if self.kind == VIDEO:
            if self._enhanced:
                return self.data[0] & 0x0F == _EX_SEQUENCE_START
            return self.data[1] == _SEQUENCE_HEADER
        return self.kind == AUDIO and self.data[0] >> 4 == _AAC and self.data[1] == _SEQUENCE_HEADER

    @property
    def is_frame(self) -> bool:
        """Whether it is a picture or a stretch of sound - not a sequence
        header, the end of a sequence, or data for the player."""
        if self.kind not in (AUDIO, VIDEO) or len(self.data) < 2 or self.is_sequence_header:
            return False
        if self.kind == AUDIO:
            return True
        if self._enhanced:
            return self.data[0] & 0x0F not in (_EX_SEQUENCE_END, _EX_METADATA)
        return self.data[1] != _END_OF_SEQUENCE

    def encoded(self, time: int) -> bytes:
        """The tag as it stands in a file, at `time` (milliseconds), with
        the size that follows it."""
        time = min(max(time, 0), 0xFFFFFFFF)
        size = len(self.data)
        header = (
            bytes([self.kind])
            + size.to_bytes(3, "big")
            + (time & 0xFFFFFF).to_bytes(3, "big")
            + bytes([time >> 24])
            + b"\0\0\0"
        )
        return header + self.data + struct.pack(">I", _TAG_HEADER + size)


class FlvReader:
    """Takes an FLV stream in as it arrives, in pieces of any size, and
    hands back its tags whole (`feed`). The file header is checked, and what
    its flags say kept (`flags`: whether there is sound, a picture)."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._header_read = False
        self.flags = HAS_AUDIO | HAS_VIDEO

    def feed(self, chunk: bytes) -> Iterator[Tag]:
        self._buffer += chunk
        if not self._header_read:
            if len(self._buffer) < HEADER_SIZE:
                return
            if self._buffer[:3] != SIGNATURE:
                raise FlvError("the stream does not begin as an FLV file does")
            (data_offset,) = struct.unpack(">I", self._buffer[5:9])
            if data_offset < HEADER_SIZE:
                raise FlvError(f"an FLV header can't be {data_offset} bytes long")
            if len(self._buffer) < data_offset + _PREVIOUS_SIZE:
                return
            self.flags = self._buffer[4] & (HAS_AUDIO | HAS_VIDEO)
            del self._buffer[: data_offset + _PREVIOUS_SIZE]
            self._header_read = True
        while len(self._buffer) >= _TAG_HEADER:
            kind = self._buffer[0] & _TAG_TYPE_MASK
            size = int.from_bytes(self._buffer[1:4], "big")
            time = int.from_bytes(self._buffer[4:7], "big") | (self._buffer[7] << 24)
            end = _TAG_HEADER + size + _PREVIOUS_SIZE
            if len(self._buffer) < end:
                return
            data = bytes(self._buffer[_TAG_HEADER : _TAG_HEADER + size])
            del self._buffer[:end]
            if kind in (AUDIO, VIDEO, SCRIPT):
                yield Tag(kind, time, data)


# -- the stream's own data (onMetaData) ----------------------------------------


class _Amf0:
    """Just enough of AMF0 to read what an FLV's onMetaData says."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._at = 0

    def _take(self, size: int) -> bytes:
        if self._at + size > len(self._data):
            raise FlvError("onMetaData ends part-way through a value")
        taken = self._data[self._at : self._at + size]
        self._at += size
        return taken

    def _string(self, size_bytes: int = 2) -> str:
        size = int.from_bytes(self._take(size_bytes), "big")
        return self._take(size).decode("utf-8", "replace")

    def _properties(self) -> dict[str, object]:
        found: dict[str, object] = {}
        while True:
            name = self._string()
            if not name and self._data[self._at : self._at + 1] == b"\x09":
                self._at += 1
                return found
            found[name] = self.value()

    def value(self) -> object:
        marker = self._take(1)[0]
        if marker == 0x00:
            return struct.unpack(">d", self._take(8))[0]
        if marker == 0x01:
            return bool(self._take(1)[0])
        if marker == 0x02:
            return self._string()
        if marker == 0x03:
            return self._properties()
        if marker in (0x05, 0x06):
            return None
        if marker == 0x08:
            self._take(4)
            return self._properties()
        if marker == 0x0A:
            count = int.from_bytes(self._take(4), "big")
            return [self.value() for _ in range(count)]
        if marker == 0x0B:
            (milliseconds,) = struct.unpack(">d", self._take(8))
            self._take(2)
            return milliseconds
        if marker == 0x0C:
            return self._string(size_bytes=4)
        raise FlvError(f"onMetaData holds a value of a kind ({marker}) that is not read here")


def metadata(tag: Tag) -> dict[str, object]:
    """What a script tag says, if it is the stream's onMetaData - nothing
    otherwise, or if it can't be read."""
    if tag.kind != SCRIPT:
        return {}
    try:
        reader = _Amf0(tag.data)
        if reader.value() != "onMetaData":
            return {}
        found = reader.value()
    except FlvError:
        return {}
    return found if isinstance(found, dict) else {}


def start_time(said: dict[str, object]) -> int | None:
    """When the stream began, in epoch milliseconds, as TikTok's onMetaData
    says (`start_time`, a number or its digits) - None where it does not."""
    value = said.get("start_time")
    if isinstance(value, float) and value > 0:
        return int(value)
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
        return int(value)
    return None


# -- pieces a recorder keeps -------------------------------------------------------


@dataclass(frozen=True)
class Piece:
    """A few seconds of the stream that play on their own, as a small FLV
    file: from a keyframe up to the next piece's.

    `started_at` and `duration` are in milliseconds, on the stream's own
    clock - since 1970 where onMetaData said when the stream began. `run`
    counts the breaks before it (see the module's docstring): the pieces of
    one run follow each other. `codec` is what its picture is encoded with
    (see Tag.codec)."""

    started_at: int
    duration: int
    run: int
    codec: str | None
    flv: bytes


def _file_header(flags: int) -> bytes:
    return SIGNATURE + bytes([1, flags]) + struct.pack(">I", HEADER_SIZE) + b"\0\0\0\0"


class Cutter:
    """Makes pieces of a stream's tags as they come (`add`). `flags` is what
    the stream's own file header said (see FlvReader.flags)."""

    def __init__(self, flags: int = HAS_AUDIO | HAS_VIDEO) -> None:
        self.flags = flags
        self.run = 0
        self._headers: dict[int, Tag] = {}
        # The tags of the piece being gathered, its first a keyframe.
        self._tags: list[Tag] = []
        # The last time of each kind of frame, for telling a break.
        self._last: dict[int, int] = {}
        # When the stream began as onMetaData says it, and the clock that
        # turns a tag's time into the stream's: epoch milliseconds at time 0.
        self._said_start: int | None = None
        self._clock: int | None = None
        # The time pieces of this run are counted from.
        self._base: int | None = None

    def add(self, tag: Tag, received_ms: int) -> Iterator[Piece]:
        """Takes in the next tag of the stream, which arrived at
        `received_ms` (epoch milliseconds); yields a piece whenever this one
        completes one."""
        if tag.kind == SCRIPT:
            said = start_time(metadata(tag))
            if said is not None:
                self._said_start, self._clock = said, None
            return
        if tag.is_sequence_header:
            before = self._headers.get(tag.kind)
            if before is not None and before.data != tag.data:
                # Encoded otherwise from here on: not joinable onto before.
                self._break()
            self._headers[tag.kind] = tag
            return
        if not tag.is_frame:
            return
        last = self._last.get(tag.kind)
        if last is not None and not 0 <= tag.time - last <= MAX_STEP_MS:
            # The times are not the ones the stream was keeping: the clock
            # that went with them is not to be gone by either.
            self._break()
            self._said_start = self._clock = None
        self._last[tag.kind] = tag.time
        if self._clock is None:
            self._clock = self._said_start if self._said_start is not None else received_ms - tag.time
        if tag.is_keyframe and self._tags and tag.time - self._tags[0].time >= PIECE_MS:
            yield self._piece(until=tag.time)
        if self._tags or tag.is_keyframe:
            # A piece begins on a keyframe; what comes before the first one
            # belongs to no piece.
            self._tags.append(tag)

    def _break(self) -> None:
        """Ends the run: what was gathered since its last keyframe is left
        out, as nothing says where it ends."""
        self._tags, self._last, self._base = [], {}, None
        self.run += 1

    def _piece(self, until: int) -> Piece:
        tags, self._tags = self._tags, []
        first = tags[0].time
        if self._base is None:
            self._base = first
        out = [_file_header(self.flags)]
        # The sequence headers go in front, at the piece's own start.
        out.extend(header.encoded(first - self._base) for header in self._headers.values())
        out.extend(tag.encoded(tag.time - self._base) for tag in tags)
        assert self._clock is not None
        return Piece(
            started_at=self._clock + first,
            duration=until - first,
            run=self.run,
            # Its first tag is its keyframe.
            codec=tags[0].codec,
            flv=b"".join(out),
        )


# -- the pieces as MPEG-TS ----------------------------------------------------------

TS_PACKET = 188
_SYNC = 0x47
# What of a picture ffmpeg is told to leave out when copying a piece into
# MPEG-TS, by its codec: SEI units - data for TikTok's own player, of no use
# to any other. TikTok's H.264 has one after the picture of every keyframe,
# where they belong before it: ffmpeg's reader of MPEG-TS takes that for a
# new picture beginning, and the frame after every keyframe loses its time.
# HEVC's are left out as well, should one ever stand there.
SEI_UNITS = {"h264": "6", "h265": "39|40"}


class TsCounters:
    """Joins the MPEG-TS copies of a run's pieces into one stream.

    Each copy is made by an ffmpeg of its own, which numbers the packets of
    every stream in it from zero (their continuity counters). Joined as they
    are, the numbering jumps at every seam, and a reader takes the packet in
    progress there for damaged. `carry_on` numbers a copy's packets on from
    where the one before stopped."""

    def __init__(self) -> None:
        self._counters: dict[int, int] = {}

    def carry_on(self, ts: bytes) -> bytes:
        if len(ts) % TS_PACKET:
            raise ValueError("MPEG-TS comes in packets of 188 bytes")
        out = bytearray(ts)
        for at in range(0, len(out), TS_PACKET):
            if out[at] != _SYNC:
                raise ValueError("an MPEG-TS packet does not begin where one should")
            pid = ((out[at + 1] & 0x1F) << 8) | out[at + 2]
            # Only a packet that carries a payload counts on; one that is
            # all adaptation field repeats the count before it.
            if out[at + 3] & 0x10:
                self._counters[pid] = (self._counters.get(pid, -1) + 1) & 0x0F
            out[at + 3] = (out[at + 3] & 0xF0) | self._counters.get(pid, 0)
        return bytes(out)
