"""The loop that decides, channel by channel, whether to record right now.

The rule it implements matters operationally: asking for a stream URL opens
a visible browser window, so a recorder must only ever be ticked for a
channel the (browser-free) official API says is live.

Recorders are replaced by stand-ins that count their calls; the "is it live"
question goes through the real client code against the fake Kick API.

The same loop keeps a channel recorded while its stream is being kept whole,
and reports when such a recording is over - and, for a channel that is
recorded always, when its stream is on with no recording of it.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kick_clip_hunter import recording_manager, stream_recording
from kick_clip_hunter.platforms import StreamUrlError
from kick_clip_hunter.recorder import PRE_ROLL_SECONDS, ChannelRecorder, RunState
from tests.support.waiting import async_wait_until

pytestmark = pytest.mark.anyio


class StandInRecorder:
    def __init__(self, active: bool = False, tick_error: Exception | None = None) -> None:
        self.ticks = 0
        self.stops = 0
        self.active = active
        self.tick_error = tick_error
        # How far back it was told to keep its buffer, with how many ticks
        # it had had by then.
        self.kept: list[tuple[float, int]] = []

    def keep(self, seconds: float) -> None:
        self.kept.append((seconds, self.ticks))

    def tick(self) -> None:
        self.ticks += 1
        if self.tick_error is not None:
            raise self.tick_error
        self.active = True

    def stop(self) -> None:
        self.stops += 1
        self.active = False

    @property
    def is_active(self) -> bool:
        return self.active


@pytest.fixture
def recorders(monkeypatch) -> dict[str, StandInRecorder]:
    """The manager's recorder registry, pre-populated by the test."""
    monkeypatch.setattr(recording_manager, "TICK_INTERVAL_SECONDS", 0.01)
    registry: dict[str, StandInRecorder] = {}
    monkeypatch.setattr(recording_manager, "_recorders", registry)
    return registry


async def run_loop_until(
    condition, what: str, *, slugs, enabled=lambda: True, tracked=lambda slug: True, chat_only=lambda slug: False
) -> None:
    """Runs the real loop until `condition` holds, then cancels it."""
    task = asyncio.create_task(
        recording_manager.run_forever(lambda: list(slugs), enabled, tracked, chat_only=chat_only)
    )
    try:
        await async_wait_until(condition, what)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def run_a_few_rounds(kick_api, **kwargs) -> None:
    """Long enough for every channel to have been considered several times."""
    rounds = len(kick_api.calls("GET", "/channels")) + 3 * max(len(kwargs["slugs"]), 1)
    task = asyncio.create_task(
        recording_manager.run_forever(
            lambda: list(kwargs["slugs"]),
            kwargs.get("enabled", lambda: True),
            kwargs.get("tracked", lambda slug: True),
            chat_only=kwargs.get("chat_only", lambda slug: False),
        )
    )
    try:
        # A disabled loop makes no API calls at all, so fall back to time.
        with suppress(AssertionError):
            await async_wait_until(lambda: len(kick_api.calls("GET", "/channels")) >= rounds, "rounds", timeout=0.3)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


class TestWhichChannelsGetRecorded:
    async def test_a_live_channel_is_ticked(self, kick_api, recorders):
        kick_api.add_channel("live_one", 1, is_live=True)
        recorders["live_one"] = StandInRecorder()

        await run_loop_until(lambda: recorders["live_one"].ticks >= 2, "two ticks", slugs=["live_one"])

        assert recorders["live_one"].stops == 0

    async def test_an_offline_channel_is_never_ticked(self, kick_api, recorders):
        # A tick would open a browser window; offline channels must cost
        # nothing more than the API call.
        kick_api.add_channel("offline_one", 2, is_live=False)
        recorders["offline_one"] = StandInRecorder()

        await run_a_few_rounds(kick_api, slugs=["offline_one"])

        assert recorders["offline_one"].ticks == 0
        assert len(kick_api.calls("GET", "/channels")) >= 2

    async def test_a_live_twitch_channel_is_ticked_and_an_offline_one_is_not(self, twitch_api, recorders):
        twitch_api.add_channel("live_one", "1", is_live=True)
        twitch_api.add_channel("offline_one", "2", is_live=False)
        recorders["live_one@twitch"] = StandInRecorder()
        recorders["offline_one@twitch"] = StandInRecorder()

        await run_loop_until(
            lambda: recorders["live_one@twitch"].ticks >= 2 and len(twitch_api.calls("GET", "/helix/streams")) >= 4,
            "two ticks of the live channel",
            slugs=["live_one@twitch", "offline_one@twitch"],
        )

        assert recorders["offline_one@twitch"].ticks == 0
        assert {query["user_login"] for query in twitch_api.calls("GET", "/helix/streams")} == {
            "live_one",
            "offline_one",
        }

    async def test_a_recorder_is_stopped_when_its_channel_goes_offline(self, kick_api, recorders):
        channel = kick_api.add_channel("was_live", 3, is_live=True)
        recorders["was_live"] = StandInRecorder()
        await run_loop_until(lambda: recorders["was_live"].is_active, "recording to start", slugs=["was_live"])

        channel.is_live = False
        await run_loop_until(lambda: recorders["was_live"].stops == 1, "recording to stop", slugs=["was_live"])

        assert recorders["was_live"].is_active is False

    async def test_an_idle_recorder_of_an_offline_channel_is_not_stopped_over_and_over(self, kick_api, recorders):
        kick_api.add_channel("offline_one", 2, is_live=False)
        recorders["offline_one"] = StandInRecorder(active=False)

        await run_a_few_rounds(kick_api, slugs=["offline_one"])

        assert recorders["offline_one"].stops == 0

    async def test_only_the_live_channels_of_a_watchlist_are_ticked(self, kick_api, recorders):
        kick_api.add_channel("live_one", 1, is_live=True)
        kick_api.add_channel("offline_one", 2, is_live=False)
        kick_api.add_channel("live_two", 3, is_live=True)
        for slug in ("live_one", "offline_one", "live_two"):
            recorders[slug] = StandInRecorder()

        await run_loop_until(
            lambda: recorders["live_one"].ticks and recorders["live_two"].ticks,
            "both live channels to be ticked",
            slugs=["live_one", "offline_one", "live_two"],
        )

        assert recorders["offline_one"].ticks == 0

    async def test_a_channel_added_to_the_watchlist_later_is_picked_up(self, kick_api, recorders):
        kick_api.add_channel("first", 1, is_live=True)
        kick_api.add_channel("added_later", 2, is_live=True)
        recorders["first"] = StandInRecorder()
        recorders["added_later"] = StandInRecorder()
        watchlist = ["first"]
        task = asyncio.create_task(
            recording_manager.run_forever(lambda: list(watchlist), lambda: True, lambda slug: True)
        )
        try:
            await async_wait_until(lambda: recorders["first"].ticks, "the first channel")
            assert recorders["added_later"].ticks == 0

            watchlist.append("added_later")

            await async_wait_until(lambda: recorders["added_later"].ticks, "the new channel")
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


