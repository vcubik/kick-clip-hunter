"""The recorder following a live stream.

A real `ChannelRecorder` - its own thread, its own HTTP client - runs against
a scripted HLS stream on loopback (tests/support/hls.py). Only two things are
replaced: where the stream URL comes from (a browser against kick.com in
production) and how often the playlist is polled.
"""

from __future__ import annotations

import itertools
import logging
from datetime import datetime, timedelta, timezone

import pytest

from kick_clip_hunter import recorder
from kick_clip_hunter.kick_stream import StreamUrlError
from kick_clip_hunter.recorder import RecorderError
from kick_clip_hunter.stream_recording import StreamWriter
from tests.support.hls import SEGMENT_SECONDS, Segment, fake_segments
from tests.support.waiting import wait_until


def stored_data(rec) -> list[bytes]:
    return [segment.path.read_bytes() for segment in rec._stored_segments()]


def groups(rec) -> list[str]:
    """Group of each stored segment in order, with the run prefix dropped:
    "0", "0", "1", ... - the part that changes at a break."""
    return [segment.group.split("-")[1] for segment in rec._stored_segments()]


def wait_for_segments(rec, count: int) -> None:
    wait_until(lambda: len(rec._stored_segments()) >= count, f"{count} recorded segments")


def start(rec, already_published: int = 3) -> None:
    """Starts the recording and waits until it has caught up with what was
    published beforehand. Without the wait, whether segments published right
    after this call count as "already there" or "new" would be a race."""
    rec.tick()
    wait_for_segments(rec, min(already_published, recorder.INITIAL_SEGMENTS))


class TestFollowingTheStream:
    def test_saves_every_segment_as_it_is_published(self, hls, recording):
        timeline = fake_segments(10)
        server = hls(timeline, published=3)
        rec = recording.of(server)

        start(rec)
        server.publish(2)
        wait_for_segments(rec, 5)
        server.publish_all()
        wait_for_segments(rec, 10)

        assert stored_data(rec) == [segment.data for segment in timeline]

    def test_names_segments_by_their_program_time(self, hls, recording):
        server = hls(fake_segments(6), published=6)
        rec = recording.of(server)

        start(rec)

        stored = rec._stored_segments()
        assert [segment.started_at for segment in stored] == [server.segment_start(index) for index in (3, 4, 5)]
        assert {segment.duration for segment in stored} == {SEGMENT_SECONDS}

    def test_starts_near_the_live_edge_rather_than_downloading_the_whole_window(self, hls, recording):
        timeline = fake_segments(6)
        server = hls(timeline, published=6, window=6)
        rec = recording.of(server)

        start(rec, already_published=6)

        assert stored_data(rec) == [segment.data for segment in timeline[-recorder.INITIAL_SEGMENTS :]]

    def test_follows_the_best_quality_variant(self, hls, recording):
        server = hls(fake_segments(4), published=4)
        rec = recording.of(server)

        start(rec)

        assert "/variant/high.m3u8" in server.requests
        assert "/variant/low.m3u8" not in server.requests

    def test_downloads_each_segment_exactly_once(self, hls, recording):
        server = hls(fake_segments(12), published=3)
        rec = recording.of(server)

        start(rec)
        for _ in range(9):
            server.publish()
            wait_for_segments(rec, server.published)

        assert server.segment_requests() == list(range(12))

    def test_an_uninterrupted_stream_is_one_group(self, hls, recording):
        server = hls(fake_segments(8), published=3)
        rec = recording.of(server)

        start(rec)
        server.publish_all()
        wait_for_segments(rec, 8)

        assert set(groups(rec)) == {"0"}

    def test_asks_for_the_stream_url_of_its_own_channel(self, hls, recording):
        server = hls(fake_segments(3), published=3)
        rec = recording.of(server, channel="a_channel")

        rec.tick()

        assert recording.url_requests == ["a_channel"]
        assert rec.is_active is True

    def test_without_program_times_it_falls_back_to_the_clock_and_stays_contiguous(self, hls, recording):
        server = hls(fake_segments(6), published=3, program_date_time=False)
        rec = recording.of(server)
        started = datetime.now(timezone.utc)

        start(rec)
        server.publish_all()
        wait_for_segments(rec, 6)

        stored = rec._stored_segments()
        assert abs(stored[0].started_at - started) < timedelta(seconds=30)
        for previous, following in itertools.pairwise(stored):
            assert following.started_at - previous.ended_at == timedelta(0)


