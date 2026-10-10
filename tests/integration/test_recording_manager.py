"""The loop that decides, channel by channel, whether to record right now.

The rule it implements matters operationally: asking for a stream URL opens
a visible browser window, so a recorder must only ever be ticked for a
channel the (browser-free) official API says is live.

Recorders are replaced by stand-ins that count their calls; the "is it live"
question goes through the real client code against the fake Kick API.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kick_clip_hunter import recording_manager
from kick_clip_hunter.kick_stream import StreamUrlError
from kick_clip_hunter.recorder import PRE_ROLL_SECONDS, ChannelRecorder
from tests.support.waiting import async_wait_until

pytestmark = pytest.mark.anyio


class StandInRecorder:
    def __init__(self, active: bool = False, tick_error: Exception | None = None) -> None:
        self.ticks = 0
        self.stops = 0
        self.active = active
        self.tick_error = tick_error

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


async def run_loop_until(condition, what: str, *, slugs, enabled=lambda: True, tracked=lambda slug: True) -> None:
    """Runs the real loop until `condition` holds, then cancels it."""
    task = asyncio.create_task(recording_manager.run_forever(lambda: list(slugs), enabled, tracked))
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
            lambda: list(kwargs["slugs"]), kwargs.get("enabled", lambda: True), kwargs.get("tracked", lambda slug: True)
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
