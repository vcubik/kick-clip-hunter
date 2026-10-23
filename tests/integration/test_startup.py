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
from datetime import datetime, timedelta, timezone

import pytest

from kick_clip_hunter import db, detector, recording_manager, tiktok_chat, twitch_auth, twitch_chat, youtube_chat
from kick_clip_hunter.channel_badges import Picture
from kick_clip_hunter.config import load_settings
from tests.support.data import add_streamer
from tests.support.eventsub import chat_event
from tests.support.kick_api import seventv_emote_id
from tests.support.tiktok import FakeTikTokChats, comment
from tests.support.twitch_api import FakeAccount
from tests.support.waiting import async_wait_until
from tests.support.youtube import emoji as youtube_emoji
from tests.support.youtube import message as youtube_message

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

    async def test_channels_of_other_platforms_are_none_of_kicks_business(self, service, caplog):
        kick_channel = service.watch("kick_channel", subscribed=False)
        conn = db.get_connection()
        try:
            db.add_platform_streamer(conn, "twitch", "123", "twitch_channel@twitch")
        finally:
            conn.close()

        with caplog.at_level(logging.INFO, logger="kick_clip_hunter"):
            await service.main._ensure_chat_subscriptions()

        assert subscribe_requests(service.api) == [kick_channel]
        assert service.main._subscriptions.watched == 1


class TestLifespan:
    async def test_startup_restores_switches_heals_subscriptions_and_starts_recording(self, service, monkeypatch):
        lost = service.watch("lost_one", subscribed=False)
        await service.client.post("/settings/watching?enabled=0")
        service.main._flags.clear()  # a fresh process knows nothing yet
        started = asyncio.Event()
        loop_arguments = []

        async def recording_loop(get_slugs, recording_enabled, is_channel_tracked, stream_recording_over, *_always):
            loop_arguments.append((get_slugs(), recording_enabled(), is_channel_tracked("lost_one")))
            # ...and whom to tell that a stream recording should end.
            assert stream_recording_over is service.main._stream_recording_over
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

    async def test_a_channel_tracked_for_its_chat_alone_is_looked_at_and_never_recorded(self, service, monkeypatch):
        # Live, so a recorder ticked for it would open a browser - which
        # fails the test.
        monkeypatch.setattr(recording_manager, "TICK_INTERVAL_SECONDS", 0.01)
        service.watch("chatty_channel", live=True)
        await service.client.post("/channels/chatty_channel/tracking?what=chat")

        async with service.main.lifespan(service.main.app):
            await async_wait_until(
                lambda: len(service.api.calls("GET", "/channels")) >= 2, "two rounds of liveness checks"
            )
            now = (await service.client.get("/recordings/status")).json()["now"]

        assert now["channels"]["chatty_channel"] == {"live": False, "words": "Live, chat only"}
        assert not any(recorder.is_active for recorder in recording_manager._recorders.values())


