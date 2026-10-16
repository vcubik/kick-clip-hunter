"""Reading TikTok chat while a TikTok channel's stream is recorded: the real
ChatReader against the fake TikTok (whether a channel is live, and its room)
and a stand-in for the connection TikTokLive makes to a room's chat.

What matters: a channel's chat is read while it is wanted - its stream is
being recorded - and live, and only then; every comment that arrives is
handed on once, a batch at a time; and the reader rides out what TikTok does
- a stream that is not on yet, a connection that ends or cannot be made, a
stream that ends.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

import pytest

from kick_clip_hunter import tiktok_chat
from kick_clip_hunter.chat_message import ChatMessage
from kick_clip_hunter.tiktok_chat import Channel, ChatReader
from tests.support.tiktok import FakeTikTokChats, comment
from tests.support.waiting import async_wait_until

pytestmark = pytest.mark.anyio

FIRST = Channel(broadcaster_user_id=-1, channel="first@tiktok")
SECOND = Channel(broadcaster_user_id=-2, channel="second@tiktok")


class Rig:
    """A reader, the connections it makes, and what it hands on."""

    def __init__(self, channels: list[Channel]) -> None:
        self.channels = channels
        self.chats = FakeTikTokChats()
        self.batches: list[list[ChatMessage]] = []
        self.refuse_next_batch = False
        self.reader = ChatReader(self.wanted, self.take, self.chats.connect)

    def wanted(self) -> list[Channel]:
        return list(self.channels)

    async def take(self, batch: list[ChatMessage]) -> None:
        if self.refuse_next_batch:
            self.refuse_next_batch = False
            raise RuntimeError("the database is busy")
        self.batches.append(batch)

    @property
    def taken(self) -> list[tuple[str, str]]:
        return [(m.channel, m.message_id) for batch in self.batches for m in batch]


@pytest.fixture
def quick(monkeypatch):
    """Everything the reader waits for, a great deal shorter."""
    monkeypatch.setattr(tiktok_chat, "CHANNELS_SECONDS", 0.02)
    monkeypatch.setattr(tiktok_chat, "LOOK_SECONDS", 0.05)
    monkeypatch.setattr(tiktok_chat, "AGAIN_SECONDS", 0.01)
    monkeypatch.setattr(tiktok_chat, "RETRY_SECONDS", (0.01, 0.02))
    monkeypatch.setattr(tiktok_chat, "BATCH_SECONDS", 0.05)


@asynccontextmanager
async def running(rig: Rig):
    task = asyncio.create_task(rig.reader.run_forever())
    try:
        yield rig
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def live(tiktok, handle: str = "first", room_id: str = "7600000000000000001"):
    channel = tiktok.by_handle(handle) or tiktok.add_channel(handle, room_id=room_id)
    channel.go_live()
    return channel


class TestReadingAChat:
    async def test_the_chat_of_the_room_a_live_channel_is_in_is_read(self, tiktok, quick):
        live(tiktok, room_id="7696913392682683158")

        async with running(Rig([FIRST])) as rig:
            await async_wait_until(lambda: rig.chats.connections, "a connection")

        chat = rig.chats.latest()
        assert (chat.room_id, chat.handle) == (7696913392682683158, "first")

    async def test_what_is_written_is_handed_on_in_batches_in_order(self, tiktok, quick):
        live(tiktok)

        async with running(Rig([FIRST])) as rig:
            await async_wait_until(lambda: rig.chats.connections, "a connection")
            rig.chats.latest().say(comment(1, "alice", "one"), comment(2, "bob", "two"))
            await async_wait_until(lambda: len(rig.taken) == 2, "the first batch")
            rig.chats.latest().say(comment(3, "carol", "three"))
            await async_wait_until(lambda: len(rig.taken) == 3, "the second")
            assert rig.reader.reading.keys() == {"first@tiktok"}

        assert rig.taken == [("first@tiktok", "1"), ("first@tiktok", "2"), ("first@tiktok", "3")]
        assert [len(batch) for batch in rig.batches] == [2, 1]
        assert rig.batches[0][0].broadcaster_user_id == -1 and rig.batches[0][1].sender == "bob"
        # Read no more once the reader is stopped, and its connection closed.
        assert rig.reader.reading == {} and rig.chats.latest().closed

    async def test_each_channel_has_its_own_chat_read(self, tiktok, quick):
        live(tiktok, "first", "7600000000000000001")
        live(tiktok, "second", "7600000000000000002")

        async with running(Rig([FIRST, SECOND])) as rig:
            await async_wait_until(lambda: len(rig.chats.connections) == 2, "both connections")
            for chat in rig.chats.connections:
                chat.say(comment(chat.room_id % 10, chat.handle, "hi"))
            await async_wait_until(lambda: len(rig.taken) == 2, "both channels' comments")

        assert sorted(rig.taken) == [("first@tiktok", "1"), ("second@tiktok", "2")]

    async def test_a_comment_that_cannot_be_read_is_left_out_and_the_rest_kept(self, tiktok, quick, caplog):
        live(tiktok)
        # TikTokLive handing over something that is not what a comment is.
        broken = object()

        with caplog.at_level(logging.WARNING, logger="kick_clip_hunter"):
            async with running(Rig([FIRST])) as rig:
                await async_wait_until(lambda: rig.chats.connections, "a connection")
                rig.chats.latest().say(broken, comment(2, "bob", "fine"), comment(3, "carol", "   "))
                await async_wait_until(lambda: rig.taken, "the comment that can be read")

        assert rig.taken == [("first@tiktok", "2")]
        assert any("could not be read" in record.getMessage() for record in caplog.records)

    async def test_a_batch_that_cannot_be_taken_in_does_not_stop_the_chat(self, tiktok, quick, caplog):
        live(tiktok)
        rig = Rig([FIRST])
        rig.refuse_next_batch = True

        with caplog.at_level(logging.ERROR, logger="kick_clip_hunter"):
            async with running(rig):
                await async_wait_until(lambda: rig.chats.connections, "a connection")
                rig.chats.latest().say(comment(1, "alice", "lost"))
                await async_wait_until(lambda: not rig.refuse_next_batch, "the batch that is lost")
                rig.chats.latest().say(comment(2, "bob", "kept"))
                await async_wait_until(lambda: rig.taken, "the next comment")

        assert rig.taken == [("first@tiktok", "2")]
        assert any("could not be taken in" in record.getMessage() for record in caplog.records)


class TestWhoseChat:
    async def test_a_channel_that_is_not_live_is_looked_at_until_it_is(self, tiktok, quick):
        channel = tiktok.add_channel("first")

        async with running(Rig([FIRST])) as rig:
            await async_wait_until(lambda: len(tiktok.requests) >= 2, "a few looks")
            assert rig.chats.connections == []
            assert rig.reader.waiting == {"first@tiktok"}
            channel.go_live()
            await async_wait_until(lambda: rig.chats.connections, "a connection once it is live")

        assert rig.reader.waiting == set()

    async def test_a_channel_no_longer_wanted_has_its_connection_closed(self, tiktok, quick):
        live(tiktok)
        rig = Rig([FIRST])

        async with running(rig):
            await async_wait_until(lambda: rig.chats.connections, "a connection")
            rig.channels.clear()
            await async_wait_until(lambda: rig.chats.latest().closed, "the connection closed")
            await async_wait_until(lambda: not rig.reader.reading, "the channel forgotten")

        assert rig.reader.wanted == 0

    async def test_a_channel_wanted_later_is_taken_up(self, tiktok, quick):
        live(tiktok)
        rig = Rig([])

        async with running(rig):
            await asyncio.sleep(0.05)
            assert rig.chats.connections == []
            rig.channels.append(FIRST)
            await async_wait_until(lambda: rig.chats.connections, "a connection")

    async def test_an_account_tiktok_does_not_have_is_said_and_tried_again(self, tiktok, quick):
        async with running(Rig([FIRST])) as rig:
            await async_wait_until(lambda: "first@tiktok" in rig.reader.failing, "the failure")

        assert "no such TikTok account" in rig.reader.failing["first@tiktok"][0]


class TestRidingItOut:
    async def test_a_connection_that_ends_is_made_again_while_the_stream_is_on(self, tiktok, quick, monkeypatch):
        monkeypatch.setattr(tiktok_chat, "STEADY_SECONDS", 0)
        live(tiktok)

        async with running(Rig([FIRST])) as rig:
            await async_wait_until(lambda: rig.chats.connections, "a connection")
            rig.chats.latest().say(comment(1, "alice", "before"))
            rig.chats.latest().end()
            await async_wait_until(lambda: len(rig.chats.connections) == 2, "a second connection")
            rig.chats.latest().say(comment(2, "bob", "after"))
            await async_wait_until(lambda: len(rig.taken) == 2, "both comments")

        assert rig.taken == [("first@tiktok", "1"), ("first@tiktok", "2")]
        assert rig.chats.connections[0].closed

    async def test_a_stream_that_ended_is_waited_for_again(self, tiktok, quick):
        channel = live(tiktok)

        async with running(Rig([FIRST])) as rig:
            await async_wait_until(lambda: rig.chats.connections, "a connection")
            channel.go_offline()
            rig.chats.latest().end()
            await async_wait_until(lambda: rig.reader.waiting == {"first@tiktok"}, "the channel waited for")

        assert len(rig.chats.connections) == 1 and not rig.reader.reading

    async def test_a_connection_that_cannot_be_made_is_tried_again_later(self, tiktok, quick, caplog):
        live(tiktok)
        rig = Rig([FIRST])
        rig.chats.refuse = [RuntimeError("[RATE_LIMIT] Too many requests"), RuntimeError("[RATE_LIMIT] again")]

        with caplog.at_level(logging.WARNING, logger="kick_clip_hunter"):
            async with running(rig):
                await async_wait_until(lambda: rig.chats.connections, "the connection that could be made")
                await async_wait_until(lambda: rig.reader.reading, "the chat read")

        assert rig.reader.failing == {}
        # Said once, however many times it failed in a row.
        warnings = [r.getMessage() for r in caplog.records if "could not be read" in r.getMessage()]
        assert warnings == [
            "tiktok chat: the chat of first@tiktok could not be read (RuntimeError: [RATE_LIMIT] Too many requests)"
        ]

    async def test_failing_connections_are_tried_again_less_and_less_often(self, tiktok, quick, monkeypatch):
        waits: list[float] = []
        real_sleep = asyncio.sleep

        async def sleep(seconds: float, *args, **kwargs):
            waits.append(seconds)
            await real_sleep(0, *args, **kwargs)

        monkeypatch.setattr(tiktok_chat, "RETRY_SECONDS", (1.0, 2.0, 3.0))
        live(tiktok)
        rig = Rig([FIRST])
        rig.chats.refuse = [RuntimeError("no") for _ in range(5)]
        monkeypatch.setattr(tiktok_chat.asyncio, "sleep", sleep)

        async with running(rig):
            await async_wait_until(lambda: rig.chats.connections, "a connection at last")

        assert [wait for wait in waits if wait >= 1.0][:5] == [1.0, 2.0, 3.0, 3.0, 3.0]