class TestBreaksInTheStream:
    """Everything that makes the bytes on either side unsafe to join."""

    def test_a_discontinuity_starts_a_new_group(self, hls, recording):
        timeline = fake_segments(8)
        timeline[5].discontinuity = True
        server = hls(timeline, published=3)
        rec = recording.of(server)

        start(rec)
        server.publish_all()
        wait_for_segments(rec, 8)

        assert groups(rec) == ["0"] * 5 + ["1"] * 3

    def test_an_ad_break_is_its_own_group_and_recording_continues_after_it(self, hls, recording):
        # What a server-side stitched ad looks like from the playlist: a
        # discontinuity into segments with a different title, and another
        # one back. This is the situation that used to leave the old
        # ffmpeg-based recorder writing unusable video for hours.
        ad_marker = '#EXT-X-DATERANGE:ID="ad",CLASS="stitched-ad"'
        timeline = [
            *fake_segments(4),
            Segment(b"ad-1", title="Amazon|123", discontinuity=True, tags=(ad_marker,)),
            Segment(b"ad-2", title="Amazon|123"),
            Segment(b"back-1", discontinuity=True),
            Segment(b"back-2"),
            Segment(b"back-3"),
        ]
        server = hls(timeline, published=3)
        rec = recording.of(server)

        start(rec)
        server.publish_all()
        wait_for_segments(rec, 9)

        assert groups(rec) == ["0"] * 4 + ["1"] * 2 + ["2"] * 3
        assert stored_data(rec)[-3:] == [b"back-1", b"back-2", b"back-3"]

    def test_segments_from_a_source_other_than_the_broadcast_are_stored_as_an_ad(self, hls, recording):
        def source(name: str) -> str:
            return (
                f'#EXT-X-DATERANGE:ID="{name}",CLASS="{recorder.STREAM_SOURCE_CLASS}",'
                f'{recorder.STREAM_SOURCE_ATTRIBUTE}="{name}"'
            )

        timeline = [
            *fake_segments(3),
            Segment(b"ad-1", title="creative", discontinuity=True, tags=(source("creative"),)),
            Segment(b"ad-2", title="creative"),
            Segment(b"back-1", discontinuity=True, tags=(source(recorder.LIVE_STREAM_SOURCE),)),
            Segment(b"back-2"),
        ]
        server = hls(timeline, published=3, window=len(timeline))
        rec = recording.of(server)

        start(rec)
        server.publish_all()
        wait_for_segments(rec, 7)

        assert [segment.is_ad for segment in rec._stored_segments()] == [False] * 3 + [True] * 2 + [False] * 2

    def test_a_change_of_segment_title_alone_starts_a_new_group(self, hls, recording):
        timeline = fake_segments(6)
        for segment in timeline[3:]:
            segment.title = "something-else"
        server = hls(timeline, published=3)
        rec = recording.of(server)

        start(rec)
        server.publish_all()
        wait_for_segments(rec, 6)

        assert groups(rec) == ["0"] * 3 + ["1"] * 3

    def test_segments_missed_while_falling_behind_start_a_new_group(self, hls, recording):
        # The playlist only lists the last few segments. If more than that
        # are published between two polls, the ones in between are gone.
        timeline = fake_segments(14)
        server = hls(timeline, published=3, window=4)
        rec = recording.of(server)

        start(rec)
        server.publish(10)  # 13 published, only the last 4 still listed
        wait_for_segments(rec, 7)

        assert stored_data(rec) == [s.data for s in timeline[:3]] + [s.data for s in timeline[9:13]]
        assert groups(rec) == ["0"] * 3 + ["1"] * 4

    def test_each_break_is_logged_and_its_playlist_kept_for_inspection(self, hls, recording, caplog):
        timeline = fake_segments(6)
        timeline[4].discontinuity = True
        server = hls(timeline, published=3)
        rec = recording.of(server, channel="a_channel")

        with caplog.at_level(logging.INFO, logger="kick_clip_hunter"):
            start(rec)
            server.publish_all()
            wait_for_segments(rec, 6)

        assert any("stream discontinuity" in record.getMessage() for record in caplog.records)
        (saved,) = sorted(recorder.DEBUG_PLAYLIST_DIR.glob("a_channel_*.m3u8"))
        assert "#EXT-X-DISCONTINUITY" in saved.read_text(encoding="utf-8")

    def test_only_the_most_recent_break_playlists_are_kept(self, hls, recording, monkeypatch):
        monkeypatch.setattr(recorder, "DEBUG_PLAYLISTS_KEPT", 2)
        timeline = fake_segments(12)
        for index in (4, 6, 8, 10):
            timeline[index].discontinuity = True
        server = hls(timeline, published=3)
        rec = recording.of(server)

        start(rec)
        for _ in range(9):
            server.publish()
            wait_for_segments(rec, server.published)

        assert len(list(recorder.DEBUG_PLAYLIST_DIR.glob("*.m3u8"))) == 2

    def test_new_date_range_classes_are_logged_once(self, hls, recording, caplog):
        timeline = fake_segments(6)
        timeline[4].tags = ('#EXT-X-DATERANGE:ID="x",CLASS="stitched-ad",START-DATE="2026-01-01T12:00:08Z"',)
        server = hls(timeline, published=3)
        rec = recording.of(server)

        with caplog.at_level(logging.INFO, logger="kick_clip_hunter"):
            start(rec)
            server.publish_all()
            wait_for_segments(rec, 6)

        seen = [record.getMessage() for record in caplog.records if "date-range class seen" in record.getMessage()]
        assert sum("stitched-ad" in message for message in seen) == 1
        assert sum(message.endswith(": timestamp") for message in seen) == 1


