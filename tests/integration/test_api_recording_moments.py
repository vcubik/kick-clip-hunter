"""Moments looked for in a stream that was kept whole, from the Recordings
page: the chat stored with the recording replayed through the detector,
what that finds listed against the recording's file, and any of it made
into a moment with its clip cut out of that file.

What a replay finds is tests/unit/test_replay_chat.py's to settle, and
where a moment lies in a file tests/unit/test_recording_moments.py's. Here
the two run behind the service's requests, on a scripted stream: chat in
the database as a stream leaves it, and - where clips are cut - real video
for ffmpeg to cut them from.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from kick_clip_hunter import dashboard_view as view
from kick_clip_hunter import db, detector, recorder, recording_manager, recording_moments, replay, stream_recording
from kick_clip_hunter import tuning as tuning_module
from tests.support.chat import StoredStream, exact_crowd, people
from tests.support.data import T0, add_moment, add_stream_recording, add_streamer, stream_recordings
from tests.support.html import Element, parse
from tests.support.media import decodes_cleanly, make_video, probe, require_ffmpeg
from tests.support.waiting import async_wait_until

pytestmark = pytest.mark.anyio

# The channel as the watchlist and its recordings have it, and as Kick sends
# its chat - which is what its moments are stored under.
CHANNEL = "Some_Channel"
CHAT_NAME = "some_channel"
SECONDS = 300.0
# How far into the recording the first crowd laughs, and how long after it
# the second one starts.
FIRST = 60
BETWEEN = 120
TOLERANCE = 0.35


def stream_chat(*, began: datetime = T0, channel: str = CHAT_NAME, broadcaster_user_id: int = 1) -> None:
    """The chat of a stream whose recording began at `began`: talk from as
    long before its first frame as the detector looks back, a minute into it
    a crowd that laughs once, and two minutes later one that keeps at it."""
    conn = db.get_connection()
    try:
        lead = detector.BASELINE_WINDOW_SECONDS
        chat = StoredStream(
            channel=channel,
            now=(began - timedelta(seconds=lead)).timestamp(),
            conn=conn,
            broadcaster_user_id=broadcaster_user_id,
        )
        chat.chatter(lead / 2)
        chat.say("viewer1", "xd")
        chat.chatter(lead / 2 + FIRST)
        chat.burst(exact_crowd(), "xDDD")
        chat.chatter(BETWEEN)
        chat.burst(people(8, "second_wave"), "xDDD")
        chat.wait(3)
        chat.burst(people(8, "still_laughing"), "xDDD")
        chat.chatter(60)
    finally:
        conn.close()


def a_recording(**stored) -> int:
    """A recording of the channel that began at T0, with made-up bytes for
    its file: enough to look for moments in."""
    return add_stream_recording(CHANNEL, **{"seconds": SECONDS, **stored})


@pytest.fixture(scope="module")
def footage(tmp_path_factory) -> Path:
    """Five minutes of video with a frame a cut can begin on every two
    seconds."""
    require_ffmpeg()
    return make_video(tmp_path_factory.mktemp("footage") / "footage.mp4", seconds=SECONDS, keyframe_every=2.0)


def recording_of(footage: Path, **stored) -> tuple[int, Path]:
    """The same with real video for its file, and where that file is."""
    recording_id = a_recording(**stored)
    (row,) = [row for row in stream_recordings() if row["id"] == recording_id]
    file = stream_recording.STREAMS_DIR / row["file_path"]
    file.write_bytes(footage.read_bytes())
    return recording_id, file


def replayed(parts: list[stream_recording.Part] | None = None) -> list[recording_moments.Found]:
    """What the stored chat comes to when the same code is asked directly,
    under what is recommended."""
    parts = parts or [stream_recording.Part(T0, SECONDS)]
    conn = db.get_connection()
    try:
        messages, keywords = replay.load_between(conn, 1, *recording_moments.chat_span(parts))
    finally:
        conn.close()
    tuned = tuning_module.recommended()
    return recording_moments.find(replay.replay(messages, keywords, tuned), parts, tuned)[0]


async def look(service, recording_id: int):
    response = await service.client.post(f"/recordings/{recording_id}/moments")
    await service.settle()
    return response


async def make(service, recording_id: int, *keys: int):
    """Asks for clips of the moments with those keys - of all, with none."""
    body = {"moments": list(keys)} if keys else {}
    response = await service.client.post(f"/recordings/{recording_id}/moments/clips", json=body)
    await service.settle()
    return response


async def section(service, recording_id: int) -> Element:
    response = await service.client.get(f"/dashboard/recordings/{recording_id}/moments")
    assert response.status_code == 200, response.text
    return parse(response.text).one("section", data_rec_moments=True)


def words(part: Element) -> str:
    return part.one("p", class_="rec-moments-words").text


def rows(part: Element) -> list[Element]:
    return part.find("li", data_found=True)


def keys(part: Element) -> list[int]:
    return [int(row.attrs["data-found"]) for row in rows(part)]


def buttons(part: Element) -> list[str]:
    return [button.text for button in part.one("div", class_="ch-line").find("button")]


def clip_of(moment) -> Path:
    return recorder.CLIPS_DIR / moment["clip_path"]


@pytest.fixture
def held_replay(monkeypatch) -> Iterator[threading.Event]:
    """Holds every replay before its first message until the event is set."""
    gate = threading.Event()
    real = replay.replay

    def held(*args, **kwargs):
        assert gate.wait(20), "the replay was never let go"
        return real(*args, **kwargs)

    monkeypatch.setattr(replay, "replay", held)
    yield gate
    gate.set()


@pytest.fixture
def held_cut(monkeypatch) -> Iterator[threading.Event]:
    """Holds every cut out of a file until the event is set."""
    gate = threading.Event()
    real = recorder.trim_clip

    def held(*args, **kwargs):
        assert gate.wait(20), "the cut was never let go"
        return real(*args, **kwargs)

    monkeypatch.setattr(recorder, "trim_clip", held)
    yield gate
    gate.set()


class TestLookingForMoments:
    async def test_asking_replays_the_recordings_chat_and_lists_what_would_have_been_a_moment(self, service):
        stream_chat()
        recording_id = a_recording()

        response = await look(service, recording_id)

        assert response.status_code == 200
        assert response.json() == {"recording_id": recording_id, "state": "looking"}
        found = await section(service, recording_id)
        assert found.attrs["data-state"] == "found" and "data-busy" not in found.attrs
        assert keys(found) == [one.key for one in replayed()] and len(rows(found)) == 2
        assert words(found).startswith("2 moments in the recording's chat (")

    async def test_each_is_where_its_clip_begins_in_the_file(self, service):
        stream_chat()
        recording_id = a_recording()
        recommended = tuning_module.recommended()
        # The first crowd is just big enough, so the moment fires on its last
        # laugh and is over; its clip reaches back from there.
        fired = FIRST + (len(exact_crowd()) - 1) * 0.2
        begins = (
            fired - detector.SHORT_WINDOW_SECONDS - recommended.pre_roll_seconds - recommended.playback_delay_seconds
        )

        await look(service, recording_id)

        first, second = rows(await section(service, recording_id))
        go = first.one("button", data_found_go=True)
        assert float(go.attrs["data-found-go"]) == pytest.approx(begins, abs=0.1)
        assert go.text == view.stream_time_words(begins)
        assert float(second.one("button", data_found_go=True).attrs["data-found-go"]) > begins + BETWEEN - 5

    async def test_each_says_what_set_it_off_and_how_long_its_clip_is(self, service):
        stream_chat()
        recording_id = a_recording()

        await look(service, recording_id)

        shown = [row.one("span").text for row in rows(await section(service, recording_id))]
        assert shown == [
            view.found_words(",".join(one.spike.reasons), one.spike.score, one.clip_seconds) for one in replayed()
        ]
        assert all(said.startswith("Laughing, score ") for said in shown)

    async def test_each_carries_where_its_reaction_goes_on_the_trace(self, service):
        stream_chat()
        recording_id = a_recording()

        await look(service, recording_id)

        first = rows(await section(service, recording_id))[0]
        began, ended = float(first.attrs["data-began"]), float(first.attrs["data-ended"])
        # Placed as the trace places chat: where its writers were watching.
        delay = service.main.chat_trace.CHAT_DELAY_SECONDS
        assert began == pytest.approx(FIRST - detector.SHORT_WINDOW_SECONDS - delay, abs=1)
        assert ended == pytest.approx(FIRST - delay, abs=1)

    async def test_looking_stores_nothing(self, service):
        stream_chat()
        recording_id = a_recording()

        await look(service, recording_id)

        assert service.moments() == []
        assert not recorder.CLIPS_DIR.exists() or list(recorder.CLIPS_DIR.rglob("*.mp4")) == []

    async def test_none_of_it_is_a_moment_yet_so_each_can_be_made_one(self, service):
        stream_chat()
        recording_id = a_recording()

        await look(service, recording_id)

        found = await section(service, recording_id)
        assert [row.find("button")[-1].text for row in rows(found)] == ["Make a clip", "Make a clip"]
        assert found.find("a") == []
        assert buttons(found) == ["Make clips of all 2", "Look again"]

    async def test_a_recording_that_has_not_been_looked_through_says_what_looking_does(self, service):
        recording_id = a_recording()

        found = await section(service, recording_id)

        assert found.attrs["data-state"] == "none" and rows(found) == []
        assert words(found) == view.FIND_WORDS[view.FIND_NONE]
        assert buttons(found) == ["Find moments"]

    async def test_the_button_that_asks_shows_once_the_script_is_there(self, service):
        found = await section(service, a_recording())

        assert "hidden" in found.one("button", data_find_moments=True).attrs
        assert found.attrs["data-url"] == "/recordings/1/moments"
        assert found.attrs["data-page"] == "/dashboard/recordings/1/moments"
        assert found.attrs["data-trace"] == "/dashboard/recordings/1/trace"

    async def test_a_channel_that_was_never_tracked_is_looked_through_all_the_same(self, service):
        # Not on the watchlist, let alone tracked: its recording has its chat.
        stream_chat()
        recording_id = a_recording()

        await look(service, recording_id)

        assert len(rows(await section(service, recording_id))) == 2

    async def test_chat_is_replayed_under_the_settings_saved_for_the_channel(self, service):
        add_streamer(CHANNEL, 1)
        stream_chat()
        recording_id = a_recording()
        # Laughing no longer counts for this channel, and it goes by that.
        await service.client.post(f"/channels/{CHANNEL}/tuning", json={"values": {"laugh_signal": False}})
        await service.client.post(f"/channels/{CHANNEL}/own_tuning?enabled=1")

        await look(service, recording_id)

        found = await section(service, recording_id)
        assert rows(found) == []
        assert words(found).startswith("Nothing in the recording's chat (")

    async def test_settings_saved_afterwards_show_only_once_it_is_looked_through_again(self, service):
        add_streamer(CHANNEL, 1)
        stream_chat()
        recording_id = a_recording()
        await look(service, recording_id)

        await service.client.post("/tuning", json={"values": {"laugh_signal": False}})

        assert len(rows(await section(service, recording_id))) == 2
        await look(service, recording_id)
        assert rows(await section(service, recording_id)) == []

    async def test_only_the_channels_own_chat_is_replayed(self, service):
        stream_chat(channel="another_channel", broadcaster_user_id=2)
        recording_id = a_recording()

        await look(service, recording_id)

        assert words(await section(service, recording_id)) == view.find_words(view.FIND_FOUND, messages=0)

    async def test_a_recording_with_no_chat_says_so(self, service):
        recording_id = a_recording()

        await look(service, recording_id)

        found = await section(service, recording_id)
        assert "No chat was stored for this recording" in words(found)
        assert buttons(found) == ["Look again"]

    async def test_a_moment_from_a_break_in_the_recording_is_counted_and_not_listed(self, service):
        stream_chat()
        # Nothing was recorded from 20 s to 100 s of the stream, which is
        # when the first crowd laughed.
        parts = [(T0, 20.0), (T0 + timedelta(seconds=100), SECONDS - 100)]
        recording_id = a_recording(parts=parts)

        await look(service, recording_id)

        found = await section(service, recording_id)
        assert len(rows(found)) == 1
        assert words(found).endswith("1 more fell where the recording has too little footage of it.")

    async def test_a_moment_after_a_break_is_where_the_file_has_it(self, service):
        stream_chat()
        parts = [(T0, 20.0), (T0 + timedelta(seconds=100), SECONDS - 100)]
        recording_id = a_recording(parts=parts)

        await look(service, recording_id)

        (row,) = rows(await section(service, recording_id))
        (expected,) = replayed([stream_recording.Part(began, length) for began, length in parts])
        # Eighty seconds of the stream are not in the file.
        assert float(row.one("button", data_found_go=True).attrs["data-found-go"]) == pytest.approx(
            expected.clip[0], abs=0.1
        )
        assert expected.clip_started_at - T0 == timedelta(seconds=expected.clip[0] + 80)

    async def test_looking_again_puts_what_is_found_now_in_place_of_what_was(self, service):
        recording_id = a_recording()
        await look(service, recording_id)
        assert rows(await section(service, recording_id)) == []

        stream_chat()
        await look(service, recording_id)

        assert len(rows(await section(service, recording_id))) == 2

    async def test_each_recording_has_its_own(self, service):
        stream_chat()
        one = a_recording()
        other = a_recording(started_at=T0 + timedelta(hours=5))

        await look(service, one)

        assert len(rows(await section(service, one))) == 2
        assert (await section(service, other)).attrs["data-state"] == "none"


class TestWhileItIsBeingLookedThrough:
    async def test_the_page_says_how_far_the_replay_has_got(self, service, held_replay):
        stream_chat()
        recording_id = a_recording()
        await service.client.post(f"/recordings/{recording_id}/moments")
        finds = service.main._recording_finds
        await async_wait_until(lambda: finds[recording_id]["of"], "the chat to be read")

        found = await section(service, recording_id)

        assert found.attrs["data-state"] == "looking" and "data-busy" in found.attrs
        assert words(found).startswith("Replaying the recording's chat: 0 of ")
        assert all("disabled" in button.attrs for button in found.find("button"))

        held_replay.set()
        await service.settle()
        assert (await section(service, recording_id)).attrs["data-state"] == "found"

    async def test_asking_again_starts_nothing_more(self, service, held_replay, monkeypatch):
        stream_chat()
        recording_id = a_recording()
        loaded = []
        real = replay.load_between
        monkeypatch.setattr(replay, "load_between", lambda *args: loaded.append(1) or real(*args))

        for _ in range(3):
            response = await service.client.post(f"/recordings/{recording_id}/moments")
            assert response.json()["state"] == "looking"
        held_replay.set()
        await service.settle()

        assert loaded == [1]

    async def test_one_replay_at_a_time_whatever_it_is_for(self, service, held_replay):
        stream_chat()
        add_streamer(CHANNEL, 1)
        one = a_recording()
        other = a_recording(started_at=T0 + timedelta(hours=5))
        await service.client.post(f"/recordings/{one}/moments")

        refused = await service.client.post(f"/recordings/{other}/moments")
        trial = await service.client.post(f"/channels/{CHANNEL}/tuning/trial", json={"values": {}})

        assert refused.status_code == 409 and "still running" in refused.json()["detail"]
        assert trial.status_code == 409 and "still running" in trial.json()["detail"]
        assert (await section(service, other)).attrs["data-state"] == "none"

        held_replay.set()
        await service.settle()
        assert (await look(service, other)).status_code == 200

    async def test_a_replay_that_fails_is_said_in_words_and_can_be_tried_again(self, service, monkeypatch, caplog):
        stream_chat()
        recording_id = a_recording()
        real = replay.replay
        monkeypatch.setattr(replay, "replay", lambda *args: 1 / 0)

        with caplog.at_level(logging.ERROR, logger="kick_clip_hunter"):
            await look(service, recording_id)

        found = await section(service, recording_id)
        assert found.attrs["data-state"] == "failed" and "data-busy" not in found.attrs
        assert words(found) == view.FIND_WORDS[view.FIND_FAILED]
        assert buttons(found) == ["Look again"]
        assert any(
            f"looking for moments in stream recording {recording_id} failed" in r.getMessage() for r in caplog.records
        )

        monkeypatch.setattr(replay, "replay", real)
        await look(service, recording_id)
        assert len(rows(await section(service, recording_id))) == 2

    async def test_it_is_given_up_when_the_service_shuts_down(self, service, held_replay, monkeypatch):
        stream_chat()
        recording_id = a_recording()
        await service.client.post(f"/recordings/{recording_id}/moments")
        finds = service.main._recording_finds
        await async_wait_until(lambda: finds[recording_id]["of"], "the chat to be read")

        monkeypatch.setattr(service.main, "_shutdown_requested", True)
        held_replay.set()
        await service.settle()

        # Not minutes later, and with nothing half-found left behind.
        assert recording_id not in finds
        assert service.main._replay_running is False

    async def test_the_recording_is_not_deleted_meanwhile(self, service, held_replay):
        stream_chat()
        recording_id = a_recording()
        await service.client.post(f"/recordings/{recording_id}/moments")

        refused = await service.client.post(f"/recordings/{recording_id}/delete")

        assert refused.status_code == 409 and "Moments are being looked for" in refused.json()["detail"]
        held_replay.set()
        await service.settle()
        assert (await service.client.post(f"/recordings/{recording_id}/delete")).status_code == 200
        assert recording_id not in service.main._recording_finds


class TestWhatCanNotBeLookedThrough:
    async def test_an_unknown_recording_is_not_found(self, service):
        assert (await service.client.post("/recordings/999/moments")).status_code == 404
        assert (await service.client.post("/recordings/999/moments/clips", json={})).status_code == 404
        assert (await service.client.get("/dashboard/recordings/999/moments")).status_code == 404

    async def test_a_recording_without_a_file_has_nothing_to_look_in(self, service):
        failed = add_stream_recording(CHANNEL, state="failed")
        gone = add_stream_recording(CHANNEL, file=False, started_at=T0 + timedelta(hours=5))

        for recording_id in (failed, gone):
            response = await service.client.post(f"/recordings/{recording_id}/moments")

            assert response.status_code == 409, recording_id
            assert response.json()["detail"] == "The recording has no file to look for moments in."
            assert service.main._replay_running is False

    async def test_nor_has_one_that_is_not_over(self, service):
        service.watch(CHANNEL, 1)
        await service.client.post(f"/channels/{CHANNEL}/recording/start")

        assert (await service.client.post("/recordings/1/moments")).status_code == 409

    async def test_nothing_is_started_once_the_service_is_shutting_down(self, service, monkeypatch):
        stream_chat()
        recording_id = a_recording()
        await look(service, recording_id)
        monkeypatch.setattr(service.main, "_shutdown_requested", True)

        again = await service.client.post(f"/recordings/{recording_id}/moments")
        clips = await service.client.post(f"/recordings/{recording_id}/moments/clips", json={})

        assert again.status_code == 409 and clips.status_code == 409
        assert service.main._background_tasks == set() and service.moments() == []


@pytest.mark.ffmpeg
class TestMakingAMomentOfOne:
    async def test_it_is_stored_as_the_moment_the_service_would_have_detected(self, service, footage):
        stream_chat()
        recording_id, _file = recording_of(footage)
        await look(service, recording_id)
        first, _second = replayed()

        response = await make(service, recording_id, first.key)

        assert response.status_code == 200
        assert response.json() == {"recording_id": recording_id, "state": "cutting"}
        (moment,) = service.moments()
        # Under the name its chat is stored under, at the time it fired.
        assert (moment["channel_slug"], moment["broadcaster_user_id"]) == (CHAT_NAME, 1)
        assert datetime.fromisoformat(moment["detected_at"]) == first.fired_at
        assert datetime.fromisoformat(moment["window_start"]) == first.window_start
        assert datetime.fromisoformat(moment["window_end"]) == first.window_end
        assert (moment["reason"], moment["score"]) == (",".join(first.spike.reasons), first.spike.score)
        assert moment["message_count"] == first.spike.message_count
        assert moment["baseline_message_rate"] == first.spike.baseline_message_rate
        assert moment["current_message_rate"] == first.spike.current_message_rate
        assert (moment["emote_count"], moment["keyword_hits"]) == (first.spike.emote_count, first.spike.keyword_hits)
        assert moment["rating"] is None and moment["stream_elapsed_seconds"] is None
        # Everyone in the crowd laughed, and nobody else near it.
        assert moment["laughing"] == first.laughing == len(exact_crowd())

    async def test_its_file_is_cut_out_of_the_recording_clip_and_context_in_one(self, service, footage):
        stream_chat()
        recording_id, file = recording_of(footage)
        await look(service, recording_id)
        _first, second = replayed()

        await make(service, recording_id, second.key)

        (moment,) = service.moments()
        clip = clip_of(moment)
        assert clip == recorder.CLIPS_DIR / CHAT_NAME / f"moment_{moment['id']}.mp4"
        assert decodes_cleanly(clip) and probe(clip)["streams"] == ["audio", "video"]
        recommended = tuning_module.recommended()
        # The clip is the one that was found; the footage before it begins on
        # the last frame a cut can begin on, so up to two seconds early.
        assert moment["clip_duration"] == pytest.approx(second.clip_seconds, abs=0.01)
        assert datetime.fromisoformat(moment["clip_start"]) == second.clip_started_at
        assert 0 <= moment["context_before"] - recommended.context_before_seconds <= 2.0 + TOLERANCE
        assert moment["context_after"] == pytest.approx(recommended.context_after_seconds, abs=TOLERANCE)
        whole = moment["context_before"] + moment["clip_duration"] + moment["context_after"]
        assert probe(clip)["duration"] == pytest.approx(whole, abs=TOLERANCE)
        # The recording itself is what it was, and nothing else was left.
        assert file.read_bytes() == footage.read_bytes()
        assert [path.name for path in clip.parent.iterdir()] == [clip.name]

    async def test_context_is_only_as_much_as_the_recording_has(self, service, footage):
        stream_chat()
        recording_id, _file = recording_of(footage)
        await look(service, recording_id)
        first, _second = replayed()

        await make(service, recording_id, first.key)

        # Its clip begins less than half a minute into the recording.
        (moment,) = service.moments()
        assert first.clip[0] < tuning_module.recommended().context_before_seconds
        assert moment["context_before"] == pytest.approx(first.clip[0], abs=0.01)

    async def test_from_then_on_it_is_listed_as_the_moment_it_is(self, service, footage):
        stream_chat()
        recording_id, _file = recording_of(footage)
        await look(service, recording_id)
        first, _second = replayed()

        await make(service, recording_id, first.key)

        (moment,) = service.moments()
        found = await section(service, recording_id)
        made, left = rows(found)
        assert made.attrs["data-found-moment"] == str(moment["id"])
        assert made.one("a").attrs["href"] == view.review_url(view.SHOW_ALL, moment=moment["id"])
        assert [button.text for button in made.find("button")] == [made.one("button", data_found_go=True).text]
        assert "data-found-moment" not in left.attrs and left.find("button")[-1].text == "Make a clip"
        assert words(found).startswith("1 clip made, and in the review queue. 2 moments in")
        assert words(found).endswith("1 of them is a moment already.")
        # One left: no button for all of them.
        assert buttons(found) == ["Look again"]

    async def test_the_review_page_plays_it_with_its_chat_like_any_other(self, service, footage):
        stream_chat()
        recording_id, _file = recording_of(footage)
        await look(service, recording_id)
        first, _second = replayed()
        await make(service, recording_id, first.key)
        (moment,) = service.moments()

        response = await service.client.get(f"/dashboard/moments/{moment['id']}")

        assert response.status_code == 200
        page = parse(response.text)
        # The player opens on the clip, past the footage that led up to it.
        address, starts = page.one("video", data_clip=True).attrs["src"].split("#t=")
        assert address == f"/clips/{CHAT_NAME}/moment_{moment['id']}.mp4"
        assert float(starts) == pytest.approx(moment["context_before"], abs=0.01)
        assert (await service.client.get(address)).status_code == 200
        # The crowd's laughs are the lines of the moment.
        during = [line.one("b").text for line in page.find("p", class_="ch-chat") if "is-moment" in line.classes]
        assert set(exact_crowd()) <= set(during)
        queue = parse((await service.client.get("/dashboard")).text)
        assert [row.attrs["data-moment"] for row in queue.find("a", class_="ch-row")] == [str(moment["id"])]

    async def test_the_trace_under_the_recording_marks_it_as_a_moment(self, service, footage):
        stream_chat()
        recording_id, _file = recording_of(footage)
        await look(service, recording_id)
        await make(service, recording_id, replayed()[0].key)

        trace = parse((await service.client.get(f"/dashboard/recordings/{recording_id}/trace")).text)

        assert len(trace.find("div", class_="ch-moment")) == 1

    async def test_all_of_them_at_once(self, service, footage):
        stream_chat()
        recording_id, _file = recording_of(footage)
        await look(service, recording_id)

        response = await make(service, recording_id)

        assert response.status_code == 200
        moments = service.moments()
        assert [datetime.fromisoformat(moment["detected_at"]) for moment in moments] == [
            one.fired_at for one in replayed()
        ]
        assert all(clip_of(moment).exists() for moment in moments)
        found = await section(service, recording_id)
        assert words(found).startswith("2 clips made, and in the review queue.")
        assert words(found).endswith("All of them are moments already.")
        assert [row.one("a").text for row in rows(found)] == ["Open in review", "Open in review"]

    async def test_one_that_is_a_moment_already_is_not_made_again(self, service, footage):
        stream_chat()
        recording_id, _file = recording_of(footage)
        await look(service, recording_id)
        first, second = replayed()
        await make(service, recording_id, first.key)

        again = await make(service, recording_id, first.key)
        rest = await make(service, recording_id)
        nothing = await make(service, recording_id)

        assert again.status_code == 409 and nothing.status_code == 409
        assert again.json()["detail"] == "There is nothing here that is not a moment already."
        assert rest.status_code == 200
        assert [datetime.fromisoformat(moment["detected_at"]) for moment in service.moments()] == [
            first.fired_at,
            second.fired_at,
        ]

    async def test_a_moment_the_service_detected_while_the_stream_was_on_counts_as_one(self, service, footage):
        stream_chat()
        recording_id, _file = recording_of(footage)
        first, second = replayed()
        live = add_moment(CHAT_NAME, detected_at=first.fired_at + timedelta(seconds=1))
        await look(service, recording_id)

        found = await section(service, recording_id)
        await make(service, recording_id)

        assert rows(found)[0].attrs["data-found-moment"] == str(live)
        assert words(found).endswith("1 of them is a moment already.")
        made = [moment for moment in service.moments() if moment["id"] != live]
        assert [datetime.fromisoformat(moment["detected_at"]) for moment in made] == [second.fired_at]

    async def test_looking_again_still_knows_which_ones_are_moments(self, service, footage):
        stream_chat()
        recording_id, _file = recording_of(footage)
        await look(service, recording_id)
        await make(service, recording_id)

        await look(service, recording_id)

        found = await section(service, recording_id)
        assert [row.one("a").text for row in rows(found)] == ["Open in review", "Open in review"]
        assert (await make(service, recording_id)).status_code == 409 and len(service.moments()) == 2

    async def test_a_clip_is_framed_by_the_settings_its_chat_was_replayed_under(self, service, footage):
        add_streamer(CHANNEL, 1)
        stream_chat()
        recording_id, _file = recording_of(footage)
        await service.client.post("/tuning", json={"values": {"context_before_seconds": 5, "context_after_seconds": 0}})
        await look(service, recording_id)
        # Saved since: the next time it is looked through, not this one.
        await service.client.post("/tuning", json={"values": {"context_after_seconds": 40}})

        await make(service, recording_id, int(rows(await section(service, recording_id))[1].attrs["data-found"]))

        (moment,) = service.moments()
        assert 5 <= moment["context_before"] <= 7 + TOLERANCE
        assert moment["context_after"] == pytest.approx(0, abs=TOLERANCE)

    async def test_the_moments_stay_when_the_recording_is_deleted(self, service, footage):
        stream_chat()
        recording_id, _file = recording_of(footage)
        await look(service, recording_id)
        await make(service, recording_id)

        assert (await service.client.post(f"/recordings/{recording_id}/delete")).status_code == 200

        moments = service.moments()
        assert len(moments) == 2 and all(clip_of(moment).exists() for moment in moments)


@pytest.mark.ffmpeg
class TestWhileClipsAreBeingCut:
    async def test_the_page_says_which_one_and_nothing_more_can_be_asked_for(self, service, footage, held_cut):
        stream_chat()
        recording_id, _file = recording_of(footage)
        await look(service, recording_id)
        await service.client.post(f"/recordings/{recording_id}/moments/clips", json={})

        found = await section(service, recording_id)
        more = await service.client.post(f"/recordings/{recording_id}/moments/clips", json={})
        again = await service.client.post(f"/recordings/{recording_id}/moments")
        deleted = await service.client.post(f"/recordings/{recording_id}/delete")

        assert found.attrs["data-state"] == "cutting" and "data-busy" in found.attrs
        assert words(found) == "Cutting clip 1 of 2."
        assert all("disabled" in button.attrs for button in found.find("button") if "data-found-go" not in button.attrs)
        assert more.status_code == 409 and more.json()["detail"].startswith("Clips are still being cut.")
        # Looking again waits its turn too, and says where things stand.
        assert again.status_code == 200 and again.json()["state"] == "cutting"
        assert deleted.status_code == 409

        held_cut.set()
        await service.settle()
        assert len(service.moments()) == 2
        assert (await section(service, recording_id)).attrs["data-state"] == "found"

    async def test_a_cut_that_fails_leaves_no_moment_behind_and_is_said(self, service, footage, caplog):
        stream_chat()
        recording_id, file = recording_of(footage)
        await look(service, recording_id)
        file.write_bytes(b"not really video")

        with caplog.at_level(logging.ERROR, logger="kick_clip_hunter"):
            response = await make(service, recording_id)

        assert response.status_code == 200
        assert service.moments() == []
        assert list(recorder.CLIPS_DIR.rglob("*.mp4")) == []
        found = await section(service, recording_id)
        assert words(found).startswith("2 clips could not be cut. The server log says why.")
        assert len([r for r in caplog.records if "could not be made" in r.getMessage() and r.exc_info]) == 2

        # With the file back they can be asked for again.
        file.write_bytes(footage.read_bytes())
        await make(service, recording_id)
        assert len(service.moments()) == 2

    async def test_a_moment_whose_file_can_not_be_put_in_its_place_is_taken_back(
        self, service, footage, monkeypatch, caplog
    ):
        stream_chat()
        recording_id, _file = recording_of(footage)
        await look(service, recording_id)
        monkeypatch.setattr(service.main, "update_moment_clip_path", lambda *args, **kwargs: 1 / 0)

        with caplog.at_level(logging.ERROR, logger="kick_clip_hunter"):
            await make(service, recording_id)

        # All of a moment or none of it: no row without a file, no file
        # without a row.
        assert service.moments() == []
        assert list(recorder.CLIPS_DIR.rglob("*.mp4")) == []
        assert words(await section(service, recording_id)).startswith("2 clips could not be cut.")


@pytest.mark.ffmpeg
class TestAnalysisOfTheirClips:
    async def test_the_steps_that_are_switched_on_run_on_each_clip_alone(self, service, footage, monkeypatch):
        stream_chat()
        recording_id, _file = recording_of(footage)
        seen: list[float] = []

        def transcribe(clip: Path) -> str:
            seen.append(probe(clip)["duration"])
            return "what was said"

        monkeypatch.setattr(service.main.transcriber, "transcribe_clip", transcribe)
        await service.client.post("/settings/transcript?enabled=1")
        await look(service, recording_id)

        await make(service, recording_id)

        moments = service.moments()
        assert [moment["transcript"] for moment in moments] == ["what was said", "what was said"]
        # Given the clip without the footage around it.
        assert seen == [pytest.approx(moment["clip_duration"], abs=2.0 + TOLERANCE) for moment in moments]

    async def test_nothing_is_analysed_unless_switched_on(self, service, footage, monkeypatch):
        stream_chat()
        recording_id, _file = recording_of(footage)
        monkeypatch.setattr(service.main.transcriber, "transcribe_clip", lambda clip: 1 / 0)
        await look(service, recording_id)

        await make(service, recording_id)

        assert [moment["transcript"] for moment in service.moments()] == [None, None]


class TestWhatCanNotBeMade:
    async def test_nothing_before_the_recording_has_been_looked_through(self, service):
        recording_id = a_recording()

        response = await make(service, recording_id)

        assert response.status_code == 409
        assert response.json()["detail"] == "Look for moments in the recording first."

    async def test_a_moment_that_was_not_found_in_this_recording(self, service):
        stream_chat()
        recording_id = a_recording()
        await look(service, recording_id)

        response = await make(service, recording_id, 12345)

        assert response.status_code == 400
        assert response.json()["detail"] == "1 of the moments asked for were not found in this recording."
        assert service.main._background_tasks == set() and service.moments() == []

    @pytest.mark.parametrize(
        "body",
        [
            b'{"moments": "all"}',
            b'{"moments": [1.5]}',
            b'{"moments": [true]}',
            b'{"moments": ["1"]}',
            b"[1, 2]",
            b"moments=1",
        ],
        ids=["a-word", "a-fraction", "a-switch", "text", "a-bare-list", "a-form"],
    )
    async def test_a_request_that_does_not_say_which_in_a_way_that_can_be_read(self, service, body):
        stream_chat()
        recording_id = a_recording()
        await look(service, recording_id)

        response = await service.client.post(
            f"/recordings/{recording_id}/moments/clips", content=body, headers={"Content-Type": "application/json"}
        )

        assert response.status_code == 400 and service.moments() == []

    async def test_clips_that_would_fill_the_disk(self, service, monkeypatch):
        stream_chat()
        recording_id = a_recording()
        await look(service, recording_id)
        # Three gigabytes of recording, of which the two files are this share.
        share = sum(one.footage[1] - one.footage[0] for one in replayed()) / SECONDS
        room = recording_manager.MIN_FREE_BYTES + share * 3 * 1024**3
        monkeypatch.setattr(stream_recording, "free_bytes", lambda: int(room) - 1)

        refused = await make(service, recording_id)

        assert refused.status_code == 409 and "disk is too full" in refused.json()["detail"]
        assert service.moments() == []
        assert (await section(service, recording_id)).attrs["data-state"] == "found"

    async def test_nothing_from_a_recording_whose_file_is_gone_since(self, service):
        stream_chat()
        recording_id = a_recording()
        await look(service, recording_id)
        (row,) = stream_recordings()
        (stream_recording.STREAMS_DIR / row["file_path"]).unlink()

        response = await make(service, recording_id)

        assert response.status_code == 409
        assert response.json()["detail"] == "The recording has no file to cut clips from."
