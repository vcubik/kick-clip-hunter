"""Real (tiny) media for the tests that cut clips with ffmpeg.

`make_ts_segments` has ffmpeg synthesise a short test pattern with a tone and
split it into MPEG-TS segments - the same container a live stream's segments
arrive in - so clip cutting is tested against input ffmpeg actually has to
parse, not against a stub of ffmpeg.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"


def require_ffmpeg() -> None:
    """Skip the calling test when ffmpeg isn't installed - or fail it when
    the environment says it must be (CI sets REQUIRE_FFMPEG=1 so these tests
    can never be skipped there unnoticed)."""
    if shutil.which(FFMPEG) and shutil.which(FFPROBE):
        return
    if os.environ.get("REQUIRE_FFMPEG") == "1":
        pytest.fail("ffmpeg/ffprobe not found on PATH, but REQUIRE_FFMPEG=1")
    pytest.skip("ffmpeg/ffprobe not found on PATH")


def make_ts_segments(directory: Path, count: int, seconds_each: float = 1.0, size: str = "160x120") -> list[Path]:
    """`count` consecutive MPEG-TS segments of `seconds_each` seconds, cut
    from one continuous test-pattern stream with a tone as its audio."""
    directory.mkdir(parents=True, exist_ok=True)
    fps = 10
    total = count * seconds_each
    keyframe_interval = str(int(fps * seconds_each))
    # fmt: off
    subprocess.run(
        [
            FFMPEG, "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc=size={size}:rate={fps}:duration={total}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={total}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            # A keyframe at every segment boundary, so each segment can be
            # decoded on its own like a real HLS media segment.
            "-g", keyframe_interval, "-keyint_min", keyframe_interval, "-sc_threshold", "0",
            "-c:a", "aac", "-b:a", "32k",
            "-f", "segment", "-segment_time", str(seconds_each), "-segment_format", "mpegts",
            str(directory / "%03d.ts"),
        ],
        check=True,
    )
    # fmt: on
    segments = sorted(directory.glob("*.ts"))[:count]
    assert len(segments) == count, f"ffmpeg produced {len(segments)} segments, expected {count}"
    return segments


def probe(path: Path) -> dict:
    """Duration and stream types of a media file, via ffprobe."""
    # fmt: off
    result = subprocess.run(
        [
            FFPROBE, "-v", "error",
            "-show_entries", "format=duration:stream=codec_type,codec_name",
            "-of", "json", str(path),
        ],
        check=True, capture_output=True, text=True,
    )
    # fmt: on
    data = json.loads(result.stdout)
    return {
        "duration": float(data["format"]["duration"]),
        "streams": sorted(stream["codec_type"] for stream in data["streams"]),
    }


def decodes_cleanly(path: Path) -> bool:
    """Whether ffmpeg can decode the whole file without reporting an error."""
    result = subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-f", "null", "-"], capture_output=True, text=True)
    return result.returncode == 0 and not result.stderr.strip()