class TestHowMuchOfAStreamIsKept:
    async def run_until(self, condition, what: str, slugs, buffer_seconds) -> None:
        task = asyncio.create_task(
            recording_manager.run_forever(
                lambda: list(slugs), lambda: True, lambda slug: True, buffer_seconds=buffer_seconds
            )
        )
        try:
            await async_wait_until(condition, what)
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def test_a_recorder_is_told_its_channels_reach_before_every_tick(self, kick_api, recorders):
        kick_api.add_channel("short", 1, is_live=True)
        kick_api.add_channel("long", 2, is_live=True)
        recorders["short"] = StandInRecorder()
        recorders["long"] = StandInRecorder()
        reaches = {"short": 200.0, "long": 500.0}

        await self.run_until(
            lambda: recorders["short"].ticks >= 2 and recorders["long"].ticks >= 2,
            "two ticks of each",
            ["short", "long"],
            reaches.get,
        )

        assert recorders["short"].kept[:2] == [(200.0, 0), (200.0, 1)]
        assert recorders["long"].kept[:2] == [(500.0, 0), (500.0, 1)]

    async def test_an_offline_channels_recorder_is_not_told(self, kick_api, recorders):
        kick_api.add_channel("live_one", 1, is_live=True)
        kick_api.add_channel("offline_one", 2, is_live=False)
        recorders["live_one"] = StandInRecorder()
        recorders["offline_one"] = StandInRecorder()

        await self.run_until(
            lambda: recorders["live_one"].ticks >= 2, "two ticks", ["live_one", "offline_one"], lambda slug: 200.0
        )

        assert recorders["offline_one"].kept == []

    async def test_settings_that_cannot_be_read_do_not_cost_the_tick(self, kick_api, recorders, caplog):
        kick_api.add_channel("live_one", 1, is_live=True)
        recorders["live_one"] = StandInRecorder()

        def unreadable(slug: str) -> float:
            raise RuntimeError("database is locked")

        with caplog.at_level(logging.ERROR, logger="kick_clip_hunter"):
            await self.run_until(lambda: recorders["live_one"].ticks >= 2, "two ticks", ["live_one"], unreadable)

        assert recorders["live_one"].kept == []
        assert any(
            "[live_one] could not tell how much of its stream to keep" in record.getMessage()
            for record in caplog.records
        )
        assert not any("recording tick failed" in record.getMessage() for record in caplog.records)


class TestPausing:
    async def test_with_watching_off_nothing_is_checked_or_ticked(self, kick_api, recorders):
        kick_api.add_channel("live_one", 1, is_live=True)
        recorders["live_one"] = StandInRecorder()

        await run_a_few_rounds(kick_api, slugs=["live_one"], enabled=lambda: False)

        assert recorders["live_one"].ticks == 0
        assert kick_api.requests == []

    async def test_turning_watching_off_stops_running_recorders(self, kick_api, recorders):
        kick_api.add_channel("live_one", 1, is_live=True)
        recorders["live_one"] = StandInRecorder(active=True)
        recorders["idle"] = StandInRecorder(active=False)

        await run_loop_until(
            lambda: recorders["live_one"].stops, "the recorder to stop", slugs=["live_one"], enabled=lambda: False
        )

        assert recorders["live_one"].is_active is False
        assert recorders["idle"].stops == 0

    async def test_a_channel_with_tracking_off_is_skipped_and_stopped(self, kick_api, recorders):
        kick_api.add_channel("paused", 1, is_live=True)
        kick_api.add_channel("tracked", 2, is_live=True)
        recorders["paused"] = StandInRecorder(active=True)
        recorders["tracked"] = StandInRecorder()

        await run_loop_until(
            lambda: recorders["tracked"].ticks >= 2 and recorders["paused"].stops,
            "the tracked channel to tick and the paused one to stop",
            slugs=["paused", "tracked"],
            tracked=lambda slug: slug != "paused",
        )

        assert recorders["paused"].ticks == 0
        assert recorders["paused"].stops == 1
        # Not even the liveness check is spent on a paused channel.
        assert {query["slug"] for query in kick_api.calls("GET", "/channels")} == {"tracked"}

    async def test_a_channel_tracked_for_its_chat_alone_is_looked_at_and_never_recorded(self, kick_api, recorders):
        kick_api.add_channel("chatty", 1, is_live=True)
        kick_api.add_channel("tracked", 2, is_live=True)
        recorders["chatty"] = StandInRecorder(active=True)
        recorders["tracked"] = StandInRecorder()

        await run_loop_until(
            lambda: recorders["tracked"].ticks >= 2 and recorders["chatty"].stops,
            "the tracked channel to tick and the other to stop",
            slugs=["chatty", "tracked"],
            tracked=lambda slug: slug == "tracked",
            chat_only=lambda slug: slug == "chatty",
        )

        assert recorders["chatty"].ticks == 0
        # Whether it is live is what the dashboard, the Health page and the
        # bot go by.
        assert recording_manager.looks()["chatty"].live_since is not None

    async def test_with_watching_off_a_channel_tracked_for_its_chat_alone_is_not_looked_at(self, kick_api, recorders):
        kick_api.add_channel("chatty", 1, is_live=True)
        recorders["chatty"] = StandInRecorder()

        await run_a_few_rounds(kick_api, slugs=["chatty"], enabled=lambda: False, chat_only=lambda slug: True)

        assert kick_api.calls("GET", "/channels") == []
        assert "chatty" not in recording_manager.looks()