class TestTwitchChat:
    """Set up for Twitch, the service reads the chat of the Twitch channels
    on the watchlist from its start, and takes it in as it takes Kick's."""

    @pytest.fixture
    def twitch_service(self, service, twitch_api, eventsub, monkeypatch):
        async def no_recording(*_args):
            await asyncio.Event().wait()

        monkeypatch.setattr(recording_manager, "run_forever", no_recording)
        monkeypatch.setattr(service.main, "settings", load_settings())
        monkeypatch.setattr(twitch_chat, "EVENTSUB_URL", eventsub.url)
        monkeypatch.setattr(twitch_chat, "BATCH_SECONDS", 0.01)
        tokens = twitch_api.tokens_for(FakeAccount())
        twitch_auth.save(
            twitch_auth.AccountToken(
                tokens["access_token"],
                tokens["refresh_token"],
                datetime.now(timezone.utc) + timedelta(hours=4),
                FakeAccount().user_id,
                FakeAccount().login,
            )
        )
        return service

    def watch_on_twitch(self, login: str, twitch_id: str) -> int:
        conn = db.get_connection()
        try:
            return db.add_platform_streamer(conn, "twitch", twitch_id, f"{login}@twitch")
        finally:
            conn.close()

    async def test_a_twitch_channels_chat_is_stored_and_counted(self, twitch_service, twitch_api, eventsub):
        key = self.watch_on_twitch("some_channel", "101")

        async with twitch_service.main.lifespan(twitch_service.main.app):
            await async_wait_until(lambda: twitch_api.subscribed_on("session-1") == ["101"], "the chat subscription")
            await eventsub.notify(chat_event("101", "some_channel", "alice", "xDDD", message_id="m-1"))
            await async_wait_until(lambda: twitch_service.rows("SELECT * FROM chat_messages"), "the message")

        (row,) = twitch_service.rows("SELECT * FROM chat_messages")
        stored = (row["message_id"], row["broadcaster_user_id"], row["channel_slug"])
        assert stored == ("m-1", key, "some_channel@twitch")
        assert (row["sender_username"], row["content"]) == ("alice", "xDDD")
        assert [entry[1] for entry in detector._entries["some_channel@twitch"]] == ["alice"]

    async def test_a_kick_channel_is_none_of_the_readers_business(self, twitch_service, twitch_api, eventsub):
        twitch_service.watch("kick_channel")
        self.watch_on_twitch("some_channel", "101")

        async with twitch_service.main.lifespan(twitch_service.main.app):
            await async_wait_until(lambda: twitch_api.subscribed_on("session-1"), "the chat subscription")

        assert twitch_api.subscribed_on("session-1") == ["101"]

    async def test_without_twitch_set_up_there_is_no_reader(self, service, monkeypatch):
        async def no_recording(*_args):
            await asyncio.Event().wait()

        monkeypatch.setattr(recording_manager, "run_forever", no_recording)

        async with service.main.lifespan(service.main.app):
            assert service.main._twitch_reader is None


class TestYouTubeChat:
    """The service reads the chat of the YouTube channels on the watchlist
    while they are live, from its start, and takes it in as it takes Kick's.
    YouTube needs nothing set up."""

    @pytest.fixture
    def youtube_service(self, service, youtube, monkeypatch):
        async def no_recording(*_args):
            await asyncio.Event().wait()

        monkeypatch.setattr(recording_manager, "run_forever", no_recording)
        monkeypatch.setattr(youtube_chat, "POLL_SECONDS", (0.01,))
        monkeypatch.setattr(youtube_chat, "LOOK_SECONDS", 0.05)
        return service

    def watch_on_youtube(self, handle: str, channel_id: str) -> int:
        conn = db.get_connection()
        try:
            return db.add_platform_streamer(conn, "youtube", channel_id, f"{handle}@youtube")
        finally:
            conn.close()

    async def test_a_youtube_channels_chat_is_stored_and_counted(self, youtube_service, youtube):
        channel = youtube.add_channel("some_channel")
        channel.go_live("livevideo01")
        youtube.chat("livevideo01").say(
            youtube_message("m-1", "alice", "xDDD ", youtube_emoji("😂", "face_with_tears_of_joy", "1f602"))
        )
        key = self.watch_on_youtube("some_channel", channel.channel_id)

        async with youtube_service.main.lifespan(youtube_service.main.app):
            await async_wait_until(lambda: youtube_service.rows("SELECT * FROM chat_messages"), "the message")

        (row,) = youtube_service.rows("SELECT * FROM chat_messages")
        stored = (row["message_id"], row["broadcaster_user_id"], row["channel_slug"])
        assert stored == ("m-1", key, "some_channel@youtube")
        assert (row["sender_username"], row["content"]) == (
            "alice",
            "xDDD [emote:emoji.1f602:face_with_tears_of_joy]",
        )
        (entry,) = detector._entries["some_channel@youtube"]
        assert (entry[1], entry[3]) == ("alice", 1)

    async def test_a_kick_or_twitch_channel_is_none_of_the_readers_business(self, youtube_service, youtube):
        youtube_service.watch("kick_channel")
        conn = db.get_connection()
        try:
            db.add_platform_streamer(conn, "twitch", "101", "twitch_channel@twitch")
        finally:
            conn.close()

        async with youtube_service.main.lifespan(youtube_service.main.app):
            await async_wait_until(lambda: youtube_service.main._youtube_reader.watched == 0, "the watchlist")

        assert youtube.requests == []

    async def test_the_reader_starts_with_the_service(self, youtube_service):
        async with youtube_service.main.lifespan(youtube_service.main.app):
            assert youtube_service.main._youtube_reader is not None