class TestFailures:
    def test_a_segment_that_fails_once_is_retried(self, hls, recording):
        timeline = fake_segments(6)
        server = hls(timeline, published=3)
        server.fail_next["/segment/4.ts"] = [500]
        rec = recording.of(server)

        start(rec)
        server.publish_all()
        wait_for_segments(rec, 6)

        assert stored_data(rec) == [segment.data for segment in timeline]
        assert set(groups(rec)) == {"0"}

    def test_a_segment_that_keeps_failing_is_skipped_and_leaves_a_break(self, hls, recording, caplog):
        timeline = fake_segments(7)
        server = hls(timeline, published=3)
        server.fail_next["/segment/4.ts"] = [500, 503]
        rec = recording.of(server)

        with caplog.at_level(logging.WARNING, logger="kick_clip_hunter"):
            start(rec)
            server.publish_all()
            wait_for_segments(rec, 6)

        assert stored_data(rec) == [segment.data for index, segment in enumerate(timeline) if index != 4]
        # Segments 5 and 6 can't be joined straight onto 3.
        assert groups(rec) == ["0"] * 4 + ["1"] * 2
        assert any("segment download failed" in record.getMessage() for record in caplog.records)

    def test_playlist_errors_are_ridden_out(self, hls, recording):
        timeline = fake_segments(6)
        server = hls(timeline, published=3)
        rec = recording.of(server)
        start(rec)

        server.fail_next["playlist"] = [500, 502, 404]
        server.publish_all()
        wait_for_segments(rec, 6)

        assert stored_data(rec) == [segment.data for segment in timeline]
        assert recording.url_requests == ["some_channel"]  # no restart needed

    def test_no_half_written_file_is_ever_visible_as_a_segment(self, hls, recording):
        timeline = [Segment(bytes([index]) * 200_000) for index in range(8)]
        server = hls(timeline, published=3)
        rec = recording.of(server)

        start(rec)
        server.publish_all()
        # Sample the buffer while downloads are in flight.
        seen_sizes: set[int] = set()

        def all_downloaded() -> bool:
            stored = rec._stored_segments()
            seen_sizes.update(segment.path.stat().st_size for segment in stored)
            return len(stored) == 8

        wait_until(all_downloaded, "all 8 segments")

        assert seen_sizes <= {200_000}
        assert not list(rec._base_dir.glob("*.part"))

    def test_an_unexpected_error_ends_the_run_loudly_and_the_next_tick_recovers(
        self, hls, recording, monkeypatch, caplog
    ):
        # Whatever goes wrong inside the recording thread must not vanish
        # with it: it is logged, and the dead run is replaced.
        server = hls(fake_segments(6), published=3)
        rec = recording.of(server)
        start(rec)
        broken_run = rec._run
        healthy_parse = recorder._parse_playlist

        def exploding_parse(text, url):
            raise ValueError("playlist from another planet")

        with caplog.at_level(logging.ERROR, logger="kick_clip_hunter"):
            monkeypatch.setattr(recorder, "_parse_playlist", exploding_parse)
            wait_until(lambda: not broken_run.thread.is_alive(), "the recording thread to die")
        monkeypatch.setattr(recorder, "_parse_playlist", healthy_parse)
        rec.tick()
        server.publish_all()

        assert any("recording thread failed" in r.getMessage() and r.exc_info for r in caplog.records)
        assert rec._run is not broken_run
        wait_until(lambda: len(server.segment_requests()) >= 6, "recording to resume")

    def test_a_master_playlist_without_variants_fails_the_start(self, hls, recording, monkeypatch):
        server = hls(fake_segments(3), published=3)
        rec = recording.of(server)
        monkeypatch.setattr(recorder.httpx, "get", lambda url, **kwargs: _Response("#EXTM3U\n"))

        with pytest.raises(RecorderError):
            rec.tick()

        assert rec.is_active is False

    def test_a_channel_that_is_not_live_fails_the_start(self, recording, monkeypatch):
        def not_live(slug):
            raise StreamUrlError(f"No live stream URL captured for {slug!r} - is it live?")

        rec = recorder.ChannelRecorder("offline_channel")
        monkeypatch.setattr(recorder, "get_stream_urls", not_live)

        with pytest.raises(StreamUrlError):
            rec.tick()

        assert rec.is_active is False