class TestOneChannelFailing:
    async def test_does_not_stop_the_others(self, kick_api, recorders, caplog):
        kick_api.add_channel("broken", 1, is_live=True)
        kick_api.add_channel("healthy", 2, is_live=True)
        recorders["broken"] = StandInRecorder(tick_error=RuntimeError("disk full"))
        recorders["healthy"] = StandInRecorder()

        with caplog.at_level(logging.ERROR, logger="kick_clip_hunter"):
            await run_loop_until(
                lambda: recorders["healthy"].ticks >= 2 and recorders["broken"].ticks >= 2,
                "both channels to be attempted twice",
                slugs=["broken", "healthy"],
            )

        assert any("[broken] recording tick failed" in record.getMessage() for record in caplog.records)

    async def test_an_api_error_for_one_channel_is_survived(self, kick_api, recorders, caplog):
        kick_api.add_channel("healthy", 2, is_live=True)
        recorders["healthy"] = StandInRecorder()
        recorders["unknown_to_kick"] = StandInRecorder()

        with caplog.at_level(logging.ERROR, logger="kick_clip_hunter"):
            await run_loop_until(
                lambda: recorders["healthy"].ticks >= 2, "the healthy channel", slugs=["unknown_to_kick", "healthy"]
            )

        assert recorders["unknown_to_kick"].ticks == 0
        assert any("[unknown_to_kick] recording tick failed" in record.getMessage() for record in caplog.records)

    async def test_a_stream_ending_between_the_check_and_the_tick_is_not_an_error(self, kick_api, recorders, caplog):
        kick_api.add_channel("just_ended", 1, is_live=True)
        recorders["just_ended"] = StandInRecorder(tick_error=StreamUrlError("No live stream URL captured"))

        with caplog.at_level(logging.INFO, logger="kick_clip_hunter"):
            await run_loop_until(lambda: recorders["just_ended"].ticks >= 2, "two attempts", slugs=["just_ended"])

        messages = [record.getMessage() for record in caplog.records]
        assert any("stream ended between the is_live check and recording" in message for message in messages)
        assert not any(record.levelno >= logging.ERROR for record in caplog.records)


class TestRecorderRegistry:
    def test_each_channel_gets_one_recorder_that_is_reused(self):
        first = recording_manager._get_recorder("channel_a")
        again = recording_manager._get_recorder("channel_a")
        other = recording_manager._get_recorder("channel_b")

        assert isinstance(first, ChannelRecorder)
        assert again is first
        assert other is not first
        assert other.channel_slug == "channel_b"

    async def test_stop_all_stops_exactly_the_running_ones(self, recorders):
        recorders["running"] = StandInRecorder(active=True)
        recorders["idle"] = StandInRecorder(active=False)

        await recording_manager.stop_all()

        assert recorders["running"].stops == 1
        assert recorders["idle"].stops == 0

    def test_the_channels_being_recorded_are_the_ones_whose_recorder_is_running(self, recorders):
        recorders["running"] = StandInRecorder(active=True)
        recorders["idle"] = StandInRecorder(active=False)
        recorders["also_running"] = StandInRecorder(active=True)

        assert recording_manager.recording_channels() == ["running", "also_running"]

    def test_nothing_is_being_recorded_before_any_channel_was_looked_at(self, recorders):
        assert recording_manager.recording_channels() == []


class TestClipRequests:
    START = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    END = START + timedelta(seconds=10)

    async def test_a_clip_is_cut_from_that_channels_buffer_with_the_standard_pre_roll(self, monkeypatch):
        calls = []

        def extract_clip(recorder, start, end, name, pre_roll, post_roll):
            calls.append((recorder.channel_slug, start, end, name, pre_roll, post_roll))
            return Path("data/clips/channel_a") / name

        monkeypatch.setattr(recording_manager, "extract_clip", extract_clip)

        path = await recording_manager.create_clip_for_moment(
            "channel_a", self.START, self.END, "moment_1.mp4", post_roll_seconds=7
        )

        assert path == Path("data/clips/channel_a/moment_1.mp4")
        assert calls == [("channel_a", self.START, self.END, "moment_1.mp4", PRE_ROLL_SECONDS, 7)]

    async def test_a_channels_own_framing_is_handed_on_to_the_cut(self, monkeypatch):
        calls = []

        def extract_clip(recorder, start, end, name, pre_roll, post_roll, **framing):
            calls.append((pre_roll, post_roll, framing))
            return Path("data/clips/channel_a") / name

        monkeypatch.setattr(recording_manager, "extract_clip", extract_clip)

        await recording_manager.create_clip_for_moment(
            "channel_a",
            self.START,
            self.END,
            "moment_1.mp4",
            pre_roll_seconds=20,
            post_roll_seconds=3,
            playback_delay_seconds=4,
            context_before_seconds=0,
            context_after_seconds=90,
        )

        assert calls == [
            (20, 3, {"playback_delay_seconds": 4, "context_before_seconds": 0, "context_after_seconds": 90})
        ]


