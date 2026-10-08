"""Continuous per-channel recording into a rolling local buffer, so a clip
can be cut for a moment shortly after it's detected.

Each `ChannelRecorder.tick()` call (meant to be driven periodically, e.g.
every 30s) makes sure a background thread is following the channel's live HLS
playlist and saving every media segment it lists, exactly as served, into
`data/recordings/<slug>/` - starting one if there isn't one running, or
restarting against a freshly fetched URL if the previous one died or stalled
(see STALL_TIMEOUT_SECONDS) - and prunes segments older than the retention
window.

The segments are downloaded directly rather than by a long-running
`ffmpeg -c copy` process (which is how this used to work) because a live
stream's layout can change mid-broadcast - seen on channels in categories
where Kick stitches ads into the stream server-side. ffmpeg picks its
input-to-output stream mapping once at startup, so after such a switch it
stayed alive for hours writing audio packets into the video stream, and every
clip cut in that time was lost. Source segments are each self-contained
MPEG-TS, so saving them untouched is immune to that: whatever comes after a
discontinuity is just more files, and clip cutting (extract_clip) deals with
the boundary. It also means ffmpeg never sees the playlist URLs, whose signed
token is long enough to overflow ffmpeg's ~4096-byte URL limit (this module
used to need a local proxy to work around that).

A segment's file name carries everything needed to find it again:
`<start epoch ms>_<duration ms>_<group>.ts`. Start time comes from the
playlist's own EXT-X-PROGRAM-DATE-TIME (UTC, like the timestamps in
detector.py and db.py). `group` identifies a stretch of segments that are
safe to join byte-for-byte - it changes on every restart, playlist
discontinuity, segment-title change or gap.
"""

import logging
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import httpx

from .kick_stream import get_live_stream_url

logger = logging.getLogger("kick_clip_hunter")

FFMPEG_BIN = "ffmpeg"
BUFFER_RETENTION_SECONDS = 600
# Chat reacts to a moment with a lag - the detector's window_start/window_end
# mark when the *reaction* (message spike) was seen, not when the funny thing
# actually happened, which is typically a few seconds earlier. So pre-roll is
# weighted heavier than post-roll. Post-roll is generous too, though: the
# payoff/aftermath of a bit often runs on past the chat spike, and clips were
# felt to be cut off at the end.
PRE_ROLL_SECONDS = 25
POST_ROLL_SECONDS = 35
# Segment times are the stream's own program clock, i.e. when a frame was
# broadcast - viewers (and so chat) see it some seconds later, after the
# player's buffer. Clip windows are shifted this much earlier to line the two
# clocks up. It also keeps clip framing where the pre/post-roll values above
# were tuned: the old ffmpeg-based recorder labelled footage 6-15s later than
# it was broadcast (it started a few segments behind the live edge but counted
# time from launch), so those values already assume a shift of about this size.
PLAYBACK_DELAY_SECONDS = 10
# How long after a clip window's end to wait before cutting, so the last
# segment it needs has been published and downloaded.
CLIP_SETTLE_SECONDS = 10
# If no new segment has been saved in this long, the run is presumed stalled
# (playlist requests hanging or failing, stream gone) and gets restarted
# against a freshly fetched URL. Generous relative to segment length so
# ordinary jitter doesn't trigger a false-positive restart.
STALL_TIMEOUT_SECONDS = 90
PLAYLIST_POLL_SECONDS = 2
PLAYLIST_TIMEOUT_SECONDS = 10
SEGMENT_TIMEOUT_SECONDS = 20
# A fresh run starts this many segments back from the live edge rather than
# downloading the playlist's whole window.
INITIAL_SEGMENTS = 3
# Raw playlists saved at each discontinuity, for working out how ad breaks
# are marked. Capped per channel.
DEBUG_PLAYLIST_DIR = Path("data/hls_debug")
DEBUG_PLAYLISTS_KEPT = 20

RECORDINGS_DIR = Path("data/recordings")
CLIPS_DIR = Path("data/clips")

_SEGMENT_NAME = re.compile(r"^(\d+)_(\d+)_(.+)\.ts$")
_ATTRIBUTE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


class RecorderError(RuntimeError):
    pass


