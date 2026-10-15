"""The Health page, and what the service notices for it as it runs: webhooks
arriving, what a check of Kick's chat subscriptions found, what the recording
loop saw, the room on the disk and where the tunnel stands.

How each of those is worded, and what counts as trouble, is covered where it
is decided (tests/unit/test_health.py). Here the question is whether what
really happens in the service - a signed delivery, a subscription Kick has
dropped, a client that is not connected - ends up on the page, and beside
"Health" in the bar of every other page.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kick_clip_hunter import (
    db,
    health,
    recorder,
    recording_manager,
    stream_recording,
    tunnel,
    twitch_chat,
    youtube_chat,
)
from kick_clip_hunter.config import load_settings
from kick_clip_hunter.dashboard_view import when_words
from kick_clip_hunter.twitch_auth import AccountToken, TokenKeeper, save
from tests.support.data import add_chat, add_streamer
from tests.support.html import Element, parse
from tests.support.tailscale import ADDRESS
from tests.support.waiting import async_wait_until
from tests.support.webhook import chat_payload

pytestmark = pytest.mark.anyio

GB = 1024**3
MB = 1024**2
PAGES = ["/dashboard", "/dashboard/recordings", "/dashboard/analyze", "/dashboard/channels", "/dashboard/settings"]


def ago(**length: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(**length)


async def page_at(service, path: str = "/dashboard/health") -> Element:
    response = await service.client.get(path)
    assert response.status_code == 200, response.text
    return parse(response.text)


def part(page: Element, name: str) -> Element:
    return page.one("div", data_health_part=name)


def said(page: Element, name: str) -> str:
    return part(page, name).one("p", class_="health-said").text


def more(page: Element, name: str) -> list[str]:
    return [sentence.text for sentence in part(page, name).find("p", class_="health-more")]


def in_trouble(page: Element, name: str) -> bool:
    return "is-trouble" in part(page, name).one("p", class_="health-said").classes


def table(page: Element, name: str) -> list[list[str]]:
    """The rows of a part's table, cell by cell."""
    return [[cell.text for cell in row.find("th") + row.find("td")] for row in part(page, name).one("tbody").find("tr")]


def flag(page: Element) -> str:
    return page.one("span", data_health_flag=True).text


class RunningRecorder:
    """Stands in for a recorder that is bringing a stream in."""

    is_active = True

    def __init__(self, since: datetime, footage_at: datetime | None, has_vod: bool = False) -> None:
        self.run = recorder.RunState(since=since, has_vod=has_vod, footage_at=footage_at)

    def stop(self) -> None:
        self.is_active = False

    def state(self) -> recorder.RunState:
        return self.run


class TestThePage:
    async def test_renders_on_an_empty_installation(self, service):
        page = await page_at(service)

        assert page.one("h1").text == "Webhooks, subscriptions, the recorders, the disk and the tunnel all look fine."
        assert [title.text for title in page.one("main").find("h2")] == [
            "Webhooks",
            "Subscriptions",
            "Recorders",
            "Disk",
            "Tunnel",
        ]
        assert said(page, "webhooks").startswith("No webhook has arrived since the service started")
        assert said(page, "subscriptions") == "Not checked yet."
        assert said(page, "recorders") == "No channels are on the watchlist."
        # Nothing to list of channels there are none of.
        assert part(page, "webhooks").find("table") == [] and part(page, "recorders").find("table") == []
        # Twitch's and YouTube's chat are no part of a service that has
        # nothing to do with them.
        assert page.find("div", data_health_part="twitch") == []
        assert page.find("div", data_health_part="youtube") == []
        assert not any(in_trouble(page, name) for name, _, _ in health.PARTS if name not in ("twitch", "youtube"))

    async def test_every_part_is_marked_for_the_script_that_keeps_it_current(self, service):
        page = await page_at(service)

        assert "data-health" in page.one("main").attrs
        assert [marked.attrs["data-health-part"] for marked in page.find("div", data_health_part=True)] == [
            "lead",
            "webhooks",
            "subscriptions",
            "recorders",
            "disk",
            "tunnel",
        ]
        # The button is not swapped out from under whoever is pressing it.
        button = page.one("button", data_subscriptions_check=True)
        assert all(button not in marked.find("button") for marked in page.find("div", data_health_part=True))

    async def test_says_since_when_the_service_runs_and_where_its_log_is(self, service, monkeypatch):
        monkeypatch.setattr(service.main, "_started_at", ago(hours=3, minutes=12))

        lead = part(await page_at(service), "lead").one("p").text

        assert lead.startswith("The service has been running for 3 hours 12 minutes, since ")
        assert lead.endswith("The log is in data/logs/kick_clip_hunter.log.")

    async def test_starting_the_service_is_when_it_started(self, service, monkeypatch):
        monkeypatch.setattr(service.main, "_started_at", ago(days=3))
        monkeypatch.setattr(recording_manager, "run_forever", lambda *args: asyncio.Event().wait())

        async with service.main.lifespan(service.main.app):
            assert ago(seconds=30) < service.main._started_at