@asynccontextmanager
async def looping(
    slugs, *, enabled=lambda: True, tracked=lambda slug: True, over=None, always=lambda: (), stream_is_on=None
):
    """The real loop, running for as long as the block does."""
    task = asyncio.create_task(
        recording_manager.run_forever(lambda: list(slugs), enabled, tracked, over, always, stream_is_on)
    )
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def rounds(kick_api, count: int = 3) -> None:
    """Waits for the loop to have asked Kick about a channel that many more
    times - at least that many rounds, with one channel being looked at."""
    asked = len(kick_api.calls("GET", "/channels")) + count
    await async_wait_until(lambda: len(kick_api.calls("GET", "/channels")) >= asked, f"{count} more rounds")


class TestStreamsKeptWhole:
    """A stream that is being kept whole (stream_recording.py) is recorded
    whatever the switches that pause clip tracking say, and the loop is what
    notices that such a recording is over."""

    BEGAN = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def keep(self, slug: str, broadcaster_user_id: int = 1, recording_id: int = 1):
        return recording_manager.start_stream_recording(slug, broadcaster_user_id, recording_id)

    def footage(self, kept, size: int = 7) -> None:
        kept.writer.add(self.BEGAN, 2.0, "7-0", b"x" * size)

    @pytest.fixture
    def over(self) -> list[tuple[str, str]]:
        """What the loop reported, in order."""
        return []

    @pytest.fixture
    def told(self, over):
        """Whoever is told ends the recording, as the service does."""

        async def note(slug: str, why: str) -> None:
            over.append((slug, why))
            recording_manager.end_stream_recording(slug)

        return note

    async def test_it_is_recorded_though_its_channel_is_not_tracked(self, kick_api, recorders):
        kick_api.add_channel("kept", 1, is_live=True)
        recorders["kept"] = StandInRecorder()
        self.keep("kept")

        async with looping(["kept"], tracked=lambda slug: False):
            await async_wait_until(lambda: recorders["kept"].ticks >= 2, "two ticks")

        assert recorders["kept"].stops == 0

    async def test_it_is_recorded_though_watching_is_off_and_nothing_else_is(self, kick_api, recorders):
        kick_api.add_channel("kept", 1, is_live=True)
        kick_api.add_channel("other", 2, is_live=True)
        recorders["kept"] = StandInRecorder()
        recorders["other"] = StandInRecorder(active=True)
        self.keep("kept")

        async with looping(["kept", "other"], enabled=lambda: False):
            await async_wait_until(
                lambda: recorders["kept"].ticks >= 2 and recorders["other"].stops, "one to tick, the other to stop"
            )

        assert recorders["other"].ticks == 0
        assert recorders["kept"].stops == 0
        # Not even the liveness check is spent on a channel nobody looks at.
        assert {query["slug"] for query in kick_api.calls("GET", "/channels")} == {"kept"}

    async def test_a_channel_that_is_not_live_yet_is_waited_for(self, kick_api, recorders):
        channel = kick_api.add_channel("kept", 1, is_live=False)
        recorders["kept"] = StandInRecorder()
        self.keep("kept")

        async with looping(["kept"], tracked=lambda slug: False):
            await rounds(kick_api)
            assert recorders["kept"].ticks == 0  # no browser for an offline channel

            channel.is_live = True
            await async_wait_until(lambda: recorders["kept"].ticks, "the recording to start with the stream")

        assert recording_manager.stream_recording_of("kept") is not None

    async def test_once_it_is_over_a_channel_that_is_not_tracked_is_let_go(self, kick_api, recorders):
        kick_api.add_channel("kept", 1, is_live=True)
        recorders["kept"] = StandInRecorder()
        self.keep("kept")

        async with looping(["kept"], tracked=lambda slug: False):
            await async_wait_until(lambda: recorders["kept"].is_active, "the recording to start")
            recording_manager.end_stream_recording("kept")
            await async_wait_until(lambda: recorders["kept"].stops == 1, "the recorder to be stopped")

    async def test_a_tracked_channel_goes_on_being_recorded_for_its_clips(self, kick_api, recorders):
        kick_api.add_channel("kept", 1, is_live=True)
        recorders["kept"] = StandInRecorder()
        self.keep("kept")

        async with looping(["kept"]):
            await async_wait_until(lambda: recorders["kept"].is_active, "the recording to start")
            recording_manager.end_stream_recording("kept")
            await rounds(kick_api)

        assert recorders["kept"].stops == 0

    async def test_it_is_recorded_even_if_the_watchlist_no_longer_has_its_channel(self, kick_api, recorders):
        kick_api.add_channel("kept", 1, is_live=True)
        recorders["kept"] = StandInRecorder()
        self.keep("kept")

        async with looping([]):
            await async_wait_until(lambda: recorders["kept"].ticks >= 2, "two ticks")

    async def test_asking_for_one_does_not_wait_for_the_next_round(self, kick_api, recorders, monkeypatch):
        # Rounds are half a minute apart, which is half a minute of the
        # stream lost if a recording had to wait for the next one.
        monkeypatch.setattr(recording_manager, "TICK_INTERVAL_SECONDS", 3600)
        kick_api.add_channel("other", 1, is_live=True)
        kick_api.add_channel("kept", 2, is_live=True)
        recorders["other"] = StandInRecorder()
        recorders["kept"] = StandInRecorder()

        async with looping(["other", "kept"], tracked=lambda slug: slug == "other"):
            await async_wait_until(lambda: recorders["other"].ticks, "the first round")
            assert recorders["kept"].ticks == 0

            self.keep("kept", 2)

            await async_wait_until(lambda: recorders["kept"].ticks, "a round straight away")

    def test_starting_one_gives_the_channels_recorder_its_writer(self, recorders):
        recorders["kept"] = StandInRecorder()

        kept = self.keep("kept", broadcaster_user_id=41, recording_id=7)

        assert recorders["kept"].whole_stream is kept.writer
        assert kept.writer.folder == stream_recording.parts_folder("kept", 7) and kept.writer.folder.is_dir()
        assert recording_manager.stream_recording_of("kept") is kept
        assert recording_manager.stream_recordings() == {"kept": kept}
        assert recording_manager.keeps_chat_of(41) is True
        assert recording_manager.keeps_chat_of(42) is False

    def test_ending_one_takes_the_writer_away_and_closes_it(self, recorders):
        recorders["kept"] = StandInRecorder()
        kept = self.keep("kept")
        self.footage(kept)

        ended = recording_manager.end_stream_recording("kept")
        ended.writer.add(self.BEGAN + timedelta(seconds=2), 2.0, "7-0", b"too late")

        assert ended is kept
        assert recorders["kept"].whole_stream is None
        assert kept.writer.size == 7
        assert recording_manager.stream_recording_of("kept") is None
        assert recording_manager.keeps_chat_of(1) is False
        assert recording_manager.end_stream_recording("kept") is None

    async def test_a_stream_that_has_been_gone_for_a_while_ends_its_recording(
        self, kick_api, recorders, monkeypatch, over, told
    ):
        monkeypatch.setattr(recording_manager, "STREAM_END_GRACE_SECONDS", 0.05)
        kick_api.add_channel("kept", 1, is_live=False)
        recorders["kept"] = StandInRecorder()
        self.footage(self.keep("kept"))

        async with looping(["kept"], over=told):
            await async_wait_until(lambda: over, "the recording to be reported over")

        assert over == [("kept", stream_recording.STREAM_ENDED)]

    async def test_until_then_it_waits_for_the_stream_to_come_back(self, kick_api, recorders, over, told):
        kick_api.add_channel("kept", 1, is_live=False)
        recorders["kept"] = StandInRecorder()
        self.footage(self.keep("kept"))

        async with looping(["kept"], over=told):
            await rounds(kick_api)

        assert over == []

    async def test_one_started_ahead_of_its_stream_waits_for_as_long_as_it_takes(
        self, kick_api, recorders, monkeypatch, over, told
    ):
        monkeypatch.setattr(recording_manager, "STREAM_END_GRACE_SECONDS", 0)
        kick_api.add_channel("kept", 1, is_live=False)
        recorders["kept"] = StandInRecorder()
        self.keep("kept")

        async with looping(["kept"], over=told):
            await rounds(kick_api)

        assert over == []

    async def test_footage_that_can_no_longer_be_written_ends_it(self, kick_api, recorders, over, told):
        kick_api.add_channel("kept", 1, is_live=True)
        recorders["kept"] = StandInRecorder()
        self.keep("kept").writer.failed = True

        async with looping(["kept"], over=told):
            await async_wait_until(lambda: over, "the recording to be reported over")

        assert over == [("kept", stream_recording.WRITE_FAILED)]

    async def test_a_disk_running_out_ends_every_recording_while_they_can_still_be_saved(
        self, kick_api, recorders, monkeypatch, over, told
    ):
        # Saving takes as much room again as a recording has kept.
        room = recording_manager.MIN_FREE_BYTES + 100
        monkeypatch.setattr(stream_recording, "free_bytes", lambda: room)
        for user_id, slug in enumerate(("first", "second"), start=1):
            kick_api.add_channel(slug, user_id, is_live=True)
            recorders[slug] = StandInRecorder()
        first, second = self.keep("first", 1, 1), self.keep("second", 2, 2)
        self.footage(first, size=60)

        async with looping(["first", "second"], over=told):
            await rounds(kick_api, 4)
            assert over == []

            self.footage(second, size=41)
            await async_wait_until(lambda: len(over) == 2, "both recordings to be reported over")

        assert sorted(over) == [("first", stream_recording.DISK_FULL), ("second", stream_recording.DISK_FULL)]

    def test_a_recording_that_is_being_saved_still_has_its_room_spoken_for(self, monkeypatch):
        monkeypatch.setattr(stream_recording, "free_bytes", lambda: 1000)
        self.footage(self.keep("kept"), size=100)

        recording_manager.hold_room(5, 300)
        assert recording_manager.room_left() == 600

        recording_manager.release_room(5)
        recording_manager.release_room(5)
        assert recording_manager.room_left() == 900

    async def test_with_nobody_to_tell_nothing_is_ended(self, kick_api, recorders):
        kick_api.add_channel("kept", 1, is_live=True)
        recorders["kept"] = StandInRecorder()
        self.keep("kept").writer.failed = True

        async with looping(["kept"]):
            await rounds(kick_api)

        assert recording_manager.stream_recording_of("kept") is not None

    async def test_failing_to_look_at_the_disk_does_not_stop_the_loop(
        self, kick_api, recorders, monkeypatch, caplog, over, told
    ):
        def unreadable() -> int:
            raise OSError("the disk is gone")

        monkeypatch.setattr(stream_recording, "free_bytes", unreadable)
        kick_api.add_channel("kept", 1, is_live=True)
        recorders["kept"] = StandInRecorder()
        self.keep("kept")

        with caplog.at_level(logging.ERROR, logger="kick_clip_hunter"):
            async with looping(["kept"], over=told):
                await async_wait_until(lambda: recorders["kept"].ticks >= 3, "the loop to carry on")

        assert over == []
        assert any("could not check on the stream recordings" in record.getMessage() for record in caplog.records)

    async def test_failing_to_end_one_does_not_stop_the_loop(self, kick_api, recorders, caplog):
        kick_api.add_channel("kept", 1, is_live=True)
        recorders["kept"] = StandInRecorder()
        self.keep("kept").writer.failed = True

        async def explode(slug: str, why: str) -> None:
            raise RuntimeError("database is locked")

        with caplog.at_level(logging.ERROR, logger="kick_clip_hunter"):
            async with looping(["kept"], over=explode):
                await async_wait_until(lambda: recorders["kept"].ticks >= 3, "the loop to carry on")

        assert any("[kept] could not end the stream recording" in record.getMessage() for record in caplog.records)


