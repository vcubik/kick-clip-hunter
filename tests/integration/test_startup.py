"""What the service does when it starts, before the first request.

The interesting part is self-healing: Kick has more than once silently
dropped every chat subscription, after which no chat arrives for any channel
until each one is re-subscribed. Startup reconciles the watchlist against
what Kick actually has, so a restart is enough to recover.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from kick_clip_hunter import recording_manager
from tests.support.data import add_streamer
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

        assert any("could not check chat subscriptions on startup" in r.getMessage() for r in caplog.records)

    async def test_a_token_failure_does_not_stop_it_either(self, service, caplog):
        service.watch("lost_one", subscribed=False)
        service.api.failures["/oauth/token"] = 401

        with caplog.at_level(logging.ERROR, logger="kick_clip_hunter"):
            await service.main._ensure_chat_subscriptions()

        assert subscribe_requests(service.api) == []
        assert any("could not check chat subscriptions on startup" in r.getMessage() for r in caplog.records)

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
