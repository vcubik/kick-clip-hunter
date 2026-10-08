"""The buttons on the dashboard: switches, review actions on a moment,
watchlist management and shutdown - each one an HTTP endpoint."""

from __future__ import annotations

import asyncio
import logging

import pytest

from kick_clip_hunter import db, detector
from kick_clip_hunter.db import MOMENT_TYPES, STREAM_TYPES
from tests.support.data import add_moment, add_streamer, moment
from tests.support.waiting import async_wait_until

pytestmark = pytest.mark.anyio


def stored_flag(key: str, default: bool) -> bool:
    conn = db.get_connection()
    try:
        return db.get_flag(conn, key, default=default)
    finally:
        conn.close()


async def test_health_check(service):
    response = await service.client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


class TestSwitches:
    SETTINGS = ("watching", "transcript", "audio_events", "frames", "sound_events")

    @pytest.mark.parametrize("name", SETTINGS)
    async def test_each_switch_can_be_turned_on_and_off(self, service, name):
        key = service.main.SETTING_KEYS[name]

        on = await service.client.post(f"/settings/{name}?enabled=1")
        assert on.json() == {"setting": name, "enabled": True}
        assert service.main._flags[key] is True and stored_flag(key, default=False) is True

        off = await service.client.post(f"/settings/{name}?enabled=0")
        assert off.json() == {"setting": name, "enabled": False}
        assert service.main._flags[key] is False and stored_flag(key, default=True) is False

    async def test_a_switch_without_a_value_means_on(self, service):
        response = await service.client.post("/settings/transcript")

        assert response.json() == {"setting": "transcript", "enabled": True}

    async def test_an_unknown_switch_is_a_404(self, service):
        response = await service.client.post("/settings/self_destruct?enabled=1")

        assert response.status_code == 404
        assert "self_destruct" in response.json()["detail"]

    async def test_switches_are_independent(self, service):
        await service.client.post("/settings/frames?enabled=1")

        assert service.main._flags == {"frames_enabled": True}

    async def test_positions_survive_a_restart(self, service):
        await service.client.post("/settings/watching?enabled=0")
        await service.client.post("/settings/audio_events?enabled=1")

        service.main._flags.clear()  # what a fresh process starts with
        service.main._load_flags()

        assert service.main._flags == {
            "watching_enabled": False,
            "transcript_enabled": False,
            "audio_events_enabled": True,
            "frames_enabled": False,
            "sound_events_enabled": False,
        }

    async def test_a_fresh_installation_watches_but_analyses_nothing(self, service):
        service.main._load_flags()

        assert service.main._flags == service.main.SETTING_DEFAULTS
        assert service.main.SETTING_DEFAULTS["watching_enabled"] is True
        assert not any(value for key, value in service.main.SETTING_DEFAULTS.items() if key != "watching_enabled")

    async def test_every_switch_has_a_default_and_every_analysis_switch_a_label(self, service):
        main = service.main

        assert set(main.SETTING_DEFAULTS) == set(main.SETTING_KEYS.values())
        assert {name for name, _label in main.ANALYSIS_SETTINGS} == set(main.SETTING_KEYS) - {"watching"}


class TestRating:
    @pytest.mark.parametrize("value", [1, 2, 3, 4, 5])
    async def test_a_rating_from_one_to_five_is_stored_as_a_number(self, service, value):
        moment_id = add_moment()

        response = await service.client.post(f"/moments/{moment_id}/rating?value={value}")

        assert response.json() == {"moment_id": moment_id, "rating": value}
        assert moment(moment_id)["rating"] == value  # an int, not "3": the dashboard compares numbers

    async def test_zero_clears_the_rating(self, service):
        moment_id = add_moment(rating=4)

        response = await service.client.post(f"/moments/{moment_id}/rating?value=0")

        assert response.json() == {"moment_id": moment_id, "rating": None}
        assert moment(moment_id)["rating"] is None

    @pytest.mark.parametrize("value", [-1, 6, 100])
    async def test_a_rating_outside_the_scale_is_refused(self, service, value):
        moment_id = add_moment(rating=2)

        response = await service.client.post(f"/moments/{moment_id}/rating?value={value}")

        assert response.status_code == 400
        assert moment(moment_id)["rating"] == 2

    async def test_a_rating_that_is_not_a_number_is_refused(self, service):
        moment_id = add_moment()

        response = await service.client.post(f"/moments/{moment_id}/rating?value=great")

        assert response.status_code == 422

    async def test_rating_one_moment_leaves_the_others_alone(self, service):
        first, second = add_moment(rating=1), add_moment(rating=5)

        await service.client.post(f"/moments/{first}/rating?value=3")

        assert (moment(first)["rating"], moment(second)["rating"]) == (3, 5)