class TestTheCountInTheBar:
    @pytest.mark.parametrize("path", [*PAGES, "/dashboard/health"])
    async def test_nothing_stands_beside_health_while_all_is_well(self, service, path):
        page = await page_at(service, path)

        assert flag(page) == ""
        assert page.one("nav", class_="bar-nav").find("a")[-1].text == "Health"

    @pytest.mark.parametrize("path", [*PAGES, "/dashboard/health"])
    async def test_trouble_is_counted_on_every_page(self, service, path):
        service.watch("channel_a")
        service.api.failures["/events/subscriptions"] = 503
        await service.main._ensure_chat_subscriptions()

        page = await page_at(service, path)

        assert flag(page) == "1 problem"
        assert page.one("nav", class_="bar-nav").find("a")[-1].text == "Health 1 problem"

    async def test_it_is_the_count_the_health_page_itself_comes_to(self, service, monkeypatch):
        service.watch("channel_a")
        service.api.failures["/events/subscriptions"] = 503
        await service.main._ensure_chat_subscriptions()
        monkeypatch.setattr(stream_recording, "free_bytes", lambda: 1 * GB)

        page = await page_at(service)

        assert page.one("h1").text == "Subscriptions and the disk need a look."
        assert flag(page) == flag(await page_at(service, "/dashboard")) == "2 problems"