class TestTikTokChat:
    """The service reads the chat of a TikTok channel while its stream is
    being recorded, from its start, keeps it with the recording - and looks
    for nothing in it."""

    @pytest.fixture
    def chats(self, monkeypatch) -> FakeTikTokChats:
        chats = FakeTikTokChats()
        monkeypatch.setattr(tiktok_chat, "tiktoklive_chat", chats.connect)
        monkeypatch.setattr(tiktok_chat, "CHANNELS_SECONDS", 0.02)
        monkeypatch.setattr(tiktok_chat, "BATCH_SECONDS", 0.01)
        return chats

    @pytest.fixture
    def tiktok_service(self, service, tiktok, chats, monkeypatch):
        async def no_recording(*_args):
            await asyncio.Event().wait()

        monkeypatch.setattr(recording_manager, "run_forever", no_recording)
        return service

    async def add(self, service, tiktok, handle: str) -> int:
        tiktok.add_channel(handle).go_live()
        response = await service.client.post("/channels", params={"slug": f"{handle}@tiktok"})
        return response.json()["broadcaster_user_id"]

    async def test_a_recorded_tiktok_channels_chat_is_kept_and_looked_for_nothing(self, tiktok_service, tiktok, chats):
        key = await self.add(tiktok_service, tiktok, "some_channel")

        async with tiktok_service.main.lifespan(tiktok_service.main.app):
            started = await tiktok_service.client.post("/channels/some_channel@tiktok/recording/start")
            assert started.status_code == 200
            await async_wait_until(lambda: chats.connections, "the chat connected to")
            chats.latest().say(comment(11, "Alice", "xDDD 😂"), comment(12, "Bob", "KEKW"))
            await async_wait_until(lambda: len(tiktok_service.rows("SELECT * FROM chat_messages")) == 2, "the chat")

        rows = tiktok_service.rows("SELECT * FROM chat_messages ORDER BY message_id")
        assert [(row["message_id"], row["broadcaster_user_id"], row["channel_slug"]) for row in rows] == [
            ("11", key, "some_channel@tiktok"),
            ("12", key, "some_channel@tiktok"),
        ]
        assert [(row["sender_username"], row["content"]) for row in rows] == [("Alice", "xDDD 😂"), ("Bob", "KEKW")]
        # Nothing of it is counted, and no moment can come of it.
        assert "some_channel@tiktok" not in detector._entries
        assert tiktok_service.moments() == []

    async def test_a_tiktok_channel_that_is_not_recorded_has_no_chat_read(self, tiktok_service, tiktok, chats):
        await self.add(tiktok_service, tiktok, "some_channel")
        tiktok.requests.clear()

        async with tiktok_service.main.lifespan(tiktok_service.main.app):
            reader = tiktok_service.main._tiktok_reader
            await asyncio.sleep(0.1)
            assert reader.wanted == 0

        assert chats.connections == [] and tiktok.requests == []

    async def test_the_reader_starts_with_the_service_and_signs_with_a_key_where_there_is_one(
        self, tiktok_service, monkeypatch
    ):
        monkeypatch.setenv("EULER_STREAM_API_KEY", "a-key")
        monkeypatch.setattr(tiktok_service.main, "settings", load_settings())

        async with tiktok_service.main.lifespan(tiktok_service.main.app):
            assert tiktok_service.main._tiktok_reader is not None

        assert tiktok_chat._sign_key == "a-key"


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

    async def test_a_twitch_channels_emotes_are_asked_for_by_its_twitch_id(self, service, twitch_api):
        kick_channel = service.watch("kick_channel")
        service.api.channels["kick_channel"].seventv_emotes = ["KEKW"]
        twitch_api.add_channel("twitch_channel", "123", seventv_emotes=["OMEGALUL"])
        conn = db.get_connection()
        try:
            twitch_channel = db.add_platform_streamer(conn, "twitch", "123", "twitch_channel@twitch")
        finally:
            conn.close()

        await service.main._refresh_emote_pictures()

        assert set(self.pictures(kick_channel)) == {"KEKW"}
        assert set(self.pictures(twitch_channel)) == {"OMEGALUL"}

    async def test_a_youtube_channel_has_no_set_to_fetch(self, service, youtube, caplog):
        conn = db.get_connection()
        try:
            db.add_platform_streamer(conn, "youtube", "UCAAAAAAAAAAAAAAAAAAAAAA", "youtube_channel@youtube")
        finally:
            conn.close()

        with caplog.at_level(logging.INFO, logger="kick_clip_hunter"):
            await service.main._refresh_emote_pictures()

        assert not [path for _, path, _ in service.api.requests if "/users/" in path]
        assert not any("youtube_channel" in record.getMessage() for record in caplog.records)

    async def test_it_happens_on_startup_without_holding_it_up(self, service, monkeypatch):
        async def no_recording(*_args):
            await asyncio.Event().wait()

        monkeypatch.setattr(recording_manager, "run_forever", no_recording)
        user_id = service.watch("channel_a")
        service.api.channels["channel_a"].seventv_emotes = ["KEKW"]

        async with service.main.lifespan(service.main.app):
            await async_wait_until(lambda: self.pictures(user_id), "the channel's emote pictures to be stored")

        assert set(self.pictures(user_id)) == {"KEKW"}