class _Response:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        pass


class TestRunLifecycle:
    def test_a_healthy_run_is_left_alone_by_further_ticks(self, hls, recording):
        server = hls(fake_segments(6), published=3)
        rec = recording.of(server)

        start(rec)
        rec.tick()
        rec.tick()

        assert recording.url_requests == ["some_channel"]

    def test_when_the_stream_ends_the_next_tick_starts_over_with_a_fresh_url(self, hls, recording):
        server = hls(fake_segments(4), published=3)
        rec = recording.of(server)
        start(rec)
        first_run = rec._run

        server.end_stream()
        wait_until(lambda: not first_run.thread.is_alive(), "the recording thread to notice the stream ended")
        rec.tick()

        assert recording.url_requests == ["some_channel", "some_channel"]
        assert rec._run is not first_run
        assert rec._run.thread.is_alive()

    def test_a_stream_that_stops_delivering_is_restarted(self, hls, recording, monkeypatch, caplog):
        # Alive but producing nothing: the failure mode a plain "is the
        # process still running" check can't see.
        monkeypatch.setattr(recorder, "STALL_TIMEOUT_SECONDS", 0.3)
        server = hls(fake_segments(6), published=3)
        rec = recording.of(server)
        start(rec)
        stalled_run = rec._run

        wait_until(rec._is_stalled, "the run to count as stalled")
        with caplog.at_level(logging.WARNING, logger="kick_clip_hunter"):
            rec.tick()

        assert stalled_run.thread.is_alive() is False
        assert rec._run is not stalled_run
        assert recording.url_requests == ["some_channel", "some_channel"]
        assert any("recording stalled" in record.getMessage() for record in caplog.records)

    def test_a_run_that_has_only_just_started_is_not_stalled(self, hls, recording, monkeypatch):
        server = hls(fake_segments(3), published=0)  # nothing to download yet
        rec = recording.of(server)

        rec.tick()

        assert rec._is_stalled() is False

    def test_segments_keep_arriving_after_the_sequence_numbering_restarts(self, hls, recording):
        timeline = fake_segments(10)
        server = hls(timeline, published=4, first_sequence=5000)
        rec = recording.of(server)
        start(rec)

        server.first_sequence = 0
        server.publish_all()

        wait_until(lambda: timeline[-1].data in stored_data(rec), "segments published after the reset")

    def test_stop_ends_the_run_and_nothing_more_is_downloaded(self, hls, recording):
        server = hls(fake_segments(8), published=3)
        rec = recording.of(server)
        start(rec)
        thread = rec._run.thread

        rec.stop()
        requests_at_stop = len(server.requests)
        server.publish_all()

        assert rec.is_active is False
        assert thread.is_alive() is False
        assert len(rec._stored_segments()) == 3
        assert len(server.requests) == requests_at_stop
        assert not list(rec._base_dir.glob("*.part"))

    def test_stopping_twice_or_before_starting_is_harmless(self, hls, recording):
        rec = recording.of(hls(fake_segments(3), published=3))

        rec.stop()
        rec.tick()
        rec.stop()
        rec.stop()

        assert rec.is_active is False

    def test_a_tick_prunes_the_buffer_while_recording_continues(self, hls, recording, monkeypatch):
        server = hls(fake_segments(6), published=3, stream_start=datetime.now(timezone.utc) - timedelta(seconds=30))
        rec = recording.of(server)
        start(rec)

        # Shrink the retention window so everything recorded so far is "old".
        monkeypatch.setattr(recorder, "BUFFER_RETENTION_SECONDS", 1)
        rec.tick()

        assert rec._stored_segments() == []
        server.publish_all()
        wait_until(lambda: len(server.segment_requests()) == 6, "recording to carry on")


