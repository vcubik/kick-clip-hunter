"""Continuous per-channel recording into a rolling local buffer, so a clip
can be cut for a moment shortly after it's detected.

Each `ChannelRecorder.tick()` call (meant to be driven periodically, e.g.
every 30s) makes sure an ffmpeg process is copying the channel's live HLS
stream into timestamped segment files - starting one if there isn't one
running, or restarting against a freshly fetched URL if the previous
process died or stalled (see STALL_TIMEOUT_SECONDS - a hung network read
can leave ffmpeg alive but producing nothing, which plain process-exit
detection can't catch) - and prunes segments older than the retention
window.

Segments for one continuous ffmpeg run live under a directory named for that
run's UTC start time, numbered sequentially (000000.ts, 000001.ts, ...) -
each segment's absolute time range is `run_start + index * SEGMENT_SECONDS`.
This sidesteps ffmpeg's segment `-strftime` option, which uses local time
and would otherwise have to be reconciled with the UTC timestamps used
elsewhere (detector.py, db.py).
"""

import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .kick_stream import get_live_stream_url

logger = logging.getLogger("kick_clip_hunter")

FFMPEG_BIN = "ffmpeg"
SEGMENT_SECONDS = 10
BUFFER_RETENTION_SECONDS = 600
# Chat reacts to a moment with a lag - the detector's window_start/window_end
# mark when the *reaction* (message spike) was seen, not when the funny thing
# actually happened, which is typically a few seconds earlier. So pre-roll is
# weighted heavier than post-roll. Post-roll is generous too, though: the
# payoff/aftermath of a bit often runs on past the chat spike, and clips were
# felt to be cut off at the end. 25 + the 10s detection window + 35 = 70s
# (snapped up to whole 10s buffer segments when the clip is cut).
PRE_ROLL_SECONDS = 25
POST_ROLL_SECONDS = 35
# A stalled network read (e.g. the HLS connection quietly stops delivering
# data without erroring out) can leave ffmpeg hung indefinitely without it
# ever exiting - process.poll() alone can't detect this, since it only
# reports actual termination. If no new segment has landed in this long,
# the run is presumed stalled and gets restarted anyway. Generous relative
# to SEGMENT_SECONDS so ordinary jitter (a slow manifest fetch, one missed
# tick) doesn't trigger a false-positive restart.
STALL_TIMEOUT_SECONDS = 90

RECORDINGS_DIR = Path("data/recordings")
CLIPS_DIR = Path("data/clips")

_TIME_FORMAT = "%Y%m%dT%H%M%SZ"


class RecorderError(RuntimeError):
    pass


@dataclass
class _Run:
    started_at: datetime
    dir: Path
    process: subprocess.Popen


class ChannelRecorder:
    def __init__(self, channel_slug: str):
        self.channel_slug = channel_slug
        self._base_dir = RECORDINGS_DIR / channel_slug
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._run: _Run | None = None

    def _start_run(self) -> None:
        url = get_live_stream_url(self.channel_slug)
        started_at = datetime.now(timezone.utc)
        run_dir = self._base_dir / started_at.strftime(_TIME_FORMAT)
        run_dir.mkdir(parents=True, exist_ok=True)

        process = subprocess.Popen(
            [
                FFMPEG_BIN, "-y",
                "-i", url,
                "-c", "copy",
                "-f", "segment",
                "-segment_time", str(SEGMENT_SECONDS),
                "-reset_timestamps", "1",
                str(run_dir / "%06d.ts"),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._run = _Run(started_at=started_at, dir=run_dir, process=process)
        logger.info("[%s] recording started -> %s", self.channel_slug, run_dir)

    def _stop_run(self) -> None:
        if self._run is None:
            return
        self._run.process.terminate()
        try:
            self._run.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._run.process.kill()
        self._run = None

    def _is_stalled(self) -> bool:
        if self._run is None:
            return False
        segments = list(self._run.dir.glob("*.ts"))
        # No segment yet right after starting isn't stalled - give ffmpeg a
        # moment to actually connect and write the first one.
        reference_time = (
            max(f.stat().st_mtime for f in segments) if segments else self._run.started_at.timestamp()
        )
        age = datetime.now(timezone.utc).timestamp() - reference_time
        return age > STALL_TIMEOUT_SECONDS

    def _prune_old_runs(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=BUFFER_RETENTION_SECONDS)
        for run_dir in self._base_dir.iterdir():
            if self._run is not None and run_dir == self._run.dir:
                continue
            try:
                run_started = datetime.strptime(run_dir.name, _TIME_FORMAT).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            segment_count = sum(1 for _ in run_dir.glob("*.ts"))
            run_ends = run_started + timedelta(seconds=segment_count * SEGMENT_SECONDS)
            if run_ends < cutoff:
                for f in run_dir.glob("*.ts"):
                    f.unlink(missing_ok=True)
                run_dir.rmdir()

    def tick(self) -> None:
        # ffmpeg follows a live HLS source the same way a browser player does
        # (periodically re-fetching the manifest for new segments), so one
        # process keeps working indefinitely on its original URL - no need
        # to preemptively restart against a fresh one. Restart if it's never
        # been started, has actually died, or is stalled (see _is_stalled -
        # a hung read means it's alive but producing nothing).
        needs_restart = self._run is None or self._run.process.poll() is not None or self._is_stalled()
        if needs_restart:
            if self._run is not None and self._run.process.poll() is None:
                logger.warning(
                    "[%s] recording stalled (no new segment in %ds), restarting", self.channel_slug, STALL_TIMEOUT_SECONDS
                )
            self._stop_run()
            self._start_run()
        self._prune_old_runs()

    def stop(self) -> None:
        self._stop_run()

    @property
    def is_active(self) -> bool:
        return self._run is not None

    def segments_overlapping(self, start: datetime, end: datetime) -> list[Path]:
        """All segment files (across runs) whose time range overlaps [start, end], in order."""
        matches: list[tuple[datetime, Path]] = []
        for run_dir in self._base_dir.iterdir():
            try:
                run_started = datetime.strptime(run_dir.name, _TIME_FORMAT).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            for f in sorted(run_dir.glob("*.ts")):
                index = int(f.stem)
                seg_start = run_started + timedelta(seconds=index * SEGMENT_SECONDS)
                seg_end = seg_start + timedelta(seconds=SEGMENT_SECONDS)
                if seg_end >= start and seg_start <= end:
                    matches.append((seg_start, f))
        matches.sort(key=lambda pair: pair[0])
        return [f for _, f in matches]


def extract_clip(
    recorder: ChannelRecorder,
    start: datetime,
    end: datetime,
    output_name: str,
    pre_roll_seconds: int = PRE_ROLL_SECONDS,
    post_roll_seconds: int = POST_ROLL_SECONDS,
) -> Path:
    segments = recorder.segments_overlapping(
        start - timedelta(seconds=pre_roll_seconds),
        end + timedelta(seconds=post_roll_seconds),
    )
    if not segments:
        raise RecorderError(f"No buffered segments cover {start} - {end} for {recorder.channel_slug!r}")

    out_dir = CLIPS_DIR / recorder.channel_slug
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / output_name

    concat_list = out_dir / f".{output_name}.concat.txt"
    concat_list.write_text(
        "\n".join(f"file '{seg.resolve().as_posix()}'" for seg in segments), encoding="utf-8"
    )
    try:
        subprocess.run(
            [
                FFMPEG_BIN, "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(concat_list),
                "-c", "copy",
                str(output_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    finally:
        concat_list.unlink(missing_ok=True)

    return output_path
