"""Recording a channel's whole stream, from the button to the file: starting
and stopping one, what happens to chat meanwhile, what is done when one ends
without being stopped, a channel that is recorded always, and getting rid
of one.

The service is the real one. What stands in for the stream is footage handed
straight to a running recording's writer, as its channel's recorder would
hand it (that hand-over itself is in test_recorder_recording.py, the loop
that keeps the recorder going in test_recording_manager.py). Files are made
by real ffmpeg from real, tiny MPEG-TS segments.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kick_clip_hunter import dashboard_view as view
from kick_clip_hunter import detector, recording_manager, stream_recording
from tests.support.data import add_stream_recording, add_streamer, stream_recordings
from tests.support.media import decodes_cleanly, probe
from tests.support.waiting import async_wait_until

pytestmark = pytest.mark.anyio

CHANNEL = "some_channel"
# When the footage a test hands over was on air.
BEGAN = datetime(2026, 3, 1, 20, 0, 0, tzinfo=timezone.utc)


class RunningRecorder:
    """Stands in for a channel's recorder while the stream is coming in."""

    is_active = True
    whole_stream = None

    def stop(self) -> None:
        self.is_active = False

    def state(self) -> None:
        return None  # nothing the Health page could say of it


def broadcast(channel: str, segments: list[Path], *, began: datetime = BEGAN, group: str = "7-0") -> None:
    """Hands the recording of a channel real footage, a second a segment, as
    the channel's recorder would while the stream is live."""
    kept = recording_manager.stream_recording_of(channel)
    for index, path in enumerate(segments):
        kept.writer.add(began + timedelta(seconds=index), 1.0, group, path.read_bytes())


async def start(service, channel: str = CHANNEL):
    return await service.client.post(f"/channels/{channel}/recording/start")


async def stop(service, channel: str = CHANNEL):
    return await service.client.post(f"/channels/{channel}/recording/stop")


def the_recording() -> dict:
    (row,) = stream_recordings()
    return row


def file_of(row: dict) -> Path:
    return stream_recording.STREAMS_DIR / row["file_path"]


class TestStarting:
    async def test_starting_one_stores_it_and_begins_keeping_the_stream(self, service):
        service.watch(CHANNEL, broadcaster_user_id=41)
        before = datetime.now(timezone.utc)

        response = await start(service)

        assert response.status_code == 200
        assert response.json() == {
            "slug": CHANNEL,
            "recording": {"id": 1, "running": True, "words": "Waiting for the stream", "channel": CHANNEL},
        }
        row = the_recording()
        assert (row["channel_slug"], row["broadcaster_user_id"], row["state"]) == (CHANNEL, 41, "recording")
        assert before <= datetime.fromisoformat(row["requested_at"]) <= datetime.now(timezone.utc)
        kept = recording_manager.stream_recording_of(CHANNEL)
        assert (kept.recording_id, kept.broadcaster_user_id) == (1, 41)
        # Its channel's recorder hands what it downloads on to it.
        assert recording_manager._recorders[CHANNEL].whole_stream is kept.writer

    async def test_tracking_has_no_say_in_it(self, service):
        add_streamer(CHANNEL, 1, tracking=False)
        await service.client.post("/settings/watching?enabled=0")

        response = await start(service)

        assert response.status_code == 200
        assert recording_manager.stream_recording_of(CHANNEL) is not None

    async def test_a_channel_whose_stream_is_coming_in_already_is_starting(self, service, monkeypatch):
        service.watch(CHANNEL)
        monkeypatch.setitem(recording_manager._recorders, CHANNEL, RunningRecorder())

        response = await start(service)

        assert response.json()["recording"]["words"] == "Starting"

    async def test_a_channel_that_is_not_on_the_watchlist_is_a_404(self, service):
        response = await start(service, "nobody")

        assert response.status_code == 404
        assert stream_recordings() == []

    async def test_a_channel_has_one_recording_at_a_time(self, service):
        service.watch(CHANNEL)
        await start(service)

        again = await start(service)

        assert again.status_code == 409
        assert again.json()["detail"] == f"{CHANNEL} is being recorded already."
        assert len(stream_recordings()) == 1

    async def test_several_channels_can_be_recorded_at_once(self, service):
        service.watch("channel_a")
        service.watch("channel_b")

        await start(service, "channel_a")
        await start(service, "channel_b")

        assert sorted(recording_manager.stream_recordings()) == ["channel_a", "channel_b"]
        assert [row["channel_slug"] for row in stream_recordings()] == ["channel_a", "channel_b"]

    async def test_none_is_started_while_the_service_is_shutting_down(self, service, monkeypatch):
        service.watch(CHANNEL)
        monkeypatch.setattr(service.main, "_shutdown_requested", True)

        response = await start(service)

        assert response.status_code == 409
        assert stream_recordings() == []

    async def test_none_is_started_onto_a_disk_that_is_nearly_full(self, service, monkeypatch):
        service.watch(CHANNEL)
        room = recording_manager.MIN_FREE_BYTES - 1024**3
        monkeypatch.setattr(stream_recording, "free_bytes", lambda: room)

        response = await start(service)

        assert response.status_code == 409
        assert response.json()["detail"] == (
            "The disk is too full to start a recording: 4 GB would be left once what is being recorded is saved, "
            "and it takes 5 GB."
        )
        assert stream_recordings() == []
        assert recording_manager.stream_recording_of(CHANNEL) is None

    async def test_with_nowhere_to_keep_it_there_is_no_recording(self, service, monkeypatch, caplog):
        service.watch(CHANNEL)

        def no_folder(*_args):
            raise PermissionError(13, "Access is denied")

        monkeypatch.setattr(recording_manager, "start_stream_recording", no_folder)

        with caplog.at_level(logging.ERROR, logger="kick_clip_hunter"):
            response = await start(service)

        assert response.status_code == 500
        assert "Could not make a place to keep the recording" in response.json()["detail"]
        assert stream_recordings() == []
        assert any("could not be started" in record.getMessage() and record.exc_info for record in caplog.records)