class TestWebhooks:
    async def test_a_delivery_is_noticed(self, service):
        service.watch("channel_a")

        assert (await service.chat("channel_a", "viewer", "hello")).status_code == 200
        page = await page_at(service)

        assert said(page, "webhooks").startswith("The last webhook arrived ")
        assert table(page, "webhooks") == [["channel_a", "Not live", "Just now", ""]]

    async def test_a_channel_that_has_said_nothing_says_so(self, service):
        service.watch("channel_a")

        assert table(await page_at(service), "webhooks") == [["channel_a", "Not live", "Nothing yet", ""]]

    async def test_chat_that_is_not_kept_while_watching_is_off_still_arrived(self, service):
        user_id = service.watch("channel_a")
        await service.client.post("/settings/watching?enabled=0")

        response = await service.chat("channel_a", "viewer", "hello")

        assert response.json() == {"status": "watching disabled"} and service.chat_log() == []
        assert service.main._heard.last_at is not None and user_id in service.main._heard.chat_at
        assert "Watching is off" in more(await page_at(service), "webhooks")[-1]

    async def test_so_did_chat_of_a_channel_whose_tracking_is_off(self, service):
        user_id = service.watch("channel_a")
        await service.client.post("/channels/channel_a/tracking?enabled=0")

        response = await service.chat("channel_a", "viewer", "hello")

        assert response.json() == {"status": "channel tracking disabled"}
        assert user_id in service.main._heard.chat_at

    async def test_a_delivery_with_a_signature_that_is_not_kicks_is_refused_and_counted_as_that(self, service):
        service.watch("channel_a")
        payload = chat_payload("channel_a", service.user_id("channel_a"), "viewer", "hello")

        response = await service.deliver(payload, **{"Kick-Event-Signature": "bm90IGEgc2lnbmF0dXJl"})
        page = await page_at(service)

        assert response.status_code == 401
        assert said(page, "webhooks").startswith("No webhook has arrived since the service started")
        assert more(page, "webhooks")[-1].startswith(
            "1 delivery was refused for a signature that is not Kick's, the last one "
        )
        assert service.main._heard.chat_at == {}

    async def test_an_event_that_is_not_chat_is_a_webhook_all_the_same(self, service):
        response = await service.deliver({"broadcaster": {"user_id": 7}}, event_type="channel.followed")

        assert response.status_code == 200
        assert service.main._heard.last_at is not None and service.main._heard.chat_at == {}

    async def test_what_was_stored_before_a_restart_is_what_a_channel_last_said(self, service):
        add_streamer("channel_a", 7)
        add_chat("channel_a", "viewer", "hello", ago(hours=2, minutes=5), broadcaster_user_id=7)

        assert table(await page_at(service), "webhooks") == [["channel_a", "Not live", "2 hours 5 minutes ago", ""]]

    async def test_what_has_arrived_since_is_newer_than_that(self, service):
        user_id = service.watch("channel_a")
        add_chat("channel_a", "viewer", "old", ago(hours=2), broadcaster_user_id=user_id)

        await service.chat("channel_a", "viewer", "new")

        assert table(await page_at(service), "webhooks")[0][2] == "Just now"

    async def test_a_channels_chat_is_found_by_its_id_whatever_it_is_stored_as(self, service):
        # The watchlist keeps the name as it was added; Kick sends chat
        # under its own spelling of it.
        add_streamer("Channel_A", 7)
        add_chat("channel_a", "viewer", "hello", ago(minutes=3), broadcaster_user_id=7)

        assert table(await page_at(service), "webhooks") == [["Channel_A", "Not live", "3 minutes ago", ""]]

    async def test_nothing_arriving_while_a_channel_is_live_is_trouble(self, service, monkeypatch):
        add_streamer("channel_a", 7)
        long = health.CHAT_SILENT_SECONDS + 60
        monkeypatch.setattr(service.main, "_started_at", ago(seconds=long))
        monkeypatch.setitem(
            recording_manager._looks, "channel_a", recording_manager.Look(at=ago(), live_since=ago(seconds=long))
        )

        page = await page_at(service)

        assert in_trouble(page, "webhooks")
        assert said(page, "webhooks").startswith("Nothing has arrived from Kick since the service started")
        assert page.one("h1").text == "Webhooks need a look."
        assert flag(await page_at(service, "/dashboard")) == "1 problem"

    async def test_one_delivery_is_enough_to_end_that(self, service, monkeypatch):
        service.watch("channel_a")
        long = health.CHAT_SILENT_SECONDS + 60
        monkeypatch.setattr(service.main, "_started_at", ago(seconds=long))
        monkeypatch.setitem(
            recording_manager._looks, "channel_a", recording_manager.Look(at=ago(), live_since=ago(seconds=long))
        )

        await service.chat("channel_a", "viewer", "hello")

        assert not in_trouble(await page_at(service), "webhooks")


