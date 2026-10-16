"""Notifications, through the running service: what is told of - a clip
saved, a recording started, saved or not, a part of the Health page that
needs a look and looks fine again - where it goes (the fake Telegram and
Discord, through the real clients), the switches that keep each kind from
going, and what the Health and Channels pages say of it all.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kick_clip_hunter import db, notify, recorder, recording_manager, stream_recording
from kick_clip_hunter.chat_trace import TIMES_SIGN as TIMES
from kick_clip_hunter.config import load_settings
from tests.support.data import add_chat
from tests.support.html import Element, parse
from tests.support.media import make_video, probe

pytestmark = pytest.mark.anyio

CHANNEL = "some_channel"
USER_ID = 41
DASHBOARD = "http://pc.tail1234.ts.net:8000"
STREAMER = {"username_color": "#00ff00", "badges": [{"type": "broadcaster", "text": "Broadcaster"}]}
# When the footage a test hands over was on air.
BEGAN = datetime(2026, 3, 1, 20, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def notified(service, telegram, discord, monkeypatch):
    """The service set up to send notifications to both, with links to the
    dashboard - and a watched channel."""
    monkeypatch.setenv("DASHBOARD_URL", DASHBOARD)
    monkeypatch.setattr(service.main, "settings", load_settings())
    service.main._notifier = None
    service.watch(CHANNEL, broadcaster_user_id=USER_ID)
    return service


def told(telegram) -> list[str]:
    """The title of every note Telegram got, after the channel it is of."""
    return [shown.split("\n")[0] for shown in telegram.shown()]


async def switch(service, name: str, on: bool) -> None:
    response = await service.client.post(f"/settings/{name}?enabled={int(on)}")
    assert response.status_code == 200


async def page_at(service, path: str) -> Element:
    response = await service.client.get(path)
    assert response.status_code == 200, response.text
    return parse(response.text)


class TestAClipSaved:
    @pytest.fixture
    def cut(self, service, monkeypatch):
        """Cuts the clip of a moment fired 1:23:45 into the stream, from a
        stand-in for the recorder: a file of `footage`, or a few bytes, of
        which the clip is `seconds` long with `before` and `after` around it."""

        def stand_in(footage: Path | None, seconds: float, before: float, after: float):
            async def create_clip(channel, start, end, name, **framing):
                path = recorder.CLIPS_DIR / channel / name
                path.parent.mkdir(parents=True, exist_ok=True)
                if footage is None:
                    path.write_bytes(b"clip")
                else:
                    shutil.copyfile(footage, path)
                return recorder.CutClip(path, BEGAN, BEGAN + timedelta(seconds=seconds), before, after)

            monkeypatch.setattr(service.main.recording_manager, "create_clip_for_moment", create_clip)

        async def cut(
            footage: Path | None = None, seconds: float = 35, before: float = 0.0, after: float = 0.0, **moment
        ) -> int:
            stand_in(footage, seconds, before, after)
            conn = db.get_connection()
            try:
                moment_id = db.insert_moment(
                    conn,
                    **{
                        "broadcaster_user_id": USER_ID,
                        "channel_slug": CHANNEL,
                        "window_start": BEGAN.isoformat(),
                        "window_end": BEGAN.isoformat(),
                        "reason": "laugh",
                        "score": 1.0,
                        "message_count": 1,
                        "baseline_message_rate": 1.0,
                        "current_message_rate": 1.0,
                        "emote_count": 0,
                        "keyword_hits": 0,
                        "stream_elapsed_seconds": 5025,
                        **moment,
                    },
                )
            finally:
                conn.close()
            tuned = service.main._tuning_now(USER_ID)
            await service.main._create_clip_background(moment_id, CHANNEL, BEGAN, BEGAN, tuned)
            await service.settle()
            return moment_id

        return cut

    async def test_it_is_told_of_everywhere_with_what_chat_said_and_where_to_see_it(
        self, notified, telegram, discord, cut
    ):
        for number in range(3):
            add_chat(CHANNEL, f"fan{number}", "KEKW", BEGAN + timedelta(seconds=number), USER_ID)

        moment_id = await cut()

        link = f"{DASHBOARD}/dashboard?moment={moment_id}"
        (message,) = telegram.sent
        assert message["shown"] == (
            "some_channel · Clip saved\n35 seconds, 1:23:45 into the stream.\n"
            f"Chat: KEKW {TIMES}3\nOpen on the dashboard"
        )
        assert message["links"] == [link]
        (embed,) = discord.embeds()
        assert (embed["author"], embed["title"], embed["url"]) == ({"name": CHANNEL}, "Clip saved", link)
        assert embed["description"] == f"35 seconds, 1:23:45 into the stream.\nChat: KEKW {TIMES}3"

    async def test_one_chat_said_nothing_in_particular_about_says_what_set_it_off(self, notified, telegram, cut):
        await cut(reason="laugh", stream_elapsed_seconds=None)

        assert telegram.shown()[0].split("\n")[1:3] == ["35 seconds.", "Set off by laughing."]

    async def test_one_asked_for_in_chat_says_so(self, notified, telegram, cut):
        await cut(reason="command")

        assert telegram.shown()[0].split("\n")[2] == "Asked for in chat."

    async def test_switched_off_it_is_not(self, notified, telegram, discord, cut):
        await switch(notified, "notify_clips", False)

        await cut()

        assert telegram.sent == [] and discord.posted == []

    async def test_with_nowhere_to_send_it_nothing_is_asked_of_anyone(self, service, telegram, cut):
        # .env names a bot, but the service was started without it.
        service.watch(CHANNEL, broadcaster_user_id=USER_ID)

        await cut()

        assert service.main._notes().destinations == ()
        assert telegram.requests == 0

    @pytest.mark.ffmpeg
    async def test_with_the_clip_itself_where_that_is_switched_on_and_it_fits(
        self, notified, telegram, discord, cut, tmp_path
    ):
        footage = make_video(tmp_path / "footage.mp4", 14, keyframe_every=1.0)
        discord.file_max_bytes = 100
        await switch(notified, "notify_clip_videos", True)

        await cut(footage, seconds=6, before=4, after=4)

        (message,) = telegram.sent
        assert message["method"] == "sendVideo"
        name, content = message["video"]
        assert name.startswith("moment_")
        sent = tmp_path / "sent.mp4"
        sent.write_bytes(content)
        # The clip alone, without the footage around it.
        assert probe(sent)["duration"] == pytest.approx(6, abs=1.2)
        (posted,) = discord.posted
        assert posted["file"] is None
        assert discord.embeds()[0]["description"].endswith(" is more than Discord takes.")
        # Nothing of it is left behind.
        assert sorted(path.name for path in (recorder.CLIPS_DIR / CHANNEL).iterdir()) == [f"{name}"]

    async def test_a_clip_that_cannot_be_cut_out_goes_as_words(self, notified, telegram, cut, monkeypatch):
        await switch(notified, "notify_clip_videos", True)

        # Not a video ffmpeg can cut anything out of.
        await cut(None, seconds=6, before=4, after=4)

        (message,) = telegram.sent
        assert message["method"] == "sendMessage"
        assert message["shown"].split("\n")[-2] == "The clip could not be cut out to be sent."


class TestARecording:
    async def test_one_started_from_the_dashboard_says_so_and_leads_to_it(self, notified, telegram, discord):
        response = await notified.client.post(f"/channels/{CHANNEL}/recording/start")
        assert response.status_code == 200
        await notified.settle()

        (shown,) = telegram.shown()
        assert shown.split("\n")[:3] == [
            "some_channel · Recording started",
            "Recording the stream from now on.",
            "Started from the dashboard.",
        ]
        recording_id = recording_manager.stream_recording_of(CHANNEL).recording_id
        assert telegram.sent[0]["links"] == [f"{DASHBOARD}/dashboard/recordings?recording={recording_id}"]
        assert discord.embeds()[0]["title"] == "Recording started"

    async def test_one_asked_for_in_chat_says_who_asked(self, notified, telegram):
        response = await notified.client.post(
            f"/channels/{CHANNEL}/tuning", json={"values": {"record_commands": "!rec"}}
        )
        assert response.status_code == 200

        response = await notified.chat(CHANNEL, "the_streamer", "!rec", identity=STREAMER)
        assert response.status_code == 200
        await notified.settle()

        assert telegram.shown()[0].split("\n")[2] == "Asked for in chat by the_streamer."

    async def test_one_started_by_itself_says_the_channel_is_recorded_always(self, notified, telegram):
        conn = db.get_connection()
        try:
            db.set_streamer_always_record(conn, USER_ID, True)
        finally:
            conn.close()

        await notified.main._record_stream_by_itself(CHANNEL)
        await notified.settle()

        assert telegram.shown()[0].split("\n")[2] == "Started by itself: the channel is recorded always."

    async def test_one_stopped_before_its_stream_began_says_nothing_was_recorded(self, notified, telegram):
        await notified.client.post(f"/channels/{CHANNEL}/recording/start")
        await notified.client.post(f"/channels/{CHANNEL}/recording/stop")
        await notified.settle()

        assert told(telegram) == ["some_channel · Recording started", "some_channel · Recording ended"]
        assert telegram.shown()[1].split("\n")[1] == "It was stopped. Nothing had been recorded yet."

    @pytest.mark.ffmpeg
    async def test_one_that_was_saved_says_why_it_ended_and_what_it_holds(self, notified, telegram, ts_segments):
        await notified.client.post(f"/channels/{CHANNEL}/recording/start")
        kept = recording_manager.stream_recording_of(CHANNEL)
        for index, path in enumerate(ts_segments[:4]):
            kept.writer.add(BEGAN + timedelta(seconds=index), 1.0, "7-0", path.read_bytes())

        await notified.main._stream_recording_over(CHANNEL, stream_recording.STREAM_ENDED)
        await notified.settle()

        assert told(telegram) == ["some_channel · Recording started", "some_channel · Recording saved"]
        lines = telegram.shown()[1].split("\n")
        assert lines[1] == "The stream is over."
        assert lines[2].startswith("4 seconds of the stream, ") and lines[2].endswith(" MB.")
        assert telegram.sent[1]["links"] == [f"{DASHBOARD}/dashboard/recordings?recording={kept.recording_id}"]

    async def test_one_that_could_not_be_saved_says_the_recordings_page_can_try_again(
        self, notified, telegram, monkeypatch, caplog
    ):
        def fails(folder, output):
            raise OSError("no room")

        monkeypatch.setattr(stream_recording, "save", fails)
        await notified.client.post(f"/channels/{CHANNEL}/recording/start")
        kept = recording_manager.stream_recording_of(CHANNEL)
        kept.writer.add(BEGAN, 1.0, "7-0", b"G" * 188)

        with caplog.at_level(logging.ERROR, logger="kick_clip_hunter"):
            await notified.client.post(f"/channels/{CHANNEL}/recording/stop")
            await notified.settle()

        assert told(telegram)[-1] == "some_channel · Recording not saved"

    async def test_switched_off_none_of_it_is_told(self, notified, telegram):
        await switch(notified, "notify_recordings", False)

        await notified.client.post(f"/channels/{CHANNEL}/recording/start")
        await notified.client.post(f"/channels/{CHANNEL}/recording/stop")
        await notified.settle()

        assert telegram.sent == []


class TestTheHealthPageWatched:
    @pytest.fixture
    def full(self, monkeypatch):
        """Makes the disk too full for a recording, or not."""

        def full(on: bool) -> None:
            free = recording_manager.MIN_FREE_BYTES // 2 if on else 500 * 1024**3
            monkeypatch.setattr(stream_recording, "free_bytes", lambda: free)

        return full

    async def test_a_part_that_needs_a_look_for_a_while_is_told_of_and_again_once_it_is_fine(
        self, notified, telegram, discord, full
    ):
        now = datetime.now(timezone.utc)
        full(True)

        notified.main._tell_of_health(now)
        await notified.settle()
        assert telegram.sent == []

        notified.main._tell_of_health(now + timedelta(seconds=notified.main.HEALTH_NOTE_AFTER_SECONDS))
        await notified.settle()
        assert told(telegram) == ["The disk needs a look"]
        assert telegram.shown()[0].split("\n")[1].startswith("Only ")
        assert telegram.sent[0]["links"] == [f"{DASHBOARD}/dashboard/health"]
        assert discord.embeds()[0]["color"] == notify.TROUBLE_COLOUR

        full(False)
        notified.main._tell_of_health(now + timedelta(seconds=notified.main.HEALTH_NOTE_AFTER_SECONDS + 60))
        await notified.settle()
        assert told(telegram) == ["The disk needs a look", "The disk looks fine again"]

    async def test_switched_off_nothing_is_told_or_kept(self, notified, telegram, full):
        now = datetime.now(timezone.utc)
        full(True)
        notified.main._tell_of_health(now)
        await switch(notified, "notify_health", False)

        notified.main._tell_of_health(now + timedelta(hours=1))
        await switch(notified, "notify_health", True)
        notified.main._tell_of_health(now + timedelta(hours=1, seconds=1))
        await notified.settle()

        # Found anew once it is on again, and told of once it has lasted.
        assert telegram.sent == []
        notified.main._tell_of_health(now + timedelta(hours=2))
        await notified.settle()
        assert told(telegram) == ["The disk needs a look"]

    async def test_nothing_is_told_while_the_service_shuts_down(self, notified, telegram, full):
        now = datetime.now(timezone.utc)
        full(True)
        notified.main._shutdown_requested = True

        for minutes in (0, 5, 10):
            notified.main._tell_of_health(now + timedelta(minutes=minutes))
        await notified.settle()

        assert telegram.sent == []

    async def test_it_is_looked_at_every_so_often_once_the_service_runs(self, notified, telegram, full, monkeypatch):
        import asyncio

        looked = []
        monkeypatch.setattr(notified.main, "_tell_of_health", lambda: looked.append(True))
        task = asyncio.create_task(notified.main._keep_telling_of_health())
        try:
            # main's waits run a thousand times faster here.
            while len(looked) < 2:
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
        assert looked == [True, True]


class TestTheHealthPage:
    async def test_it_says_where_notifications_go_and_what_they_tell_of(self, notified):
        page = await page_at(notified, "/dashboard/health")

        part = page.one("div", data_health_part="notifications")
        assert part.one("p", class_="health-said").text == "Notifications go to Telegram and Discord."
        assert [sentence.text for sentence in part.find("p", class_="health-more")] == [
            "They tell of clips, recordings of whole streams and what on this page needs a look. "
            "The Channels page switches each kind on and off.",
            f"Their links lead to the dashboard at {DASHBOARD}.",
        ]
        rows = [[cell.text for cell in row.find("th") + row.find("td")] for row in part.one("tbody").find("tr")]
        assert rows == [
            ["Telegram", "chat 4242", "Nothing yet", ""],
            ["Discord", "the webhook's channel", "Nothing yet", ""],
        ]
        assert page.find("button", data_notify_test=True)

    async def test_a_destination_that_turned_the_last_one_down_is_trouble(self, notified, telegram, discord):
        discord.gone = True

        response = await notified.client.post("/notifications/test")

        assert response.json() == {"sent": 1, "of": 2}
        assert telegram.shown() == ["Test\nNotifications from kick-clip-hunter arrive here.\nOpen on the dashboard"]
        page = await page_at(notified, "/dashboard/health")
        part = page.one("div", data_health_part="notifications")
        said = part.one("p", class_="health-said")
        assert said.text == "The last notification could not be sent to Discord." and "is-trouble" in said.classes
        assert "Unknown Webhook" in part.one("tbody").find("tr")[1].text
        assert page.one("span", data_health_flag=True).text == "1 problem"

        discord.gone = False
        assert (await notified.client.post("/notifications/test")).json() == {"sent": 2, "of": 2}
        page = await page_at(notified, "/dashboard/health")
        assert page.one("span", data_health_flag=True).text == ""

    async def test_switched_off_kinds_and_no_dashboard_address_are_said(self, notified, monkeypatch):
        for name in ("notify_clips", "notify_recordings", "notify_health"):
            await switch(notified, name, False)
        monkeypatch.setenv("DASHBOARD_URL", "")
        monkeypatch.setattr(notified.main, "settings", load_settings())

        page = await page_at(notified, "/dashboard/health")

        assert [sentence.text for sentence in page.one("div", data_health_part="notifications").find("p")][1:] == [
            "They tell of nothing: the Channels page has every kind switched off.",
            "They have no links to the dashboard: DASHBOARD_URL in .env says where it is reached from.",
        ]

    async def test_with_nowhere_to_send_one_there_is_no_test_to_send(self, service):
        response = await service.client.post("/notifications/test")

        assert response.status_code == 409
        assert response.json()["detail"] == "Notifications go nowhere: .env sets up none."
        page = await page_at(service, "/dashboard/health")
        assert page.find("div", data_health_part="notifications") == []


class TestTheChannelsPage:
    async def test_each_kind_has_a_switch(self, notified):
        page = await page_at(notified, "/dashboard/channels")

        section = page.one("section", aria_labelledby="notify-title")
        assert section.one("p", class_="section-lead").text == (
            "They go to Telegram and Discord. Each kind is sent until it is switched off here."
        )
        switches = section.find("span", data_switch=True)
        assert [group.attrs["aria-label"] for group in switches] == [
            "Notify of clips",
            "Notify of clip videos",
            "Notify of recordings",
            "Notify of problems",
        ]
        assert [group.attrs["data-switch"] for group in switches] == [
            "/settings/notify_clips",
            "/settings/notify_clip_videos",
            "/settings/notify_recordings",
            "/settings/notify_health",
        ]

    async def test_without_anywhere_to_send_them_it_says_how_they_are_set_up(self, service):
        page = await page_at(service, "/dashboard/channels")

        section = page.one("section", aria_labelledby="notify-title")
        assert section.one("p", class_="section-lead").text.startswith("None are set up. TELEGRAM_BOT_TOKEN")
        assert section.find("span", data_switch=True) == []


class TestStarting:
    async def test_where_they_go_and_what_keeps_them_from_going_is_logged(self, service, monkeypatch, caplog):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.com/not-a-webhook")
        monkeypatch.setattr(service.main, "settings", load_settings())

        with caplog.at_level(logging.INFO, logger="kick_clip_hunter"):
            service.main._say_where_notes_go()

        assert "notifications: TELEGRAM_BOT_TOKEN is set but TELEGRAM_CHAT_ID is not" in caplog.text
        assert "notifications: DISCORD_WEBHOOK_URL does not look like a Discord webhook's address" in caplog.text
        assert "notifications go to Discord" in caplog.text