class TestChannelsRecordedAlways:
    """A channel can be set to have every stream kept whole. The loop looks
    at whether it is live whatever the tracking switches say, and tells
    whoever starts recordings when it is and nothing is keeping its stream."""

    @pytest.fixture
    def seen_on(self) -> list[str]:
        """The channels the loop reported as live and not recorded, in order."""
        return []

    @pytest.fixture
    def starts(self, seen_on):
        """Whoever is told starts a recording, as the service does."""

        async def start(slug: str) -> None:
            seen_on.append(slug)
            recording_manager.start_stream_recording(slug, 1, len(seen_on))

        return start

    @pytest.fixture
    def declines(self, seen_on):
        """Whoever is told starts nothing."""

        async def note(slug: str) -> None:
            seen_on.append(slug)

        return note

    async def test_its_stream_is_reported_once_it_is_on(self, kick_api, recorders, seen_on, starts):
        channel = kick_api.add_channel("always", 1, is_live=False)
        recorders["always"] = StandInRecorder()

        async with looping(["always"], always=lambda: ["always"], stream_is_on=starts):
            await rounds(kick_api)
            assert seen_on == []
            assert recorders["always"].ticks == 0  # no browser for an offline channel

            channel.is_live = True
            await async_wait_until(lambda: recorders["always"].ticks >= 2, "the stream to be recorded")

        # Once: from then on its stream is being kept.
        assert seen_on == ["always"]

    async def test_what_is_started_for_it_gets_the_same_rounds_tick(self, kick_api, recorders, starts, monkeypatch):
        # Not the next round's, half a minute of the stream later.
        monkeypatch.setattr(recording_manager, "TICK_INTERVAL_SECONDS", 3600)
        kick_api.add_channel("always", 1, is_live=True)
        recorders["always"] = StandInRecorder()

        async with looping([], tracked=lambda slug: False, always=lambda: ["always"], stream_is_on=starts):
            await async_wait_until(lambda: recorders["always"].ticks, "the recorder to be ticked")

    async def test_tracking_has_no_say_in_it(self, kick_api, recorders, seen_on, starts):
        kick_api.add_channel("always", 1, is_live=True)
        recorders["always"] = StandInRecorder()

        async with looping(["always"], tracked=lambda slug: False, always=lambda: ["always"], stream_is_on=starts):
            await async_wait_until(lambda: recorders["always"].ticks >= 2, "the stream to be recorded")

        assert seen_on == ["always"]
        assert recorders["always"].stops == 0

    async def test_nor_has_the_watching_switch(self, kick_api, recorders, seen_on, starts):
        kick_api.add_channel("always", 1, is_live=True)
        kick_api.add_channel("other", 2, is_live=True)
        recorders["always"] = StandInRecorder()
        recorders["other"] = StandInRecorder(active=True)

        async with looping(["always", "other"], enabled=lambda: False, always=lambda: ["always"], stream_is_on=starts):
            await async_wait_until(
                lambda: recorders["always"].ticks >= 2 and recorders["other"].stops, "one to tick, the other to stop"
            )

        assert seen_on == ["always"]
        assert recorders["other"].ticks == 0
        # Not even the liveness check is spent on a channel nobody looks at.
        assert {query["slug"] for query in kick_api.calls("GET", "/channels")} == {"always"}

    async def test_a_channel_that_is_being_recorded_already_is_not_reported(self, kick_api, recorders, seen_on, starts):
        kick_api.add_channel("always", 1, is_live=True)
        recorders["always"] = StandInRecorder()
        recording_manager.start_stream_recording("always", 1, 7)

        async with looping(["always"], always=lambda: ["always"], stream_is_on=starts):
            await async_wait_until(lambda: recorders["always"].ticks >= 3, "a few rounds")

        assert seen_on == []
        assert recording_manager.stream_recording_of("always").recording_id == 7

    async def test_if_nothing_is_started_for_it_no_browser_is_opened(self, kick_api, recorders, seen_on, declines):
        # Its stream is wanted whole or not at all: a channel that is not
        # tracked has nothing else to be recorded for.
        kick_api.add_channel("always", 1, is_live=True)
        recorders["always"] = StandInRecorder()

        async with looping(["always"], tracked=lambda slug: False, always=lambda: ["always"], stream_is_on=declines):
            await async_wait_until(lambda: len(seen_on) >= 3, "a few rounds")

        assert recorders["always"].ticks == 0

    async def test_a_channel_that_is_held_off_is_left_alone_until_its_stream_is_over(
        self, kick_api, recorders, seen_on, starts
    ):
        # Its recording was stopped while the stream was on.
        channel = kick_api.add_channel("always", 1, is_live=True)
        recorders["always"] = StandInRecorder(active=True)
        recording_manager.hold_off("always")

        async with looping(["always"], tracked=lambda slug: False, always=lambda: ["always"], stream_is_on=starts):
            await rounds(kick_api)
            assert seen_on == []
            # With nothing to record it for, the channel's recorder is stopped.
            assert (recorders["always"].ticks, recorders["always"].stops) == (0, 1)

            # The stream ends, and the next one is recorded again.
            channel.is_live = False
            await async_wait_until(lambda: not recording_manager.is_held_off("always"), "the stream to be seen off")
            assert seen_on == []
            channel.is_live = True
            await async_wait_until(lambda: recorders["always"].ticks, "the next stream to be recorded")

        assert seen_on == ["always"]

    async def test_a_tracked_channel_that_is_held_off_is_still_recorded_for_its_clips(
        self, kick_api, recorders, seen_on, starts
    ):
        kick_api.add_channel("always", 1, is_live=True)
        recorders["always"] = StandInRecorder()
        recording_manager.hold_off("always")

        async with looping(["always"], always=lambda: ["always"], stream_is_on=starts):
            await async_wait_until(lambda: recorders["always"].ticks >= 2, "two ticks")

        assert seen_on == []
        assert recording_manager.stream_recording_of("always") is None

    async def test_holding_off_can_be_called_off(self):
        recording_manager.hold_off("always")
        assert recording_manager.is_held_off("always")

        recording_manager.stop_holding_off("always")
        recording_manager.stop_holding_off("never_held_off")

        assert not recording_manager.is_held_off("always")

    async def test_a_channel_that_is_no_longer_recorded_always_is_let_go(self, kick_api, recorders, seen_on, declines):
        kick_api.add_channel("always", 1, is_live=True)
        kick_api.add_channel("tracked", 2, is_live=True)
        recorders["always"] = StandInRecorder()
        recorders["tracked"] = StandInRecorder()
        always = ["always"]

        async def more_rounds(count: int) -> None:
            # A tracked channel is ticked once a round.
            ticks = recorders["tracked"].ticks + count
            await async_wait_until(lambda: recorders["tracked"].ticks >= ticks, f"{count} more rounds")

        async with looping(["tracked"], always=lambda: always, stream_is_on=declines):
            await async_wait_until(lambda: seen_on, "the channel to be reported")
            always.clear()
            await more_rounds(2)  # the round that was under way is over
            reported = len(seen_on)
            asked = [query["slug"] for query in kick_api.calls("GET", "/channels")].count("always")
            await more_rounds(3)

        assert len(seen_on) == reported
        # Not even the liveness check is spent on it any more.
        assert [query["slug"] for query in kick_api.calls("GET", "/channels")].count("always") == asked

    async def test_failing_to_start_one_does_not_cost_a_tracked_channel_its_tick(self, kick_api, recorders, caplog):
        async def broken(slug: str) -> None:
            raise RuntimeError("no recording today")

        kick_api.add_channel("always", 1, is_live=True)
        recorders["always"] = StandInRecorder()

        with caplog.at_level(logging.ERROR, logger="kick_clip_hunter"):
            async with looping(["always"], always=lambda: ["always"], stream_is_on=broken):
                await async_wait_until(lambda: recorders["always"].ticks >= 2, "the loop to carry on")

        assert any("its recording could not be started" in record.getMessage() for record in caplog.records)

    async def test_failing_to_tell_which_channels_they_are_does_not_stop_the_loop(
        self, kick_api, recorders, caplog, starts
    ):
        def unreadable() -> list[str]:
            raise OSError("the database is gone")

        kick_api.add_channel("tracked", 1, is_live=True)
        recorders["tracked"] = StandInRecorder()

        with caplog.at_level(logging.ERROR, logger="kick_clip_hunter"):
            async with looping(["tracked"], always=unreadable, stream_is_on=starts):
                await async_wait_until(lambda: recorders["tracked"].ticks >= 2, "the loop to carry on")

        assert any("which channels are always recorded" in record.getMessage() for record in caplog.records)

    async def test_with_nobody_to_tell_its_stream_is_still_not_recorded_for_nothing(self, kick_api, recorders):
        kick_api.add_channel("always", 1, is_live=True)
        recorders["always"] = StandInRecorder()

        async with looping(["always"], tracked=lambda slug: False, always=lambda: ["always"]):
            await rounds(kick_api)

        assert recorders["always"].ticks == 0


