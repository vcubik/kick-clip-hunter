"""Continuous per-channel recording into a rolling local buffer, so a clip
can be cut for a moment shortly after it's detected.

Each `ChannelRecorder.tick()` call (meant to be driven periodically, e.g.
every 30s) makes sure an ffmpeg process is copying the channel's live HLS
stream into timestamped segment files - starting one if there isn't one
running, or restarting against a freshly fetched URL if the previous
process died - and prunes segments older than the retention window.

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
PRE_ROLL_SECONDS = 5
POST_ROLL_SECONDS = 5

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
        # to preemptively restart against a fresh one. Only restart if it's
        # never been started, or has actually died.
        needs_restart = self._run is None or self._run.process.poll() is not None
        if needs_restart:
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