class TestSubscriptions:
    def remembered(self) -> list:
        conn = db.get_connection()
        try:
            return db.get_subscription_drops(conn)
        finally:
            conn.close()

    async def test_says_what_the_last_check_found(self, service):
        service.watch("channel_a")
        service.watch("channel_b")

        await service.main._ensure_chat_subscriptions()
        page = await page_at(service)

        assert said(page, "subscriptions").startswith(
            "Kick has a chat subscription for all 2 watched channels. Checked"
        )
        assert part(page, "subscriptions").find("table") == []
        assert self.remembered() == []

    async def test_a_subscription_kick_dropped_is_said_and_remembered(self, service):
        service.watch("channel_a")
        service.watch("channel_b", subscribed=False)

        await service.main._ensure_chat_subscriptions()
        page = await page_at(service)

        assert said(page, "subscriptions").startswith(
            "Kick had no chat subscription for 1 of the 2 watched channels (channel_b) when it was checked"
        )
        assert said(page, "subscriptions").endswith("It was subscribed again.")
        # Mended, so nothing to look into - but there to be seen.
        assert not in_trouble(page, "subscriptions")
        found = datetime.fromisoformat(self.remembered()[0]["at"])
        assert table(page, "subscriptions") == [
            [when_words(found.astimezone(), service.main._today()), "1 of 2 channels: channel_b", "Subscribed again"]
        ]

    async def test_kick_dropping_everything_is_said_as_that(self, service):
        for slug in ("channel_a", "channel_b", "channel_c"):
            service.watch(slug)
        service.api.subscribed.clear()

        await service.main._ensure_chat_subscriptions()
        page = await page_at(service)

        assert "for any of the 3 watched channels" in said(page, "subscriptions")
        assert table(page, "subscriptions")[0][1:] == ["All 3 channels", "Subscribed again"]

    async def test_a_restart_does_not_forget_it(self, service):
        service.watch("channel_a", subscribed=False)
        await service.main._ensure_chat_subscriptions()

        service.main._subscriptions = None  # what a fresh process knows
        page = await page_at(service)

        assert said(page, "subscriptions") == "Not checked yet."
        assert table(page, "subscriptions")[0][1:] == ["The watched channel", "Subscribed again"]

    async def test_every_drop_is_listed_the_newest_first(self, service):
        service.watch("channel_a", subscribed=False)
        await service.main._ensure_chat_subscriptions()
        service.watch("channel_b")
        service.api.subscribed.clear()
        await service.main._ensure_chat_subscriptions()

        rows = table(await page_at(service), "subscriptions")

        assert [row[1] for row in rows] == ["All 2 channels", "The watched channel"]

    async def test_a_check_that_finds_everything_in_place_again_is_not_another_drop(self, service):
        service.watch("channel_a", subscribed=False)
        await service.main._ensure_chat_subscriptions()

        await service.main._ensure_chat_subscriptions()

        assert len(self.remembered()) == 1
        assert said(await page_at(service), "subscriptions").startswith("Kick has a chat subscription for the watched")

    async def test_kick_not_answering_is_trouble_and_no_drop(self, service):
        service.watch("channel_a")
        service.api.failures["/events/subscriptions"] = 503

        await service.main._ensure_chat_subscriptions()
        page = await page_at(service)

        assert in_trouble(page, "subscriptions")
        assert said(page, "subscriptions").startswith("Kick could not be asked about its subscriptions")
        assert "(HTTPStatusError)" in said(page, "subscriptions")
        assert self.remembered() == []

    async def test_a_channel_that_could_not_be_subscribed_again_is_trouble(self, service):
        service.watch("channel_a", subscribed=False)
        broken = service.watch("broken", subscribed=False)
        service.api.subscribe_failures.add(broken)

        await service.main._ensure_chat_subscriptions()
        page = await page_at(service)

        assert in_trouble(page, "subscriptions")
        assert said(page, "subscriptions").endswith("Subscribing broken again failed. The server log says why.")
        assert table(page, "subscriptions")[0][2] == "Subscribing broken again failed"
        assert self.remembered()[0]["failed"] == ["broken"]

    async def test_an_empty_watchlist_has_nothing_to_check(self, service):
        await service.main._ensure_chat_subscriptions()

        assert said(await page_at(service), "subscriptions") == (
            "No channels are on the watchlist, so there is nothing to subscribe to."
        )

    async def test_failing_to_remember_a_drop_does_not_fail_the_check(self, service, monkeypatch, caplog):
        user_id = service.watch("channel_a", subscribed=False)

        def locked(conn, drop):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(service.main, "add_subscription_drop", locked)

        with caplog.at_level(logging.ERROR, logger="kick_clip_hunter"):
            await service.main._ensure_chat_subscriptions()  # must not raise: it runs before the service starts

        assert service.api.subscribed == [user_id]
        assert service.main._subscriptions.missing == ("channel_a",)
        assert any("could not keep a record" in record.getMessage() for record in caplog.records)