class TestWhatTheLoopSaw:
    """Kept for the Health page: what the last look at each channel came to
    and when the loop last got through all of them."""

    def trouble_with(self, slug: str) -> str | None:
        look = recording_manager.looks().get(slug)
        return look.trouble if look else None

    async def test_a_live_channel_is_live_since_it_was_first_seen_to_be(self, kick_api, recorders):
        kick_api.add_channel("live_one", 1, is_live=True)
        recorders["live_one"] = StandInRecorder()

        await run_loop_until(lambda: recorders["live_one"].ticks >= 1, "a look", slugs=["live_one"])
        first = recording_manager.looks()["live_one"]
        await run_loop_until(lambda: recorders["live_one"].ticks >= 4, "more looks", slugs=["live_one"])
        later = recording_manager.looks()["live_one"]

        assert first.live_since is not None and first.trouble is None
        assert later.at >= first.at
        assert later.live_since == first.live_since

    async def test_a_channel_that_is_not_live_is_seen_not_to_be(self, kick_api, recorders):
        kick_api.add_channel("offline_one", 2, is_live=False)
        recorders["offline_one"] = StandInRecorder()

        await run_a_few_rounds(kick_api, slugs=["offline_one"])

        look = recording_manager.looks()["offline_one"]
        assert look.live_since is None and look.trouble is None

    async def test_going_off_the_air_is_the_end_of_having_been_live(self, kick_api, recorders):
        channel = kick_api.add_channel("was_live", 3, is_live=True)
        recorders["was_live"] = StandInRecorder()
        await run_loop_until(lambda: recorders["was_live"].is_active, "recording to start", slugs=["was_live"])
        assert recording_manager.looks()["was_live"].live_since is not None

        channel.is_live = False
        await run_loop_until(lambda: recorders["was_live"].stops == 1, "recording to stop", slugs=["was_live"])

        assert recording_manager.looks()["was_live"].live_since is None

    async def test_kick_not_answering_is_kept_as_why_and_says_nothing_of_being_live(self, kick_api, recorders):
        kick_api.add_channel("live_one", 1, is_live=True)
        recorders["live_one"] = StandInRecorder()
        await run_loop_until(lambda: recorders["live_one"].ticks >= 1, "a look", slugs=["live_one"])
        since = recording_manager.looks()["live_one"].live_since

        kick_api.failures["/channels"] = 503
        await run_loop_until(lambda: self.trouble_with("live_one") is not None, "a look that fails", slugs=["live_one"])

        look = recording_manager.looks()["live_one"]
        assert look.trouble == "HTTPStatusError"
        assert look.live_since == since

    async def test_a_look_that_works_again_clears_it(self, kick_api, recorders):
        kick_api.add_channel("live_one", 1, is_live=True)
        recorders["live_one"] = StandInRecorder()
        kick_api.failures["/channels"] = 503
        await run_loop_until(lambda: self.trouble_with("live_one") is not None, "a look that fails", slugs=["live_one"])

        kick_api.failures.clear()
        await run_loop_until(lambda: recorders["live_one"].ticks >= 1, "a look that works", slugs=["live_one"])

        assert self.trouble_with("live_one") is None

    async def test_a_recorder_that_cannot_be_started_is_kept_as_why(self, kick_api, recorders):
        kick_api.add_channel("broken", 1, is_live=True)
        recorders["broken"] = StandInRecorder(tick_error=RuntimeError("disk full"))

        await run_loop_until(
            lambda: self.trouble_with("broken") == "RuntimeError", "the failed start to be kept", slugs=["broken"]
        )

        # Kick did say it was live.
        assert recording_manager.looks()["broken"].live_since is not None

    async def test_so_is_a_channel_kick_calls_live_whose_page_gives_no_stream_address(self, kick_api, recorders):
        kick_api.add_channel("just_ended", 1, is_live=True)
        recorders["just_ended"] = StandInRecorder(tick_error=StreamUrlError("No live stream URL captured"))

        await run_loop_until(
            lambda: self.trouble_with("just_ended") == recording_manager.NO_STREAM_ADDRESS,
            "the missing address to be kept",
            slugs=["just_ended"],
        )

    async def test_one_channels_trouble_is_not_anothers(self, kick_api, recorders):
        kick_api.add_channel("broken", 1, is_live=True)
        kick_api.add_channel("healthy", 2, is_live=True)
        recorders["broken"] = StandInRecorder(tick_error=RuntimeError("disk full"))
        recorders["healthy"] = StandInRecorder()

        await run_loop_until(
            lambda: recorders["healthy"].ticks >= 2 and self.trouble_with("broken") is not None,
            "both channels to be looked at",
            slugs=["broken", "healthy"],
        )

        assert self.trouble_with("healthy") is None

    async def test_a_channel_whose_tracking_was_switched_off_is_no_longer_seen(self, kick_api, recorders):
        kick_api.add_channel("live_one", 1, is_live=True)
        recorders["live_one"] = StandInRecorder()
        await run_loop_until(lambda: "live_one" in recording_manager.looks(), "a look", slugs=["live_one"])

        await run_loop_until(
            lambda: "live_one" not in recording_manager.looks(),
            "the look to be dropped",
            slugs=["live_one"],
            tracked=lambda slug: False,
        )

    async def test_nor_is_any_channel_once_watching_is_off(self, kick_api, recorders):
        kick_api.add_channel("live_one", 1, is_live=True)
        recorders["live_one"] = StandInRecorder()
        await run_loop_until(lambda: "live_one" in recording_manager.looks(), "a look", slugs=["live_one"])

        await run_loop_until(
            lambda: not recording_manager.looks(), "the looks to be dropped", slugs=["live_one"], enabled=lambda: False
        )

    async def test_nor_one_that_has_left_the_watchlist(self, kick_api, recorders):
        kick_api.add_channel("live_one", 1, is_live=True)
        recorders["live_one"] = StandInRecorder()
        await run_loop_until(lambda: "live_one" in recording_manager.looks(), "a look", slugs=["live_one"])

        await run_loop_until(lambda: not recording_manager.looks(), "the look to be dropped", slugs=[])

    async def test_the_loop_says_when_it_last_got_through_its_channels(self, kick_api, recorders):
        kick_api.add_channel("live_one", 1, is_live=True)
        recorders["live_one"] = StandInRecorder()
        assert recording_manager.round_finished_at() is None
        before = datetime.now(timezone.utc)

        await run_loop_until(
            lambda: recording_manager.round_finished_at() is not None, "a finished round", slugs=["live_one"]
        )

        assert recording_manager.round_finished_at() >= before

    async def test_a_round_in_which_a_channel_failed_is_still_a_round(self, kick_api, recorders):
        recorders["unknown_to_kick"] = StandInRecorder()

        await run_loop_until(
            lambda: recording_manager.round_finished_at() is not None, "a finished round", slugs=["unknown_to_kick"]
        )

        assert self.trouble_with("unknown_to_kick") == "HTTPStatusError"

    def test_where_the_running_recorders_stand_is_theirs_to_say(self, recorders):
        since = datetime(2026, 3, 1, 20, 0, tzinfo=timezone.utc)

        class Standing(StandInRecorder):
            def __init__(self, run: RunState | None) -> None:
                super().__init__()
                self.run = run

            def state(self) -> RunState | None:
                return self.run

        recorders["running"] = Standing(RunState(since=since, has_vod=True))
        recorders["idle"] = Standing(None)

        assert recording_manager.recorder_runs() == {"running": RunState(since=since, has_vod=True)}
