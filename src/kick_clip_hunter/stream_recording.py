"""A stream kept whole: everything a channel broadcasts from when a recording
of it is started until it is stopped, as one video file.

The rolling buffer in recorder.py holds a few minutes, for clips. A stream
recording is the other thing: started and stopped by hand from the
dashboard, whether or not the channel is tracked for clips, and kept until
it is deleted. It rides on the same download - a channel's recorder hands
every segment it saves to the recording's `StreamWriter` as well - so the
stream is only ever fetched once.

While it runs, the footage is kept as it arrives, in a folder of its own.
Segments that follow each other are appended to one MPEG-TS file, a *part*,
named for when its first frame was broadcast. A break in the stream - the
recorder starting over, a hole in the playlist, an ad that replaced the
broadcast - ends a part, and what comes after it starts the next one. The
ad's own segments are left out: they are not the stream. Nothing has to be
finished for that footage to be safe, so a recording the service is stopped
under loses nothing it had: started again soon enough, the service goes on
with it - a writer given a folder that holds footage adds to it - and
otherwise saves it from its folder.

When it is over, `save` makes the file. Each part is remuxed to MP4 without
re-encoding - the step a clip goes through - and several parts are joined
into one video. When each part began and how long it runs is returned and
stored with the recording, which is what lets chat be lined up with the
picture afterwards: a second of the file lies in one part, and the part
knows when it was on air (`broadcast_time`, `file_second`). A stretch of the
stream that fell between two parts is simply not in the file.

Saving takes as much room again as the recording does, for as long as it
runs. That is why a recording is ended while the disk still has that much
(see recording_manager.py).
"""

import itertools
import json
import logging
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .recorder import AD_GROUP_SUFFIX, FFMPEG_BIN, FFPROBE_BIN, PROBE_TIMEOUT_SECONDS, probe_duration

logger = logging.getLogger("kick_clip_hunter")

STREAMS_DIR = Path("data/streams")

# How a recording stands, as stored with it.
RECORDING = "recording"
SAVING = "saving"
READY = "ready"
FAILED = "failed"

# Why a recording ended.
STOPPED = "stopped"
STREAM_ENDED = "stream_ended"
DISK_FULL = "disk_full"
WRITE_FAILED = "write_failed"
SHUTDOWN = "shutdown"
INTERRUPTED = "interrupted"

# A segment that ends this little after what is already kept adds nothing:
# a recorder that starts over lists the last few segments again.
OVERLAP_SECONDS = 0.5
# How far the length of the joined file may be from the sum of its parts'
# before the join is taken to have gone wrong: this much, and this much more
# for every part, as each seam can be a frame or two off.
JOIN_TOLERANCE_SECONDS = 1.0
JOIN_TOLERANCE_PER_PART_SECONDS = 0.5
# How much of what ffmpeg said is passed on when it fails.
FFMPEG_ERROR_LENGTH = 600

_PART_FILE = re.compile(r"^part_(\d+)\.(ts|mp4)$")
_UNSAFE_IN_A_NAME = re.compile(r"[^A-Za-z0-9_-]")


class SaveError(RuntimeError):
    """A recording could not be made into its file. What it kept is still
    in its folder."""


@dataclass(frozen=True)
class Part:
    """An unbroken stretch of a recording: when its first frame was
    broadcast (the stream's program clock, like the recorder's segments) and
    how long it runs."""

    started_at: datetime
    seconds: float

    @property
    def ended_at(self) -> datetime:
        return self.started_at + timedelta(seconds=self.seconds)


# -- where a recording is kept ----------------------------------------------


def _safe(channel_slug: str) -> str:
    return _UNSAFE_IN_A_NAME.sub("_", channel_slug) or "_"


def channel_folder(channel_slug: str) -> Path:
    return STREAMS_DIR / _safe(channel_slug)


def parts_folder(channel_slug: str, recording_id: int) -> Path:
    """Where a recording's footage is kept until it has been made into its
    file."""
    return channel_folder(channel_slug) / f"stream_{recording_id}_parts"


