"""Notifications, through the running service: what is told of - a clip
saved, a recording started, saved or not, a part of the Health page that
needs a look and looks fine again - where it goes (the fake Telegram and
Discord, through the real clients), the switches that keep each kind from
going, what the Health page says of it all, and setting it all up on the
Settings page.
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
from tests.support.data import add_chat, add_moment, add_stream_recording
from tests.support.html import Element, parse
from tests.support.media import make_video, probe
from tests.support.notify import TELEGRAM_CHAT, FakeDiscord, FakeTelegram

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
        # The clip goes with it to begin with: this one's file is the clip alone.
        assert message["method"] == "sendVideo" and message["video"][1] == b"clip"
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

    async def test_without_the_clip_itself_where_that_is_switched_off(self, notified, telegram, discord, cut):
        await switch(notified, "notify_clip_videos", False)

        await cut()

        assert [message["method"] for message in telegram.sent] == ["sendMessage"]
        assert [message["file"] for message in discord.posted] == [None]

    @pytest.mark.ffmpeg
    async def test_with_the_clip_itself_without_the_footage_around_it_where_it_fits(
        self, notified, telegram, discord, cut, tmp_path
    ):
        footage = make_video(tmp_path / "footage.mp4", 14, keyframe_every=1.0)
        discord.file_max_bytes = 100

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
        # Not a video ffmpeg can cut anything out of.
        await cut(None, seconds=6, before=4, after=4)

        (message,) = telegram.sent
        assert message["method"] == "sendMessage"
        assert message["shown"].split("\n")[-2] == "The clip could not be cut out to be sent."


class TestABotApiServerOfOnesOwn:
    async def test_telegram_is_asked_there_and_it_takes_a_clip_too_big_for_telegrams_own(
        self, service, telegram, cut_with_room, monkeypatch, tmp_path
    ):
        telegram.hosts = {"localhost"}
        telegram.video_max_bytes = notify.TELEGRAM_LOCAL_VIDEO_MAX_BYTES
        monkeypatch.setenv("TELEGRAM_API_URL", "http://localhost:8081/")
        monkeypatch.setattr(service.main, "settings", load_settings())
        service.watch(CHANNEL, broadcaster_user_id=USER_ID)

        await cut_with_room(60 * 1024**2)

        (message,) = telegram.sent
        assert message["method"] == "sendVideo" and len(message["video"][1]) == 60 * 1024**2
        assert service.main._notes().destinations[0].where() == f"chat {TELEGRAM_CHAT} through http://localhost:8081"

    @pytest.fixture
    def cut_with_room(self, service, monkeypatch):
        """Saves the clip of a moment, the file of which is the clip alone,
        `size` bytes of it."""

        async def cut(size: int) -> None:
            async def create_clip(channel, start, end, name, **framing):
                path = recorder.CLIPS_DIR / channel / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"v" * size)
                return recorder.CutClip(path, BEGAN, BEGAN + timedelta(seconds=35))

            monkeypatch.setattr(service.main.recording_manager, "create_clip_for_moment", create_clip)
            conn = db.get_connection()
            try:
                moment_id = db.insert_moment(
                    conn,
                    broadcaster_user_id=USER_ID,
                    channel_slug=CHANNEL,
                    window_start=BEGAN.isoformat(),
                    window_end=BEGAN.isoformat(),
                    reason="laugh",
                    score=1.0,
                    message_count=1,
                    baseline_message_rate=1.0,
                    current_message_rate=1.0,
                    emote_count=0,
                    keyword_hits=0,
                    stream_elapsed_seconds=5025,
                )
            finally:
                conn.close()
            await service.main._create_clip_background(
                moment_id, CHANNEL, BEGAN, BEGAN, service.main._tuning_now(USER_ID)
            )
            await service.settle()

        return cut

    async def test_without_it_telegrams_own_is_asked_and_takes_50_mb(self, notified, telegram):
        (destination, _discord) = notified.main._notes().destinations

        assert destination.where() == f"chat {TELEGRAM_CHAT}"
        assert destination.video_max_bytes == notify.TELEGRAM_VIDEO_MAX_BYTES


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
            f"/channels/{CHANNEL}/tuning", json={"values": {"record_commands": ["!rec"]}}
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


class TestTheDaysSummary:
    @pytest.fixture
    def morning(self):
        """Half past the hour the summary goes out at, today, local time."""
        return datetime.now().astimezone().replace(hour=9, minute=30, second=0, microsecond=0)

    @pytest.fixture
    def free(self, monkeypatch):
        monkeypatch.setattr(stream_recording, "free_bytes", lambda: 500 * 1024**3)

    def a_day(self, ended: datetime) -> None:
        """Clips, a recording and chat in the day that `ended`, and some
        on either side of it."""
        ended = ended.astimezone(timezone.utc)  # as the service stores its times
        for hours in (1, 2):
            add_moment(CHANNEL, detected_at=ended - timedelta(hours=hours), broadcaster_user_id=USER_ID)
        add_moment("other_channel", detected_at=ended - timedelta(hours=3), rating=4)
        add_moment(CHANNEL, detected_at=ended - timedelta(hours=25), broadcaster_user_id=USER_ID)
        add_moment(CHANNEL, detected_at=ended + timedelta(minutes=10), broadcaster_user_id=USER_ID)
        add_stream_recording(
            CHANNEL, broadcaster_user_id=USER_ID, started_at=ended - timedelta(hours=3), seconds=2 * 3600
        )
        for minutes in (10, 20, 30):
            add_chat(CHANNEL, "alice", "hi", ended - timedelta(minutes=minutes), broadcaster_user_id=USER_ID)
        add_chat(CHANNEL, "alice", "too early", ended - timedelta(hours=30), broadcaster_user_id=USER_ID)

    async def test_once_due_it_tells_what_the_last_day_held(self, notified, telegram, discord, morning, free):
        ended = morning.replace(minute=0)
        self.a_day(ended)

        assert await notified.main._sum_up_day(morning) is True
        await notified.settle()

        assert told(telegram) == ["The last 24 hours"]
        assert telegram.shown()[0].split("\n")[1:7] == [
            "3 clips: some_channel 2, other_channel 1.",
            "4 clips wait to be rated.",
            "1 recording saved: 2 hours of streams, 3 GB.",
            "3 chat messages taken in.",
            "Nothing on the Health page needs a look.",
            "500 GB free on the disk recordings are kept on.",
        ]
        assert telegram.sent[0]["links"] == [f"{DASHBOARD}/dashboard"]
        assert discord.embeds()[0]["color"] == notify.COLOURS[notify.DAILY]

    async def test_a_day_is_summed_up_once(self, notified, telegram, morning, free):
        assert await notified.main._sum_up_day(morning) is True
        assert await notified.main._sum_up_day(morning + timedelta(hours=5)) is False
        assert await notified.main._sum_up_day(morning + timedelta(days=1)) is True
        await notified.settle()

        assert told(telegram) == ["The last 24 hours", "The last 24 hours"]

    async def test_days_missed_while_the_service_was_not_running_are_one_summary_of_the_last(
        self, notified, telegram, morning, free
    ):
        assert await notified.main._sum_up_day(morning - timedelta(days=3)) is True

        assert await notified.main._sum_up_day(morning) is True
        assert await notified.main._sum_up_day(morning + timedelta(minutes=1)) is False
        await notified.settle()

        assert len(telegram.sent) == 2

    async def test_a_quiet_day_says_so(self, notified, telegram, morning, free):
        await notified.main._sum_up_day(morning)
        await notified.settle()

        assert telegram.shown()[0].split("\n")[1:3] == ["No clips.", "0 chat messages taken in."]

    async def test_trouble_on_the_health_page_is_said_as_trouble(self, notified, discord, morning, monkeypatch):
        monkeypatch.setattr(stream_recording, "free_bytes", lambda: recording_manager.MIN_FREE_BYTES // 2)

        await notified.main._sum_up_day(morning)
        await notified.settle()

        (embed,) = discord.embeds()
        assert embed["color"] == notify.TROUBLE_COLOUR
        assert "The disk needs a look." in embed["description"]

    async def test_switched_off_nothing_is_sent_or_marked_as_sent(self, notified, telegram, morning, free):
        await switch(notified, "notify_daily", False)
        assert await notified.main._sum_up_day(morning) is False

        await switch(notified, "notify_daily", True)
        assert await notified.main._sum_up_day(morning) is True

    async def test_with_nowhere_to_send_it_nothing_is_made(self, service, morning):
        assert await service.main._sum_up_day(morning) is False

    async def test_it_is_looked_at_every_so_often_once_the_service_runs(self, notified, monkeypatch):
        import asyncio

        looked = []

        async def sum_up_day():
            looked.append(True)

        monkeypatch.setattr(notified.main, "_sum_up_day", sum_up_day)
        task = asyncio.create_task(notified.main._keep_summing_up_days())
        try:
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
            "They tell of clips, with the clip itself where it fits, recordings of whole streams and "
            "what on this page needs a look. "
            "The Settings page switches each kind on and off.",
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
            "They tell of nothing: the Settings page has every kind switched off.",
            "They have no links to the dashboard: the Settings page says where it is reached from.",
        ]

    async def test_with_nowhere_to_send_one_there_is_no_test_to_send(self, service):
        response = await service.client.post("/notifications/test")

        assert response.status_code == 409
        assert response.json()["detail"] == "Notifications go nowhere: the Settings page sets up none."
        page = await page_at(service, "/dashboard/health")
        assert page.find("div", data_health_part="notifications") == []


class TestTheSettingsPage:
    async def test_has_a_tab_for_them_on_the_page_for_all_channels_only(self, notified):
        everyone = await page_at(notified, "/dashboard/settings")
        one_channel = await page_at(notified, f"/dashboard/settings?channel={CHANNEL}")
        channels = await page_at(notified, "/dashboard/channels")

        assert [tab.attrs["data-section"] for tab in everyone.find(role="tab")][-2:] == ["notifications", "trial"]
        assert "notifications" not in [tab.attrs["data-section"] for tab in one_channel.find(role="tab")]
        assert one_channel.find(data_notify_setup=True) == []
        # ...which is where they were before.
        assert channels.find("section", aria_labelledby="notify-title") == []

    async def test_says_where_they_go_and_has_each_kind_switched_with_it(self, notified):
        await switch(notified, "notify_recordings", False)

        page = await page_at(notified, "/dashboard/settings?section=notifications")

        pane = page.one(data_notify_setup=True)
        assert "hidden" not in pane.attrs
        assert pane.one("p", class_="section-lead").text == (
            "Notifications go to Telegram and Discord. What is saved here holds from the next one on; what was "
            "never set here comes from .env."
        )
        assert [heading.text for heading in pane.find("h3")] == ["Telegram", "Discord", "Links", "What they tell of"]
        kinds = pane.find(data_notify_kind=True)
        assert [kind.attrs["data-notify-kind"] for kind in kinds] == [
            "notify_clips",
            "notify_clip_videos",
            "notify_moments",
            "notify_recordings",
            "notify_health",
            "notify_daily",
        ]
        lit = [[half.text for half in kind.find("button") if half.attrs["aria-pressed"] == "true"] for kind in kinds]
        assert lit == [["On"], ["On"], ["On"], ["Off"], ["On"], ["On"]]
        assert "disabled" not in pane.one("button", data_notify_test=True).attrs
        assert page.one("main", data_settings=True).attrs["data-shown"] == "notifications"

    async def test_shows_what_comes_from_dotenv_but_never_a_secret(self, notified, telegram, discord):
        response = await notified.client.get("/dashboard/settings?section=notifications")
        page = parse(response.text)

        assert telegram.token not in response.text
        assert discord.url not in response.text
        token = page.one("input", data_notify_field="telegram_bot_token")
        assert (token.attrs["type"], token.attrs["value"]) == ("password", "")
        assert token.attrs["placeholder"] == "Set in .env - type to replace"
        assert page.one("input", data_notify_field="discord_webhook_url").attrs["value"] == ""
        assert page.one("input", data_notify_field="telegram_chat_id").attrs["value"] == TELEGRAM_CHAT
        assert page.one("input", data_notify_field="dashboard_url").attrs["value"] == DASHBOARD
        assert page.one("input", data_notify_field="telegram_chat_id").attrs["type"] == "text"
        assert len(page.find("button", data_notify_remove=True)) == 2
        assert page.find("button", data_find_chats=True)
        captions = [caption.text for caption in page.one(data_notify_setup=True).find("p", class_="ch-cap")]
        assert all(caption.endswith("It comes from .env.") for caption in captions[:2])

    async def test_without_anywhere_to_send_them_it_says_so(self, service):
        page = await page_at(service, "/dashboard/settings?section=notifications")

        pane = page.one(data_notify_setup=True)
        assert pane.one("p", class_="section-lead").text.startswith("Notifications go nowhere yet")
        assert "disabled" in pane.one("button", data_notify_test=True).attrs
        assert pane.one("input", data_notify_field="telegram_bot_token").attrs["placeholder"] == "123456789:AAH..."
        assert pane.find("button", data_notify_remove=True) == []


def saved_setup() -> dict[str, str]:
    conn = db.get_connection()
    try:
        return db.get_notify_setup(conn)
    finally:
        conn.close()


async def set_up(service, **setup):
    return await service.client.post("/notifications/setup", json={"setup": setup})


class TestSettingThemUpOnThePage:
    @pytest.fixture
    def fakes(self, service):
        """A fake Telegram and Discord, with nothing about them in .env."""
        fakes = FakeTelegram(), FakeDiscord()
        service.api.others.extend(fakes)
        return fakes

    async def test_what_is_saved_holds_at_once(self, service, fakes, caplog):
        telegram, discord = fakes

        with caplog.at_level(logging.INFO, logger="kick_clip_hunter"):
            response = await set_up(
                service,
                telegram_bot_token=telegram.token,
                telegram_chat_id=TELEGRAM_CHAT,
                discord_webhook_url=discord.url,
                dashboard_url=DASHBOARD,
            )
            tested = await service.client.post("/notifications/test")

        assert response.status_code == 200, response.text
        assert response.json() == {"destinations": ["Telegram", "Discord"]}
        assert tested.json() == {"sent": 2, "of": 2}
        assert telegram.sent[0]["links"] == [f"{DASHBOARD}/dashboard/health"]
        assert discord.embeds()[0]["title"] == "Test"
        assert "notifications set up from the dashboard -> Telegram and Discord" in caplog.text
        assert saved_setup() == {
            "telegram_bot_token": telegram.token,
            "telegram_chat_id": TELEGRAM_CHAT,
            "discord_webhook_url": discord.url,
            "dashboard_url": DASHBOARD,
        }

    async def test_a_secret_left_out_stays_as_it_is(self, service, fakes):
        telegram, _discord = fakes
        await set_up(service, telegram_bot_token=telegram.token, telegram_chat_id="1")
        telegram.chats.add("1")

        response = await set_up(service, telegram_chat_id=TELEGRAM_CHAT)

        assert response.json() == {"destinations": ["Telegram"]}
        await service.client.post("/notifications/test")
        assert [message["chat_id"] for message in telegram.sent] == [TELEGRAM_CHAT]

    async def test_saved_it_takes_the_place_of_dotenv_and_empty_it_says_none(self, notified, telegram, discord):
        telegram.chats.add("-1001234")

        response = await set_up(notified, telegram_chat_id="-1001234", discord_webhook_url="")
        await notified.client.post("/notifications/test")

        assert response.json() == {"destinations": ["Telegram"]}
        assert [message["chat_id"] for message in telegram.sent] == ["-1001234"]
        assert discord.posted == []
        page = await page_at(notified, "/dashboard/settings?section=notifications")
        assert page.one("input", data_notify_field="telegram_chat_id").attrs["value"] == "-1001234"
        (row,) = [row for row in page.find("div", class_="set-row") if row.find(data_notify_field="telegram_chat_id")]
        assert "is-set" in row.classes

    async def test_what_dotenv_has_anyway_goes_back_to_following_it(self, notified):
        await set_up(notified, telegram_chat_id="77", dashboard_url="http://elsewhere:8000")

        await set_up(notified, telegram_chat_id=f" {TELEGRAM_CHAT} ", dashboard_url=DASHBOARD)

        assert saved_setup() == {}

    async def test_the_kinds_are_saved_with_it(self, notified):
        response = await notified.client.post(
            "/notifications/setup", json={"setup": {}, "kinds": {"notify_clips": False, "notify_health": False}}
        )

        assert response.status_code == 200
        assert (notified.main._notifying("notify_clips"), notified.main._notifying("notify_health")) == (False, False)
        assert notified.main._notifying("notify_recordings") is True
        conn = db.get_connection()
        try:
            assert db.get_flag(conn, "notify_clips_enabled") is False
        finally:
            conn.close()

    @pytest.mark.parametrize(
        ("setup", "about"),
        [
            ({"telegram_bot_token": "123456789:abc"}, '"Chat" is needed as well'),
            ({"telegram_chat_id": "4242"}, '"Bot token" is needed as well'),
            ({"telegram_bot_token": "not a token", "telegram_chat_id": "4242"}, '"Bot token" is not a bot'),
            ({"telegram_bot_token": "123:abc", "telegram_chat_id": "my chat"}, '"Chat" has to be'),
            (
                {"telegram_bot_token": "123:abc", "telegram_chat_id": "1", "telegram_api_url": "localhost:8081"},
                "Bot API",
            ),
            ({"discord_webhook_url": "https://example.com/hook"}, '"Webhook" is not a Discord webhook'),
            ({"dashboard_url": "pc.tailnet:8000"}, '"Dashboard address" has to be'),
        ],
    )
    async def test_what_could_not_work_is_refused_and_nothing_is_saved(self, service, setup, about):
        response = await set_up(service, **setup)

        assert response.status_code == 400
        assert about in response.json()["detail"]
        assert saved_setup() == {}

    @pytest.mark.parametrize(
        "body",
        [
            {"setup": {"telegram_bot_token": 7}},
            {"setup": {"password": "x"}},
            {"setup": [], "kinds": {}},
            {"kinds": {"notify_clips": "off"}},
            {"kinds": {"watching": False}},
        ],
    )
    async def test_a_request_that_is_not_the_setup_is_refused(self, service, body):
        response = await service.client.post("/notifications/setup", json=body)

        assert response.status_code == 400
        assert response.json()["detail"] == "expected where notifications go and which kinds, as JSON"


class TestFindingTheChat:
    @pytest.fixture
    def telegram_alone(self, service):
        fake = FakeTelegram(
            updates=[
                {"update_id": 1, "message": {"chat": {"id": 4242, "type": "private", "first_name": "Alice"}}},
                {"update_id": 2, "message": {"chat": {"id": -100, "type": "group", "title": "Clips"}}},
            ]
        )
        service.api.others.append(fake)
        return fake

    async def test_lists_the_chats_that_wrote_to_the_bot_of_the_token_typed(self, service, telegram_alone):
        response = await service.client.post("/notifications/telegram/chats", json={"token": telegram_alone.token})

        assert response.status_code == 200, response.text
        assert response.json() == {
            "bot": "clip_hunter_bot",
            "chats": [
                {"id": "4242", "kind": "private", "name": "Alice"},
                {"id": "-100", "kind": "group", "name": "Clips"},
            ],
        }

    async def test_or_of_the_token_that_is_set(self, notified, telegram):
        telegram.updates = [{"update_id": 1, "message": {"chat": {"id": 4242, "type": "private", "first_name": "A"}}}]

        response = await notified.client.post("/notifications/telegram/chats", json={"token": ""})

        assert [chat["id"] for chat in response.json()["chats"]] == ["4242"]

    async def test_without_a_token_it_says_what_is_needed(self, service):
        response = await service.client.post("/notifications/telegram/chats", json={})

        assert response.status_code == 400
        assert response.json()["detail"].startswith('"Bot token" is needed first')

    async def test_a_token_telegram_turns_down_is_said_without_it(self, service, telegram_alone):
        response = await service.client.post("/notifications/telegram/chats", json={"token": "1:wrong-token"})

        assert response.status_code == 502
        assert response.json()["detail"].startswith("Telegram did not answer")
        assert "wrong-token" not in response.text


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
