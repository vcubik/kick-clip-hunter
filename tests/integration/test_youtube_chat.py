"""Reading YouTube chat while a channel is live: the real ChatReader against
the fake YouTube (its channels' /live addresses, its chat pages, every look
at a chat).

What matters: a channel's chat is read while it is live and only then; what
was written before the reader came is left out, and everything after is
handed on once, in order; and the reader rides out what YouTube does - a
chat that is turned off, looks that fail, a stream that ends and another
that begins.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

import pytest

from kick_clip_hunter import youtube_chat
from kick_clip_hunter.chat_message import ChatMessage
from kick_clip_hunter.youtube_chat import Channel, ChatReader
from tests.support.waiting import async_wait_until
from tests.support.youtube import message

pytestmark = pytest.mark.anyio

FIRST = Channel(broadcaster_user_id=-1, channel="first@youtube")
SECOND = Channel(broadcaster_user_id=-2, channel="second@youtube")


class Rig:
    """A reader and what it hands on."""

    def __init__(self, channels: list[Channel]) -> None:
        self.channels = channels
        self.batches: list[list[ChatMessage]] = []
        self.refuse_next_batch = False
        # How often the reader has looked at the watchlist.
        self.rounds = 0
        self.reader = ChatReader(self.watched, self.take)

    def watched(self) -> list[Channel]:
        self.rounds += 1
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
    monkeypatch.setattr(youtube_chat, "POLL_SECONDS", (0.01, 0.02))
    monkeypatch.setattr(youtube_chat, "RETRY_SECONDS", (0.01, 0.01))
    monkeypatch.setattr(youtube_chat, "LOOK_SECONDS", 0.05)
    monkeypatch.setattr(youtube_chat, "CHANNELS_SECONDS", 0.05)
    monkeypatch.setattr(youtube_chat, "UNAVAILABLE_SECONDS", 0.2)


@asynccontextmanager
async def running(rig: Rig):
    task = asyncio.create_task(rig.reader.run_forever())
    try:
        yield rig
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def live(youtube, handle: str = "first", video_id: str = "livevideo01"):
    """A channel streaming, and the chat of its stream."""
    channel = youtube.by_handle(handle) or youtube.add_channel(handle)
    channel.go_live(video_id)
    return youtube.chat(video_id)


class TestReadingAChat:
    async def test_what_is_written_while_the_reader_looks_is_handed_on_in_order(self, youtube, quick):
        chat = live(youtube)
        chat.say(message("m-1", "alice", "one"), message("m-2", "bob", "two"))
        chat.say(message("m-3", "carol", "three"))

        async with running(Rig([FIRST])) as rig:
            await async_wait_until(lambda: len(rig.taken) == 3, "the messages")

        assert rig.taken == [("first@youtube", "m-1"), ("first@youtube", "m-2"), ("first@youtube", "m-3")]
        assert [len(batch) for batch in rig.batches] == [2, 1]
        assert {m.broadcaster_user_id for batch in rig.batches for m in batch} == {-1}

    async def test_what_was_written_before_the_reader_came_is_left_out(self, youtube, quick):
        chat = live(youtube)
        chat.history = [message("m-old", "alice", "before")]
        chat.say(message("m-new", "bob", "after"))

        async with running(Rig([FIRST])) as rig:
            await async_wait_until(lambda: rig.taken, "the new message")

        assert rig.taken == [("first@youtube", "m-new")]

    async def test_the_chat_is_read_with_every_message_in_it(self, youtube, quick):
        chat = live(youtube)

        async with running(Rig([FIRST])) as rig:
            await async_wait_until(lambda: len(chat.looks) >= 3, "a few looks at the chat")

        assert {view for view, _, _ in chat.looks} == {"live"}
        assert rig.reader.reading.keys() == {"first@youtube"}

    async def test_each_channel_has_its_own_chat_read(self, youtube, quick):
        live(youtube, "first", "video000001").say(message("a-1", "alice", "hi"))
        live(youtube, "second", "video000002").say(message("b-1", "bob", "hey"))

        async with running(Rig([FIRST, SECOND])) as rig:
            await async_wait_until(lambda: len(rig.taken) == 2, "both channels' messages")

        assert sorted(rig.taken) == [("first@youtube", "a-1"), ("second@youtube", "b-1")]

    async def test_a_batch_that_cannot_be_taken_in_does_not_stop_the_chat(self, youtube, quick, caplog):
        chat = live(youtube)
        chat.say(message("m-1", "alice", "lost"))
        chat.say(message("m-2", "bob", "kept"))
        rig = Rig([FIRST])
        rig.refuse_next_batch = True

        with caplog.at_level(logging.ERROR, logger="kick_clip_hunter"):
            async with running(rig):
                await async_wait_until(lambda: rig.taken, "the next message")

        assert rig.taken == [("first@youtube", "m-2")]
        assert any("could not be taken in" in record.getMessage() for record in caplog.records)


class TestWhileAChannelIsNotLive:
    async def test_no_chat_is_opened_and_it_is_looked_at_until_it_goes_live(self, youtube, quick):
        channel = youtube.add_channel("first")

        async with running(Rig([FIRST])) as rig:
            await async_wait_until(lambda: len(youtube.calls("POST", "resolve_url")) >= 2, "a second look")
            assert youtube.calls("GET", "/live_chat") == []
            assert rig.reader.reading == {}

            channel.go_live("livevideo01")
            youtube.chat("livevideo01").say(message("m-1", "alice", "it's on"))
            await async_wait_until(lambda: rig.taken, "the chat of the stream")

        assert rig.taken == [("first@youtube", "m-1")]

    async def test_a_stream_only_scheduled_is_not_read_yet(self, youtube, quick):
        youtube.add_channel("first").schedule("upcoming001")
        youtube.chat("upcoming001").say(message("m-1", "alice", "waiting"))

        async with running(Rig([FIRST])) as rig:
            await async_wait_until(lambda: len(youtube.calls("POST", "resolve_url")) >= 2, "a second look")

        assert rig.taken == []
        assert youtube.calls("GET", "/live_chat") == []


class TestWhenAChatEnds:
    async def test_the_next_stream_of_the_channel_is_read_once_it_begins(self, youtube, quick):
        channel = youtube.add_channel("first")
        channel.go_live("stream00001")
        first_chat = youtube.chat("stream00001")
        first_chat.say(message("m-1", "alice", "bye"))
        first_chat.ended = True

        async with running(Rig([FIRST])) as rig:
            await async_wait_until(lambda: rig.taken, "the first stream's message")
            await async_wait_until(lambda: "first@youtube" not in rig.reader.reading, "the chat to be over")

            channel.go_offline()
            channel.go_live("stream00002")
            youtube.chat("stream00002").say(message("m-2", "bob", "back"))
            await async_wait_until(lambda: len(rig.taken) == 2, "the second stream's message")

        assert rig.taken == [("first@youtube", "m-1"), ("first@youtube", "m-2")]


class TestWhatYouTubeDoes:
    async def test_a_chat_that_is_turned_off_is_said_and_tried_again_later(self, youtube, quick):
        chat = live(youtube)
        chat.unavailable = "Chat is disabled for this live stream."

        async with running(Rig([FIRST])) as rig:
            await async_wait_until(lambda: rig.reader.unavailable, "the chat to be found turned off")
            assert rig.reader.unavailable == {"first@youtube": "Chat is disabled for this live stream."}
            assert rig.reader.reading == {}

            chat.unavailable = None
            chat.say(message("m-1", "alice", "now it works"))
            await async_wait_until(lambda: rig.taken, "the chat once it is turned on")

        assert rig.reader.unavailable == {}

    async def test_a_look_that_fails_is_tried_again(self, youtube, quick):
        chat = live(youtube)
        chat.say(message("m-1", "alice", "one"))

        async with running(Rig([FIRST])) as rig:
            await async_wait_until(lambda: len(chat.looks) >= 2, "the reader to be reading")
            chat.failing = [503]
            chat.say(message("m-2", "bob", "two"))
            await async_wait_until(lambda: len(rig.taken) == 2, "the message after the failure")

        assert rig.taken == [("first@youtube", "m-1"), ("first@youtube", "m-2")]
        # Not opened again for one failure.
        assert chat.pages_served == 1
        assert rig.reader.failing == {}

    async def test_looks_that_keep_failing_have_the_chat_opened_anew(self, youtube, quick, caplog):
        chat = live(youtube)

        with caplog.at_level(logging.INFO, logger="kick_clip_hunter"):
            async with running(Rig([FIRST])) as rig:
                await async_wait_until(lambda: len(chat.looks) >= 2, "the reader to be reading")
                chat.failing = [503] * (len(youtube_chat.RETRY_SECONDS) + 1)
                await async_wait_until(lambda: chat.pages_served == 2, "the chat to be opened anew")
                chat.say(message("m-1", "alice", "still here"))
                await async_wait_until(lambda: rig.taken, "the message after opening it anew")

        assert any("could not be asked about first@youtube" in r.getMessage() for r in caplog.records)
        assert any("answers about first@youtube again" in r.getMessage() for r in caplog.records)

    async def test_youtube_failing_to_say_whether_a_channel_is_live_is_said_and_ridden_out(self, youtube, quick):
        live(youtube).say(message("m-1", "alice", "hi"))
        youtube.failures["resolve_url"] = 429

        async with running(Rig([FIRST])) as rig:
            await async_wait_until(lambda: rig.reader.failing, "the failure to be said")
            why, _ = rig.reader.failing["first@youtube"]
            assert why == "YouTube answered 429"

            del youtube.failures["resolve_url"]
            await async_wait_until(lambda: rig.taken, "the chat once YouTube answers")

        assert rig.reader.failing == {}
        assert rig.reader.last_heard_at is not None


class TestTheWatchlist:
    async def test_a_channel_added_later_is_taken_up(self, youtube, quick):
        live(youtube, "second", "video000002").say(message("b-1", "bob", "hello"))
        rig = Rig([])

        async with running(rig):
            await asyncio.sleep(0)
            rig.channels.append(SECOND)
            await async_wait_until(lambda: rig.taken, "the new channel's chat")

        assert rig.taken == [("second@youtube", "b-1")]
        assert rig.reader.watched == 1

    async def test_a_channel_taken_off_the_watchlist_is_read_no_more(self, youtube, quick):
        chat = live(youtube)
        rig = Rig([FIRST])

        async with running(rig):
            await async_wait_until(lambda: rig.reader.reading, "the chat to be read")
            rig.channels.clear()
            await async_wait_until(lambda: not rig.reader.reading, "the reader to let it go")
            looked, rounds = len(chat.looks), rig.rounds
            chat.say(message("m-1", "alice", "nobody reads this"))
            # Long enough for a reader still looking to have looked many times.
            await async_wait_until(lambda: rig.rounds >= rounds + 2, "two more looks at the watchlist")

        assert rig.taken == []
        assert len(chat.looks) <= looked + 1

    async def test_a_watchlist_that_cannot_be_read_is_tried_again(self, youtube, quick, caplog):
        live(youtube).say(message("m-1", "alice", "hi"))
        calls = []

        def channels():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("database is locked")
            return [FIRST]

        rig = Rig([])
        rig.reader = ChatReader(channels, rig.take)

        with caplog.at_level(logging.ERROR, logger="kick_clip_hunter"):
            async with running(rig):
                await async_wait_until(lambda: rig.taken, "the chat after the watchlist could be read")

        assert any("could not tell whose chat to read" in r.getMessage() for r in caplog.records)