class TestStopping:
    async def test_one_that_kept_nothing_is_forgotten(self, service):
        service.watch(CHANNEL)
        await start(service)
        folder = recording_manager.stream_recording_of(CHANNEL).writer.folder

        response = await stop(service)

        assert response.json() == {"slug": CHANNEL, "recording": None, "said": "Nothing had been recorded yet."}
        assert stream_recordings() == []
        assert recording_manager.stream_recording_of(CHANNEL) is None
        assert not folder.exists()

    @pytest.mark.ffmpeg
    async def test_what_was_kept_becomes_a_file(self, service, ts_segments):
        service.watch(CHANNEL)
        await start(service)
        broadcast(CHANNEL, ts_segments[:6])
        folder = recording_manager.stream_recording_of(CHANNEL).writer.folder

        response = await stop(service)

        assert response.json() == {
            "slug": CHANNEL,
            "recording": {"id": 1, "running": False, "words": "Saving 0:00:06", "channel": CHANNEL},
            "said": "Saving the recording. It will be on the Recordings page.",
        }
        assert (the_recording()["state"], the_recording()["ended_by"]) == ("saving", "stopped")

        await service.settle()
        row = the_recording()
        assert row["state"] == "ready"
        file = file_of(row)
        assert file.name == stream_recording.file_name(CHANNEL, 1, BEGAN.astimezone())
        assert file.parent == stream_recording.STREAMS_DIR / CHANNEL
        assert probe(file) == {"duration": pytest.approx(6, abs=0.3), "streams": ["audio", "video"]}
        assert decodes_cleanly(file)
        # Stored with what it holds: that is what places chat against it.
        assert row["duration"] == pytest.approx(6, abs=0.3)
        assert row["size_bytes"] == file.stat().st_size
        assert datetime.fromisoformat(row["started_at"]) == BEGAN
        assert datetime.fromisoformat(row["ended_at"]) == pytest.approx(
            BEGAN + timedelta(seconds=6), abs=timedelta(seconds=0.3)
        )
        assert stream_recording.decoded(row["parts"]) == [stream_recording.Part(BEGAN, pytest.approx(6, abs=0.3))]
        # And the footage it was made from is gone.
        assert not folder.exists()
        assert [path.name for path in file.parent.iterdir()] == [file.name]

    @pytest.mark.ffmpeg
    async def test_a_stream_that_broke_off_is_stored_with_its_parts(self, service, ts_segments):
        service.watch(CHANNEL)
        await start(service)
        broadcast(CHANNEL, ts_segments[:5])
        broadcast(CHANNEL, ts_segments[20:28], began=BEGAN + timedelta(seconds=60), group="8-0")

        await stop(service)
        await service.settle()

        row = the_recording()
        parts = stream_recording.decoded(row["parts"])
        assert [part.started_at for part in parts] == [BEGAN, BEGAN + timedelta(seconds=60)]
        assert row["duration"] == pytest.approx(13, abs=0.4)
        assert probe(file_of(row))["duration"] == pytest.approx(13, abs=0.4)
        assert stream_recording.missing_seconds(parts) == pytest.approx(55, abs=0.4)

    async def test_stopping_what_is_not_being_recorded_is_refused(self, service):
        service.watch(CHANNEL)

        response = await stop(service)

        assert response.status_code == 409
        assert response.json()["detail"] == f"{CHANNEL} is not being recorded."

    async def test_stopping_a_channel_that_is_not_on_the_watchlist_is_a_404(self, service):
        assert (await stop(service, "nobody")).status_code == 404

    @pytest.mark.ffmpeg
    async def test_the_next_recording_can_start_while_the_last_is_being_saved(self, service, ts_segments, monkeypatch):
        gate = threading.Event()
        real_save = stream_recording.save

        def save_when_let(folder, output):
            gate.wait(timeout=10)
            return real_save(folder, output)

        monkeypatch.setattr(stream_recording, "save", save_when_let)
        service.watch(CHANNEL)
        await start(service)
        broadcast(CHANNEL, ts_segments[:4])
        await stop(service)

        again = await start(service)

        assert again.status_code == 200
        # The channel is said to be recording; both are there by recording.
        status = (await service.client.get("/recordings/status")).json()
        assert status["channels"][CHANNEL] == {
            "id": 2,
            "running": True,
            "words": "Waiting for the stream",
            "channel": CHANNEL,
        }
        assert sorted(status["recordings"]) == ["1", "2"]
        gate.set()
        await service.settle()
        assert [row["state"] for row in stream_recordings()] == ["ready", "recording"]