class TestTags:
    @pytest.mark.parametrize("value", [value for value, _label in STREAM_TYPES])
    async def test_every_stream_type_can_be_set(self, service, value):
        moment_id = add_moment()

        response = await service.client.post(f"/moments/{moment_id}/stream_type?value={value}")

        assert response.json() == {"moment_id": moment_id, "stream_type": value}
        assert moment(moment_id)["stream_type"] == value

    @pytest.mark.parametrize("value", [value for value, _label in MOMENT_TYPES])
    async def test_every_moment_type_can_be_set(self, service, value):
        moment_id = add_moment()

        response = await service.client.post(f"/moments/{moment_id}/moment_type?value={value}")

        assert response.json() == {"moment_id": moment_id, "moment_type": value}
        assert moment(moment_id)["moment_type"] == value

    @pytest.mark.parametrize("field", ["stream_type", "moment_type"])
    async def test_an_empty_value_clears_the_tag(self, service, field):
        moment_id = add_moment(stream_type="irl", moment_type="funny")

        response = await service.client.post(f"/moments/{moment_id}/{field}?value=")

        assert response.json() == {"moment_id": moment_id, field: None}
        assert moment(moment_id)[field] is None

    @pytest.mark.parametrize("field", ["stream_type", "moment_type"])
    async def test_a_value_outside_the_vocabulary_is_refused(self, service, field):
        moment_id = add_moment(stream_type="irl", moment_type="funny")

        response = await service.client.post(f"/moments/{moment_id}/{field}?value=made_up")

        assert response.status_code == 400
        assert "must be one of" in response.json()["detail"]
        assert moment(moment_id)[field] in {"irl", "funny"}

    async def test_the_two_tags_do_not_share_a_vocabulary(self, service):
        moment_id = add_moment()

        response = await service.client.post(f"/moments/{moment_id}/stream_type?value=funny")

        assert response.status_code == 400


class TestNotes:
    async def test_a_note_is_stored_trimmed(self, service):
        moment_id = add_moment()

        response = await service.client.post(f"/moments/{moment_id}/notes", json={"notes": "  cut starts too late \n"})

        assert response.json() == {"moment_id": moment_id, "notes": "cut starts too late"}
        assert moment(moment_id)["notes"] == "cut starts too late"

    @pytest.mark.parametrize("body", [{"notes": ""}, {"notes": "   \n\t"}, {"notes": None}, {}])
    async def test_an_empty_note_clears_it(self, service, body):
        moment_id = add_moment(notes="old note")

        response = await service.client.post(f"/moments/{moment_id}/notes", json=body)

        assert response.json() == {"moment_id": moment_id, "notes": None}
        assert moment(moment_id)["notes"] is None

    async def test_a_note_can_hold_anything_typed(self, service):
        moment_id = add_moment()
        text = 'false positive - "papa" is an emote here; 9 msgs </textarea> ěščřžýáíé 🤡'

        await service.client.post(f"/moments/{moment_id}/notes", json={"notes": text})

        assert moment(moment_id)["notes"] == text


class TestMomentStatus:
    async def test_counts_moments_and_how_many_have_a_clip(self, service):
        assert (await service.client.get("/moments/status")).json() == {"moments": 0, "clips": 0}

        add_moment()
        add_moment(clip_path="some_channel/moment_2.mp4")

        assert (await service.client.get("/moments/status")).json() == {"moments": 2, "clips": 1}

    async def test_is_not_mistaken_for_a_moment_id(self, service):
        # /moments/status sits next to /moments/{id}/...
        response = await service.client.get("/moments/status")

        assert response.status_code == 200


