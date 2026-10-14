"""A stream recorded whole, from the button to the page it is played on.

Everything inside the service is real: the loop that keeps a channel's
recorder going, the recorder thread downloading HLS segments over HTTP, the
writer keeping them, ffmpeg making the file, the database, the rendered
pages. As in test_webhook_to_clip.py only the outside world is replaced -
the stream is a scripted HLS server on loopback serving real MPEG-TS
segments, and Kick's API and webhook signatures come from the fakes.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone

import pytest

from kick_clip_hunter import chat_trace, clip_trim, recording_manager, stream_recording
from tests.support.data import stream_recordings
from tests.support.hls import Segment
from tests.support.html import parse
from tests.support.media import decodes_cleanly, probe
from tests.support.waiting import async_wait_until

pytestmark = [pytest.mark.anyio, pytest.mark.ffmpeg]

CHANNEL = "some_channel"


@asynccontextmanager
async def recording_loop(service, monkeypatch):
    """The recording loop as the service runs it (see main.lifespan), with
    its rounds a moment apart instead of half a minute."""
    monkeypatch.setattr(recording_manager, "TICK_INTERVAL_SECONDS", 0.05)
    main = service.main
    task = asyncio.create_task(
        recording_manager.run_forever(
            main._watchlist_slugs,
            lambda: main._flags.get("watching_enabled", True),
            main._is_channel_tracked,
            main._stream_recording_over,
            main._always_recorded_slugs,
            main._record_stream_by_itself,
        )
    )
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def live_stream(hls, recording, ts_segments, *, seconds: int, live_for: int = 20):
    """A channel's stream, live for `live_for` seconds already, of which the
    first three seconds are out and the rest is still to be broadcast."""
    timeline = [Segment(path.read_bytes(), duration=1.0) for path in ts_segments[:seconds]]
    began = datetime.now(timezone.utc) - timedelta(seconds=live_for)
    server = hls(timeline, published=3, window=len(timeline), stream_start=began)
    # Where the recorder gets its stream from, instead of a browser.
    recording_manager._recorders[CHANNEL] = recording.of(server, channel=CHANNEL)
    return server


async def test_a_stream_is_recorded_whole_and_played_back_with_its_chat(
    service, hls, recording, ts_segments, monkeypatch
):
    # A channel that is on the watchlist and live, but not tracked for clips.
    service.watch(CHANNEL)
    await service.client.post(f"/channels/{CHANNEL}/tracking?enabled=0")
    server = live_stream(hls, recording, ts_segments, seconds=40)

    async with recording_loop(service, monkeypatch):
        # Recording is started from the Channels page...
        started = await service.client.post(f"/channels/{CHANNEL}/recording/start")
        assert started.json()["recording"]["running"] is True

        # ...and the loop has the channel's recorder running at once, handing
        # what it downloads to the recording.
        kept = recording_manager.stream_recording_of(CHANNEL)
        await async_wait_until(lambda: kept.writer.seconds >= 3, "the first footage to be kept")
        assert recording.url_requests == [CHANNEL]

        # Chat arrives while it runs. It is kept, though the channel is not
        # tracked - and nobody looks for moments in it.
        chatted = await service.chat(CHANNEL, "alice", "hello there")
        said = datetime.now(timezone.utc)
        assert chatted.json() == {"status": "stored"}

        server.publish_all()
        await async_wait_until(lambda: kept.writer.seconds == 40, "the whole stream to be kept")

        # The Channels page says what is going on, and offers to stop it.
        page = parse((await service.client.get("/dashboard/channels")).text)
        control = page.one("span", class_="rec-control", data_recording=CHANNEL)
        assert control.one("span", data_recording_words=True).text.startswith("0:00:40 recorded, ")
        assert page.one("td", data_now=CHANNEL).text == "Recording"

        stopped = await service.client.post(f"/channels/{CHANNEL}/recording/stop")
        assert stopped.json()["said"] == "Saving the recording. It will be on the Recordings page."
        await service.settle()

        # With the recording over nothing asks for the channel's stream, so
        # its recorder is stopped again.
        await async_wait_until(lambda: not recording_manager._recorders[CHANNEL].is_active, "the recorder to stop")

    # One file holds the whole forty seconds.
    (row,) = stream_recordings()
    assert (row["state"], row["ended_by"]) == ("ready", "stopped")
    file = stream_recording.STREAMS_DIR / row["file_path"]
    assert probe(file) == {"duration": pytest.approx(40, abs=0.4), "streams": ["audio", "video"]}
    assert decodes_cleanly(file)
    # (The stream's clock is told to the millisecond.)
    began = datetime.fromisoformat(row["started_at"])
    assert abs(began - server.segment_start(0)) < timedelta(milliseconds=1)
    assert service.moments() == []

    # The Recordings page lists it and plays that file...
    page = parse((await service.client.get("/dashboard/recordings")).text)
    (listed,) = page.find("a", class_="rec-row")
    assert listed.one("b").text == CHANNEL and listed.one("span", class_="rec-length").text == "0:00:40"
    video = page.one("article", class_="recording").one("video", data_recording_clip=True)
    served = await service.client.get(video.attrs["src"])
    assert served.status_code == 200 and served.content == file.read_bytes()

    # ...with the chat beside it, each line on the picture its writer was
    # watching: what was broadcast a little before the line arrived.
    (line,) = page.one("aside", class_="chat").one("div", data_recording_chat=True).find("p", class_="ch-chat")
    assert line.text == "alice hello there"
    watched = (said - server.segment_start(0)).total_seconds() - chat_trace.CHAT_DELAY_SECONDS
    assert float(line.attrs["data-at"]) == pytest.approx(watched, abs=2)

    # Under the video goes what chat did over the recording, which the page
    # fetches: that one line, in the second it was watched in.
    place = page.one("figure", data_rec_trace_load=True)
    trace = parse((await service.client.get(place.attrs["data-rec-trace-load"])).text)
    counts = [int(count) for count in trace.one("div", data_rec_strip=True).attrs["data-all"].split(",")]
    # (Second by second; a file a frame over forty seconds has a 41st.)
    assert len(counts) in (40, 41) and sum(counts) == 1
    assert counts.index(1) == pytest.approx(watched, abs=2)

    # And a stretch of it can be cut out as a video of its own, from the
    # dialog the page has for that.
    address = page.one("dialog", class_="trim").attrs["data-trim"]
    asked = await service.client.post(address, json={"start": 10, "end": 22})
    assert asked.json()["state"] == "cutting"
    await service.settle()
    cut = (await service.client.get(address)).json()
    assert cut["words"].startswith("Ready to download.")
    # It begins on the last frame a cut can begin on at second 10 or before
    # it (to the frame: timestamps of synthetic footage are not round).
    assert cut["cut"]["from"] < 10.1 and cut["cut"]["to"] == 22
    stretch = file.with_name(clip_trim.recording_video_name(file.name, 10, 22))
    assert (await service.client.get(cut["video_url"])).content == stretch.read_bytes()
    assert probe(stretch) == {"duration": pytest.approx(12, abs=1.2), "streams": ["audio", "video"]}
    assert decodes_cleanly(stretch)


async def test_a_recording_ends_and_is_saved_by_itself_when_its_stream_is_over(
    service, hls, recording, ts_segments, monkeypatch
):
    service.watch(CHANNEL)
    server = live_stream(hls, recording, ts_segments, seconds=12)

    async with recording_loop(service, monkeypatch):
        await service.client.post(f"/channels/{CHANNEL}/recording/start")
        kept = recording_manager.stream_recording_of(CHANNEL)
        await async_wait_until(lambda: kept.writer.seconds >= 3, "the first footage to be kept")
        server.publish_all()
        await async_wait_until(lambda: kept.writer.seconds == 12, "the whole stream to be kept")

        # The streamer ends the stream, and nobody presses Stop. (Once a
        # stream has been gone for ten minutes its recording is over; here
        # that is cut to a moment.)
        server.end_stream()
        service.api.channels[CHANNEL].is_live = False
        monkeypatch.setattr(recording_manager, "STREAM_END_GRACE_SECONDS", 0.2)

        await async_wait_until(
            lambda: [row["state"] for row in stream_recordings()] == ["ready"],
            "the recording to end and be saved by itself",
        )
        await service.settle()

    (row,) = stream_recordings()
    assert row["ended_by"] == "stream_ended"
    assert probe(stream_recording.STREAMS_DIR / row["file_path"])["duration"] == pytest.approx(12, abs=0.4)
    assert recording_manager.stream_recording_of(CHANNEL) is None
    summary = parse((await service.client.get("/dashboard/recordings")).text).one("p", class_="moment-summary").text
    assert "It ended by itself once the stream had been gone for" in summary


async def test_a_channel_recorded_always_has_every_stream_kept_without_anyone_starting_it(
    service, hls, recording, ts_segments, monkeypatch
):
    # A channel that is neither live nor tracked for clips, set to be
    # recorded always.
    service.watch(CHANNEL, live=False)
    await service.client.post(f"/channels/{CHANNEL}/tracking?enabled=0")

    async with recording_loop(service, monkeypatch):
        switched = await service.client.post(f"/channels/{CHANNEL}/always_record?enabled=1")
        assert switched.json()["always_record"] is True
        # The loop looks at the channel now; with no stream there is nothing
        # to record, and no browser is opened to find that out.
        asked = len(service.api.calls("GET", "/channels"))
        await async_wait_until(lambda: len(service.api.calls("GET", "/channels")) >= asked + 3, "a few rounds")
        assert stream_recordings() == [] and recording.url_requests == []

        # It goes live, and nobody presses anything.
        server = live_stream(hls, recording, ts_segments, seconds=12)
        service.api.channels[CHANNEL].is_live = True
        await async_wait_until(lambda: recording_manager.stream_recording_of(CHANNEL), "a recording to start")
        kept = recording_manager.stream_recording_of(CHANNEL)
        await async_wait_until(lambda: kept.writer.seconds >= 3, "the first footage to be kept")
        server.publish_all()
        await async_wait_until(lambda: kept.writer.seconds == 12, "the whole stream to be kept")

        # Stopping it ends it for this stream: the channel is live still,
        # the switch is on still, and no second recording begins.
        stopped = await service.client.post(f"/channels/{CHANNEL}/recording/stop")
        assert stopped.json()["said"].endswith("The channel's next stream is recorded again.")
        await service.settle()
        asked = len(service.api.calls("GET", "/channels"))
        await async_wait_until(lambda: len(service.api.calls("GET", "/channels")) >= asked + 3, "a few rounds")
        assert [row["state"] for row in stream_recordings()] == ["ready"]
        assert recording_manager.stream_recording_of(CHANNEL) is None

        # The stream ends. The next one is recorded again.
        server.end_stream()
        service.api.channels[CHANNEL].is_live = False
        await async_wait_until(lambda: not recording_manager.is_held_off(CHANNEL), "the stream to be seen off")
        live_stream(hls, recording, ts_segments, seconds=6)
        service.api.channels[CHANNEL].is_live = True
        await async_wait_until(lambda: recording_manager.stream_recording_of(CHANNEL), "the next recording to start")
        again = recording_manager.stream_recording_of(CHANNEL)
        await async_wait_until(lambda: again.writer.seconds >= 3, "footage of the second stream")

        await service.client.post(f"/channels/{CHANNEL}/recording/stop")
        await service.settle()

    first, second = sorted(stream_recordings(), key=lambda row: row["id"])
    assert (first["state"], second["state"]) == ("ready", "ready")
    assert probe(stream_recording.STREAMS_DIR / first["file_path"])["duration"] == pytest.approx(12, abs=0.4)
    assert (stream_recording.STREAMS_DIR / second["file_path"]).exists()