class TestStatus:
    async def test_with_no_recording_there_is_nothing_to_say(self, service):
        assert (await service.client.get("/recordings/status")).json() == {
            "channels": {},
            "recordings": {},
            "mark": "0:",
            "now": {"live": False, "words": "Not live", "count": "No channels watched", "channels": {}},
        }

    @pytest.mark.ffmpeg
    async def test_follows_a_recording_from_its_start_to_its_file(self, service, ts_segments, monkeypatch):
        async def status() -> dict:
            answer = (await service.client.get("/recordings/status")).json()
            del answer["now"]  # see test_says_what_the_channels_are_doing_right_now
            return answer

        service.watch(CHANNEL)
        await start(service)
        waiting = {"id": 1, "running": True, "words": "Waiting for the stream", "channel": CHANNEL}
        assert await status() == {"channels": {CHANNEL: waiting}, "recordings": {"1": waiting}, "mark": "1:1r"}

        monkeypatch.setitem(recording_manager._recorders, CHANNEL, RunningRecorder())
        broadcast(CHANNEL, ts_segments[:6])
        size = view.size_words(recording_manager.stream_recording_of(CHANNEL).writer.size)
        assert (await status())["channels"][CHANNEL]["words"] == f"0:00:06 recorded, {size}"

        # The stream drops: what there is stays, and the recording waits.
        recording_manager._recorders[CHANNEL].stop()
        assert (await status())["channels"][CHANNEL]["words"] == "0:00:06 recorded, the stream is off"

        await stop(service)
        saving = {"id": 1, "running": False, "words": "Saving 0:00:06", "channel": CHANNEL}
        assert await status() == {"channels": {CHANNEL: saving}, "recordings": {"1": saving}, "mark": "1:1s"}

        await service.settle()
        assert await status() == {"channels": {}, "recordings": {}, "mark": "1:"}

    async def test_says_what_the_channels_are_doing_right_now(self, service, monkeypatch):
        # What the bar and the watchlist's lamps are kept current with while
        # a recording runs: the same words the pages were made with.
        async def now() -> dict:
            return (await service.client.get("/recordings/status")).json()["now"]

        service.watch(CHANNEL)
        add_streamer("paused_channel", 2, tracking=False)
        assert await now() == {
            "live": False,
            "words": "Not live",
            "count": "1 channel watched",
            "channels": {
                CHANNEL: {"live": False, "words": "Not live"},
                "paused_channel": {"live": False, "words": "Not watched"},
            },
        }

        monkeypatch.setitem(recording_manager._recorders, CHANNEL, RunningRecorder())
        assert (await now())["words"] == "Live"

        await start(service)
        await start(service, "paused_channel")
        assert await now() == {
            "live": True,
            "words": "Recording",
            "count": "1 of 2 channels",
            "channels": {
                CHANNEL: {"live": True, "words": "Recording"},
                "paused_channel": {"live": False, "words": "Not live"},
            },
        }

    async def test_the_mark_follows_the_list_of_recordings(self, service):
        # What the Recordings page compares to know its list is out of date.
        async def mark() -> str:
            return (await service.client.get("/recordings/status")).json()["mark"]

        first = add_stream_recording()
        assert await mark() == "1:"

        add_stream_recording()
        assert await mark() == "2:"

        await service.client.post(f"/recordings/{first}/delete")
        assert await mark() == "1:"


class TestChatWhileRecording:
    """The switches are about looking for clips. A channel whose stream is
    being kept whole has its chat kept with it whatever they say."""

    async def test_chat_is_kept_though_the_channel_is_not_tracked(self, service):
        service.watch(CHANNEL)
        await service.client.post(f"/channels/{CHANNEL}/tracking?enabled=0")
        await start(service)

        response = await service.chat(CHANNEL, "alice", "hello", identity={"username_color": "#00ff00", "badges": []})

        assert response.json() == {"status": "stored"}
        assert service.chat_log() == [(CHANNEL, "alice", "hello")]
        # Who wrote it is kept too: the Recordings page draws chat with it.
        assert service.rows("SELECT username, colour FROM chat_identities")[0]["colour"] == "#00ff00"

    async def test_and_though_watching_is_off_altogether(self, service):
        service.watch(CHANNEL)
        service.watch("other_channel")
        await service.client.post("/settings/watching?enabled=0")
        await start(service)

        kept = await service.chat(CHANNEL, "alice", "hello")
        dropped = await service.chat("other_channel", "bob", "hello")

        assert kept.json() == {"status": "stored"}
        assert dropped.json() == {"status": "watching disabled"}
        assert service.chat_log() == [(CHANNEL, "alice", "hello")]

    async def test_nobody_looks_for_moments_in_chat_that_is_only_kept(self, service):
        service.watch(CHANNEL)
        service.warm_up(CHANNEL)
        entries = len(detector._entries[CHANNEL])
        await service.client.post(f"/channels/{CHANNEL}/tracking?enabled=0")
        await start(service)

        await service.crowd_laughs(CHANNEL, people=20)

        assert service.moments() == []
        assert len(detector._entries[CHANNEL]) == entries
        assert len(service.chat_log()) == 20

    async def test_a_tracked_channel_is_searched_for_moments_as_ever(self, service):
        service.watch(CHANNEL)
        service.warm_up(CHANNEL)
        await start(service)

        await service.crowd_laughs(CHANNEL, people=20)

        (moment,) = service.moments()
        assert moment["channel_slug"] == CHANNEL
        await service.settle()

    async def test_a_message_delivered_twice_is_kept_once(self, service):
        service.watch(CHANNEL)
        await service.client.post(f"/channels/{CHANNEL}/tracking?enabled=0")
        await start(service)
        payload = {
            "message_id": "the-same-message",
            "broadcaster": {"user_id": service.user_id(CHANNEL), "channel_slug": CHANNEL},
            "sender": {"username": "alice"},
            "content": "hello",
            "emotes": [],
        }

        first = await service.deliver(payload)
        second = await service.deliver(payload)

        assert (first.json(), second.json()) == ({"status": "stored"}, {"status": "duplicate"})
        assert service.chat_log() == [(CHANNEL, "alice", "hello")]

    async def test_once_the_recording_is_over_the_switches_decide_again(self, service):
        service.watch(CHANNEL)
        await service.client.post(f"/channels/{CHANNEL}/tracking?enabled=0")
        await start(service)
        await service.chat(CHANNEL, "alice", "kept")
        await stop(service)

        response = await service.chat(CHANNEL, "alice", "dropped")

        assert response.json() == {"status": "channel tracking disabled"}
        assert service.chat_log() == [(CHANNEL, "alice", "kept")]

    async def test_a_recording_of_one_channel_keeps_no_other_channels_chat(self, service):
        service.watch(CHANNEL)
        service.watch("other_channel")
        await service.client.post("/channels/other_channel/tracking?enabled=0")
        await start(service)

        response = await service.chat("other_channel", "bob", "hello")

        assert response.json() == {"status": "channel tracking disabled"}
        assert service.chat_log() == []


