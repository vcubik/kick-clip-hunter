"""The whole pipeline in one test: a live stream is recorded, chat reacts, and
a clip of that reaction ends up on the dashboard.

Everything inside the service is real - the recorder thread downloading HLS
segments over HTTP, signature verification, the detector, the moment session,
ffmpeg cutting the clip, the SQLite database, the rendered page. Only the
outside world is replaced:

* the stream is a scripted HLS server on loopback serving real MPEG-TS
  segments (instead of kick.com behind a browser),
* Kick's API and webhook signatures come from the fakes in tests/support,
* the service's waits are time-compressed, and its clip geometry (pre-roll,
  context length) is scaled down to fit forty seconds of test footage.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kick_clip_hunter import detector, recorder, recording_manager
from tests.support.hls import Segment
from tests.support.html import parse
from tests.support.media import decodes_cleanly, probe
from tests.support.waiting import wait_until

pytestmark = [pytest.mark.anyio, pytest.mark.ffmpeg]

CHANNEL = "some_channel"
PRE_ROLL, POST_ROLL, DELAY = 3, 2, 1
CONTEXT_BEFORE, CONTEXT_AFTER = 3, 4


@pytest.fixture
def scaled_down_clip_geometry(service, monkeypatch):
    """Production keeps 15s before a reaction, 10s after and 30s/60s of
    context; the test stream is 40s long."""
    monkeypatch.setattr(recording_manager, "PRE_ROLL_SECONDS", PRE_ROLL)
    monkeypatch.setattr(service.main, "DYNAMIC_POST_ROLL_SECONDS", POST_ROLL)
    monkeypatch.setattr(recorder, "PLAYBACK_DELAY_SECONDS", DELAY)
    monkeypatch.setattr(recorder, "CONTEXT_BEFORE_SECONDS", CONTEXT_BEFORE)
    monkeypatch.setattr(recorder, "CONTEXT_AFTER_SECONDS", CONTEXT_AFTER)


def record_live_stream(hls, recording, ts_segments, *, break_at: int | None = None):
    """Broadcasts the forty one-second segments as a stream that started 30
    seconds ago and has the service's recorder follow it to the live edge."""
    timeline = [Segment(path.read_bytes(), duration=1.0) for path in ts_segments]
    if break_at is not None:
        timeline[break_at].discontinuity = True
    stream_start = datetime.now(timezone.utc) - timedelta(seconds=30)
    server = hls(timeline, published=3, stream_start=stream_start)
    channel_recorder = recording.of(server, channel=CHANNEL)
    # The service looks recorders up by channel when it cuts a clip.
    recording_manager._recorders[CHANNEL] = channel_recorder

    channel_recorder.tick()
    for published in range(3, len(timeline) + 1):
        wait_until(
            lambda published=published: len(channel_recorder._stored_segments()) >= published,
            f"segment {published} to be recorded",
        )
        server.publish()
    return channel_recorder


