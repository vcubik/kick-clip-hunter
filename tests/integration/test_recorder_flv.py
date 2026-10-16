"""The recorder following a stream that comes as one FLV download, as
TikTok's do.

A real `ChannelRecorder` - its own thread, its own HTTP client - reads an FLV
stream of real video served on loopback (FakeFlvServer), cuts it into
pieces and has ffmpeg copy each into MPEG-TS. Only where the stream is comes
from elsewhere: TikTok's API in production.
"""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kick_clip_hunter import flv, recorder, stream_recording
from kick_clip_hunter.platforms import StreamUrls
from kick_clip_hunter.stream_recording import StreamWriter
from tests.support.media import FFMPEG, FFPROBE, decodes_cleanly, probe, video_stream
from tests.support.tiktok import FakeFlvServer, as_tiktok_sends_it, flv_file, make_flv, tags_of
from tests.support.waiting import wait_until

pytestmark = pytest.mark.ffmpeg

# When the stream began, as its onMetaData says, and how long it has been
# on by the first frame the recorder gets.
STARTED_AT_MS = 1792063475583
LIVE_FOR_MS = 25_960_000
SECONDS = 13


@pytest.fixture(scope="module")
def plain_flv(tmp_path_factory) -> bytes:
    """Thirteen seconds of the test pattern as ffmpeg writes FLV: a keyframe
    every second, its times from zero, nothing said of when it began."""
    return make_flv(tmp_path_factory.mktemp("flv") / "plain.flv", SECONDS)


@pytest.fixture(scope="module")
def tiktok_flv(plain_flv) -> bytes:
    """The same, as TikTok sends a stream."""
    return as_tiktok_sends_it(plain_flv, started_at_ms=STARTED_AT_MS, live_for_ms=LIVE_FOR_MS)


@pytest.fixture
def follow(monkeypatch):
    """A recorder of a TikTok channel following an FLV stream served on
    loopback until the download ends."""
    servers: list[FakeFlvServer] = []
    recorders: list[recorder.ChannelRecorder] = []

    def start(content: bytes, *, expires_at: datetime | None = None, writer: StreamWriter | None = None):
        server = FakeFlvServer(content)
        servers.append(server)
        monkeypatch.setattr(
            recorder, "stream_urls", lambda slug: StreamUrls(live=server.url, expires_at=expires_at, flv=True)
        )
        rec = recorder.ChannelRecorder("some_channel@tiktok")
        recorders.append(rec)
        rec.whole_stream = writer
        rec.tick()
        wait_until(lambda: not rec._run.thread.is_alive(), "the download to end", timeout=60)
        return rec, server

    yield start
    for rec in recorders:
        rec.stop()
    for server in servers:
        server.close()


def joined(rec: recorder.ChannelRecorder, into: Path) -> Path:
    into.write_bytes(b"".join(segment.path.read_bytes() for segment in rec._stored_segments()))
    return into


def untimed_pictures(path: Path) -> int:
    """How many of a video's pictures ffmpeg can't tell the time of."""
    # fmt: off
    result = subprocess.run(
        [
            FFPROBE, "-v", "error", "-select_streams", "v",
            "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(path),
        ],
        check=True, capture_output=True, text=True,
    )
    # fmt: on
    # A line for each picture, the time and the separator after it.
    return sum(1 for line in result.stdout.splitlines() if line.strip().rstrip(",") == "N/A")


def first_keyframe(content: bytes) -> datetime:
    """When the first keyframe of a stream dressed as TikTok sends it was
    broadcast, by its onMetaData and its own time."""
    time = next(tag.time for tag in tags_of(content) if tag.is_keyframe and tag.is_frame)
    return datetime.fromtimestamp((STARTED_AT_MS + time) / 1000, tz=timezone.utc)