@dataclass
class _PlaylistSegment:
    sequence: int
    url: str
    duration: float
    title: str
    started_at: datetime | None
    discontinuity: bool


@dataclass
class _Playlist:
    segments: list[_PlaylistSegment]
    ended: bool
    daterange_classes: set[str]


@dataclass
class _StoredSegment:
    started_at: datetime
    duration: float
    group: str
    path: Path

    @property
    def ended_at(self) -> datetime:
        return self.started_at + timedelta(seconds=self.duration)


@dataclass
class _Run:
    started_at: datetime
    variant_url: str
    thread: threading.Thread | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    last_progress_at: datetime | None = None


def _attributes(tag_line: str) -> dict[str, str]:
    return {key: value.strip('"') for key, value in _ATTRIBUTE.findall(tag_line.split(":", 1)[1])}


def _pick_variant(master_text: str, master_url: str) -> str:
    """The highest-bandwidth variant playlist URL in a master playlist."""
    best: tuple[int, str] | None = None
    bandwidth = 0
    for line in master_text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            bandwidth = int(_attributes(line).get("BANDWIDTH", "0") or 0)
        elif line and not line.startswith("#"):
            if best is None or bandwidth > best[0]:
                best = (bandwidth, urljoin(master_url, line))
            bandwidth = 0
    if best is None:
        raise RecorderError("master playlist lists no variants")
    return best[1]


def _parse_playlist(text: str, playlist_url: str) -> _Playlist:
    segments: list[_PlaylistSegment] = []
    classes: set[str] = set()
    ended = False
    sequence = 0
    duration, title = 0.0, ""
    program_time: datetime | None = None
    discontinuity = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            sequence = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            program_time = datetime.fromisoformat(line.split(":", 1)[1].replace("Z", "+00:00"))
            if program_time.tzinfo is None:
                program_time = program_time.replace(tzinfo=timezone.utc)
        elif line.startswith("#EXTINF:"):
            raw_duration, _, title = line.split(":", 1)[1].partition(",")
            duration = float(raw_duration)
        elif line == "#EXT-X-DISCONTINUITY":
            discontinuity = True
        elif line.startswith("#EXT-X-DATERANGE:"):
            classes.add(_attributes(line).get("CLASS", ""))
        elif line == "#EXT-X-ENDLIST":
            ended = True
        elif line and not line.startswith("#"):
            segments.append(
                _PlaylistSegment(sequence, urljoin(playlist_url, line), duration, title.strip(), program_time, discontinuity)
            )
            sequence += 1
            # A date-time tag applies to the one segment after it; carry it
            # forward so a segment without its own still gets a start time.
            program_time = program_time + timedelta(seconds=duration) if program_time else None
            discontinuity = False
    return _Playlist(segments, ended, classes - {""})