KICK_BADGE = "https://files.kick.com/channel_subscriber_badges/{}/conversion/original-fullsize.png"
TWITCH_BADGE = "https://static-cdn.jtvnw.net/badges/v1/{}/3"


class TestSubscriberBadges:
    """A channel's own subscriber badges are fetched with its emote
    pictures: on every start, and kept when they can't be."""

    def badges(self, broadcaster_user_id: int) -> list[Picture]:
        conn = db.get_connection()
        try:
            return db.get_channel_badges(conn, broadcaster_user_id)
        finally:
            conn.close()

    async def test_a_kick_channels_come_from_kick(self, service):
        user_id = service.watch("channel_a")
        service.api.channels["channel_a"].subscriber_badges = {1: KICK_BADGE.format(1), 6: KICK_BADGE.format(6)}

        await service.main._refresh_emote_pictures()

        assert self.badges(user_id) == [Picture(1, 1, KICK_BADGE.format(1)), Picture(1, 6, KICK_BADGE.format(6))]

    async def test_a_twitch_channels_from_twitch_by_its_twitch_id(self, service, twitch_api):
        twitch_api.add_channel("twitch_channel", "123", subscriber_badges={"0": TWITCH_BADGE.format("a")})
        conn = db.get_connection()
        try:
            twitch_channel = db.add_platform_streamer(conn, "twitch", "123", "twitch_channel@twitch")
        finally:
            conn.close()

        await service.main._refresh_emote_pictures()

        assert self.badges(twitch_channel) == [Picture(1, 0, TWITCH_BADGE.format("a"))]
        assert twitch_api.calls("GET", "/helix/chat/badges") == [{"broadcaster_id": "123"}]

    async def test_a_channel_that_has_dropped_them_has_none(self, service):
        user_id = service.watch("channel_a")
        service.api.channels["channel_a"].subscriber_badges = {1: KICK_BADGE.format(1)}
        await service.main._refresh_emote_pictures()
        service.api.channels["channel_a"].subscriber_badges = {}

        await service.main._refresh_emote_pictures()

        assert self.badges(user_id) == []

    async def test_ones_that_cannot_be_fetched_stay_as_they_were(self, service, caplog):
        user_id = service.watch("channel_a")
        service.api.channels["channel_a"].subscriber_badges = {1: KICK_BADGE.format(1)}
        await service.main._refresh_emote_pictures()
        service.api.channels["channel_a"].seventv_emotes = ["KEKW"]
        service.api.failures["kick.com/api/v2"] = 403

        with caplog.at_level(logging.WARNING, logger="kick_clip_hunter"):
            await service.main._refresh_emote_pictures()

        assert self.badges(user_id) == [Picture(1, 1, KICK_BADGE.format(1))]
        assert set(TestEmotePictures().pictures(user_id)) == {"KEKW"}
        assert any("could not refresh the subscriber badges of channel_a" in r.getMessage() for r in caplog.records)


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