@pytest.mark.ffmpeg
class TestEndingByItself:
    """What the recording loop reports (see test_recording_manager.py) ends
    the recording like a stop does, and is remembered as the reason."""

    async def test_a_stream_that_is_over_ends_its_recording(self, service, ts_segments):
        service.watch(CHANNEL)
        await start(service)
        broadcast(CHANNEL, ts_segments[:5])

        await service.main._stream_recording_over(CHANNEL, stream_recording.STREAM_ENDED)
        await service.settle()

        row = the_recording()
        assert (row["state"], row["ended_by"]) == ("ready", "stream_ended")
        assert file_of(row).exists()
        assert recording_manager.stream_recording_of(CHANNEL) is None

    async def test_being_told_twice_does_no_harm(self, service, ts_segments):
        service.watch(CHANNEL)
        await start(service)
        broadcast(CHANNEL, ts_segments[:5])

        await service.main._stream_recording_over(CHANNEL, stream_recording.STREAM_ENDED)
        await service.main._stream_recording_over(CHANNEL, stream_recording.DISK_FULL)
        await service.settle()

        assert [(row["state"], row["ended_by"]) for row in stream_recordings()] == [("ready", "stream_ended")]

    async def test_a_disk_running_out_is_said_out_loud(self, service, ts_segments, caplog):
        service.watch(CHANNEL)
        await start(service)
        broadcast(CHANNEL, ts_segments[:5])

        with caplog.at_level(logging.WARNING, logger="kick_clip_hunter"):
            await service.main._stream_recording_over(CHANNEL, stream_recording.DISK_FULL)
        await service.settle()

        assert the_recording()["ended_by"] == "disk_full"
        assert any("ended (disk_full)" in record.getMessage() for record in caplog.records)

    async def test_room_stays_spoken_for_until_the_file_is_made(self, service, ts_segments, monkeypatch):
        gate = threading.Event()
        real_save = stream_recording.save

        def save_when_let(folder, output):
            gate.wait(timeout=10)
            return real_save(folder, output)

        free = 2 * recording_manager.MIN_FREE_BYTES
        monkeypatch.setattr(stream_recording, "save", save_when_let)
        monkeypatch.setattr(stream_recording, "free_bytes", lambda: free)
        service.watch(CHANNEL)
        await start(service)
        broadcast(CHANNEL, ts_segments[:5])
        kept = recording_manager.stream_recording_of(CHANNEL).writer.size

        await stop(service)
        assert recording_manager.room_left() == free - kept

        gate.set()
        await service.settle()
        assert recording_manager.room_left() == free