class TestCheckingNow:
    async def test_the_button_checks_at_once_and_mends_what_is_missing(self, service):
        user_id = service.watch("channel_a", subscribed=False)

        response = await service.client.post("/subscriptions/check")
        page = await page_at(service)

        assert (response.status_code, response.json()) == (200, {"status": "checked"})
        assert service.api.subscribed == [user_id]
        assert said(page, "subscriptions").startswith("Kick had no chat subscription for the watched channel")

    async def test_it_can_be_pressed_again(self, service):
        service.watch("channel_a")

        first = await service.client.post("/subscriptions/check")
        second = await service.client.post("/subscriptions/check")

        assert (first.status_code, second.status_code) == (200, 200)

    async def test_a_check_that_is_under_way_is_not_joined_by_another(self, service):
        # Two at once would both find the channel missing and subscribe it
        # twice.
        service.watch("channel_a", subscribed=False)
        service.main._subscription_check_running = True

        response = await service.client.post("/subscriptions/check")

        assert response.status_code == 409
        assert response.json()["detail"] == "A check is under way already."
        assert service.api.requests == []

    async def test_nor_does_the_periodic_check_run_into_one(self, service):
        service.watch("channel_a", subscribed=False)
        service.main._subscription_check_running = True

        await service.main._ensure_chat_subscriptions()

        assert service.api.requests == [] and service.main._subscriptions is None


class TestRecorders:
    async def test_a_running_recorder_is_shown_with_how_fresh_its_footage_is(self, service, monkeypatch):
        add_streamer("channel_a", 1)
        add_streamer("channel_b", 2)
        running = RunningRecorder(since=ago(minutes=30), footage_at=ago(seconds=1), has_vod=True)
        monkeypatch.setitem(recording_manager._recorders, "channel_a", running)

        page = await page_at(service)

        assert said(page, "recorders") == "1 stream is coming in."
        rows = table(page, "recorders")
        assert rows[0][:3] == ["channel_a", "Live", "Just now"] and rows[0][4:] == ["Found", ""]
        assert rows[1] == ["channel_b", "Not live", "", "", "", ""]
        assert part(page, "recorders").one("span", class_="ch-lamp-rec").text == "Live"

    async def test_a_channel_the_loop_could_not_look_at_is_trouble(self, service, monkeypatch):
        add_streamer("channel_a", 1)
        monkeypatch.setitem(
            recording_manager._looks, "channel_a", recording_manager.Look(at=ago(), trouble="ConnectError")
        )

        page = await page_at(service)

        assert in_trouble(page, "recorders")
        assert said(page, "recorders") == "Something is wrong with channel_a."
        assert table(page, "recorders")[0][-1] == "The last look at it failed: ConnectError"
        assert flag(await page_at(service, "/dashboard")) == "1 problem"

    async def test_a_recorder_that_gets_no_footage_is_trouble(self, service, monkeypatch):
        add_streamer("channel_a", 1)
        late = health.FOOTAGE_LATE_SECONDS + 90
        stalled = RunningRecorder(since=ago(hours=1), footage_at=ago(seconds=late))
        monkeypatch.setitem(recording_manager._recorders, "channel_a", stalled)

        page = await page_at(service)

        assert in_trouble(page, "recorders")
        assert table(page, "recorders")[0][-1] == f"No footage for {health.span_words(late)}"

    async def test_says_when_the_loop_last_went_round(self, service, monkeypatch):
        monkeypatch.setattr(recording_manager, "_round_finished_at", ago(seconds=12))

        sentences = more(await page_at(service), "recorders")

        assert sentences[0].startswith("Channels are looked at every 30 seconds; the last round finished 1")

    async def test_a_loop_that_no_longer_goes_round_is_trouble(self, service, monkeypatch):
        monkeypatch.setattr(recording_manager, "_round_finished_at", ago(seconds=health.LOOP_LATE_SECONDS + 5))

        page = await page_at(service)

        assert in_trouble(page, "recorders")
        assert said(page, "recorders").startswith("The loop that looks at the channels last finished a round")