class ChannelRecorder:
    def __init__(self, channel_slug: str):
        self.channel_slug = channel_slug
        self._base_dir = RECORDINGS_DIR / channel_slug
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._run: _Run | None = None

    def _start_run(self) -> None:
        master_url = get_live_stream_url(self.channel_slug)
        response = httpx.get(master_url, timeout=PLAYLIST_TIMEOUT_SECONDS)
        response.raise_for_status()
        run = _Run(
            started_at=datetime.now(timezone.utc),
            variant_url=_pick_variant(response.text, master_url),
        )
        run.thread = threading.Thread(target=self._record, args=(run,), daemon=True)
        self._run = run
        run.thread.start()
        logger.info("[%s] recording started -> %s", self.channel_slug, self._base_dir)

    def _stop_run(self) -> None:
        if self._run is None:
            return
        self._run.stop_event.set()
        self._run.thread.join(timeout=SEGMENT_TIMEOUT_SECONDS + 5)
        self._run = None

    def _record(self, run: _Run) -> None:
        try:
            with httpx.Client(timeout=SEGMENT_TIMEOUT_SECONDS) as client:
                self._follow_playlist(run, client)
        except Exception:
            logger.exception("[%s] recording thread failed", self.channel_slug)

    def _follow_playlist(self, run: _Run, client: httpx.Client) -> None:
        run_id = int(run.started_at.timestamp())
        group = 0
        last_sequence: int | None = None
        last_title: str | None = None
        last_end: datetime | None = None
        known_classes: set[str] = set()

        while not run.stop_event.is_set():
            try:
                response = client.get(run.variant_url, timeout=PLAYLIST_TIMEOUT_SECONDS)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                # Transient failures are retried on the next poll; if they
                # keep up, tick()'s stall check restarts the run.
                logger.warning("[%s] playlist fetch failed: %s", self.channel_slug, type(exc).__name__)
                run.stop_event.wait(PLAYLIST_POLL_SECONDS)
                continue

            playlist = _parse_playlist(response.text, run.variant_url)
            for new_class in sorted(playlist.daterange_classes - known_classes):
                logger.info("[%s] playlist date-range class seen: %s", self.channel_slug, new_class)
            known_classes |= playlist.daterange_classes

            segments = playlist.segments
            if last_sequence is None:
                segments = segments[-INITIAL_SEGMENTS:]
            elif segments and segments[-1].sequence < last_sequence:
                pass  # sequence numbering restarted - everything listed is new
            else:
                segments = [s for s in segments if s.sequence > last_sequence]

            for segment in segments:
                if run.stop_event.is_set():
                    return
                gap = last_sequence is not None and segment.sequence != last_sequence + 1
                title_changed = last_title is not None and segment.title != last_title
                if segment.discontinuity or title_changed or gap:
                    group += 1
                    logger.info(
                        "[%s] stream discontinuity (tag=%s, title %r -> %r, gap=%s) - possible ad break",
                        self.channel_slug, segment.discontinuity, last_title, segment.title, gap,
                    )
                    self._save_debug_playlist(response.text)
                started_at = segment.started_at or last_end or (
                    datetime.now(timezone.utc) - timedelta(seconds=segment.duration)
                )
                last_sequence, last_title = segment.sequence, segment.title
                last_end = started_at + timedelta(seconds=segment.duration)
                if self._download(client, segment, started_at, f"{run_id}-{group}"):
                    run.last_progress_at = datetime.now(timezone.utc)
                else:
                    # The next segment can't be joined straight onto the one
                    # before this hole.
                    group += 1

            if playlist.ended:
                logger.info("[%s] stream playlist ended", self.channel_slug)
                return
            run.stop_event.wait(PLAYLIST_POLL_SECONDS)

    def _download(self, client: httpx.Client, segment: _PlaylistSegment, started_at: datetime, group: str) -> bool:
        name = f"{int(started_at.timestamp() * 1000)}_{int(segment.duration * 1000)}_{group}.ts"
        for attempt in (1, 2):
            try:
                response = client.get(segment.url)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                if attempt == 2:
                    logger.warning("[%s] segment download failed: %s", self.channel_slug, type(exc).__name__)
                    return False
                continue
            # Written under a temp name first so a clip being cut right now
            # never picks up a half-written segment.
            partial = self._base_dir / (name + ".part")
            partial.write_bytes(response.content)
            partial.replace(self._base_dir / name)
            return True
        return False

    def _save_debug_playlist(self, text: str) -> None:
        try:
            DEBUG_PLAYLIST_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            (DEBUG_PLAYLIST_DIR / f"{self.channel_slug}_{stamp}.m3u8").write_text(text, encoding="utf-8")
            saved = sorted(DEBUG_PLAYLIST_DIR.glob(f"{self.channel_slug}_*.m3u8"))
            for old in saved[:-DEBUG_PLAYLISTS_KEPT]:
                old.unlink(missing_ok=True)
        except OSError:
            logger.warning("[%s] could not save debug playlist", self.channel_slug)

    def _is_stalled(self) -> bool:
        if self._run is None:
            return False
        # Nothing saved yet right after starting isn't stalled - give it a
        # moment to actually fetch the first segment.
        reference_time = self._run.last_progress_at or self._run.started_at
        return (datetime.now(timezone.utc) - reference_time).total_seconds() > STALL_TIMEOUT_SECONDS

    def _stored_segments(self) -> list[_StoredSegment]:
        stored = []
        for path in self._base_dir.glob("*.ts"):
            match = _SEGMENT_NAME.match(path.name)
            if match is None:
                continue
            start_ms, duration_ms, group = match.groups()
            stored.append(
                _StoredSegment(
                    started_at=datetime.fromtimestamp(int(start_ms) / 1000, tz=timezone.utc),
                    duration=int(duration_ms) / 1000,
                    group=group,
                    path=path,
                )
            )
        stored.sort(key=lambda segment: segment.started_at)
        return stored

    def _prune(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=BUFFER_RETENTION_SECONDS)
        for segment in self._stored_segments():
            if segment.ended_at < cutoff:
                # A file can be transiently locked (e.g. antivirus scanning a
                # just-written segment, or a clip being cut from it) - skip
                # it and let the next tick's prune retry.
                try:
                    segment.path.unlink(missing_ok=True)
                except OSError:
                    pass
        for entry in self._base_dir.iterdir():
            # Run directories left behind by the old ffmpeg-based recorder,
            # and downloads abandoned mid-write by a previous process.
            try:
                if entry.stat().st_mtime > cutoff.timestamp():
                    continue
                if entry.is_dir():
                    shutil.rmtree(entry)
                elif entry.suffix == ".part":
                    entry.unlink(missing_ok=True)
            except OSError:
                logger.warning("[%s] could not prune %s, will retry next tick", self.channel_slug, entry)

    def tick(self) -> None:
        # One run keeps following the playlist on its original URL
        # indefinitely - no need to preemptively restart against a fresh
        # one. Restart if it's never been started, its thread has exited
        # (stream ended, unexpected error), or it's stalled.
        died = self._run is not None and not self._run.thread.is_alive()
        if self._run is None or died or self._is_stalled():
            if self._run is not None and not died:
                logger.warning(
                    "[%s] recording stalled (no new segment in %ds), restarting", self.channel_slug, STALL_TIMEOUT_SECONDS
                )
            self._stop_run()
            self._start_run()
        self._prune()

    def stop(self) -> None:
        self._stop_run()

    @property
    def is_active(self) -> bool:
        return self._run is not None

    def segments_overlapping(self, start: datetime, end: datetime) -> list[_StoredSegment]:
        """All buffered segments whose time range overlaps [start, end], in order."""
        return [s for s in self._stored_segments() if s.ended_at >= start and s.started_at <= end]