class TestAChannelRecordedAlways:
    """With the switch on, a recording is started whenever the channel is
    live and none is running. The loop is what sees that (see
    test_recording_manager.py); what it calls is the service's."""

    async def always(self, service, enabled: bool = True, channel: str = CHANNEL):
        return await service.client.post(f"/channels/{channel}/always_record?enabled={int(enabled)}")

    def is_set(self, service, channel: str = CHANNEL) -> bool:
        return channel in service.main._always_recorded_slugs()

    async def test_the_switch_is_kept_with_the_channel(self, service):
        service.watch(CHANNEL)
        service.watch("another_channel")
        assert service.main._always_recorded_slugs() == []

        response = await self.always(service)

        assert response.status_code == 200
        assert response.json() == {
            "slug": CHANNEL,
            "always_record": True,
            "said": view.ALWAYS_RECORD_WORDS[True].format(channel=CHANNEL),
        }
        assert service.main._always_recorded_slugs() == [CHANNEL]

        response = await self.always(service, False)

        assert response.json()["always_record"] is False
        assert response.json()["said"] == view.ALWAYS_RECORD_WORDS[False].format(channel=CHANNEL)
        assert service.main._always_recorded_slugs() == []

    async def test_a_channel_that_is_not_on_the_watchlist_is_a_404(self, service):
        assert (await self.always(service, channel="nobody")).status_code == 404

    async def test_switching_it_on_has_the_loop_look_at_once(self, service, monkeypatch):
        # Not at its next round: the channel may be live right now.
        service.watch(CHANNEL)
        woken = []
        monkeypatch.setattr(recording_manager, "wake", lambda: woken.append(True))

        await self.always(service)

        assert woken == [True]

    async def test_switching_it_on_starts_nothing_by_itself(self, service):
        # That is the loop's to do, once it has seen the channel live.
        service.watch(CHANNEL)

        await self.always(service)

        assert stream_recordings() == []

    async def test_a_live_channel_gets_a_recording_without_anyone_starting_it(self, service, caplog):
        service.watch(CHANNEL, broadcaster_user_id=41)
        await self.always(service)

        with caplog.at_level(logging.INFO, logger="kick_clip_hunter"):
            await service.main._record_stream_by_itself(CHANNEL)

        row = the_recording()
        assert (row["channel_slug"], row["broadcaster_user_id"], row["state"]) == (CHANNEL, 41, "recording")
        kept = recording_manager.stream_recording_of(CHANNEL)
        assert (kept.recording_id, kept.broadcaster_user_id) == (row["id"], 41)
        assert any("started by itself" in record.getMessage() for record in caplog.records)

    async def test_it_is_a_recording_like_any_other(self, service, ts_segments):
        service.watch(CHANNEL)
        await self.always(service)
        await service.main._record_stream_by_itself(CHANNEL)
        broadcast(CHANNEL, ts_segments[:5])

        response = await stop(service)
        await service.settle()

        assert response.status_code == 200
        row = the_recording()
        assert (row["state"], row["ended_by"]) == ("ready", "stopped")
        assert file_of(row).exists()

    async def test_tracking_has_no_say_in_it(self, service):
        add_streamer(CHANNEL, 1, tracking=False)
        await service.client.post("/settings/watching?enabled=0")
        await self.always(service)

        await service.main._record_stream_by_itself(CHANNEL)

        assert recording_manager.stream_recording_of(CHANNEL) is not None

    async def test_a_channel_whose_switch_is_off_gets_none(self, service):
        # Switched off between the loop looking and it telling.
        service.watch(CHANNEL)

        await service.main._record_stream_by_itself(CHANNEL)
        await service.main._record_stream_by_itself("nobody")

        assert stream_recordings() == []

    async def test_none_is_started_while_the_service_is_shutting_down(self, service, monkeypatch):
        service.watch(CHANNEL)
        await self.always(service)
        monkeypatch.setattr(service.main, "_shutdown_requested", True)

        await service.main._record_stream_by_itself(CHANNEL)

        assert stream_recordings() == []
        # Nor is the loop given any channel to look at for it.
        assert service.main._always_recorded_slugs() == []

    async def test_stopping_one_ends_it_for_that_stream_and_says_so(self, service, ts_segments):
        service.watch(CHANNEL)
        await self.always(service)
        await service.main._record_stream_by_itself(CHANNEL)
        broadcast(CHANNEL, ts_segments[:5])

        response = await stop(service)
        await service.settle()

        assert response.json()["said"] == view.recording_stopped_words(True, True)
        # The loop leaves the channel alone until this stream is over...
        assert recording_manager.is_held_off(CHANNEL)
        # ...and the switch is as it was.
        assert self.is_set(service)

    async def test_stopping_a_recording_of_any_other_channel_says_nothing_of_the_next(self, service):
        service.watch(CHANNEL)
        await start(service)

        response = await stop(service)

        assert response.json()["said"] == view.recording_stopped_words(False, False)

    async def test_a_recording_that_ends_with_its_stream_holds_nothing_off(self, service, ts_segments):
        # The next stream is the next recording.
        service.watch(CHANNEL)
        await self.always(service)
        await service.main._record_stream_by_itself(CHANNEL)
        broadcast(CHANNEL, ts_segments[:5])

        await service.main._stream_recording_over(CHANNEL, stream_recording.STREAM_ENDED)
        await service.settle()

        assert not recording_manager.is_held_off(CHANNEL)

    async def test_one_ended_because_the_disk_ran_out_is_not_started_again_in_that_stream(self, service, ts_segments):
        service.watch(CHANNEL)
        await self.always(service)
        await service.main._record_stream_by_itself(CHANNEL)
        broadcast(CHANNEL, ts_segments[:5])

        await service.main._stream_recording_over(CHANNEL, stream_recording.DISK_FULL)
        await service.settle()

        assert recording_manager.is_held_off(CHANNEL)

    async def test_setting_the_switch_afresh_holds_for_the_stream_that_is_on(self, service):
        service.watch(CHANNEL)
        await self.always(service)
        await start(service)
        await stop(service)
        assert recording_manager.is_held_off(CHANNEL)

        await self.always(service, False)
        await self.always(service)

        assert not recording_manager.is_held_off(CHANNEL)

    async def test_a_disk_that_is_too_full_is_said_once_and_not_tried_every_round(self, service, monkeypatch, caplog):
        service.watch(CHANNEL)
        await self.always(service)
        monkeypatch.setattr(stream_recording, "free_bytes", lambda: recording_manager.MIN_FREE_BYTES - 1024**3)

        with caplog.at_level(logging.WARNING, logger="kick_clip_hunter"):
            await service.main._record_stream_by_itself(CHANNEL)

        assert stream_recordings() == []
        (said,) = [record.getMessage() for record in caplog.records if "recorded always" in record.getMessage()]
        assert "this stream is not being recorded: The disk is too full to start a recording" in said
        # Which is what keeps the loop from asking again before the next stream.
        assert recording_manager.is_held_off(CHANNEL)

    async def test_the_pages_count_a_channel_that_is_waited_for_as_looked_at(self, service):
        async def now() -> dict:
            return (await service.client.get("/recordings/status")).json()["now"]

        add_streamer(CHANNEL, 1, tracking=False)
        assert (await now())["count"] == "No channels watched"
        assert (await now())["channels"][CHANNEL]["words"] == "Not watched"

        await self.always(service)

        assert (await now())["count"] == "1 channel watched"
        assert (await now())["channels"][CHANNEL]["words"] == "Not live"

        # Not while its recording was stopped and its stream is still on.
        recording_manager.hold_off(CHANNEL)

        assert (await now())["channels"][CHANNEL]["words"] == "Not watched"

    async def test_the_loop_is_told_which_channels_and_whom_to_tell(self, service, monkeypatch):
        # As the service starts it (see test_startup.py for the rest).
        given = []

        async def recording_loop(*arguments):
            given.extend(arguments)
            await asyncio.Event().wait()

        monkeypatch.setattr(recording_manager, "run_forever", recording_loop)

        async with service.main.lifespan(service.main.app):
            await async_wait_until(lambda: given, "the loop to start")

        assert given[4:] == [
            service.main._always_recorded_slugs,
            service.main._record_stream_by_itself,
            # ...and whom to tell that a channel could not be looked at, which
            # may have renamed itself (see test_channel_rename.py).
            service.main._look_failed,
        ]