class TestHandingTheStreamOn:
    """While a stream is kept whole, every segment of the broadcast is also
    given to the recording's writer as it is saved (stream_recording.py)."""

    @pytest.fixture
    def writer(self, tmp_path):
        writer = StreamWriter(tmp_path / "stream_1_parts")
        yield writer
        writer.close()

    def parts(self, writer) -> list[bytes]:
        """What the writer kept, part by part, in the order it was on air."""
        return [path.read_bytes() for path in sorted(writer.folder.iterdir())]

    def test_everything_recorded_while_it_is_attached_is_kept_in_order(self, hls, recording, writer):
        timeline = fake_segments(10)
        server = hls(timeline, published=3, window=len(timeline))
        rec = recording.of(server)
        rec.whole_stream = writer

        start(rec)
        server.publish_all()
        wait_for_segments(rec, 10)
        wait_until(lambda: writer.seconds == 10 * SEGMENT_SECONDS, "all of it to be handed on")

        assert self.parts(writer) == [b"".join(segment.data for segment in timeline)]
        assert writer.started_at == server.segment_start(0)

    def test_the_buffer_gets_the_same_segments_either_way(self, hls, recording, writer):
        timeline = fake_segments(6)
        server = hls(timeline, published=3)
        rec = recording.of(server)
        rec.whole_stream = writer

        start(rec)
        server.publish_all()
        wait_for_segments(rec, 6)

        assert stored_data(rec) == [segment.data for segment in timeline]
        assert server.segment_requests() == list(range(6))  # fetched once, kept twice

    def test_only_what_is_broadcast_from_then_on_is_kept(self, hls, recording, writer):
        # The buffer is not emptied into a recording that starts later.
        timeline = fake_segments(8)
        server = hls(timeline, published=3)
        rec = recording.of(server)
        start(rec)

        rec.whole_stream = writer
        server.publish_all()
        wait_until(lambda: writer.seconds == 5 * SEGMENT_SECONDS, "the rest to be handed on")

        assert self.parts(writer) == [b"".join(segment.data for segment in timeline[3:])]

    def test_it_stops_when_the_writer_is_taken_away_and_the_buffer_carries_on(self, hls, recording, writer):
        timeline = fake_segments(8)
        server = hls(timeline, published=3)
        rec = recording.of(server)
        rec.whole_stream = writer
        start(rec)
        wait_until(lambda: writer.seconds == 3 * SEGMENT_SECONDS, "the first three to be handed on")

        rec.whole_stream = None
        server.publish_all()
        wait_for_segments(rec, 8)

        assert self.parts(writer) == [b"".join(segment.data for segment in timeline[:3])]

    def test_a_break_in_the_stream_is_a_break_in_what_is_kept(self, hls, recording, writer):
        timeline = fake_segments(8)
        timeline[5].discontinuity = True
        server = hls(timeline, published=3)
        rec = recording.of(server)
        rec.whole_stream = writer

        start(rec)
        server.publish_all()
        wait_until(lambda: writer.seconds == 8 * SEGMENT_SECONDS, "all of it to be handed on")

        assert self.parts(writer) == [
            b"".join(segment.data for segment in timeline[:5]),
            b"".join(segment.data for segment in timeline[5:]),
        ]

    def test_an_ad_is_not_part_of_the_stream(self, hls, recording, writer):
        def source(name: str) -> str:
            return (
                f'#EXT-X-DATERANGE:ID="{name}",CLASS="{recorder.STREAM_SOURCE_CLASS}",'
                f'{recorder.STREAM_SOURCE_ATTRIBUTE}="{name}"'
            )

        timeline = [
            *fake_segments(3),
            Segment(b"ad-1;", title="creative", discontinuity=True, tags=(source("creative"),)),
            Segment(b"ad-2;", title="creative"),
            Segment(b"back-1;", discontinuity=True, tags=(source(recorder.LIVE_STREAM_SOURCE),)),
            Segment(b"back-2;"),
        ]
        server = hls(timeline, published=3, window=len(timeline))
        rec = recording.of(server)
        rec.whole_stream = writer

        start(rec)
        server.publish_all()
        wait_for_segments(rec, 7)
        wait_until(lambda: writer.seconds == 5 * SEGMENT_SECONDS, "the broadcast's own segments")

        assert self.parts(writer) == [b"".join(segment.data for segment in timeline[:3]), b"back-1;back-2;"]

    def test_a_segment_that_could_not_be_fetched_leaves_a_break(self, hls, recording, writer):
        timeline = fake_segments(7)
        server = hls(timeline, published=3)
        server.fail_next["/segment/4.ts"] = [500, 503]
        rec = recording.of(server)
        rec.whole_stream = writer

        start(rec)
        server.publish_all()
        wait_until(lambda: writer.seconds == 6 * SEGMENT_SECONDS, "everything that could be fetched")

        assert self.parts(writer) == [
            b"".join(segment.data for segment in timeline[:4]),
            b"".join(segment.data for segment in timeline[5:]),
        ]

    def test_a_recorder_that_starts_over_does_not_have_the_same_footage_kept_twice(self, hls, recording, writer):
        timeline = fake_segments(9)
        server = hls(timeline, published=5, window=len(timeline))
        rec = recording.of(server)
        rec.whole_stream = writer
        start(rec, already_published=5)
        wait_until(lambda: writer.seconds == 3 * SEGMENT_SECONDS, "the first run's segments")

        # The run is stopped and a fresh one starts three segments back from
        # the live edge again: two it has already kept, and one that is new.
        rec.stop()
        server.publish(1)
        rec.tick()
        wait_until(lambda: writer.seconds == 4 * SEGMENT_SECONDS, "the second run's one new segment")
        server.publish_all()
        wait_until(lambda: writer.seconds == 7 * SEGMENT_SECONDS, "the second run to catch up")

        assert b"".join(self.parts(writer)) == b"".join(segment.data for segment in timeline[2:])

    def test_a_writer_that_fails_does_not_cost_the_buffer_anything(self, hls, recording, caplog):
        class Broken:
            def add(self, *segment):
                raise RuntimeError("no room left")

        timeline = fake_segments(6)
        server = hls(timeline, published=3)
        rec = recording.of(server)
        rec.whole_stream = Broken()

        with caplog.at_level(logging.ERROR, logger="kick_clip_hunter"):
            start(rec)
            server.publish_all()
            wait_for_segments(rec, 6)

        assert stored_data(rec) == [segment.data for segment in timeline]
        assert any("could not be handed on" in record.getMessage() for record in caplog.records)