def file_name(channel_slug: str, recording_id: int, started: datetime) -> str:
    """What a recording's file is called: when it began, as the clock on
    the wall said it (`started` is taken as it is given), and whose stream
    it is - so a folder of them reads in order."""
    return f"{started:%Y-%m-%d_%H-%M}_{_safe(channel_slug)}_{recording_id}.mp4"


def free_bytes() -> int:
    """How much room is left on the disk recordings are kept on."""
    STREAMS_DIR.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(STREAMS_DIR).free


# -- while it runs ----------------------------------------------------------


class StreamWriter:
    """Keeps a stream as it is downloaded: what a `ChannelRecorder` hands
    its segments to (`whole_stream`) while a recording of the channel runs.

    `add` is called from the recorder's thread, everything else from
    wherever the recording is looked after, so what the two share is behind
    a lock. A disk that can't be written to ends the keeping, not the
    recorder: `failed` says so.

    A folder that holds footage already is a recording being taken up again
    after the service was restarted. It goes on from what there is: that
    footage counts, what follows is added to it as further parts, and the
    time it has been without any is counted from now.
    """

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._part = None
        self._group: str | None = None
        # How far, on the stream's clock, what is kept reaches.
        self._until: datetime | None = None
        self._last_kept: float | None = None
        self._closed = False
        self.started_at = kept_from(folder)
        self.seconds = 0.0
        self.size = 0
        self.failed = False
        if self.started_at is not None:
            lengths = kept_lengths(folder)
            self.seconds = sum(lengths.values())
            self.size = kept_bytes(folder)
            newest = max(lengths)
            self._until = newest + timedelta(seconds=lengths[newest])
            self._last_kept = time.monotonic()

    def add(self, started_at: datetime, duration: float, group: str, content: bytes) -> None:
        """Keeps one segment. `group` is the recorder's: segments of one
        group are one continuous stream, cut into pieces."""
        with self._lock:
            if self._closed:
                return
            if group.endswith(AD_GROUP_SUFFIX):
                # Not the stream - and what follows it can't be joined
                # straight onto what came before.
                self._end_part()
                return
            ended_at = started_at + timedelta(seconds=duration)
            if self._until is not None and ended_at <= self._until + timedelta(seconds=OVERLAP_SECONDS):
                return
            try:
                if self._part is None or group != self._group:
                    self._end_part()
                    self._part = open(self.folder / part_name(started_at), "ab")
                    self._group = group
                self._part.write(content)
                # Handed to the system as it arrives: a recording the
                # service dies under loses the segment in flight, no more.
                self._part.flush()
            except OSError:
                logger.exception("stream recording: could not write to %s - keeping no more of it", self.folder)
                self.failed = True
                self._close()
                return
            if self.started_at is None:
                self.started_at = started_at
            self._until = ended_at
            self._last_kept = time.monotonic()
            self.seconds += duration
            self.size += len(content)

    def idle_seconds(self) -> float | None:
        """How long ago the last segment was kept - None before the first."""
        return None if self._last_kept is None else time.monotonic() - self._last_kept

    def close(self) -> None:
        """Keeps nothing more. Whatever is handed over afterwards is
        dropped, so the folder can be made into the file."""
        with self._lock:
            self._close()

    def _close(self) -> None:
        self._closed = True
        self._end_part()

    def _end_part(self) -> None:
        part, self._part, self._group = self._part, None, None
        if part is not None:
            try:
                part.close()
            except OSError:
                logger.warning("stream recording: could not close %s", part.name)


def part_name(started_at: datetime) -> str:
    # Rounded, not cut off, so that the arithmetic can never name a part a
    # millisecond before its first frame.
    return f"part_{round(started_at.timestamp() * 1000)}.ts"


# -- once it is over --------------------------------------------------------


def _part_files(folder: Path) -> dict[int, dict[str, Path]]:
    """What a recording's folder holds of each part, by when the part began
    (epoch milliseconds): its footage as it arrived ("ts"), and as a video
    once one has been made of it ("mp4")."""
    found: dict[int, dict[str, Path]] = {}
    if not folder.is_dir():
        return found
    for path in folder.iterdir():
        match = _PART_FILE.match(path.name)
        # A part that was opened and never written to is not one.
        if match is None or path.stat().st_size == 0:
            continue
        found.setdefault(int(match.group(1)), {})[match.group(2)] = path
    return found