class TestDisk:
    async def test_says_how_much_room_there_is(self, service):
        page = await page_at(service)

        assert said(page, "disk") == "500 GB free on the disk recordings are kept on."
        assert more(page, "disk") == ["With less than 5 GB left no recording is started, and those that run are ended."]

    async def test_a_disk_that_is_running_out_is_trouble(self, service, monkeypatch):
        monkeypatch.setattr(stream_recording, "free_bytes", lambda: 2 * GB)

        page = await page_at(service)

        assert in_trouble(page, "disk")
        assert said(page, "disk").startswith("Only 2 GB is free on the disk recordings are kept on.")
        assert page.one("h1").text == "The disk needs a look."

    async def test_a_disk_that_cannot_be_looked_at_does_not_take_the_bar_down_with_it(self, service, monkeypatch):
        def gone() -> int:
            raise OSError("the device is not ready")

        monkeypatch.setattr(stream_recording, "free_bytes", gone)

        assert said(await page_at(service), "disk") == "The disk recordings are kept on could not be looked at."
        # Every page counts the trouble in its bar. The Recordings page is
        # left out: it says how much room is free itself, and always has.
        for path in PAGES:
            if path != "/dashboard/recordings":
                assert flag(await page_at(service, path)) == "1 problem"

    async def test_lists_what_takes_the_room(self, service):
        for folder, megabytes in (
            ("data/clips", 3),
            ("data/clips/some_channel", 2),
            ("data/streams/some_channel/stream_1_parts", 7),
            ("data/recordings/some_channel", 4),
        ):
            Path(folder).mkdir(parents=True, exist_ok=True)
            (Path(folder) / "footage.bin").write_bytes(bytes(megabytes * MB))

        rows = table(await page_at(service), "disk")

        assert [row[0] for row in rows] == [
            "Clips",
            "Stream recordings",
            "Buffers clips are cut from",
            "Database",
            "Logs",
        ]
        # A folder counts with everything under it.
        assert [row[1] for row in rows[:3]] == ["5 MB", "7 MB", "4 MB"]

    async def test_the_folders_are_not_walked_again_for_every_load(self, service, monkeypatch):
        Path("data/clips").mkdir(parents=True, exist_ok=True)
        (Path("data/clips") / "one.bin").write_bytes(bytes(2 * MB))
        assert table(await page_at(service), "disk")[0] == ["Clips", "2 MB"]

        (Path("data/clips") / "two.bin").write_bytes(bytes(4 * MB))
        assert table(await page_at(service), "disk")[0] == ["Clips", "2 MB"]

        monkeypatch.setattr(service.main, "DISK_USE_SECONDS", 0)
        assert table(await page_at(service), "disk")[0] == ["Clips", "6 MB"]

    async def test_what_is_not_there_takes_no_room(self, service):
        assert service.main._size_of(Path("data/nothing/here")) == 0
        assert service.main._size_of(Path("data/nothing/here.db")) == 0