class TestWhereItStands:
    """What a recorder says of itself for the Health page (`state`)."""

    @staticmethod
    def has_footage(rec) -> bool:
        return rec.state().footage_at is not None

    def test_a_recorder_that_is_not_running_has_nothing_to_say(self, hls, recording):
        rec = recording.of(hls(fake_segments(3), published=3))
        assert rec.state() is None

        start(rec)
        rec.stop()

        assert rec.state() is None

    def test_a_running_one_says_since_when_and_when_its_newest_footage_arrived(self, hls, recording):
        before = datetime.now(timezone.utc)
        rec = recording.of(hls(fake_segments(6), published=3))

        start(rec)
        wait_until(lambda: self.has_footage(rec), "footage")

        state = rec.state()
        assert before <= state.since <= state.footage_at <= datetime.now(timezone.utc)
        assert state.has_vod is False

    def test_until_its_first_segment_it_has_no_footage(self, hls, recording):
        rec = recording.of(hls(fake_segments(3), published=0))  # nothing to download yet

        rec.tick()

        assert rec.state().footage_at is None

    def test_it_says_whether_the_broadcasts_own_recording_was_found(self, hls, recording):
        rec = recording.of(hls(fake_segments(6), published=3), vod=hls(fake_segments(6), published=3))

        start(rec)

        assert rec.state().has_vod is True

    def test_starting_over_does_not_make_it_look_freshly_started(self, hls, recording, monkeypatch):
        monkeypatch.setattr(recorder, "STALL_TIMEOUT_SECONDS", 0.3)
        rec = recording.of(hls(fake_segments(6), published=3))
        start(rec)
        wait_until(lambda: self.has_footage(rec), "footage")
        before = rec.state()

        wait_until(rec._is_stalled, "the run to count as stalled")
        rec.tick()

        assert rec._run.started_at > before.since
        assert rec.state().since == before.since

    def test_fetching_the_same_segments_again_is_not_fresh_footage(self, hls, recording, monkeypatch):
        # A stream that lists nothing new: the run that starts over downloads
        # the last few segments once more, and nothing has come in.
        monkeypatch.setattr(recorder, "STALL_TIMEOUT_SECONDS", 0.3)
        rec = recording.of(hls(fake_segments(6), published=3))
        start(rec)
        wait_until(lambda: self.has_footage(rec), "footage")
        before = rec.state()

        wait_until(rec._is_stalled, "the run to count as stalled")
        rec.tick()
        again = rec._run
        wait_until(lambda: again.last_progress_at is not None, "the new run to have fetched a segment")

        assert rec.state().footage_at == before.footage_at

    def test_footage_that_is_new_after_starting_over_is(self, hls, recording, monkeypatch):
        monkeypatch.setattr(recorder, "STALL_TIMEOUT_SECONDS", 0.3)
        server = hls(fake_segments(8), published=3)
        rec = recording.of(server)
        start(rec)
        wait_until(lambda: self.has_footage(rec), "footage")
        before = rec.state()

        wait_until(rec._is_stalled, "the run to count as stalled")
        rec.tick()
        server.publish_all()

        wait_until(lambda: rec.state().footage_at > before.footage_at, "the new segments to count as footage")

    def test_stopping_it_forgets_how_long_it_had_been_running(self, hls, recording):
        rec = recording.of(hls(fake_segments(6), published=3))
        start(rec)
        wait_until(lambda: self.has_footage(rec), "footage")
        first = rec.state()

        rec.stop()
        rec.tick()

        assert rec.state().since > first.since