class TestShuttingDown:
    @pytest.mark.ffmpeg
    async def test_a_running_recording_is_ended_and_saved_before_the_process_exits(
        self, service, ts_segments, monkeypatch
    ):
        service.watch(CHANNEL)
        await start(service)
        broadcast(CHANNEL, ts_segments[:5])
        states_at_exit: list[str] = []
        monkeypatch.setattr(service.main.os, "_exit", lambda code: states_at_exit.append(the_recording()["state"]))

        response = await service.client.post("/shutdown")

        assert response.json() == {"status": "shutting down", "pending_work_count": 1}
        assert recording_manager.stream_recording_of(CHANNEL) is None
        await async_wait_until(lambda: states_at_exit, "the process to exit")
        assert states_at_exit == ["ready"]
        row = the_recording()
        assert row["ended_by"] == "shutdown" and file_of(row).exists()

    async def test_one_that_kept_nothing_does_not_hold_the_shutdown_up(self, service, process_exits):
        service.watch(CHANNEL)
        await start(service)

        response = await service.client.post("/shutdown")

        assert response.json() == {"status": "shutting down", "pending_work_count": 0}
        await async_wait_until(lambda: process_exits, "the process to exit")
        assert stream_recordings() == []


@pytest.mark.ffmpeg
class TestAfterARestart:
    """Footage is safe in a recording's folder as it arrives, so a recording
    the service was stopped under loses nothing it had. What is done about
    it on the next start depends on how long ago that was."""

    A_MOMENT = timedelta(seconds=5)
    HOURS = timedelta(hours=3)

    def left_behind(
        self, ts_segments, *, written_ago: timedelta = A_MOMENT, state: str = "recording", **columns
    ) -> int:
        """A recording as a service that stopped left it: its row, and what
        it had kept in its folder, last added to that long ago."""
        recording_id = add_stream_recording(CHANNEL, state=state, started_at=BEGAN, **columns)
        folder = stream_recording.parts_folder(CHANNEL, recording_id)
        folder.mkdir(parents=True)
        part = folder / stream_recording.part_name(BEGAN)
        part.write_bytes(b"".join(path.read_bytes() for path in ts_segments))
        written = (datetime.now(timezone.utc) - written_ago).timestamp()
        os.utime(part, (written, written))
        return recording_id

    async def test_a_recording_that_was_running_a_moment_ago_goes_on(self, service, ts_segments, caplog):
        add_streamer(CHANNEL, 1)
        self.left_behind(ts_segments[:7])

        with caplog.at_level(logging.WARNING, logger="kick_clip_hunter"):
            service.main._take_up_stream_recordings()

        # Its channel is recorded again, into the same recording, which
        # counts what it had.
        kept = recording_manager.stream_recording_of(CHANNEL)
        assert kept.recording_id == 1
        assert kept.writer.seconds == pytest.approx(7, abs=0.3)
        assert kept.writer.size == sum(path.stat().st_size for path in ts_segments[:7])
        assert the_recording()["state"] == "recording"
        status = (await service.client.get("/recordings/status")).json()
        assert status["channels"][CHANNEL]["words"] == "0:00:07 recorded, the stream is off"
        assert any("goes on after the restart" in record.getMessage() for record in caplog.records)

        # What is broadcast from now on is added to it; the stretch the
        # service was away for is a break like any other.
        broadcast(CHANNEL, ts_segments[20:25], began=BEGAN + timedelta(seconds=60), group="9-0")
        assert (await stop(service)).status_code == 200
        await service.settle()

        row = the_recording()
        assert (row["state"], row["ended_by"]) == ("ready", "stopped")
        parts = stream_recording.decoded(row["parts"])
        assert [part.started_at for part in parts] == [BEGAN, BEGAN + timedelta(seconds=60)]
        assert probe(file_of(row))["duration"] == pytest.approx(12, abs=0.4)

    async def test_what_it_already_has_is_not_kept_a_second_time(self, service, ts_segments):
        # The recorder starts a few segments back from the live edge - which,
        # after a quick restart, the recording has from before.
        self.left_behind(ts_segments[:7])
        service.main._take_up_stream_recordings()

        broadcast(CHANNEL, ts_segments[4:10], began=BEGAN + timedelta(seconds=4), group="9-0")

        kept = recording_manager.stream_recording_of(CHANNEL)
        assert kept.writer.seconds == pytest.approx(10, abs=0.3)

    async def test_from_then_on_a_stream_that_stays_away_ends_it_as_ever(self, service, ts_segments):
        # The time it has been without footage is counted from the restart,
        # not from never: it must not wait for the next stream instead.
        self.left_behind(ts_segments[:7])

        service.main._take_up_stream_recordings()

        assert 0 <= recording_manager.stream_recording_of(CHANNEL).writer.idle_seconds() < 5

    async def test_footage_that_can_not_be_measured_is_never_thrown_away_as_nothing(self, service):
        # A recording that goes on counts what it had by asking ffprobe. If
        # that fails, stopping it must not take it for one that kept nothing.
        add_streamer(CHANNEL, 1)
        recording_id = add_stream_recording(CHANNEL, state="recording", started_at=BEGAN)
        folder = stream_recording.parts_folder(CHANNEL, recording_id)
        folder.mkdir(parents=True)
        (folder / stream_recording.part_name(BEGAN)).write_bytes(b"footage no one can measure" * 50)
        service.main._take_up_stream_recordings()
        assert recording_manager.stream_recording_of(CHANNEL).writer.seconds == 0

        response = await stop(service)
        await service.settle()

        assert response.json()["said"] == "Saving the recording. It will be on the Recordings page."
        assert the_recording()["state"] == "failed"
        assert [path.name for path in folder.iterdir()] == [stream_recording.part_name(BEGAN)]

    async def test_so_does_one_that_was_still_waiting_for_its_stream_to_begin(self, service):
        add_stream_recording(CHANNEL, state="recording", started_at=BEGAN - timedelta(days=2))

        service.main._take_up_stream_recordings()

        kept = recording_manager.stream_recording_of(CHANNEL)
        assert kept.recording_id == 1 and kept.writer.seconds == 0
        # It was started ahead of its stream, and waits for as long as that takes.
        assert kept.writer.idle_seconds() is None
        status = (await service.client.get("/recordings/status")).json()
        assert status["channels"][CHANNEL]["words"] == "Waiting for the stream"

    async def test_one_whose_footage_is_old_ends_where_it_was_cut_short(self, service, ts_segments, caplog):
        # The stream it was started for is long over. Going on would put
        # the next one into the same file.
        self.left_behind(ts_segments[:7], written_ago=self.HOURS)

        with caplog.at_level(logging.WARNING, logger="kick_clip_hunter"):
            service.main._take_up_stream_recordings()
        assert recording_manager.stream_recording_of(CHANNEL) is None
        assert the_recording()["state"] == "saving"
        await service.settle()

        row = the_recording()
        assert (row["state"], row["ended_by"]) == ("ready", "interrupted")
        assert probe(file_of(row))["duration"] == pytest.approx(7, abs=0.3)
        assert not stream_recording.parts_folder(CHANNEL, row["id"]).exists()
        assert any("was not finished when the service stopped" in record.getMessage() for record in caplog.records)

    @pytest.mark.parametrize(("ago", "goes_on"), [(-60, True), (60, False)], ids=["just-inside", "just-outside"])
    async def test_old_is_what_a_stream_may_be_gone_for_before_its_recording_ends(
        self, service, ts_segments, ago, goes_on
    ):
        grace = timedelta(seconds=recording_manager.STREAM_END_GRACE_SECONDS)
        self.left_behind(ts_segments[:4], written_ago=grace + timedelta(seconds=ago))

        service.main._take_up_stream_recordings()

        assert (recording_manager.stream_recording_of(CHANNEL) is not None) is goes_on
        await service.settle()

    async def test_one_that_was_being_saved_is_saved_and_keeps_why_it_ended(self, service, ts_segments):
        self.left_behind(ts_segments[:4], state="saving", ended_by="stream_ended")

        service.main._take_up_stream_recordings()
        assert recording_manager.stream_recording_of(CHANNEL) is None
        await service.settle()

        row = the_recording()
        assert (row["state"], row["ended_by"]) == ("ready", "stream_ended")

    async def test_one_that_was_being_saved_and_has_nothing_left_to_save_is_forgotten(self, service, caplog):
        # Its folder was removed by hand while the service was down.
        add_stream_recording(CHANNEL, state="saving", ended_by="stopped")

        with caplog.at_level(logging.INFO, logger="kick_clip_hunter"):
            service.main._take_up_stream_recordings()
            await service.settle()

        assert stream_recordings() == []
        assert any("kept no footage" in record.getMessage() for record in caplog.records)

    async def test_it_is_said_to_be_saving_while_that_runs(self, service, ts_segments):
        self.left_behind(ts_segments[:4], written_ago=self.HOURS)

        service.main._take_up_stream_recordings()

        status = (await service.client.get("/recordings/status")).json()
        assert status["channels"][CHANNEL] == {"id": 1, "running": False, "words": "Saving", "channel": CHANNEL}
        await service.settle()

    async def test_recordings_that_are_over_are_left_as_they_are(self, service):
        add_stream_recording(CHANNEL)
        add_stream_recording(CHANNEL, state="failed")
        before = stream_recordings()

        service.main._take_up_stream_recordings()
        await service.settle()

        assert stream_recordings() == before
        assert recording_manager.stream_recordings() == {}

    async def test_it_happens_when_the_service_starts(self, service, ts_segments, monkeypatch):
        looked_at: list[str] = []

        async def recording_loop(get_slugs, *_rest):
            # What the loop finds to do when it starts.
            looked_at.extend(recording_manager.stream_recordings())
            await asyncio.Event().wait()

        monkeypatch.setattr(recording_manager, "run_forever", recording_loop)
        self.left_behind(ts_segments[:4], written_ago=self.HOURS)
        add_stream_recording("another_channel", state="recording")

        async with service.main.lifespan(service.main.app):
            await service.settle()
            await async_wait_until(lambda: looked_at, "the recording loop to start")

        assert [(row["channel_slug"], row["state"]) for row in stream_recordings()] == [
            (CHANNEL, "ready"),
            ("another_channel", "recording"),
        ]
        assert looked_at == ["another_channel"]