class TestTunnel:
    async def test_a_machine_without_tailscale_is_said_as_that_and_is_no_trouble(self, service):
        page = await page_at(service)

        assert said(page, "tunnel") == (
            "No Tailscale client was found on this machine, so there is no tunnel to look at from here."
        )
        assert not in_trouble(page, "tunnel") and flag(page) == ""

    async def test_a_funnel_that_is_up_says_the_address_kick_has_to_have(self, service, tailscale):
        page = await page_at(service)

        assert said(page, "tunnel") == f"Tailscale Funnel carries {ADDRESS} to this service."
        assert more(page, "tunnel")[0].startswith("It forwards to http://127.0.0.1:8000/webhooks/kick.")
        assert not in_trouble(page, "tunnel")

    async def test_the_port_the_page_was_asked_on_is_what_the_funnel_is_held_against(self, service, tailscale):
        response = await service.client.get("http://testserver:8001/dashboard/health")
        page = parse(response.text)

        assert in_trouble(page, "tunnel")
        assert said(page, "tunnel").endswith("which is not the port this service answers on (8001).")

    async def test_a_funnel_to_that_port_is_up(self, service, tailscale):
        response = await service.client.get("http://testserver:8000/dashboard/health")

        assert not in_trouble(parse(response.text), "tunnel")
        assert service.main._served_port == 8000

    async def test_tailscale_not_being_connected_is_trouble_on_every_page(self, service, tailscale):
        tailscale.backend = "NoState"

        page = await page_at(service)

        assert in_trouble(page, "tunnel")
        assert said(page, "tunnel") == "Tailscale is not connected (it says NoState), so Kick can't reach this service."
        assert flag(await page_at(service, "/dashboard")) == "1 problem"

    async def test_a_look_that_is_fresh_is_not_repeated(self, service, tailscale):
        await page_at(service)
        await page_at(service)

        assert tailscale.commands == [["status", "--json"], ["funnel", "status", "--json"]]

    async def test_a_look_that_has_gone_stale_is_repeated_behind_the_page(self, service, tailscale):
        await page_at(service)
        looked_at, funnel = service.main._funnel
        service.main._funnel = (looked_at - timedelta(seconds=service.main.TUNNEL_FRESH_SECONDS), funnel)
        tailscale.backend = "Stopped"

        page = await page_at(service)

        # The page does not wait for the client...
        assert said(page, "tunnel") == f"Tailscale Funnel carries {ADDRESS} to this service."
        # ...and says what it answered the next time round.
        await service.main._tunnel_look
        assert in_trouble(await page_at(service), "tunnel")

    async def test_a_look_that_is_still_under_way_is_not_started_again(self, service, tailscale):
        await page_at(service)
        looked_at, funnel = service.main._funnel
        service.main._funnel = (looked_at - timedelta(seconds=service.main.TUNNEL_FRESH_SECONDS), funnel)

        await service.client.get("/dashboard/health")
        under_way = service.main._tunnel_look
        service.main._funnel = (looked_at - timedelta(seconds=service.main.TUNNEL_FRESH_SECONDS), funnel)
        await service.client.get("/dashboard/health")

        assert service.main._tunnel_look is under_way
        await under_way

    async def test_the_log_says_when_it_changes_and_only_then(self, service, tailscale, caplog):
        with caplog.at_level(logging.INFO, logger="kick_clip_hunter"):
            await service.main._look_at_tunnel()
            await service.main._look_at_tunnel()
            tailscale.backend = "NoState"
            await service.main._look_at_tunnel()
            await service.main._look_at_tunnel()

        about_it = [
            (record.levelno, record.getMessage()) for record in caplog.records if "tunnel" in record.getMessage()
        ]
        assert about_it == [(logging.INFO, "tunnel: up"), (logging.WARNING, "tunnel: not_connected (NoState)")]

    async def test_the_running_service_looks_at_it_without_being_asked(self, service, tailscale, monkeypatch):
        monkeypatch.setattr(recording_manager, "run_forever", lambda *args: asyncio.Event().wait())

        async with service.main.lifespan(service.main.app):
            await async_wait_until(lambda: service.main._funnel is not None, "the tunnel to be looked at")

            assert service.main._funnel[1].state == tunnel.UP
            # ...and so every page can say when something is wrong with it.
            assert flag(await page_at(service, "/dashboard")) == ""


def signed_in_tokens() -> TokenKeeper:
    """The tokens of an account signed in for the service to read chat as."""
    save(AccountToken("access", "refresh", ago(hours=-4), "777", "chat_reader"))
    return TokenKeeper("id", "secret")


def a_reader(service) -> twitch_chat.ChatReader:
    """The service's reader of Twitch's chat, not running."""
    return twitch_chat.ChatReader(service.main._twitch_channels, service.main._take_chats, signed_in_tokens(), "id")


