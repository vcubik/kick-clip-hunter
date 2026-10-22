"""A moment's file made longer with footage of its broadcast that it does not
hold - from the broadcast's own recording (its VOD), days later too.

A moment's file is cut from the recorder's buffer, which reaches back a few
minutes only: whatever led up to the clip, or followed it, beyond the context
kept with it is gone from there by the time anyone looks at the clip. Kick
and Twitch keep a recording of every broadcast as it goes along, which stays
there once the broadcast is over (see kick_stream.vod_at, twitch_stream.vod_at),
and a YouTube stream that is still on can be wound back (youtube_stream.vod_at):
footage from there makes the file longer, before the clip or after it
(`lengthen`).

The new file is that recording's own footage only - the stretch the moment's
file held too, not just what is added - joined the way the recorder joins
segments and cut on a frame a cut can begin on, nothing re-encoded. Two
sources are never stitched together: the segments of a live stream and of
its VOD are cut differently and can't be joined byte for byte. The VOD is
the same encoding of the broadcast as the live stream - its pictures decode
to the very same frames, and its sound to the same samples, as measured on
Kick and Twitch - so the clip comes out as it was; where the live stream had
an ad in the file (one the VOD was not found for when the clip was cut), the
new file has what was on instead.

The clocks can't be trusted to the frame, though. A moment's file is placed
on the live stream's program clock; Kick's VOD gives a clock of its own on
every segment, seen two seconds off the live one; Twitch's past broadcasts
give none, only when the broadcast began (to the second, and three seconds
off where it was measured); YouTube's older segments are counted back from
the newest. So the moment's file is found in the recording by its pictures:
a frame of the file a cut can begin on (a keyframe), whose picture is found
among the keyframes of the recording's segments near where the clocks put
it, says exactly where the file lies in the recording. Where the new file
begins is then measured the same way, so the clip's place in it is exact.

What is not had: a VOD kept for subscribers only, or one that is gone (see
the vod_at functions); footage the VOD does not reach yet - a stream that is
on is written to its VOD some seconds behind - of which what there is is
taken, and said. Twitch silences the sound of a stretch with music it may
not play ("-muted" in a segment's address): a file whose own stretch is
silenced in the VOD is not made, as the clip would lose its sound; silenced
footage among what is added is taken, and said.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import recorder
from .platforms import Vod

# What `lengthen` is doing, as it tells whoever asked: finding the file in
# the recording, fetching the footage, putting it together.
FINDING = "finding"
FETCHING = "fetching"
JOINING = "joining"
# How much footage may be asked for on one side at a time: five minutes of
# Kick's 1080p is some 300 MB to fetch, on top of the file's own stretch.
ADD_LEAST_SECONDS = 1
ADD_MOST_SECONDS = 300
# How far either side of where the clocks put a moment's file its pictures
# are looked for in the recording. The clocks were seen a few seconds apart;
# a live stream's slips a little at every ad break, and Kick's streams have
# one every half hour.
SEARCH_SECONDS = 60.0
# Footage added of less than this is none, and footage that falls short of
# what was asked by less than this is all of it.
LEAST_SECONDS = 0.5
# How far the place the pictures put the old file in the new one may be
# from where the recording's clock put it before the new file is taken to
# have gone wrong.
PLACE_TOLERANCE_SECONDS = 1.0
# Where a segment of Twitch's whose sound was silenced says so.
_MUTED = re.compile(r"-muted\.\w+$")
# Where a segment of a live YouTube stream says its number.
_NUMBER = re.compile(r"/sq/(\d+)/")


class FootageError(RuntimeError):
    """No footage could be added - its words say why, as the dashboard
    says it."""


@dataclass(frozen=True)
class Piece:
    """A segment of a broadcast's recording: where it is, when it begins
    on the recording's clock - or as near to that as can be told - and how
    long it runs. `init_url` is the initialization segment a fragmented-MP4
    segment plays after."""

    url: str
    started_at: datetime
    duration: float
    init_url: str | None = None

    @property
    def ended_at(self) -> datetime:
        return self.started_at + timedelta(seconds=self.duration)

    @property
    def muted(self) -> bool:
        return bool(_MUTED.search(urlsplit(self.url).path))


@dataclass(frozen=True)
class Lengthened:
    """A moment's file made longer, not yet in the old file's place.

    `lead` is how many seconds into the new file the old file's first frame
    is, `seconds` how long the new file runs; `added_before` and
    `added_after` are the footage it has before and after what the old file
    held. `muted`: some of what was added has no sound - the VOD silenced
    it. `short`: the VOD did not reach as far as was asked."""

    path: Path
    lead: float
    seconds: float
    added_before: float
    added_after: float
    muted: bool = False
    short: bool = False


def pieces(playlist: recorder._Playlist, began: datetime | None) -> list[Piece]:
    """A recording's segments, each with when it begins: by its program
    date-time, or failing that counted on from when the broadcast began.
    Pure."""
    found: list[Piece] = []
    at = began
    for segment in playlist.segments:
        started = segment.started_at or at
        if started is None:
            raise FootageError("The VOD's playlist says nothing of when its footage was broadcast.")
        found.append(Piece(segment.url, started, segment.duration, segment.init_url))
        at = started + timedelta(seconds=segment.duration)
    return found


def numbered(playlist: recorder._Playlist, first: datetime, last: datetime) -> list[Piece]:
    """The segments of a live YouTube stream that hold [first, last]: the
    playlist lists only the newest, the rest are had by their numbers in
    the address of the first listed, counted back from it. None past the
    newest listed, or before the stream's first. Pure."""
    listed = [segment for segment in playlist.segments if segment.started_at is not None]
    if not listed:
        raise FootageError("YouTube's playlist says nothing of when its footage was broadcast.")
    # The first listed is the one the playlist's date-time is of.
    known, newest = listed[0], playlist.segments[-1].sequence
    number = _NUMBER.search(urlsplit(known.url).path)
    if number is None or int(number.group(1)) != known.sequence:
        raise FootageError("YouTube's playlist does not say how earlier footage of the stream is had.")
    length = known.duration
    if length <= 0:
        raise FootageError("YouTube's playlist says its footage has no length.")

    def started(sequence: int) -> datetime:
        return known.started_at + timedelta(seconds=(sequence - known.sequence) * length)

    lowest = known.sequence + int((first - known.started_at).total_seconds() // length)
    highest = known.sequence + int((last - known.started_at).total_seconds() // length)
    return [
        Piece(known.url.replace(number.group(0), f"/sq/{sequence}/", 1), started(sequence), length)
        for sequence in range(max(lowest, 0), min(highest, newest) + 1)
    ]


def covering(found: list[Piece], start: datetime, end: datetime) -> list[Piece]:
    """The segments that overlap [start, end], in order. Pure."""
    return [piece for piece in found if piece.ended_at > start and piece.started_at < end]


def nearest_first(found: list[Piece], when: datetime) -> list[Piece]:
    """Segments in the order to look for something in them that should be
    at `when`: the one holding it, then outwards. Pure."""

    def distance(piece: Piece) -> float:
        if piece.started_at <= when < piece.ended_at:
            return 0.0
        return min(abs((piece.started_at - when).total_seconds()), abs((piece.ended_at - when).total_seconds()))

    return sorted(found, key=distance)


def unique_marks(marks: list[tuple[float, str]]) -> dict[str, float]:
    """The keyframes of a file whose picture is in it once, by picture: a
    picture shown again and again (a still screen) says nothing of where in
    a recording the file is. Pure."""
    seen = Counter(picture for _time, picture in marks)
    return {picture: time for time, picture in marks if seen[picture] == 1}


def agreed(offsets: list[float]) -> tuple[float, int] | None:
    """The offset most of a list agree on, to a hundredth of a second, and
    how many do. Pure."""
    if not offsets:
        return None
    votes = Counter(round(offset, 2) for offset in offsets)
    offset, count = votes.most_common(1)[0]
    return offset, count


# -- reading pictures --------------------------------------------------------


def keyframes(path: Path) -> list[tuple[float, str]]:
    """When each keyframe of a video file is shown, on the file's own time,
    and its picture (an MD5 of the decoded frame) - only keyframes are
    decoded, so a minute of 1080p takes a second or two. ffprobe says when
    they are shown and ffmpeg what they show, each in the same order; where
    the two do not come to as many, nothing can be said of the file."""
    # fmt: off
    shown = subprocess.run(
        [
            recorder.FFPROBE_BIN, "-v", "error",
            "-select_streams", "v:0", "-skip_frame", "nokey",
            "-show_entries", "frame=pts_time", "-of", "csv=p=0",
            str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    pictures = subprocess.run(
        [
            recorder.FFMPEG_BIN, "-v", "error",
            "-skip_frame", "nokey", "-i", str(path),
            "-map", "0:v:0", "-fps_mode", "passthrough",
            "-f", "framemd5", "-",
        ],
        capture_output=True, text=True, check=True,
    )
    # fmt: on
    try:
        times = [float(line.strip().strip(",")) for line in shown.stdout.splitlines() if line.strip().strip(",")]
    except ValueError:
        return []
    hashes = [line.rsplit(",", 1)[-1].strip() for line in pictures.stdout.splitlines() if line and line[0] != "#"]
    return list(zip(times, hashes, strict=True)) if len(times) == len(hashes) else []


def video_start(path: Path) -> float:
    """When a file's picture begins, on its own time."""
    # fmt: off
    result = subprocess.run(
        [
            recorder.FFPROBE_BIN, "-v", "error",
            "-select_streams", "v:0", "-show_entries", "stream=start_time", "-of", "csv=p=0",
            str(path),
        ],
        capture_output=True, text=True, check=True, timeout=recorder.PROBE_TIMEOUT_SECONDS,
    )
    # fmt: on
    try:
        return float(next(line for line in result.stdout.splitlines() if line.strip().strip(",")).strip(","))
    except (StopIteration, ValueError):
        return 0.0


# -- fetching ----------------------------------------------------------------


def _get(client: httpx.Client, url: str, timeout: float) -> httpx.Response:
    for attempt in (1, 2):
        try:
            response = client.get(url, timeout=timeout)
            response.raise_for_status()
            return response
        except httpx.HTTPError as error:
            if attempt == 2:
                status = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
                raise FootageError(
                    f"The VOD could not be fetched ({status or type(error).__name__}). "
                    "It may be gone, or no longer reach that far back."
                ) from error
    raise AssertionError("unreachable")  # pragma: no cover


def _playlist(client: httpx.Client, vod: Vod) -> recorder._Playlist:
    """The recording's playlist, of the variant the recorder would follow."""
    master = _get(client, vod.master, recorder.PLAYLIST_TIMEOUT_SECONDS)
    try:
        variant = recorder._pick_variant(master.text, vod.master)
    except recorder.RecorderError as error:
        raise FootageError("The VOD's playlist lists no footage.") from error
    playlist = recorder._parse_playlist(_get(client, variant, recorder.PLAYLIST_TIMEOUT_SECONDS).text, variant)
    if not playlist.segments:
        raise FootageError("The VOD's playlist lists no footage.")
    return playlist


class _Kept:
    """The segments of a recording fetched so far, kept in a folder of their
    own while the file is made, so that none is fetched twice."""

    def __init__(self, client: httpx.Client, folder: Path) -> None:
        self._client = client
        self._folder = folder
        self._paths: dict[str, Path] = {}
        self._inits: dict[str, Path] = {}

    def segment(self, piece: Piece) -> Path:
        if piece.url not in self._paths:
            path = self._folder / f"{len(self._paths):05d}.seg"
            path.write_bytes(_get(self._client, piece.url, recorder.SEGMENT_TIMEOUT_SECONDS).content)
            self._paths[piece.url] = path
        return self._paths[piece.url]

    def init(self, url: str) -> Path:
        if url not in self._inits:
            path = self._folder / f"init{len(self._inits)}.mp4"
            path.write_bytes(_get(self._client, url, recorder.SEGMENT_TIMEOUT_SECONDS).content)
            self._inits[url] = path
        return self._inits[url]

    def playable(self, piece: Piece) -> Path:
        """A segment as a file of its own that plays: a fragment of
        fragmented MP4 only does after its initialization segment."""
        path = self.segment(piece)
        if piece.init_url is None:
            return path
        whole = path.with_suffix(".mp4")
        if not whole.exists():
            whole.write_bytes(self.init(piece.init_url).read_bytes() + path.read_bytes())
        return whole


def _find(kept: _Kept, around: list[Piece], marks: dict[str, float], at: datetime) -> datetime:
    """Where on the recording's clock the old file's first frame is: its
    keyframes looked for among those of the segments near `at`, the nearest
    first, until two agree on it (or one alone does, once there is no more
    to look at)."""
    offsets: list[float] = []
    for piece in nearest_first(around, at):
        path = kept.playable(piece)
        start = video_start(path)
        for time, picture in keyframes(path):
            if picture in marks:
                offsets.append((piece.started_at - at).total_seconds() + time - start - marks[picture])
        best = agreed(offsets)
        if best is not None and best[1] >= 2:
            return at + timedelta(seconds=best[0])
    best = agreed(offsets)
    if best is None:
        raise FootageError(
            "The moment's footage was not found in the VOD by its pictures: the VOD is not of the "
            "same broadcast, or not of the same picture."
        )
    return at + timedelta(seconds=best[0])


def _join(kept: _Kept, chosen: list[Piece], output: Path) -> None:
    """The segments joined byte for byte - after their initialization
    segment, for fragmented MP4 - and remuxed into one file, as the recorder
    joins a group (see recorder._write_clip)."""
    joined = output.with_name(f"{output.stem}.joined")
    with open(joined, "wb") as into:
        if chosen[0].init_url is not None:
            into.write(kept.init(chosen[0].init_url).read_bytes())
        for piece in chosen:
            into.write(kept.segment(piece).read_bytes())
    # fmt: off
    subprocess.run(
        [recorder.FFMPEG_BIN, "-v", "error", "-y", "-i", str(joined), "-c", "copy", str(output)],
        check=True, capture_output=True,
    )
    # fmt: on


def lengthen(
    old: Path,
    first_at: datetime,
    vod: Vod,
    before: float,
    after: float,
    output: Path,
    progress: Callable[[str, float | None], None] | None = None,
) -> Lengthened:
    """A moment's file made longer by `before` seconds of footage ahead of
    it and `after` seconds after it, from the broadcast's recording. `old`
    is the moment's file and `first_at` when its first frame was broadcast,
    on the live stream's clock. The new file is written to `output`, the
    old one is left as it is. `progress` is told what is being done, and how
    much of the footage has been fetched; it may raise to give up. Raises
    FootageError, in words that say why, where nothing could be added."""
    tell = progress or (lambda _what, _done: None)
    length = recorder.probe_duration(old)
    if not length:
        raise FootageError("The moment's file can't be read.")
    marks = unique_marks(keyframes(old))
    if not marks:
        raise FootageError("The moment's picture does not change, so it can't be found in the VOD by it.")
    work = output.with_name(f".{output.stem}.footage")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        with httpx.Client(follow_redirects=True) as client:
            kept = _Kept(client, work)
            tell(FINDING, None)
            playlist = _playlist(client, vod)
            if vod.numbered:

                def listing(start: datetime, end: datetime) -> list[Piece]:
                    return numbered(playlist, start, end)

            else:
                found = pieces(playlist, vod.started_at)

                def listing(start: datetime, end: datetime) -> list[Piece]:
                    return covering(found, start, end)

            search = timedelta(seconds=SEARCH_SECONDS)
            anchor = _find(kept, listing(first_at - search, first_at + search), marks, first_at)
            wanted = (anchor - timedelta(seconds=before), anchor + timedelta(seconds=length + after))
            chosen = listing(*wanted)
            if any(piece.muted for piece in covering(chosen, anchor, anchor + timedelta(seconds=length))):
                raise FootageError(
                    "Twitch silenced the sound of this stretch in its VOD: the clip would lose its sound."
                )
            tell(FETCHING, 0.0)
            for number, piece in enumerate(chosen, start=1):
                kept.segment(piece)
                tell(FETCHING, number / len(chosen))
            tell(JOINING, None)
            whole = work / "whole.mp4"
            _join(kept, chosen, whole)
        whole_length = recorder.probe_duration(whole) or 0.0
        begins = max((wanted[0] - chosen[0].started_at).total_seconds(), 0.0)
        ends = min((wanted[1] - chosen[0].started_at).total_seconds(), whole_length)
        cut_from, seconds = recorder.trim_clip(whole, begins, ends, output)
        lead = _place(output, marks)
        expected = (anchor - chosen[0].started_at).total_seconds() - cut_from
        if lead is None or abs(lead - expected) > PLACE_TOLERANCE_SECONDS:
            output.unlink(missing_ok=True)
            raise FootageError(
                "The footage from the VOD did not come out right: the moment's pictures are not where they should be."
            )
        if seconds - lead < length - PLACE_TOLERANCE_SECONDS:
            output.unlink(missing_ok=True)
            raise FootageError("The VOD holds less of the stream there than the moment's file does.")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    added_before, added_after = max(lead, 0.0), max(seconds - lead - length, 0.0)
    if added_before < LEAST_SECONDS and added_after < LEAST_SECONDS:
        output.unlink(missing_ok=True)
        raise FootageError("The VOD has nothing of the stream there that the moment's file does not have already.")
    return Lengthened(
        path=output,
        lead=lead,
        seconds=seconds,
        added_before=added_before,
        added_after=added_after,
        muted=any(piece.muted for piece in chosen),
        short=added_before < before - LEAST_SECONDS or added_after < after - LEAST_SECONDS,
    )


def _place(path: Path, marks: dict[str, float]) -> float | None:
    """Where in a file the old file's first frame is, by the old file's
    keyframes found in it - where most of those found agree."""
    offsets = [time - marks[picture] for time, picture in keyframes(path) if picture in marks]
    best = agreed(offsets)
    return best[0] if best else None
