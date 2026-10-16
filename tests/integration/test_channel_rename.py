"""A watched Kick channel that renames itself on Kick.

It is kept by its Kick user id, which stays, and its chat comes under the
new name from then on; but the watchlist had the old one, by which Kick no
longer finds it - so its recorder failed every round, and every clip with
it. The service notices the rename two ways - chat under a name that is not
the watchlist's, and Kick knowing no channel by the watchlist's name when
the recording loop asks - and then asks Kick what the channel is called now,
by its id, and has the watchlist follow it.

Kick's API is the fake one throughout: the channel lookups go through the
real client code.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

import pytest

from kick_clip_hunter import db, recording_manager, stream_recording
from kick_clip_hunter.kick_client import NoSuchChannel
from tests.support.data import add_stream_recording
from tests.support.html import parse
from tests.support.waiting import async_wait_until

pytestmark = pytest.mark.anyio

OLD, NEW = "OldName", "newname"


def watchlist() -> list[str]:
    conn = db.get_connection()
    try:
        return [row["slug"] for row in db.get_streamers(conn)]
    finally:
        conn.close()


def asked_by_id(service) -> list[object]:
    return [query for query in service.api.calls("GET", "/public/v1/channels") if "broadcaster_user_id" in query]


def renamed_on_kick(service, owner: int) -> None:
    """The channel renames itself on Kick; its chat comes under the new name."""
    service.api.rename_channel(OLD, NEW)
    service._user_ids[NEW] = owner


class StandInRecorder:
    def __init__(self, channel_slug: str, active: bool = False) -> None:
        self.channel_slug = channel_slug
        self.active = active
        self.ticks = 0
        self.whole_stream = None

    def tick(self) -> None:
        self.ticks += 1
        self.active = True

    def stop(self) -> None:
        self.active = False

    @property
    def is_active(self) -> bool:
        return self.active


@pytest.fixture
def recorders(monkeypatch) -> dict[str, StandInRecorder]:
    monkeypatch.setattr(recording_manager, "TICK_INTERVAL_SECONDS", 0.01)
    registry: dict[str, StandInRecorder] = {}
    monkeypatch.setattr(recording_manager, "_recorders", registry)
    monkeypatch.setattr(recording_manager, "ChannelRecorder", StandInRecorder)
    return registry


async def run_loop_until(service, condition, what: str) -> None:
    """The recording loop as the service runs it, until `condition` holds."""
    main = service.main
    task = asyncio.create_task(
        recording_manager.run_forever(
            main._watchlist_slugs, lambda: True, main._is_channel_tracked, look_failed=main._look_failed
        )
    )
    try:
        await async_wait_until(condition, what)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


class TestChatUnderANewName:
    async def test_has_the_watchlist_follow_the_channel_to_it(self, service, caplog):
        owner = service.watch(OLD)
        renamed_on_kick(service, owner)

        with caplog.at_level(logging.WARNING, logger="kick_clip_hunter"):
            response = await service.chat(NEW, "viewer", "hello")
            await service.settle()

        assert response.status_code == 200
        assert watchlist() == [NEW]
        assert asked_by_id(service) == [{"broadcaster_user_id": str(owner)}]
        assert any(f"[{OLD}] renamed itself on Kick to {NEW}" in record.getMessage() for record in caplog.records)
        # Its chat is stored as ever, under the name it came with.
        assert service.chat_log() == [(NEW, "viewer", "hello")]

    async def test_everything_kept_by_its_id_goes_with_it(self, service):
        owner = service.watch(OLD)
        conn = db.get_connection()
        try:
            db.set_streamer_tracking(conn, owner, False)
            db.set_streamer_always_record(conn, owner, True)
        finally:
            conn.close()
        renamed_on_kick(service, owner)

        await service.chat(NEW, "viewer", "hello")
        await service.settle()

        conn = db.get_connection()
        try:
            row = db.get_streamer_by_slug(conn, NEW)
        finally:
            conn.close()
        assert row["broadcaster_user_id"] == owner
        assert (row["tracking_enabled"], row["always_record"]) == (0, 1)

    async def test_kick_is_asked_once_for_a_name_however_much_chat_comes_under_it(self, service):
        owner = service.watch(OLD)
        renamed_on_kick(service, owner)

        for number in range(5):
            await service.chat(NEW, f"viewer{number}", "hello")
        await service.settle()

        assert len(asked_by_id(service)) == 1

    async def test_the_same_name_spelled_otherwise_is_no_rename(self, service):
        # The watchlist keeps a name as it was added, capitals and all; Kick
        # sends it in small letters.
        service.watch("SomeChannel")
        service._user_ids["somechannel"] = service.user_id("SomeChannel")

        await service.chat("somechannel", "viewer", "hello")
        await service.settle()

        assert watchlist() == ["SomeChannel"]
        assert asked_by_id(service) == []

    async def test_a_channel_kick_still_calls_by_the_watchlists_name_is_left_as_it_is(self, service):
        # Chat under another name, but Kick says the channel is called what
        # the watchlist says: nothing to follow.
        owner = service.watch(OLD)
        service._user_ids["othername"] = owner

        await service.chat("othername", "viewer", "hello")
        await service.settle()

        assert watchlist() == [OLD]
        assert len(asked_by_id(service)) == 1

    async def test_a_name_another_watched_channel_has_is_not_taken(self, service, caplog):
        owner = service.watch(OLD)
        conn = db.get_connection()
        try:
            db.add_streamer(conn, 77, NEW)
        finally:
            conn.close()
        renamed_on_kick(service, owner)

        with caplog.at_level(logging.ERROR, logger="kick_clip_hunter"):
            await service.chat(NEW, "viewer", "hello")
            await service.settle()

        assert watchlist() == [OLD, NEW]
        assert any("another watched channel is called" in record.getMessage() for record in caplog.records)

    async def test_kick_failing_to_answer_leaves_the_watchlist_alone(self, service):
        owner = service.watch(OLD)
        renamed_on_kick(service, owner)
        service.api.failures["broadcaster_user_id"] = 500

        response = await service.chat(NEW, "viewer", "hello")
        await service.settle()

        assert response.status_code == 200
        assert watchlist() == [OLD]

    async def test_the_chat_delay_set_for_it_is_carried_over_to_the_new_name(self, service):
        owner = service.watch(OLD)
        old_chat = OLD.lower()
        conn = db.get_connection()
        try:
            db.insert_moment(
                conn,
                broadcaster_user_id=owner,
                channel_slug=old_chat,
                window_start="2026-10-15T20:00:00+00:00",
                window_end="2026-10-15T20:00:10+00:00",
                reason="laugh",
                score=1.0,
                message_count=1,
                baseline_message_rate=0.1,
                current_message_rate=1.0,
                emote_count=0,
                keyword_hits=0,
            )
            db.set_chat_delay(conn, old_chat, -2)
        finally:
            conn.close()
        renamed_on_kick(service, owner)

        await service.chat(NEW, "viewer", "hello")
        await service.settle()

        conn = db.get_connection()
        try:
            # The new name has it, and the old one keeps it for its moments.
            assert (db.get_chat_delay(conn, NEW), db.get_chat_delay(conn, old_chat)) == (-2, -2)
        finally:
            conn.close()

    async def test_the_health_page_lists_it(self, service):
        owner = service.watch(OLD)
        renamed_on_kick(service, owner)
        await service.chat(NEW, "viewer", "hello")
        await service.settle()

        response = await service.client.get("/dashboard/health")

        assert response.status_code == 200
        recorders = parse(response.text).one("div", data_health_part="recorders")
        (renames,) = [
            table
            for table in recorders.find("table")
            if any("renamed themselves" in caption.text for caption in table.find("caption"))
        ]
        ((noticed, was, now),) = [
            [cell.text for cell in row.find("th") + row.find("td")] for row in renames.one("tbody").find("tr")
        ]
        assert (was, now) == (OLD, NEW)
        assert noticed


class TestAChannelKickNoLongerFindsByName:
    async def test_is_looked_up_by_its_id_and_followed(self, service, recorders):
        service.api.unknown_slug_status = None  # Kick's answer: no such channel
        owner = service.watch(OLD, live=False)
        renamed_on_kick(service, owner)

        await run_loop_until(service, lambda: NEW in recording_manager.looks(), f"the loop to look at {NEW}")

        assert watchlist() == [NEW]
        assert OLD not in recording_manager.looks()
        assert recording_manager.looks()[NEW].trouble is None

    async def test_a_channel_that_is_gone_is_asked_about_once_in_a_while(self, service, recorders):
        service.api.unknown_slug_status = None
        service.watch(OLD, live=False)
        service.api.remove_channel(OLD)
        rounds = []
        original = recording_manager._could_not_see

        def counted(slug, trouble):
            rounds.append(slug)
            original(slug, trouble)

        recording_manager._could_not_see = counted
        try:
            await run_loop_until(service, lambda: len(rounds) >= 5, "five failed looks")
        finally:
            recording_manager._could_not_see = original

        assert watchlist() == [OLD]
        assert len(asked_by_id(service)) == 1
        assert recording_manager.looks()[OLD].trouble == NoSuchChannel.__name__

    async def test_other_failures_are_not_taken_for_a_rename(self, service, recorders):
        service.watch(OLD, live=False)
        service.api.failures["/public/v1/channels"] = 500
        failed = []

        async def spy(slug, error):
            failed.append(error)
            await service.main._look_failed(slug, error)

        task = asyncio.create_task(recording_manager.run_forever(service.main._watchlist_slugs, look_failed=spy))
        try:
            await async_wait_until(lambda: len(failed) >= 2, "two failed looks")
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        assert asked_by_id(service) == []
        assert watchlist() == [OLD]


class TestWhatTheRecordingLoopKeepsByName:
    """What recording_manager keeps by a channel's name goes by its new one."""

    async def test_a_running_recorder_carries_on_under_the_new_name(self, recorders):
        recorders[OLD] = running = StandInRecorder(OLD, active=True)

        recording_manager.rename(OLD, NEW)

        assert recorders == {NEW: running}
        # Its stream is asked for by the new name from now on.
        assert running.channel_slug == NEW

    async def test_one_that_is_not_running_is_simply_forgotten(self, recorders):
        recorders[OLD] = StandInRecorder(OLD)

        recording_manager.rename(OLD, NEW)

        assert recorders == {}

    async def test_a_stream_kept_whole_goes_on_in_the_folder_it_was_started_in(self, recorders, service):
        owner = service.watch(OLD)
        kept = recording_manager.start_stream_recording(OLD, owner, 5)

        recording_manager.rename(OLD, NEW)

        assert recording_manager.stream_recordings() == {NEW: kept}
        assert kept.channel_slug == NEW
        assert kept.writer.folder == stream_recording.parts_folder(OLD, 5)
        # The recorder that will bring its footage in hands it on.
        assert recorders[NEW].whole_stream is kept.writer

    async def test_what_was_seen_under_the_old_name_is_forgotten_and_holding_off_moves(self, recorders):
        recording_manager._saw(OLD, True)
        recording_manager.hold_off(OLD)

        recording_manager.rename(OLD, NEW)

        assert recording_manager.looks() == {}
        assert recording_manager.is_held_off(NEW) and not recording_manager.is_held_off(OLD)

    async def test_after_a_restart_a_recording_goes_on_under_the_name_the_channel_has_now(self, service):
        # Started under the old name, its row and folder have that one.
        owner = service.watch(NEW)
        recording_id = add_stream_recording(OLD, state="recording", broadcaster_user_id=owner)

        service.main._take_up_stream_recordings()

        kept = recording_manager.stream_recording_of(NEW)
        assert kept is not None and kept.recording_id == recording_id
        assert kept.writer.folder == stream_recording.parts_folder(OLD, recording_id)
        assert recording_manager.stream_recording_of(OLD) is None