class TestAddingAChannel:
    async def test_adds_it_everywhere_it_has_to_exist(self, service):
        service.api.add_channel("new_channel", 555, seventv_emotes=["KEKW", "Sadge", "catJAM", "xd"])

        response = await service.client.post("/channels?slug=new_channel")

        assert response.status_code == 200
        assert response.json() == {
            "slug": "new_channel",
            "broadcaster_user_id": 555,
            "emote_count": 4,
            "laugh_emote_count": 1,
        }
        # Subscribed to its chat on Kick's side...
        assert service.api.subscribed == [555]
        # ...on the local watchlist...
        (streamer,) = service.rows("SELECT slug, broadcaster_user_id, tracking_enabled FROM streamers")
        assert tuple(streamer) == ("new_channel", 555, 1)
        # ...and its usable 7TV emote names stored as keywords (the dance
        # emote and the too-short name are dropped).
        conn = db.get_connection()
        try:
            keywords = db.get_channel_keywords(conn, 555)
        finally:
            conn.close()
        assert keywords == {
            "kekw": detector.EMOTE_MENTION_LAUGH_WEIGHT,
            "sadge": detector.EMOTE_MENTION_OTHER_WEIGHT,
        }

    async def test_surrounding_whitespace_in_the_slug_is_ignored(self, service):
        service.api.add_channel("new_channel", 555)

        response = await service.client.post("/channels", params={"slug": "  new_channel  "})

        assert response.status_code == 200
        assert response.json()["slug"] == "new_channel"

    async def test_a_channel_without_a_7tv_account_is_added_with_no_keywords(self, service):
        service.api.add_channel("plain_channel", 556, seventv_emotes=None)

        response = await service.client.post("/channels?slug=plain_channel")

        assert response.status_code == 200
        assert response.json()["emote_count"] == 0

    @pytest.mark.parametrize("slug", ["", "   "])
    async def test_an_empty_slug_is_refused_without_asking_kick(self, service, slug):
        response = await service.client.post("/channels", params={"slug": slug})

        assert response.status_code == 400
        assert service.api.requests == []

    @pytest.mark.parametrize("unknown_slug_status", [400, None], ids=["kick-answers-400", "kick-answers-empty"])
    async def test_a_channel_kick_does_not_know_is_a_404(self, service, unknown_slug_status):
        service.api.unknown_slug_status = unknown_slug_status

        response = await service.client.post("/channels?slug=no_such_channel")

        assert response.status_code == 404
        assert "no_such_channel" in response.json()["detail"]
        assert service.rows("SELECT * FROM streamers") == []
        assert service.api.subscribed == []

    async def test_kick_being_down_is_reported_as_a_gateway_error(self, service, caplog):
        service.api.failures["/channels"] = 503

        with caplog.at_level(logging.ERROR, logger="kick_clip_hunter"):
            response = await service.client.post("/channels?slug=new_channel")

        assert response.status_code == 502
        assert "see server log" in response.json()["detail"]
        assert any("failed to add new_channel" in record.getMessage() for record in caplog.records)
        assert service.rows("SELECT * FROM streamers") == []

    async def test_any_other_failure_is_a_gateway_error_too_never_a_crash(self, service, monkeypatch, caplog):
        async def unreachable(slug):
            raise OSError("network is unreachable")

        monkeypatch.setattr(service.main, "add_channel_to_watchlist", unreachable)

        with caplog.at_level(logging.ERROR, logger="kick_clip_hunter"):
            response = await service.client.post("/channels?slug=new_channel")

        assert response.status_code == 502
        assert any(record.exc_info for record in caplog.records)

    async def test_a_new_channels_emote_names_are_used_straight_away(self, service):
        service.api.add_channel("new_channel", 555, seventv_emotes=["KEKW"])
        await service.client.post("/channels?slug=new_channel")
        service._user_ids["new_channel"] = 555

        await service.chat("new_channel", "alice", "KEKW")

        assert detector._entries["new_channel"][0][6] == detector.EMOTE_MENTION_LAUGH_WEIGHT