class TestFollowingAnFlvStream:
    def test_it_is_kept_as_mpeg_ts_segments_named_by_the_streams_own_clock(self, follow, tiktok_flv):
        rec, server = follow(tiktok_flv)

        stored = rec._stored_segments()
        began = first_keyframe(tiktok_flv)
        assert began - datetime.fromtimestamp((STARTED_AT_MS + LIVE_FOR_MS) / 1000, tz=timezone.utc) < timedelta(
            seconds=0.1
        )
        # Keyframes every second, a piece at least four long; what follows
        # the last keyframe it was cut at has no end to keep it by.
        assert [segment.started_at for segment in stored] == [began + timedelta(seconds=s) for s in (0, 4, 8)]
        assert [segment.duration for segment in stored] == [4.0, 4.0, 4.0]
        assert len({segment.group for segment in stored}) == 1
        assert all(segment.path.suffix == ".ts" and segment.path.read_bytes()[:1] == b"\x47" for segment in stored)
        assert server.requests == 1

    def test_its_segments_join_into_one_stream_that_plays(self, follow, tiktok_flv, tmp_path):
        rec, _ = follow(tiktok_flv)

        whole = joined(rec, tmp_path / "joined.ts")

        assert decodes_cleanly(whole)
        assert probe(whole)["duration"] == pytest.approx(12.0, abs=0.2)
        assert untimed_pictures(whole) == 0

    def test_what_it_brings_in_is_noted_for_the_health_page(self, follow, tiktok_flv):
        before = datetime.now(timezone.utc)

        rec, _ = follow(tiktok_flv)

        state = rec.state()
        assert state.footage_at is not None and state.footage_at >= before
        assert state.has_vod is False

    def test_without_a_start_time_the_stream_is_timed_by_when_it_arrived(self, follow, plain_flv):
        before = datetime.now(timezone.utc)

        rec, _ = follow(plain_flv)

        first = rec._stored_segments()[0].started_at
        assert before - timedelta(seconds=1) <= first <= datetime.now(timezone.utc)

    def test_a_picture_encoded_otherwise_part_way_starts_a_new_group(self, follow, plain_flv, tmp_path):
        other = make_flv(tmp_path / "other.flv", 9, size="176x144")
        after = [
            flv.Tag(tag.kind, tag.time + SECONDS * 1000 if tag.is_frame else 0, tag.data)
            for tag in tags_of(other)
            if tag.kind != flv.SCRIPT
        ]

        rec, _ = follow(flv_file([*tags_of(plain_flv), *after]))

        groups = [segment.group.split("-")[1] for segment in rec._stored_segments()]
        assert groups == ["0", "0", "0", "1", "1"]

    def test_a_piece_that_cannot_be_copied_leaves_a_hole_after_which_a_new_group_starts(
        self, follow, tiktok_flv, monkeypatch, caplog
    ):
        copy = recorder._flv_piece_as_ts
        copies = []

        def failing_second(piece):
            copies.append(piece)
            if len(copies) == 2:
                raise subprocess.CalledProcessError(1, FFMPEG)
            return copy(piece)

        monkeypatch.setattr(recorder, "_flv_piece_as_ts", failing_second)
        with caplog.at_level(logging.WARNING, logger="kick_clip_hunter"):
            rec, _ = follow(tiktok_flv)

        stored = rec._stored_segments()
        assert len(stored) == 2 and stored[0].group != stored[1].group
        assert any("could not be copied into MPEG-TS" in record.getMessage() for record in caplog.records)

    def test_a_stream_that_cannot_be_downloaded_saves_nothing(self, follow, tiktok_flv, caplog):
        with caplog.at_level(logging.ERROR, logger="kick_clip_hunter"):
            server = FakeFlvServer(b"")
            server.close()
            rec, _ = follow(b"<html>Access Denied</html>")

        assert rec._stored_segments() == []
        assert any("recording thread failed" in record.getMessage() for record in caplog.records)

    def test_an_address_that_is_running_out_ends_the_run_for_a_new_one(self, follow, tiktok_flv, caplog):
        soon = datetime.now(timezone.utc) + timedelta(seconds=recorder.RENEW_BEFORE_SECONDS - 1)

        with caplog.at_level(logging.INFO, logger="kick_clip_hunter"):
            rec, _ = follow(tiktok_flv, expires_at=soon)

        assert rec._stored_segments() == []
        assert any("running out" in record.getMessage() for record in caplog.records)


class TestKeepingTheStreamWhole:
    def test_its_recording_is_one_video_with_every_frame_timed(self, follow, tiktok_flv, tmp_path):
        writer = StreamWriter(tmp_path / "parts")

        follow(tiktok_flv, writer=writer)
        writer.close()
        (footage,) = writer.folder.glob("part_*.ts")
        assert untimed_pictures(footage) == 0
        parts = stream_recording.save(writer.folder, tmp_path / "stream.mp4")

        assert [(part.started_at, round(part.seconds)) for part in parts] == [(first_keyframe(tiktok_flv), 12)]
        assert decodes_cleanly(tmp_path / "stream.mp4")
        # Ten pictures a second, every one of them there.
        assert video_stream(tmp_path / "stream.mp4")["nb_frames"] == "120"

    def test_tiktoks_sei_units_would_cost_a_frame_its_time_if_they_were_kept(self, tiktok_flv, tmp_path):
        # Why they are left out (flv.SEI_UNITS): copied as they come, the
        # picture after every keyframe loses its time.
        source = tmp_path / "tiktok.flv"
        source.write_bytes(tiktok_flv)
        copied = tmp_path / "copied.ts"
        subprocess.run([FFMPEG, "-v", "error", "-i", str(source), "-c", "copy", str(copied)], check=True)

        assert untimed_pictures(copied) > 0