def _best_group(segments: list[_StoredSegment], start: datetime, end: datetime) -> list[_StoredSegment]:
    """The segments of whichever group covers the most of [start, end].

    Segments from different groups can't be reliably joined into one clip
    (the stream layout may differ on either side of the boundary), so a
    window straddling one keeps only its longer side.
    """
    coverage: dict[str, float] = {}
    for segment in segments:
        overlap = (min(segment.ended_at, end) - max(segment.started_at, start)).total_seconds()
        coverage[segment.group] = coverage.get(segment.group, 0.0) + max(overlap, 0.0)
    best = max(coverage, key=coverage.get)
    return [s for s in segments if s.group == best]


def extract_clip(
    recorder: ChannelRecorder,
    start: datetime,
    end: datetime,
    output_name: str,
    pre_roll_seconds: int = PRE_ROLL_SECONDS,
    post_roll_seconds: int = POST_ROLL_SECONDS,
) -> Path:
    clip_start = start - timedelta(seconds=pre_roll_seconds + PLAYBACK_DELAY_SECONDS)
    clip_end = end + timedelta(seconds=post_roll_seconds - PLAYBACK_DELAY_SECONDS)
    segments = recorder.segments_overlapping(clip_start, clip_end)
    if not segments:
        raise RecorderError(f"No buffered segments cover {start} - {end} for {recorder.channel_slug!r}")
    segments = _best_group(segments, clip_start, clip_end)

    out_dir = CLIPS_DIR / recorder.channel_slug
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / output_name

    # Consecutive MPEG-TS segments of one group are a single continuous
    # stream cut into pieces, so they're joined byte-for-byte and remuxed in
    # one go - no per-file timestamp stitching for ffmpeg to get wrong.
    joined = out_dir / f".{output_name}.ts"
    try:
        with open(joined, "wb") as joined_file:
            for segment in segments:
                joined_file.write(segment.path.read_bytes())
        subprocess.run(
            [
                FFMPEG_BIN, "-y",
                "-i", str(joined),
                "-c", "copy",
                str(output_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    finally:
        joined.unlink(missing_ok=True)

    return output_path