def _broadcast(milliseconds: int) -> datetime:
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)


def kept_from(folder: Path) -> datetime | None:
    """When the first frame kept in a recording's folder was broadcast, or
    None if nothing was kept there."""
    return min(map(_broadcast, _part_files(folder)), default=None)


def kept_bytes(folder: Path) -> int:
    """How much a recording's folder holds."""
    return sum(path.stat().st_size for kinds in _part_files(folder).values() for path in kinds.values())


def kept_lengths(folder: Path) -> dict[datetime, float]:
    """How long each part in a recording's folder runs, by when the part
    began - nothing for one that can't be measured. Asks ffprobe about each,
    so it is for when a recording is taken up again, not for every look."""
    lengths = {}
    for start, kinds in _part_files(folder).items():
        lengths[_broadcast(start)] = probe_duration(kinds.get("ts") or kinds["mp4"]) or 0.0
    return lengths


def last_written(folder: Path) -> datetime | None:
    """When a recording's folder was last added to - by the clock on the
    wall, not the stream's - or None if it holds nothing."""
    written = [path.stat().st_mtime for kinds in _part_files(folder).values() for path in kinds.values()]
    return datetime.fromtimestamp(max(written), tz=timezone.utc) if written else None


def _ffmpeg(arguments: Sequence[str], what: str) -> None:
    result = subprocess.run(
        [FFMPEG_BIN, "-y", "-v", "error", *arguments],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
    )
    if result.returncode != 0:
        said = " ".join(result.stderr.split())[-FFMPEG_ERROR_LENGTH:]
        raise SaveError(f"ffmpeg could not {what}: {said or f'exit code {result.returncode}'}")


def _remux(footage: Path, video: Path) -> None:
    """A part as it arrived, made into a video without re-encoding."""
    _ffmpeg(["-i", str(footage), "-c", "copy", "-f", "mp4", str(video)], f"make a video of {footage.name}")


def _length(video: Path) -> float:
    seconds = probe_duration(video)
    if not seconds or seconds <= 0:
        raise SaveError(f"{video.name} came out with no footage in it")
    return seconds


def _part_videos(files: dict[int, dict[str, Path]]) -> list[tuple[int, Path]]:
    """Every part as a video, in order. A part's footage is removed once
    its video is complete, so that the two are never both there for long;
    one that no video can be made of is left out, and left where it is."""
    videos = []
    for start in sorted(files):
        footage, video = files[start].get("ts"), files[start].get("mp4")
        if footage is not None:
            if video is None:
                video = footage.with_suffix(".mp4")
                unfinished = video.with_name(f"{video.stem}.unfinished.mp4")
                try:
                    _remux(footage, unfinished)
                    _length(unfinished)
                except SaveError as error:
                    logger.warning("stream recording: leaving %s out - %s", footage.name, error)
                    unfinished.unlink(missing_ok=True)
                    continue
                unfinished.replace(video)
            # Its video is complete, or it would not be under that name.
            footage.unlink(missing_ok=True)
        videos.append((start, video))
    return videos


