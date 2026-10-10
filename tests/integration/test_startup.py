"""What the service does when it starts, before the first request.

The interesting part is self-healing: Kick has more than once silently
dropped every chat subscription, after which no chat arrives for any channel
until each one is re-subscribed. Startup reconciles the watchlist against
what Kick actually has, and the running service repeats that check every few
minutes, so it recovers without a restart.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from kick_clip_hunter import db, recording_manager
from tests.support.data import add_streamer
from tests.support.kick_api import seventv_emote_id
from tests.support.waiting import async_wait_until

pytestmark = pytest.mark.anyio


def subscribe_requests(api) -> list[int]:
    return [body["broadcaster_user_id"] for body in api.calls("POST", "/events/subscriptions")]


class TestSubscriptionReconciliation:
    async def test_nothing_is_touched_when_every_channel_is_subscribed(self, service, caplog):
        service.watch("channel_a")
        service.watch("channel_b")

        with caplog.at_level(logging.INFO, logger="kick_clip_hunter"):
            await service.main._ensure_chat_subscriptions()

        assert subscribe_requests(service.api) == []
        assert any("chat subscriptions present for all 2 watched channels" in r.getMessage() for r in caplog.records)

    async def test_only_the_missing_channels_are_re_subscribed(self, service):
        kept = service.watch("still_subscribed")
        lost_one = service.watch("lost_one", subscribed=False)
        lost_two = service.watch("lost_two", subscribed=False)

        await service.main._ensure_chat_subscriptions()

        assert subscribe_requests(service.api) == [lost_one, lost_two]
        assert sorted(service.api.subscribed) == sorted([kept, lost_one, lost_two])

    async def test_after_kick_dropped_everything_every_channel_comes_back(self, service):
        ids = [service.watch(slug) for slug in ("channel_a", "channel_b", "channel_c")]
        service.api.subscribed.clear()

        await service.main._ensure_chat_subscriptions()

        assert sorted(service.api.subscribed) == sorted(ids)

    async def test_running_it_again_subscribes_nothing_twice(self, service):
        service.watch("lost_one", subscribed=False)

        await service.main._ensure_chat_subscriptions()
        await service.main._ensure_chat_subscriptions()

        assert len(subscribe_requests(service.api)) == 1

    async def test_an_empty_watchlist_makes_no_api_calls(self, service):
        await service.main._ensure_chat_subscriptions()

        assert service.api.requests == []

    async def test_the_request_asks_for_chat_messages_by_webhook(self, service):
        user_id = service.watch("lost_one", subscribed=False)

        await service.main._ensure_chat_subscriptions()

        assert service.api.calls("POST", "/events/subscriptions") == [
            {
                "broadcaster_user_id": user_id,
                "events": [{"name": "chat.message.sent", "version": 1}],
                "method": "webhook",
            }
        ]

    async def test_kick_being_unreachable_does_not_stop_the_service_from_starting(self, service, caplog):
        service.watch("lost_one", subscribed=False)
        service.api.failures["/events/subscriptions"] = 503

        with caplog.at_level(logging.ERROR, logger="kick_clip_hunter"):
            await service.main._ensure_chat_subscriptions()  # must not raise

        assert any("could not check chat subscriptions" in r.getMessage() for r in caplog.records)

    async def test_a_token_failure_does_not_stop_it_either(self, service, caplog):
        service.watch("lost_one", subscribed=False)
        service.api.failures["/oauth/token"] = 401

        with caplog.at_level(logging.ERROR, logger="kick_clip_hunter"):
            await service.main._ensure_chat_subscriptions()

        assert subscribe_requests(service.api) == []
        assert any("could not check chat subscriptions" in r.getMessage() for r in caplog.records)

    async def test_one_channel_failing_does_not_keep_the_others_from_being_re_subscribed(self, service, caplog):
        first = service.watch("first", subscribed=False)
        broken = service.watch("broken", subscribed=False)
        last = service.watch("last", subscribed=False)
        service.api.subscribe_failures.add(broken)

        with caplog.at_level(logging.ERROR, logger="kick_clip_hunter"):
            await service.main._ensure_chat_subscriptions()

        assert service.api.subscribed == [first, last]
        assert any("failed to re-subscribe chat for broken" in r.getMessage() for r in caplog.records)


class TestLifespan:
    async def test_startup_restores_switches_heals_subscriptions_and_starts_recording(self, service, monkeypatch):
        lost = service.watch("lost_one", subscribed=False)
        await service.client.post("/settings/watching?enabled=0")
        service.main._flags.clear()  # a fresh process knows nothing yet
        started = asyncio.Event()
        loop_arguments = []

        async def recording_loop(get_slugs, recording_enabled, is_channel_tracked):
            loop_arguments.append((get_slugs(), recording_enabled(), is_channel_tracked("lost_one")))
            started.set()
            await asyncio.Event().wait()  # runs until cancelled, like the real one

        monkeypatch.setattr(recording_manager, "run_forever", recording_loop)

        async with service.main.lifespan(service.main.app):
            await asyncio.wait_for(started.wait(), timeout=5)

            assert service.main._flags["watching_enabled"] is False
            assert service.api.subscribed == [lost]
            # The loop is handed live views of the watchlist and the switches.
            assert loop_arguments == [(["lost_one"], False, True)]

    async def test_shutdown_cancels_the_recording_loop(self, service, monkeypatch):
        state = {"running": False, "cancelled": False}

        async def recording_loop(*_args):
            state["running"] = True
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise

        monkeypatch.setattr(recording_manager, "run_forever", recording_loop)

        async with service.main.lifespan(service.main.app):
            await async_wait_until(lambda: state["running"], "the recording loop to start")

        await async_wait_until(lambda: state["cancelled"], "the recording loop to be cancelled")

    async def test_the_real_recording_loop_runs_under_it_without_opening_a_browser(self, service, monkeypatch):
        # End to end through lifespan with the real loop: an offline channel
        # is checked against the API and left alone.
        monkeypatch.setattr(recording_manager, "TICK_INTERVAL_SECONDS", 0.01)
        service.watch("offline_channel", live=False)

        async with service.main.lifespan(service.main.app):
            await async_wait_until(
                lambda: len(service.api.calls("GET", "/channels")) >= 2, "two rounds of liveness checks"
            )

        assert not any(recorder.is_active for recorder in recording_manager._recorders.values())


class TestEmotePictures:
    """Channels change their 7TV emote sets all the time; a start brings
    the pictures chat is drawn with up to date."""

    def pictures(self, broadcaster_user_id: int) -> dict:
        conn = db.get_connection()
        try:
            return db.get_channel_emotes(conn, broadcaster_user_id)
        finally:
            conn.close()

    async def test_every_watched_channel_gets_its_current_emotes(self, service):
        first = service.watch("channel_a")
        second = service.watch("channel_b")
        service.api.channels["channel_a"].seventv_emotes = ["KEKW", "Sadge"]
        service.api.channels["channel_b"].seventv_emotes = ["KEKW"]
        service.api.channels["channel_b"].seventv_ids = {"KEKW": "ANOTHERKEKW"}

        await service.main._refresh_emote_pictures()

        assert self.pictures(first) == {
            "KEKW": (seventv_emote_id("KEKW"), 32, 32),
            "Sadge": (seventv_emote_id("Sadge"), 32, 32),
        }
        assert self.pictures(second) == {"KEKW": ("ANOTHERKEKW", 32, 32)}

    async def test_the_global_emotes_are_fetched_too(self, service):
        service.watch("channel_a")
        service.api.seventv_global.seventv_emotes = ["EZ", "Clap"]

        await service.main._refresh_emote_pictures()

        assert set(self.pictures(db.GLOBAL_EMOTES_OWNER)) == {"EZ", "Clap"}

    async def test_they_are_fetched_even_with_nothing_on_the_watchlist(self, service):
        service.api.seventv_global.seventv_emotes = ["EZ"]

        await service.main._refresh_emote_pictures()

        assert set(self.pictures(db.GLOBAL_EMOTES_OWNER)) == {"EZ"}

    async def test_failing_to_fetch_the_global_emotes_does_not_stop_the_channels(self, service, caplog):
        user_id = service.watch("channel_a")
        service.api.channels["channel_a"].seventv_emotes = ["KEKW"]
        service.api.seventv_global.seventv_emotes = ["EZ"]
        await service.main._refresh_emote_pictures()
        service.api.channels["channel_a"].seventv_emotes = ["OMEGALUL"]
        service.api.failures["emote-sets/global"] = 503

        with caplog.at_level(logging.ERROR, logger="kick_clip_hunter"):
            await service.main._refresh_emote_pictures()

        assert set(self.pictures(db.GLOBAL_EMOTES_OWNER)) == {"EZ"}
        assert set(self.pictures(user_id)) == {"OMEGALUL"}
        assert any("could not refresh the global 7TV emote pictures" in r.getMessage() for r in caplog.records)

    async def test_what_the_detector_listens_for_is_left_alone(self, service):
        user_id = service.watch("channel_a", keywords={"kekw": 3.5})
        service.api.channels["channel_a"].seventv_emotes = ["OMEGALUL"]

        await service.main._refresh_emote_pictures()

        conn = db.get_connection()
        try:
            assert db.get_channel_keywords(conn, user_id) == {"kekw": 3.5}
        finally:
            conn.close()

    async def test_a_7tv_outage_keeps_the_pictures_there_were(self, service, caplog):
        user_id = service.watch("channel_a")
        service.api.channels["channel_a"].seventv_emotes = ["KEKW"]
        await service.main._refresh_emote_pictures()
        service.api.failures["7tv.io"] = 503

        with caplog.at_level(logging.ERROR, logger="kick_clip_hunter"):
            await service.main._refresh_emote_pictures()

        assert set(self.pictures(user_id)) == {"KEKW"}
        assert any("could not refresh the 7TV emote pictures of channel_a" in r.getMessage() for r in caplog.records)

    async def test_it_happens_on_startup_without_holding_it_up(self, service, monkeypatch):
        async def no_recording(*_args):
            await asyncio.Event().wait()

        monkeypatch.setattr(recording_manager, "run_forever", no_recording)
        user_id = service.watch("channel_a")
        service.api.channels["channel_a"].seventv_emotes = ["KEKW"]

        async with service.main.lifespan(service.main.app):
            await async_wait_until(lambda: self.pictures(user_id), "the channel's emote pictures to be stored")

        assert set(self.pictures(user_id)) == {"KEKW"}


class TestWatchlistViews:
    async def test_tracking_follows_the_per_channel_switch(self, service):
        add_streamer("channel_a", 1)
        add_streamer("channel_b", 2, tracking=False)

        assert service.main._is_channel_tracked("channel_a") is True
        assert service.main._is_channel_tracked("channel_b") is False

    async def test_channel_keywords_are_read_once_per_channel(self, service, monkeypatch):
        user_id = service.watch("channel_a", keywords={"kekw": 3.5})
        reads = []
        real = service.main.get_channel_keywords

        def counting(conn, broadcaster_user_id):
            reads.append(broadcaster_user_id)
            return real(conn, broadcaster_user_id)

        monkeypatch.setattr(service.main, "get_channel_keywords", counting)

        for number in range(5):
            await service.chat("channel_a", "alice", f"KEKW {number}")

        assert reads == [user_id]


class TestPeriodicSubscriptionCheck:
    """The same reconciliation, repeated while the service runs."""

    @pytest.fixture
    async def checking(self, service):
        task = asyncio.create_task(service.main._keep_chat_subscriptions())
        yield service
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_subscriptions_kick_drops_while_running_come_back(self, checking):
        ids = [checking.watch(slug) for slug in ("channel_a", "channel_b")]
        checking.api.subscribed.clear()

        await async_wait_until(
            lambda: sorted(checking.api.subscribed) == sorted(ids), "the dropped subscriptions being restored"
        )

    async def test_it_keeps_checking_after_a_drop_was_repaired(self, checking):
        user_id = checking.watch("channel_a")
        for _ in range(2):
            checking.api.subscribed.clear()
            await async_wait_until(lambda: checking.api.subscribed == [user_id], "the subscription being restored")

        assert subscribe_requests(checking.api) == [user_id, user_id]

    async def test_a_check_that_finds_nothing_missing_stays_out_of_the_log(self, checking, caplog):
        checking.watch("channel_a")

        with caplog.at_level(logging.INFO, logger="kick_clip_hunter"):
            await async_wait_until(
                lambda: len(checking.api.calls("GET", "/events/subscriptions")) >= 2, "two periodic checks"
            )

        assert not any("chat subscriptions" in r.getMessage() for r in caplog.records)

    async def test_a_failed_check_does_not_end_the_checking(self, checking, monkeypatch):
        user_id = checking.watch("channel_a")
        real_check = checking.main._ensure_chat_subscriptions
        attempts = []

        async def fails_once(**kwargs):
            attempts.append(kwargs)
            if len(attempts) == 1:
                raise RuntimeError("unexpected")
            await real_check(**kwargs)

        monkeypatch.setattr(checking.main, "_ensure_chat_subscriptions", fails_once)
        checking.api.subscribed.clear()

        await async_wait_until(lambda: checking.api.subscribed == [user_id], "the check after the failed one")
