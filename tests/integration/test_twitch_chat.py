"""Reading Twitch chat over an EventSub WebSocket: the real ChatReader
against a fake EventSub server on loopback (FakeEventSub) and the fake
Twitch API (subscriptions, the account's tokens).

What matters: every watched channel's chat is subscribed to the session a
connection is greeted with; chat is handed on as it comes, a batch at a
time; and the connection survives what Twitch does to it - silence, a
request to move, a dropped connection, a subscription that ends.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone

import pytest

from kick_clip_hunter import twitch_auth, twitch_chat
from kick_clip_hunter.chat_message import ChatMessage
from kick_clip_hunter.twitch_auth import AccountToken, TokenKeeper
from kick_clip_hunter.twitch_chat import Channel, ChatReader
from tests.support.eventsub import chat_event
from tests.support.twitch_api import FakeAccount
from tests.support.waiting import async_wait_until

pytestmark = pytest.mark.anyio

CLIENT_ID = "twitch-client-id"
FIRST = Channel(broadcaster_user_id=-1, channel="first@twitch", twitch_id="101")
SECOND = Channel(broadcaster_user_id=-2, channel="second@twitch", twitch_id="202")


def signed_in(twitch_api, account: FakeAccount | None = None) -> AccountToken:
    """An account signed in for the service, as twitch_login.py leaves it."""
    account = account or FakeAccount()
    tokens = twitch_api.tokens_for(account)
    token = AccountToken(
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        expires_at=datetime.now(timezone.utc) + timedelta(hours=4),
        user_id=account.user_id,
        login=account.login,
    )
    twitch_auth.save(token)
    return token


class Rig:
    """A reader and what it hands on."""

    def __init__(self, eventsub, channels: list[Channel]) -> None:
        self.channels = channels
        self.batches: list[list[ChatMessage]] = []
        self.refuse_next_batch = False
        self.reader = ChatReader(
            lambda: list(self.channels), self.take, TokenKeeper(CLIENT_ID, "secret"), CLIENT_ID, url=eventsub.url
        )

    async def take(self, batch: list[ChatMessage]) -> None:
        if self.refuse_next_batch:
            self.refuse_next_batch = False
            raise RuntimeError("the database is busy")
        self.batches.append(batch)

    @property
    def messages(self) -> list[ChatMessage]:
        return [message for batch in self.batches for message in batch]


@pytest.fixture
def quick(monkeypatch):
    """Everything the reader waits for, a thousand times shorter."""
    monkeypatch.setattr(twitch_chat, "BATCH_SECONDS", 0.01)
    monkeypatch.setattr(twitch_chat, "RETRY_SECONDS", (0.01,))
    monkeypatch.setattr(twitch_chat, "CHANNELS_SECONDS", 0.05)
    monkeypatch.setattr(twitch_chat, "SILENCE_GRACE_SECONDS", 0.05)


@asynccontextmanager
async def running(rig: Rig):
    task = asyncio.create_task(rig.reader.run_forever())
    try:
        yield rig
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def subscribed(twitch_api, session: str, *twitch_ids: str) -> None:
    await async_wait_until(
        lambda: sorted(twitch_api.subscribed_on(session)) == sorted(twitch_ids),
        f"the chat of {twitch_ids} to be subscribed on {session}",
    )


class TestSubscribing:
    async def test_every_channels_chat_is_subscribed_to_the_session_greeted_with(self, twitch_api, eventsub, quick):
        signed_in(twitch_api, FakeAccount("chat_reader", "777"))
        rig = Rig(eventsub, [FIRST, SECOND])

        async with running(rig):
            await subscribed(twitch_api, "session-1", "101", "202")
            await async_wait_until(lambda: rig.reader.connected_since is not None, "the reader to say it is connected")

        subscription = twitch_api.eventsub_subscriptions[0]
        assert (subscription["type"], subscription["version"], subscription["account"]) == (
            "channel.chat.message",
            "1",
            "chat_reader",
        )
        assert subscription["condition"]["user_id"] == "777"
        assert subscription["transport"] == {"method": "websocket", "session_id": "session-1"}
        assert eventsub.paths == ["/ws?keepalive_timeout_seconds=30"]
        assert set(rig.reader.subscribed) == {"101", "202"}

    async def test_a_channel_added_meanwhile_is_subscribed_on_the_same_session(self, twitch_api, eventsub, quick):
        signed_in(twitch_api)
        rig = Rig(eventsub, [FIRST])

        async with running(rig):
            await subscribed(twitch_api, "session-1", "101")
            rig.channels.append(SECOND)
            await subscribed(twitch_api, "session-1", "101", "202")

        assert eventsub.sessions == ["session-1"]

    async def test_a_refused_subscription_is_said_and_the_rest_are_read(self, twitch_api, eventsub, quick):
        signed_in(twitch_api)
        twitch_api.subscribe_failures["202"] = 403
        rig = Rig(eventsub, [FIRST, SECOND])

        async with running(rig):
            await subscribed(twitch_api, "session-1", "101")
            await eventsub.notify(chat_event("101", "first", "alice", "hello"))
            await async_wait_until(lambda: rig.messages, "the first channel's chat")

        assert rig.reader.refused["202"].startswith("403")
        assert "202" not in rig.reader.subscribed

    async def test_with_every_subscription_refused_the_connection_is_not_kept(self, twitch_api, eventsub, quick):
        signed_in(twitch_api)
        twitch_api.subscribe_failures.update({"101": 403, "202": 403})
        rig = Rig(eventsub, [FIRST, SECOND])

        async with running(rig):
            await async_wait_until(lambda: rig.reader.trouble, "the reader to say what is wrong")
            await async_wait_until(lambda: eventsub.open_connections == 0, "the unused connection to be closed")

        assert "no channel's chat could be subscribed" in rig.reader.trouble

    async def test_a_token_twitch_turns_down_is_renewed_once_and_used(self, twitch_api, eventsub, quick):
        token = signed_in(twitch_api)
        del twitch_api.user_tokens[token.access_token]  # taken back, but its refresh token still works
        rig = Rig(eventsub, [FIRST])

        async with running(rig):
            await subscribed(twitch_api, "session-1", "101")

        assert twitch_auth.load().access_token != token.access_token

    async def test_without_a_signed_in_account_nothing_is_connected(self, twitch_api, eventsub, quick):
        rig = Rig(eventsub, [FIRST])

        async with running(rig):
            await async_wait_until(lambda: rig.reader.trouble, "the reader to say what is wrong")
            await asyncio.sleep(0.05)

        assert "signed in" in rig.reader.trouble
        assert eventsub.paths == []

    async def test_without_twitch_channels_nothing_is_connected(self, twitch_api, eventsub, quick):
        signed_in(twitch_api)
        rig = Rig(eventsub, [])

        async with running(rig):
            await asyncio.sleep(0.1)

        assert eventsub.paths == []
        assert rig.reader.trouble is None


class TestChat:
    async def test_a_message_is_handed_on_as_the_service_takes_chat_in(self, twitch_api, eventsub, quick):
        signed_in(twitch_api)
        rig = Rig(eventsub, [FIRST, SECOND])

        async with running(rig):
            await subscribed(twitch_api, "session-1", "101", "202")
            await eventsub.notify(chat_event("202", "second", "alice", "hello", message_id="m-1"))
            await async_wait_until(lambda: rig.messages, "the message to be handed on")

        (message,) = rig.messages
        assert (message.broadcaster_user_id, message.channel, message.sender, message.content) == (
            -2,
            "second@twitch",
            "alice",
            "hello",
        )
        assert message.message_id == "m-1"
        assert message.received_at is not None and message.created_at.endswith("Z")

    async def test_messages_that_come_together_are_handed_on_together(self, twitch_api, eventsub, quick, monkeypatch):
        monkeypatch.setattr(twitch_chat, "BATCH_SECONDS", 0.3)
        signed_in(twitch_api)
        rig = Rig(eventsub, [FIRST])

        async with running(rig):
            await subscribed(twitch_api, "session-1", "101")
            for number in range(3):
                await eventsub.notify(chat_event("101", "first", f"viewer{number}", "xDDD"))
            await async_wait_until(lambda: rig.messages, "the messages to be handed on")

        assert [len(batch) for batch in rig.batches] == [3]

    async def test_chat_of_a_channel_not_subscribed_and_other_notifications_are_left_out(
        self, twitch_api, eventsub, quick
    ):
        signed_in(twitch_api)
        rig = Rig(eventsub, [FIRST])

        async with running(rig):
            await subscribed(twitch_api, "session-1", "101")
            await eventsub.notify(chat_event("999", "stranger", "alice", "not ours"))
            await eventsub.notify({"broadcaster_user_id": "101"}, subscription_type="channel.follow")
            await eventsub.notify(chat_event("101", "first", "bob", "ours"))
            await async_wait_until(lambda: rig.messages, "the message to be handed on")

        assert [message.content for message in rig.messages] == ["ours"]

    async def test_what_cannot_be_taken_in_does_not_stop_the_chat_after_it(self, twitch_api, eventsub, quick):
        signed_in(twitch_api)
        rig = Rig(eventsub, [FIRST])
        rig.refuse_next_batch = True

        async with running(rig):
            await subscribed(twitch_api, "session-1", "101")
            await eventsub.notify(chat_event("101", "first", "alice", "lost"))
            await async_wait_until(lambda: not rig.refuse_next_batch, "the first batch to be refused")
            await eventsub.notify(chat_event("101", "first", "bob", "kept"))
            await async_wait_until(lambda: rig.messages, "the next message to be handed on")

        assert [message.content for message in rig.messages] == ["kept"]
        assert eventsub.sessions == ["session-1"]


class TestWhatTwitchDoesToTheConnection:
    async def test_keepalives_keep_a_quiet_connection(self, twitch_api, eventsub, quick):
        eventsub.keepalive_seconds = 0.1
        signed_in(twitch_api)
        rig = Rig(eventsub, [FIRST])

        async with running(rig):
            await subscribed(twitch_api, "session-1", "101")
            for _ in range(6):
                await eventsub.keepalive()
                await asyncio.sleep(0.05)

        assert eventsub.sessions == ["session-1"]

    async def test_a_silent_connection_is_given_up_and_the_next_subscribes_everything_again(
        self, twitch_api, eventsub, quick
    ):
        eventsub.keepalive_seconds = 0.1
        signed_in(twitch_api)
        rig = Rig(eventsub, [FIRST, SECOND])

        async with running(rig):
            await subscribed(twitch_api, "session-1", "101", "202")
            await subscribed(twitch_api, "session-2", "101", "202")

    async def test_a_dropped_connection_is_opened_again(self, twitch_api, eventsub, quick):
        signed_in(twitch_api)
        rig = Rig(eventsub, [FIRST])

        async with running(rig):
            await subscribed(twitch_api, "session-1", "101")
            await eventsub.drop(4005)
            await subscribed(twitch_api, "session-2", "101")
            await eventsub.notify(chat_event("101", "first", "alice", "back"))
            await async_wait_until(lambda: rig.messages, "chat over the new connection")

    async def test_moving_to_another_server_keeps_the_subscriptions(self, twitch_api, eventsub, quick):
        signed_in(twitch_api)
        rig = Rig(eventsub, [FIRST])

        async with running(rig):
            await subscribed(twitch_api, "session-1", "101")
            await eventsub.ask_to_move()
            await async_wait_until(lambda: len(eventsub.sessions) == 2, "the new connection to be greeted")
            await async_wait_until(lambda: eventsub.open_connections == 1, "the old connection to be closed")
            await eventsub.notify(chat_event("101", "first", "alice", "moved"))
            await async_wait_until(lambda: rig.messages, "chat over the new connection")

        assert eventsub.paths[1] == "/ws?reconnect=1"
        # The new connection kept what was subscribed: nothing was asked for again.
        assert len(twitch_api.eventsub_subscriptions) == 1

    async def test_a_subscription_twitch_ends_is_said(self, twitch_api, eventsub, quick):
        signed_in(twitch_api)
        rig = Rig(eventsub, [FIRST, SECOND])

        async with running(rig):
            await subscribed(twitch_api, "session-1", "101", "202")
            await eventsub.revoke("202", "user_removed")
            await async_wait_until(lambda: "202" not in rig.reader.subscribed, "the subscription to be dropped")

        assert rig.reader.refused["202"] == "user_removed"
        assert set(rig.reader.subscribed) == {"101"}

    async def test_something_that_is_not_json_ends_the_connection_and_another_is_opened(
        self, twitch_api, eventsub, quick
    ):
        signed_in(twitch_api)
        rig = Rig(eventsub, [FIRST])

        async with running(rig):
            await subscribed(twitch_api, "session-1", "101")
            await eventsub.send_raw("garbage")
            await subscribed(twitch_api, "session-2", "101")

    async def test_a_connection_that_is_never_greeted_is_given_up(self, twitch_api, eventsub, quick, monkeypatch):
        monkeypatch.setattr(twitch_chat, "WELCOME_SECONDS", 0.1)
        eventsub.greets = False
        signed_in(twitch_api)
        rig = Rig(eventsub, [FIRST])

        async with running(rig):
            await async_wait_until(lambda: len(eventsub.paths) >= 2, "a second attempt")

        assert "did not greet" in rig.reader.trouble
        assert twitch_api.eventsub_subscriptions == []