async def test_a_chat_reaction_becomes_a_clip_on_the_dashboard(
    service, hls, recording, ts_segments, scaled_down_clip_geometry
):
    # A watched channel is live, and its stream is being recorded.
    service.watch(CHANNEL, live_for_seconds=1800)
    channel_recorder = record_live_stream(hls, recording, ts_segments)
    assert len(channel_recorder._stored_segments()) == 40
    service.warm_up(CHANNEL)

    # Chat erupts: signed webhook deliveries, one per message.
    await service.crowd_laughs(CHANNEL, people=detector._dynamic_min_reaction_unique(10))

    # The detector fired exactly one moment, timed against the live stream...
    (moment,) = service.moments()
    assert moment["reason"] == "laugh"
    assert moment["stream_elapsed_seconds"] == pytest.approx(1800, abs=10)

    # ...and once the reaction has died down, its clip and context are cut.
    await service.settle()
    (moment,) = service.moments()
    clip_name = f"moment_{moment['id']}.mp4"
    assert moment["clip_path"] == f"{CHANNEL}/{clip_name}"

    clip = recorder.CLIPS_DIR / CHANNEL / clip_name
    details = probe(clip)
    assert details["streams"] == ["audio", "video"]
    # pre-roll + playback delay + the 10s detection window + (post-roll - delay),
    # in whole one-second segments.
    expected = PRE_ROLL + DELAY + detector.SHORT_WINDOW_SECONDS + (POST_ROLL - DELAY)
    assert expected - 1 <= details["duration"] <= expected + 2.5
    assert decodes_cleanly(clip)

    before, after = (clip.with_name(name) for name in recorder.context_clip_names(clip_name).values())
    assert probe(before)["duration"] == pytest.approx(CONTEXT_BEFORE, abs=0.5)
    assert probe(after)["duration"] == pytest.approx(CONTEXT_AFTER, abs=0.5)

    # The dashboard has the moment waiting in its queue and open beside it,
    # with its chat, its clip and the footage around it.
    page = parse((await service.client.get("/dashboard")).text)
    (row,) = page.find("a", class_="ch-row")
    assert row.attrs["data-moment"] == str(moment["id"])
    article = page.one("article", class_="moment")
    assert article.one("h1").text == CHANNEL
    assert "fan0 xDDD" in [line.text for line in article.find("p", class_="ch-chat")]
    assert article.one("video").attrs["src"] == f"/clips/{CHANNEL}/{clip_name}"
    assert [link.attrs["href"] for link in article.find("a", data_footage=True)] == [
        f"/clips/{CHANNEL}/moment_{moment['id']}_before.mp4",
        f"/clips/{CHANNEL}/{clip_name}",
        f"/clips/{CHANNEL}/moment_{moment['id']}_after.mp4",
    ]
    assert (await service.client.get("/moments/status")).json() == {"moments": 1, "clips": 1}

    # The clip the page points at is the file that was cut.
    served = await service.client.get(f"/clips/{CHANNEL}/{clip_name}")
    assert served.status_code == 200
    assert served.content == clip.read_bytes()

    # And the reviewer can rate it, which the next page load reflects: it
    # has left the queue of what is still to rate, and shows its rating.
    await service.client.post(f"/moments/{moment['id']}/rating?value=5")
    page = parse((await service.client.get("/dashboard")).text)
    assert page.find("a", class_="ch-row") == []
    page = parse((await service.client.get("/dashboard", params={"show": "best"})).text)
    chosen = page.one("article", class_="moment").find("button", class_="ch-key", aria_pressed="true")
    assert [key.text for key in chosen] == ["5"]


async def test_a_break_in_the_stream_right_before_the_reaction_still_yields_a_playable_clip(
    service, hls, recording, ts_segments, scaled_down_clip_geometry
):
    # The scenario behind the recorder rewrite: an ad is stitched into the
    # stream a few seconds before chat reacts. The clip must come out of the
    # footage after the break instead of being a broken file - or none at all.
    service.watch(CHANNEL)
    channel_recorder = record_live_stream(hls, recording, ts_segments, break_at=22)
    assert len({segment.group for segment in channel_recorder._stored_segments()}) == 2
    service.warm_up(CHANNEL)

    await service.crowd_laughs(CHANNEL, people=detector._dynamic_min_reaction_unique(10))
    await service.settle()

    (moment,) = service.moments()
    assert moment["clip_path"] is not None
    clip = recorder.CLIPS_DIR / moment["clip_path"]
    assert probe(clip)["streams"] == ["audio", "video"]
    assert decodes_cleanly(clip)


async def test_without_a_recording_the_moment_is_still_detected_and_shown(service):
    # The channel's stream was never recorded (say, the URL capture failed):
    # detection and review must not depend on footage existing.
    service.watch(CHANNEL)
    service.warm_up(CHANNEL)

    await service.crowd_laughs(CHANNEL, people=detector._dynamic_min_reaction_unique(10))
    await service.settle()

    (moment,) = service.moments()
    assert moment["clip_path"] is None
    page = parse((await service.client.get("/dashboard")).text)
    article = page.one("article", class_="moment")
    assert article.find("video") == []
    assert "fan0 xDDD" in [line.text for line in article.find("p", class_="ch-chat")]
    assert (await service.client.get("/moments/status")).json() == {"moments": 1, "clips": 0}