class TestAFileThatCouldNotBeMade:
    def broken(self) -> int:
        """A recording, left behind long ago by a service that stopped, whose
        footage no video can be made of."""
        recording_id = add_stream_recording(CHANNEL, state="recording", started_at=BEGAN)
        folder = stream_recording.parts_folder(CHANNEL, recording_id)
        folder.mkdir(parents=True)
        part = folder / stream_recording.part_name(BEGAN)
        part.write_bytes(b"not a video at all" * 100)
        os.utime(part, (BEGAN.timestamp(), BEGAN.timestamp()))
        return recording_id

    @pytest.mark.ffmpeg
    async def test_it_is_marked_as_failed_and_keeps_what_it_recorded(self, service, caplog):
        recording_id = self.broken()
        folder = stream_recording.parts_folder(CHANNEL, recording_id)

        with caplog.at_level(logging.ERROR, logger="kick_clip_hunter"):
            service.main._take_up_stream_recordings()
            await service.settle()

        assert the_recording()["state"] == "failed"
        assert the_recording()["file_path"] is None
        assert [path.name for path in folder.iterdir()] == [stream_recording.part_name(BEGAN)]
        assert any("could not be saved" in record.getMessage() and record.exc_info for record in caplog.records)
        # Nothing is being done with it any more.
        assert (await service.client.get("/recordings/status")).json()["recordings"] == {}
        assert recording_manager.room_left() == stream_recording.free_bytes()

    @pytest.mark.ffmpeg
    async def test_saving_can_be_tried_again(self, service, ts_segments):
        recording_id = self.broken()
        service.main._take_up_stream_recordings()
        await service.settle()
        # Whatever was wrong has been put right.
        part = stream_recording.parts_folder(CHANNEL, recording_id) / stream_recording.part_name(BEGAN)
        part.write_bytes(b"".join(path.read_bytes() for path in ts_segments[:5]))

        response = await service.client.post(f"/recordings/{recording_id}/save")

        assert response.json() == {"recording": recording_id, "state": "saving"}
        assert the_recording()["state"] == "saving"
        await service.settle()
        row = the_recording()
        assert row["state"] == "ready"
        assert probe(file_of(row))["duration"] == pytest.approx(5, abs=0.3)

    async def test_only_a_recording_that_could_not_be_saved_can_be_saved_again(self, service):
        recording_id = add_stream_recording(CHANNEL)

        response = await service.client.post(f"/recordings/{recording_id}/save")

        assert response.status_code == 409
        assert the_recording()["state"] == "ready"

    async def test_not_one_that_is_still_running(self, service):
        service.watch(CHANNEL)
        await start(service)

        assert (await service.client.post("/recordings/1/save")).status_code == 409

    async def test_nor_an_unknown_one_nor_during_a_shutdown(self, service, monkeypatch):
        recording_id = add_stream_recording(CHANNEL, state="failed")

        assert (await service.client.post("/recordings/999/save")).status_code == 404

        monkeypatch.setattr(service.main, "_shutdown_requested", True)
        assert (await service.client.post(f"/recordings/{recording_id}/save")).status_code == 409
        assert the_recording()["state"] == "failed"