def _format(video: Path) -> list[tuple]:
    """What kind of picture and sound a video holds, stream by stream - what
    has to be the same in two videos for one to be joined onto the other
    without re-encoding."""
    try:
        result = subprocess.run(
            [
                FFPROBE_BIN, "-v", "error",
                "-show_entries", "stream=codec_type,codec_name,width,height,sample_rate,channels",
                "-of", "json",
                str(video),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
        streams = json.loads(result.stdout)["streams"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError) as error:
        raise SaveError(f"{video.name} could not be looked into") from error
    keys = ("codec_type", "codec_name", "width", "height", "sample_rate", "channels")
    return [tuple(stream.get(key) for key in keys) for stream in streams]


def _join(videos: Sequence[Path], joined: Path, folder: Path) -> None:
    """Several videos back to back as one, without re-encoding."""
    listing = folder / "join.txt"
    try:
        # ffmpeg's concat list: one file per line, a quote inside a name
        # written as '\''.
        lines = ["file '" + video.resolve().as_posix().replace("'", "'\\''") + "'" for video in videos]
        listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _ffmpeg(
            ["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", "-f", "mp4", str(joined)],
            f"join {len(videos)} parts",
        )
    finally:
        listing.unlink(missing_ok=True)


def save(folder: Path, output: Path) -> list[Part]:
    """Makes a recording's file out of what its folder holds, and returns
    the parts it is made of, in the order they are in the file - none, and
    no file, if nothing was kept.

    Blocks for about as long as copying the recording takes. Nothing is
    re-encoded. The folder is left for the caller to remove once the parts
    are stored: until then, running this again makes the same file again,
    which is what lets a save that was cut short be repeated.

    Parts that differ in picture or sound format can't be joined that way.
    Then, as with anything else that goes wrong, SaveError says why and the
    parts stay where they are, each one a video that plays on its own.
    """
    files = _part_files(folder)
    if not files:
        return []
    output.parent.mkdir(parents=True, exist_ok=True)
    unfinished = output.with_name(f".{output.stem}.unfinished{output.suffix}")
    try:
        if len(files) == 1 and "ts" in next(iter(files.values())):
            # The usual case, one part: straight into the file.
            ((start, kinds),) = files.items()
            _remux(kinds["ts"], unfinished)
            parts = [Part(_broadcast(start), _length(unfinished))]
        else:
            videos = _part_videos(files)
            if not videos:
                raise SaveError(f"no video could be made of anything in {folder.name}")
            formats = {tuple(_format(video)) for _start, video in videos}
            if len(formats) > 1:
                raise SaveError(
                    "the stream changed its picture or sound format part-way, so its parts can't be joined into one"
                )
            parts = [Part(_broadcast(start), _length(video)) for start, video in videos]
            _join([video for _start, video in videos], unfinished, folder)
            off = abs(_length(unfinished) - length(parts))
            if off > JOIN_TOLERANCE_SECONDS + JOIN_TOLERANCE_PER_PART_SECONDS * len(parts):
                raise SaveError(f"joining {len(parts)} parts gave a video {off:.1f} s off their length")
        unfinished.replace(output)
    finally:
        unfinished.unlink(missing_ok=True)
    return parts


# -- a second of the file, and when it was on air ----------------------------


def encoded(parts: Sequence[Part]) -> str:
    """Parts as they are stored with a recording."""
    return json.dumps([[part.started_at.isoformat(), round(part.seconds, 3)] for part in parts])


def decoded(text: str | None) -> list[Part]:
    """The parts stored with a recording - none for one that has none yet,
    or whose stored parts can't be read."""
    try:
        return [Part(datetime.fromisoformat(started), float(seconds)) for started, seconds in json.loads(text or "[]")]
    except (TypeError, ValueError):
        return []


def length(parts: Sequence[Part]) -> float:
    """How long the file runs."""
    return sum(part.seconds for part in parts)


def missing_seconds(parts: Sequence[Part]) -> float:
    """How much of the stream fell between the parts, and so is not in the
    file."""
    gaps = (following.started_at - part.ended_at for part, following in itertools.pairwise(parts))
    return sum(max(gap.total_seconds(), 0.0) for gap in gaps)


def broadcast_time(parts: Sequence[Part], second: float) -> datetime:
    """When the frame at `second` of the file was broadcast. A second
    outside the file is taken to its nearest end."""
    second = min(max(second, 0.0), length(parts))
    begins = 0.0
    for part in parts[:-1]:
        if second < begins + part.seconds:
            return part.started_at + timedelta(seconds=second - begins)
        begins += part.seconds
    return parts[-1].started_at + timedelta(seconds=second - begins)


def file_second(parts: Sequence[Part], when: datetime) -> float:
    """Which second of the file was being broadcast at `when`. A time the
    file has no footage of - before it began, after it ended, in a break
    between two parts - is taken to the next frame there is."""
    begins = 0.0
    for part in parts:
        if when < part.started_at:
            return begins
        if when < part.ended_at:
            return begins + (when - part.started_at).total_seconds()
        begins += part.seconds
    return begins