class TestChannelTracking:
    async def test_pausing_and_resuming_one_channel(self, service):
        add_streamer("channel_a", 1)
        add_streamer("channel_b", 2)

        paused = await service.client.post("/channels/channel_a/tracking?enabled=0")

        assert paused.json() == {"slug": "channel_a", "tracking_enabled": False}
        assert service.main._is_channel_tracked("channel_a") is False
        assert service.main._is_channel_tracked("channel_b") is True

        resumed = await service.client.post("/channels/channel_a/tracking?enabled=1")

        assert resumed.json() == {"slug": "channel_a", "tracking_enabled": True}
        assert service.main._is_channel_tracked("channel_a") is True

    async def test_an_unknown_channel_is_a_404(self, service):
        response = await service.client.post("/channels/nobody/tracking?enabled=0")

        assert response.status_code == 404

    async def test_a_channel_that_is_not_on_the_watchlist_is_not_tracked(self, service):
        assert service.main._is_channel_tracked("nobody") is False

    async def test_the_watchlist_the_recording_loop_sees_is_the_stored_one(self, service):
        add_streamer("channel_a", 1)
        add_streamer("channel_b", 2)

        assert service.main._watchlist_slugs() == ["channel_a", "channel_b"]


class TestShutdown:
    async def test_with_nothing_in_flight_the_process_exits_right_away(self, service, process_exits):
        response = await service.client.post("/shutdown")

        assert response.json() == {"status": "shutting down", "pending_work_count": 0}
        await async_wait_until(lambda: process_exits, "the process to exit")
        assert process_exits == [0]

    async def test_running_recorders_are_stopped_before_exiting(self, service, process_exits, monkeypatch):
        order: list[str] = []

        async def stop_all():
            order.append("recorders stopped")

        monkeypatch.setattr(service.main.recording_manager, "stop_all", stop_all)
        monkeypatch.setattr(service.main.os, "_exit", lambda code: order.append(f"exit {code}"))

        await service.client.post("/shutdown")
        await async_wait_until(lambda: len(order) == 2, "shutdown to complete")

        assert order == ["recorders stopped", "exit 0"]

    async def test_it_waits_for_work_in_flight(self, service, process_exits):
        finish = asyncio.Event()
        service.main._track_task(finish.wait())

        response = await service.client.post("/shutdown")
        assert response.json() == {"status": "shutting down", "pending_work_count": 1}

        # Several of its (time-compressed) polls later, still waiting.
        await asyncio.sleep(0.1)
        assert process_exits == []

        finish.set()
        await async_wait_until(lambda: process_exits, "the process to exit")
        assert process_exits == [0]

    async def test_an_open_moment_counts_as_work_in_flight(self, service, process_exits, monkeypatch):
        monkeypatch.setitem(service.main._moment_sessions, "channel_a", object())

        response = await service.client.post("/shutdown")
        await asyncio.sleep(0.05)

        assert response.json()["pending_work_count"] == 1
        assert process_exits == []

        service.main._moment_sessions.clear()
        await async_wait_until(lambda: process_exits, "the process to exit")

    async def test_asking_twice_does_not_start_a_second_shutdown(self, service, process_exits):
        finish = asyncio.Event()
        service.main._track_task(finish.wait())
        await service.client.post("/shutdown")

        again = await service.client.post("/shutdown")

        assert again.json() == {"status": "already shutting down", "pending_work_count": 1}
        finish.set()
        await async_wait_until(lambda: process_exits, "the process to exit")
        await asyncio.sleep(0.05)
        assert process_exits == [0]

    async def test_no_new_moment_can_start_once_shutdown_was_requested(self, service, process_exits):
        finish = asyncio.Event()
        service.main._track_task(finish.wait())
        service.watch("some_channel")
        service.warm_up("some_channel")
        await service.client.post("/shutdown")

        await service.crowd_laughs("some_channel", people=20)

        assert service.moments() == []
        finish.set()
        await async_wait_until(lambda: process_exits, "the process to exit")
