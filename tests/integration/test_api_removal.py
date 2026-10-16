"""Deleting a moment, and taking a channel off the watchlist - with or
without what came of watching it.

A moment goes with every file it has next to its clip; a channel takes with
it what it had for being on the watchlist, stops being watched and has its
chat dropped from then on, and only with `everything` its moments, stream
recordings and chat as well. Nothing is removed while something is being
made of it.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from kick_clip_hunter import dashboard_view as view
from kick_clip_hunter import db, recorder, recording_manager, stream_recording, tuning
from tests.support.data import T0, add_chat, add_moment, add_stream_recording, stream_recordings
from tests.support.html import parse

pytestmark = pytest.mark.anyio

CHANNEL = "some_channel"
OTHER = "other_channel"


def moment_ids() -> list[int]:
    conn = db.get_connection()
    try:
        return [row[0] for row in conn.execute("SELECT id FROM moments ORDER BY id")]
    finally:
        conn.close()


def with_files(moment_id: int, channel: str = CHANNEL) -> list[Path]:
    """Puts every file a moment can have next to its clip in place, and
    returns them."""
    folder = recorder.CLIPS_DIR / channel
    folder.mkdir(parents=True, exist_ok=True)
    names = [f"moment_{moment_id}{tail}" for tail in (".mp4", "_trim.mp4", "_trim_chat.mov", "_trim.json")]
    names += [f"moment_{moment_id}_before.mp4", f"moment_{moment_id}_after.mp4"]
    files = [folder / name for name in names]
    for file in files:
        file.write_bytes(b"footage")
    return files


def clip_moment(channel: str = CHANNEL, broadcaster_user_id: int = 1, **columns) -> tuple[int, list[Path]]:
    moment_id = add_moment(channel, broadcaster_user_id=broadcaster_user_id, **columns)
    conn = db.get_connection()
    try:
        conn.execute("UPDATE moments SET clip_path = ? WHERE id = ?", (f"{channel}/moment_{moment_id}.mp4", moment_id))
        conn.commit()
    finally:
        conn.close()
    return moment_id, with_files(moment_id, channel)


class TestDeletingAMoment:
    async def test_it_goes_with_every_file_it_has(self, service):
        moment_id, files = clip_moment()
        kept, kept_files = clip_moment()

        response = await service.client.post(f"/moments/{moment_id}/delete")

        assert response.json() == {"deleted": moment_id}
        assert moment_ids() == [kept]
        assert not any(file.exists() for file in files)
        assert all(file.exists() for file in kept_files)

    async def test_a_moment_without_a_clip_is_simply_removed(self, service):
        moment_id = add_moment()

        assert (await service.client.post(f"/moments/{moment_id}/delete")).status_code == 200
        assert moment_ids() == []

    async def test_an_unknown_moment_is_a_404(self, service):
        assert (await service.client.post("/moments/999/delete")).status_code == 404

    async def test_not_while_a_stretch_of_it_is_being_cut_out(self, service):
        moment_id, files = clip_moment()
        service.main._trims[moment_id] = {"state": view.TRIM_CUTTING}

        response = await service.client.post(f"/moments/{moment_id}/delete")

        assert response.status_code == 409
        assert "being cut out" in response.json()["detail"]
        assert moment_ids() == [moment_id] and all(file.exists() for file in files)

    async def test_not_while_it_is_being_transcribed(self, service):
        moment_id, _files = clip_moment()
        service.main._transcripts[moment_id] = {"state": view.TRANSCRIPT_MAKING}

        response = await service.client.post(f"/moments/{moment_id}/delete")

        assert response.status_code == 409
        assert moment_ids() == [moment_id]

    async def test_a_file_that_can_not_be_removed_leaves_the_moment_where_it_is(self, service, monkeypatch):
        # On Windows, a clip somebody is still playing.
        moment_id, _files = clip_moment()
        real_unlink = Path.unlink

        def held_open(path, *args, **kwargs):
            if path.name == f"moment_{moment_id}.mp4":
                raise PermissionError(13, "The process cannot access the file because it is being used", str(path))
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", held_open)

        response = await service.client.post(f"/moments/{moment_id}/delete")

        assert response.status_code == 409
        assert "could not be removed" in response.json()["detail"]
        assert moment_ids() == [moment_id]

    async def test_nothing_outside_the_clips_folder_is_ever_removed(self, service):
        # Whatever a row says its clip is.
        outside = Path("data") / "kick_clip_hunter.db"
        moment_id = add_moment(clip_path="../kick_clip_hunter.db")
        assert outside.exists()

        assert (await service.client.post(f"/moments/{moment_id}/delete")).status_code == 200
        assert outside.exists()

    async def test_a_clip_cut_after_its_moment_was_removed_is_thrown_away(self, service, monkeypatch):
        moment_id = add_moment()
        clip = recorder.CLIPS_DIR / CHANNEL / f"moment_{moment_id}.mp4"

        async def create_clip(channel, start, end, name, **framing):
            # Removed while its footage was still being waited for.
            await service.client.post(f"/moments/{moment_id}/delete")
            clip.parent.mkdir(parents=True, exist_ok=True)
            clip.write_bytes(b"clip")
            return recorder.CutClip(clip, T0, T0 + timedelta(seconds=30), 0.0, 0.0)

        monkeypatch.setattr(service.main.recording_manager, "create_clip_for_moment", create_clip)

        await service.main._create_clip_background(moment_id, CHANNEL, T0, T0, tuning.recommended())
        await service.settle()

        assert moment_ids() == [] and not clip.exists()

    async def test_the_open_moment_offers_it_asked_for_twice(self, service):
        moment_id = add_moment()

        page = parse((await service.client.get(f"/dashboard/moments/{moment_id}")).text)

        control = page.one("span", data_delete=True)
        assert control.attrs["data-delete-then"] == "moment"
        confirm = control.one("span", data_delete_confirm=True)
        assert "hidden" in confirm.attrs
        assert confirm.one("button", data_delete_go=True).attrs["data-delete-go"] == f"/moments/{moment_id}/delete"


async def remove(service, slug: str = CHANNEL, everything: bool = False):
    return await service.client.post(f"/channels/{slug}/delete", params={"everything": int(everything)})


def streamer_ids() -> list[int]:
    conn = db.get_connection()
    try:
        return [row["broadcaster_user_id"] for row in db.get_streamers(conn)]
    finally:
        conn.close()


class TestRemovingAChannel:
    async def test_it_is_taken_off_the_watchlist_with_what_it_had_for_being_on_it(self, service):
        user_id = service.watch(CHANNEL, keywords={"kekw": 1.0})
        other = service.watch(OTHER, keywords={"kekw": 1.0})
        conn = db.get_connection()
        try:
            db.replace_channel_emotes(conn, user_id, {"KEKW": ("7tv-id", 32, 32)})
            db.replace_tuning(conn, user_id, {"post_roll_seconds": "12"})
        finally:
            conn.close()

        response = await remove(service)

        assert response.json() == {"slug": CHANNEL, "removed": None}
        assert streamer_ids() == [other]
        for table in ("channel_keywords", "channel_emotes", "tuning"):
            assert service.rows(f"SELECT * FROM {table} WHERE broadcaster_user_id = ?", user_id) == []
        assert len(service.rows("SELECT * FROM channel_keywords WHERE broadcaster_user_id = ?", other)) == 1

    async def test_what_came_of_watching_it_stays(self, service):
        user_id = service.watch(CHANNEL)
        moment_id, files = clip_moment(broadcaster_user_id=user_id)
        recording_id = add_stream_recording(CHANNEL, broadcaster_user_id=user_id)
        add_chat(CHANNEL, "alice", "hello", T0, broadcaster_user_id=user_id)

        await remove(service)

        assert moment_ids() == [moment_id] and all(file.exists() for file in files)
        assert [row["id"] for row in stream_recordings()] == [recording_id]
        assert service.chat_log() == [(CHANNEL, "alice", "hello")]

    async def test_kick_stops_sending_its_chat(self, service):
        user_id = service.watch(CHANNEL)
        other = service.watch(OTHER)

        await remove(service)

        assert service.api.subscribed == [other]
        assert user_id not in service.api.subscribed

    async def test_chat_kick_still_sends_of_it_is_dropped(self, service):
        service.watch(CHANNEL)
        await remove(service)

        response = await service.chat(CHANNEL, "alice", "hello")

        assert response.json() == {"status": "not on the watchlist"}
        assert service.chat_log() == []

    async def test_added_again_its_chat_is_taken_in_again(self, service):
        user_id = service.watch(CHANNEL)
        await remove(service)
        service.watch(CHANNEL, user_id)

        response = await service.chat(CHANNEL, "alice", "hello")

        assert response.json() == {"status": "received"}
        assert service.chat_log() == [(CHANNEL, "alice", "hello")]

    async def test_a_failure_to_unsubscribe_on_kick_does_not_keep_it_on_the_watchlist(self, service, caplog):
        service.watch(CHANNEL)
        service.api.failures["/public/v1/events/subscriptions"] = 500

        response = await remove(service)

        assert response.status_code == 200
        assert streamer_ids() == []
        assert "could not be removed" in caplog.text

    async def test_not_while_its_stream_is_being_recorded(self, service):
        service.watch(CHANNEL)
        await service.client.post(f"/channels/{CHANNEL}/recording/start")

        response = await remove(service)

        assert response.status_code == 409
        assert "being recorded" in response.json()["detail"]
        assert len(streamer_ids()) == 1
        recording_manager.end_stream_recording(CHANNEL)

    async def test_its_recorder_is_stopped_and_forgotten(self, service):
        service.watch(CHANNEL)
        stopped = []

        class Running:
            is_active = True

            def stop(self):
                stopped.append(True)

        recording_manager._recorders[CHANNEL] = Running()

        await remove(service)

        assert stopped == [True]
        assert CHANNEL not in recording_manager._recorders

    async def test_an_unknown_channel_is_a_404(self, service):
        assert (await remove(service, "nobody")).status_code == 404


class TestRemovingAChannelWithEverything:
    async def test_its_moments_recordings_and_chat_go_too(self, service):
        user_id = service.watch(CHANNEL)
        other = service.watch(OTHER)
        _moment, files = clip_moment(broadcaster_user_id=user_id)
        kept_moment, kept_files = clip_moment(OTHER, broadcaster_user_id=other)
        recording_id = add_stream_recording(CHANNEL, broadcaster_user_id=user_id)
        recording_file = stream_recording.STREAMS_DIR / stream_recordings()[0]["file_path"]
        kept_recording = add_stream_recording(OTHER, broadcaster_user_id=other)
        add_chat(CHANNEL, "alice", "hello", T0, broadcaster_user_id=user_id)
        add_chat(OTHER, "bob", "hi", T0, broadcaster_user_id=other)
        await service.chat(CHANNEL, "carol", "hey", identity={"username_color": "#00ff00", "badges": []})
        await service.client.post(f"/channels/{CHANNEL}/chat_delay", params={"seconds": 5})

        response = await remove(service, everything=True)

        assert response.json() == {"slug": CHANNEL, "removed": {"moments": 1, "recordings": 1, "messages": 2}}
        assert moment_ids() == [kept_moment]
        assert not any(file.exists() for file in files) and all(file.exists() for file in kept_files)
        assert [row["id"] for row in stream_recordings()] == [kept_recording]
        assert recording_id != kept_recording and not recording_file.exists()
        assert service.chat_log() == [(OTHER, "bob", "hi")]
        assert service.rows("SELECT * FROM chat_identities WHERE broadcaster_user_id = ?", user_id) == []
        assert service.rows("SELECT * FROM app_settings WHERE key = ?", f"chat_delay:{CHANNEL}") == []

    async def test_a_lot_of_chat_is_removed_a_chunk_at_a_time(self, service, monkeypatch):
        user_id = service.watch(CHANNEL)
        monkeypatch.setattr(db, "REMOVE_CHUNK", 2)
        for number in range(5):
            add_chat(CHANNEL, "alice", f"message {number}", T0, broadcaster_user_id=user_id)

        response = await remove(service, everything=True)

        assert response.json()["removed"]["messages"] == 5
        assert service.chat_log() == []

    async def test_nothing_goes_while_something_is_being_made_of_one_of_its_moments(self, service):
        user_id = service.watch(CHANNEL)
        moment_id, files = clip_moment(broadcaster_user_id=user_id)
        add_chat(CHANNEL, "alice", "hello", T0, broadcaster_user_id=user_id)
        service.main._trims[moment_id] = {"state": view.TRIM_CUTTING}

        response = await remove(service, everything=True)

        assert response.status_code == 409
        assert f"moment {moment_id}" in response.json()["detail"]
        assert streamer_ids() == [user_id] and moment_ids() == [moment_id]
        assert all(file.exists() for file in files) and len(service.chat_log()) == 1

    async def test_or_of_one_of_its_recordings(self, service):
        user_id = service.watch(CHANNEL)
        recording_id = add_stream_recording(CHANNEL, broadcaster_user_id=user_id)
        service.main._recording_trims[recording_id] = {"state": view.TRIM_CUTTING}

        response = await remove(service, everything=True)

        assert response.status_code == 409
        assert f"recording {recording_id}" in response.json()["detail"]
        assert len(stream_recordings()) == 1


class TestTheChannelsPage:
    async def test_each_channel_offers_to_be_removed_saying_what_could_go_with_it(self, service):
        user_id = service.watch(CHANNEL)
        service.watch(OTHER)
        clip_moment(broadcaster_user_id=user_id)
        clip_moment(broadcaster_user_id=user_id)
        add_stream_recording(CHANNEL, broadcaster_user_id=user_id)

        page = parse((await service.client.get("/dashboard/channels")).text)

        buttons = page.find("button", data_remove_channel=True)
        assert [(b.attrs["data-remove-channel"], b.attrs["data-kept"]) for b in buttons] == [
            (CHANNEL, "2 moments and 1 recording"),
            (OTHER, "0 moments and 0 recordings"),
        ]
        dialog = page.one("dialog", data_remove_channel_dialog=True)
        assert dialog.one("input", data_remove_everything=True).attrs["type"] == "checkbox"
        assert "checked" not in dialog.one("input", data_remove_everything=True).attrs