class TestYouTubeChat:
    """YouTube's chat, where the service watches a YouTube channel: what the
    reader of it found, on the page."""

    def watch_on_youtube(self, handle: str = "some_channel") -> None:
        conn = db.get_connection()
        try:
            db.add_platform_streamer(conn, "youtube", "UCAAAAAAAAAAAAAAAAAAAAAA", f"{handle}@youtube")
        finally:
            conn.close()

    def a_reader(self, service, monkeypatch) -> youtube_chat.ChatReader:
        reader = youtube_chat.ChatReader(service.main._youtube_channels, service.main._take_chats)
        monkeypatch.setattr(service.main, "_youtube_reader", reader)
        return reader

    async def test_without_a_youtube_channel_there_is_no_such_part(self, service, monkeypatch):
        self.a_reader(service, monkeypatch)

        page = await page_at(service)

        assert page.find("div", data_health_part="youtube") == []

    async def test_the_reader_says_whose_chat_comes_in(self, service, monkeypatch):
        self.watch_on_youtube()
        reader = self.a_reader(service, monkeypatch)
        reader.reading["some_channel@youtube"] = ago(minutes=5)
        reader.last_heard_at = ago(seconds=3)

        page = await page_at(service)

        assert said(page, "youtube") == "The chat of the YouTube channel comes in."
        assert more(page, "youtube") == ["The last answer from YouTube came 3 seconds ago."]
        assert not in_trouble(page, "youtube")

    async def test_a_chat_that_cannot_be_read_is_listed_with_why(self, service, monkeypatch):
        self.watch_on_youtube()
        reader = self.a_reader(service, monkeypatch)
        reader.unavailable["some_channel@youtube"] = "Chat is disabled for this live stream."

        page = await page_at(service)

        (row,) = part(page, "youtube").one("tbody").find("tr")
        assert row.one("th").text == "some_channel@youtube"
        assert row.one("td").text == "Chat is disabled for this live stream."
        assert not in_trouble(page, "youtube")

    async def test_youtube_not_answering_for_long_is_trouble_counted_in_the_bar(self, service, monkeypatch):
        self.watch_on_youtube()
        reader = self.a_reader(service, monkeypatch)
        reader.failing["some_channel@youtube"] = ("YouTube answered 429", ago(minutes=10))

        page = await page_at(service)
        response = await service.client.get("/dashboard/channels")

        assert in_trouble(page, "youtube")
        assert page.one("h1").text == "YouTube chat needs a look."
        assert "1 problem" in response.text

    async def test_before_the_reader_has_started_the_channel_is_not_live_yet(self, service):
        self.watch_on_youtube()

        page = await page_at(service)

        assert said(page, "youtube") == "The YouTube channel is not live, so no YouTube chat comes in."


class TestTwitchChat:
    """Twitch's chat, where the service has any: what the reader of it
    found, on the page and counted in the bar."""

    def watch_on_twitch(self, login: str = "some_channel", twitch_id: str = "101") -> None:
        conn = db.get_connection()
        try:
            db.add_platform_streamer(conn, "twitch", twitch_id, f"{login}@twitch")
        finally:
            conn.close()

    async def test_a_twitch_channel_watched_without_twitch_set_up_is_trouble(self, service):
        self.watch_on_twitch()

        page = await page_at(service)

        assert in_trouble(page, "twitch")
        assert "not set up for Twitch" in said(page, "twitch")
        assert page.one("h1").text == "Twitch chat needs a look."

    async def test_it_is_counted_in_the_bar_of_every_page(self, service):
        self.watch_on_twitch()

        response = await service.client.get("/dashboard/channels")

        assert "1 problem" in response.text

    async def test_set_up_the_reader_says_whose_chat_comes_in(self, service, twitch_api, monkeypatch):
        monkeypatch.setattr(service.main, "settings", load_settings())
        self.watch_on_twitch()
        reader = a_reader(service)
        reader.subscribed["101"] = twitch_chat.Channel(-1, "some_channel@twitch", "101")
        reader.connected_since = ago(minutes=5)
        monkeypatch.setattr(service.main, "_twitch_reader", reader)

        page = await page_at(service)

        assert said(page, "twitch") == "The chat of the Twitch channel comes in, read as chat_reader."
        assert more(page, "twitch") == ["Connected 5 minutes ago."]
        assert not in_trouble(page, "twitch")

    async def test_a_refused_channel_is_listed_with_why(self, service, twitch_api, monkeypatch):
        monkeypatch.setattr(service.main, "settings", load_settings())
        self.watch_on_twitch()
        reader = a_reader(service)
        reader._wanted()
        reader.refused["101"] = "403 subscription refused"
        monkeypatch.setattr(service.main, "_twitch_reader", reader)

        page = await page_at(service)

        (row,) = part(page, "twitch").one("tbody").find("tr")
        assert row.one("th").text == "some_channel@twitch"
        assert row.one("td").text == "403 subscription refused"
        assert in_trouble(page, "twitch")