class TestDeleting:
    async def test_a_recording_is_removed_with_its_file(self, service):
        kept = add_stream_recording("channel_a")
        recording_id = add_stream_recording(CHANNEL)
        file = file_of(stream_recordings()[1])
        assert file.exists()

        response = await service.client.post(f"/recordings/{recording_id}/delete")

        assert response.json() == {"deleted": recording_id}
        assert not file.exists()
        assert [row["id"] for row in stream_recordings()] == [kept]
        assert file_of(stream_recordings()[0]).exists()

    async def test_one_that_could_not_be_saved_is_removed_with_what_it_recorded(self, service):
        recording_id = add_stream_recording(CHANNEL, state="failed")
        folder = stream_recording.parts_folder(CHANNEL, recording_id)
        folder.mkdir(parents=True)
        (folder / stream_recording.part_name(BEGAN)).write_bytes(b"footage")

        response = await service.client.post(f"/recordings/{recording_id}/delete")

        assert response.status_code == 200
        assert not folder.exists()
        assert stream_recordings() == []

    async def test_one_whose_file_is_gone_already_is_simply_taken_off_the_list(self, service):
        recording_id = add_stream_recording(CHANNEL, file=False)

        response = await service.client.post(f"/recordings/{recording_id}/delete")

        assert response.status_code == 200
        assert stream_recordings() == []

    async def test_a_recording_that_is_running_is_not_removed(self, service):
        service.watch(CHANNEL)
        await start(service)

        response = await service.client.post("/recordings/1/delete")

        assert response.status_code == 409
        assert response.json()["detail"] == "This recording is not over yet."
        assert recording_manager.stream_recording_of(CHANNEL) is not None
        assert len(stream_recordings()) == 1

    async def test_an_unknown_recording_is_a_404(self, service):
        assert (await service.client.post("/recordings/999/delete")).status_code == 404

    async def test_a_file_that_can_not_be_removed_leaves_the_recording_where_it_is(self, service, monkeypatch, caplog):
        # On Windows, a file somebody is still playing.
        recording_id = add_stream_recording(CHANNEL)
        real_unlink = Path.unlink

        def held_open(path, *args, **kwargs):
            if path.suffix == ".mp4":
                raise PermissionError(13, "The process cannot access the file because it is being used", str(path))
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", held_open)

        response = await service.client.post(f"/recordings/{recording_id}/delete")

        assert response.status_code == 409
        assert "could not be removed" in response.json()["detail"]
        assert len(stream_recordings()) == 1 and file_of(the_recording()).exists()

    async def test_nothing_outside_the_recordings_folder_is_ever_removed(self, service):
        # Whatever a row says its file is.
        outside = Path("data") / "kick_clip_hunter.db"
        recording_id = add_stream_recording(CHANNEL, file=False, file_path="../kick_clip_hunter.db")
        assert outside.exists()

        response = await service.client.post(f"/recordings/{recording_id}/delete")

        assert response.status_code == 200
        assert outside.exists()

    async def test_the_folder_of_the_channel_stays_for_its_other_recordings(self, service):
        first = add_stream_recording(CHANNEL)
        add_stream_recording(CHANNEL, started_at=BEGAN + timedelta(hours=3))

        await service.client.post(f"/recordings/{first}/delete")

        (left,) = stream_recordings()
        assert file_of(left).exists()
