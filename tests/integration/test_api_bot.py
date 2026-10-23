"""The bot in chat, through the running service.

The commands a channel's streamer and moderators post in its chat - start a
recording of the stream, stop it, ask how things stand - are carried out
whatever the switches say, as the buttons on the Channels page are; what the
bot writes in the channel's chat about them, and about the recordings and
clips it makes, is written where the channel has it writing there, and
nowhere else. All of it is off for every channel to begin with. And the
account the bot writes in Kick's chat as signs in from the dashboard.

Kick's chat arrives as signed webhooks, Twitch's and YouTube's as the
batches their readers hand on; what the bot writes goes to the fake Kick
and Twitch, through the real clients.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from kick_clip_hunter import (
    account_tokens,
    db,
    health,
    kick_auth,
    recorder,
    recording_manager,
    stream_recording,
)
from kick_clip_hunter.chat_message import ChatMessage
from kick_clip_hunter.config import load_settings
from kick_clip_hunter.recording_manager import Look
from tests.support.bot import kick_bot_signed_in, twitch_signed_in
from tests.support.data import stream_recordings
from tests.support.html import Element, parse
from tests.support.kick_api import FakeKickAccount
from tests.support.twitch_api import FakeAccount
from tests.support.webhook import chat_payload

pytestmark = pytest.mark.anyio

CHANNEL = "some_channel"
USER_ID = 41
STREAMER = {"username_color": "#00ff00", "badges": [{"type": "broadcaster", "text": "Broadcaster"}]}
MODERATOR = {"username_color": "#00ff00", "badges": [{"type": "moderator", "text": "Moderator"}]}
# The commands, as a channel's settings take them.
COMMANDS = {"record_commands": ["!record start"], "stop_commands": ["!record stop"], "status_commands": ["!status"]}
# When the footage a test hands over was on air.
BEGAN = datetime(2026, 3, 1, 20, 0, 0, tzinfo=timezone.utc)


async def set_up(service, channel: str = CHANNEL, **values) -> None:
    """Saves settings of a channel's own - which the bot's are."""
    response = await service.client.post(f"/channels/{channel}/tuning", json={"values": values})
    assert response.status_code == 200, response.text


async def ask(service, content: str, *, identity: dict | None = STREAMER, sender: str = "the_streamer") -> None:
    """Posts a message in the channel's chat, as its streamer unless said
    otherwise, and waits for what the service does about it."""
    response = await service.chat(CHANNEL, sender, content, identity=identity)
    assert response.status_code == 200, response.text
    await service.settle()


def written(service) -> list[str]:
    return service.api.written_in(USER_ID)


def recording() -> recording_manager.StreamRecording | None:
    return recording_manager.stream_recording_of(CHANNEL)


@pytest.fixture
def channel(service) -> int:
    """A watched Kick channel, with an account signed in for the bot."""
    service.watch(CHANNEL, broadcaster_user_id=USER_ID)
    kick_bot_signed_in(service.api)
    return USER_ID


def broadcast(segments: list[Path]) -> None:
    """Hands the channel's recording real footage, a second a segment, as
    its recorder would while the stream is live."""
    kept = recording()
    for index, path in enumerate(segments):
        kept.writer.add(BEGAN + timedelta(seconds=index), 1.0, "7-0", path.read_bytes())


class TestOffToBeginWith:
    async def test_a_channel_has_the_bot_write_nothing_and_take_no_command(self, service, channel):
        for command in ("!record start", "!status", "!record stop", "!rec", "!stop"):
            await ask(service, command)
        await service.client.post(f"/channels/{CHANNEL}/recording/start")
        await service.client.post(f"/channels/{CHANNEL}/recording/stop")
        await service.settle()

        assert stream_recordings() == []
        assert service.api.chat_written == []
        assert service.api.calls("POST", "/public/v1/chat") == []

    async def test_a_channel_that_has_it_does_not_have_it_for_another(self, service, channel):
        service.watch("another_channel", broadcaster_user_id=42)
        await set_up(service, bot_writes=True, **COMMANDS)

        response = await service.chat("another_channel", "its_streamer", "!record start", identity=STREAMER)
        assert response.status_code == 200
        await service.settle()

        assert recording_manager.stream_recordings() == {}
        assert service.api.chat_written == []


class TestARecordingOnCommand:
    async def test_the_streamer_starts_one_and_the_bot_says_so(self, service, channel):
        await set_up(service, bot_writes=True, **COMMANDS)

        await ask(service, "!record start")

        kept = recording()
        assert (kept.recording_id, kept.broadcaster_user_id) == (1, USER_ID)
        (row,) = stream_recordings()
        assert (row["channel_slug"], row["state"]) == (CHANNEL, stream_recording.RECORDING)
        assert written(service) == ["Recording the stream from now on."]
        assert service.api.chat_written[0]["sender"] == "clip_bot"

    async def test_for_a_channel_seen_off_the_air_it_says_from_when(self, service, channel, monkeypatch):
        await set_up(service, bot_writes=True, **COMMANDS)
        monkeypatch.setitem(recording_manager._looks, CHANNEL, Look(at=datetime.now(timezone.utc)))

        await ask(service, "!record start")

        assert written(service) == ["The stream will be recorded from when it starts."]

    async def test_a_moderator_may_ask_and_a_viewer_may_not(self, service, channel):
        await set_up(service, bot_writes=True, **COMMANDS)

        await ask(service, "!record start", identity=None, sender="a_viewer")
        assert recording() is None

        await ask(service, "  !RECORD START ", identity=MODERATOR, sender="a_mod")
        assert recording() is not None

    async def test_the_switches_have_no_say_in_it(self, service, channel):
        await set_up(service, bot_writes=True, **COMMANDS)
        await service.client.post(f"/channels/{CHANNEL}/tracking?enabled=0")
        await service.client.post("/settings/watching?enabled=0")

        await ask(service, "!record start")

        assert recording() is not None
        assert written(service) == ["Recording the stream from now on."]

    async def test_without_writing_in_chat_it_is_carried_out_without_an_answer(self, service, channel):
        await set_up(service, **COMMANDS)

        await ask(service, "!record start")

        assert recording() is not None
        assert service.api.chat_written == []

    async def test_asked_again_it_says_that_it_is_recording_already(self, service, channel):
        await set_up(service, bot_writes=True, **COMMANDS)
        await ask(service, "!record start")

        await ask(service, "!record start")

        assert len(stream_recordings()) == 1
        assert written(service) == [
            "Recording the stream from now on.",
            "Already recording, from when the stream starts.",
        ]

    async def test_a_command_delivered_twice_is_carried_out_once(self, service, channel):
        await set_up(service, bot_writes=True, **COMMANDS)
        payload = chat_payload(CHANNEL, USER_ID, "the_streamer", "!record start", identity=STREAMER)
        await service.deliver(payload)
        await service.settle()
        await service.client.post(f"/channels/{CHANNEL}/recording/stop")
        await service.settle()

        # Kick did not see it taken in time, and delivers it again.
        await service.deliver(payload)
        await service.settle()

        assert recording() is None
        assert len(written(service)) == 2  # started, and stopped from the page

    async def test_a_disk_too_full_is_the_answer(self, service, channel, monkeypatch):
        await set_up(service, bot_writes=True, **COMMANDS)
        monkeypatch.setattr(stream_recording, "free_bytes", lambda: recording_manager.MIN_FREE_BYTES // 2)

        await ask(service, "!record start")

        assert stream_recordings() == []
        assert written(service) == ["Cannot record: the disk is too full."]

    async def test_one_is_stopped_on_command_too(self, service, channel):
        await set_up(service, bot_writes=True, **COMMANDS)
        await ask(service, "!record start")

        await ask(service, "!record stop", identity=MODERATOR, sender="a_mod")

        assert recording() is None
        assert stream_recordings() == []  # it had kept nothing
        assert written(service)[-1] == "Recording stopped. Nothing had been recorded yet."

    @pytest.mark.ffmpeg
    async def test_one_stopped_with_footage_says_how_much_and_is_saved(self, service, channel, ts_segments):
        await set_up(service, bot_writes=True, **COMMANDS)
        await ask(service, "!record start")
        broadcast(ts_segments[:6])

        await ask(service, "!record stop")

        assert written(service)[-1] == "Recording stopped. 6 seconds of the stream recorded."
        (row,) = stream_recordings()
        assert (row["state"], row["ended_by"]) == (stream_recording.READY, stream_recording.STOPPED)

    async def test_stopped_on_command_a_channel_recorded_always_waits_for_its_next_stream(self, service, channel):
        await set_up(service, bot_writes=True, **COMMANDS)
        await service.client.post(f"/channels/{CHANNEL}/always_record?enabled=1")
        await ask(service, "!record start")

        await ask(service, "!record stop")

        assert recording_manager.is_held_off(CHANNEL)

    async def test_stopping_what_is_not_being_recorded_says_so(self, service, channel):
        await set_up(service, bot_writes=True, **COMMANDS)

        await ask(service, "!record stop")

        assert written(service) == ["Not recording."]

    async def test_none_is_carried_out_once_a_shutdown_was_asked_for(self, service, channel, monkeypatch):
        await set_up(service, bot_writes=True, **COMMANDS)
        monkeypatch.setattr(service.main, "_shutdown_requested", True)

        await ask(service, "!record start")

        assert recording() is None
        assert service.api.chat_written == []


class TestHowThingsStand:
    def add_moment(self, detected_at: datetime) -> None:
        conn = db.get_connection()
        try:
            db.insert_moment(
                conn,
                broadcaster_user_id=USER_ID,
                channel_slug=CHANNEL,
                window_start=detected_at.isoformat(),
                window_end=detected_at.isoformat(),
                reason="laugh",
                score=1.0,
                message_count=1,
                baseline_message_rate=1.0,
                current_message_rate=1.0,
                emote_count=0,
                keyword_hits=0,
                detected_at=detected_at.isoformat(),
            )
        finally:
            conn.close()

    async def test_it_answers_with_the_clips_of_this_stream_and_the_recording(self, service, channel, monkeypatch):
        await set_up(service, bot_writes=True, **COMMANDS)
        now = datetime.now(timezone.utc)
        monkeypatch.setitem(recording_manager._looks, CHANNEL, Look(at=now, live_since=now - timedelta(hours=1)))
        for hours in (3, 0.5, 0.25):
            self.add_moment(now - timedelta(hours=hours))

        await ask(service, "!status")
        await ask(service, "!record start")
        await ask(service, "!status", identity=MODERATOR, sender="a_mod")

        assert written(service) == [
            "Looking for clips, 2 clips this stream so far. Not recording.",
            "Recording the stream from now on.",
            "Looking for clips, 2 clips this stream so far. Recording from when the stream starts.",
        ]

    async def test_of_a_channel_that_is_not_tracked(self, service, channel):
        await set_up(service, bot_writes=True, **COMMANDS)
        await service.client.post(f"/channels/{CHANNEL}/tracking?enabled=0")

        await ask(service, "!status")

        assert written(service) == ["Not looking for clips here. Not recording."]

    async def test_asked_twice_in_a_row_it_answers_once(self, service, channel):
        await set_up(service, bot_writes=True, **COMMANDS)

        await ask(service, "!status")
        await ask(service, "!status")

        assert written(service) == ["Looking for clips. Not recording."]

    async def test_without_writing_in_chat_it_is_not_answered(self, service, channel):
        await set_up(service, **COMMANDS)

        await ask(service, "!status")

        assert service.api.chat_written == []


class TestTheDetector:
    async def test_the_bots_commands_are_no_reaction_whoever_posts_them(self, service, channel):
        service.warm_up(CHANNEL)
        await set_up(service, status_commands=["xDDD"])

        await service.crowd_laughs(CHANNEL, people=12)
        await service.settle()

        assert service.moments() == []

    async def test_a_command_in_a_tracked_channel_is_still_chat(self, service, channel):
        await set_up(service, **COMMANDS)

        await ask(service, "!status")

        assert service.chat_log() == [(CHANNEL, "the_streamer", "!status")]


class TestWhatItSaysUnasked:
    async def test_a_recording_started_and_stopped_from_the_page_is_said_in_chat(self, service, channel):
        await set_up(service, bot_writes=True)

        await service.client.post(f"/channels/{CHANNEL}/recording/start")
        await service.client.post(f"/channels/{CHANNEL}/recording/stop")
        await service.settle()

        assert written(service) == [
            "Recording the stream from now on.",
            "Recording stopped. Nothing had been recorded yet.",
        ]

    async def test_one_started_by_itself_for_a_channel_recorded_always(self, service, channel):
        await set_up(service, bot_writes=True)
        await service.client.post(f"/channels/{CHANNEL}/always_record?enabled=1")

        await service.main._record_stream_by_itself(CHANNEL)
        await service.settle()

        assert written(service) == ["Recording the stream from now on."]

    async def test_one_ended_for_the_disk_says_why(self, service, channel):
        await set_up(service, bot_writes=True)
        await service.client.post(f"/channels/{CHANNEL}/recording/start")

        await service.main._stream_recording_over(CHANNEL, stream_recording.DISK_FULL)
        await service.settle()

        assert written(service)[-1] == "Recording stopped: the disk is nearly full. Nothing had been recorded yet."

    async def test_a_shutdown_says_so_before_the_service_is_gone(self, service, channel, process_exits):
        await set_up(service, bot_writes=True)
        await service.client.post(f"/channels/{CHANNEL}/recording/start")
        await service.settle()

        await service.client.post("/shutdown")
        await service.settle()

        assert written(service)[-1] == "Recording stopped: the bot is shutting down. Nothing had been recorded yet."

    async def test_a_channel_that_has_the_bot_write_nowhere_hears_nothing_of_it(self, service, channel):
        await service.client.post(f"/channels/{CHANNEL}/recording/start")
        await service.settle()

        assert service.api.chat_written == []

    async def test_nor_one_that_has_it_say_nothing_of_recordings(self, service, channel):
        await set_up(service, bot_writes=True, bot_tells_recordings=False, **COMMANDS)

        await service.client.post(f"/channels/{CHANNEL}/recording/start")
        await service.settle()
        await ask(service, "!record start")

        # An answer to a command is said all the same.
        assert written(service) == ["Already recording, from when the stream starts."]

    async def test_in_the_channels_own_words_where_it_set_them(self, service, channel):
        await set_up(
            service,
            bot_writes=True,
            bot_started_message="REC on for {channel}",
            bot_ended_message="{reason} Kept: {length}. {{bye}}",
        )

        await service.client.post(f"/channels/{CHANNEL}/recording/start")
        await service.client.post(f"/channels/{CHANNEL}/recording/stop")
        await service.settle()

        assert written(service) == [f"REC on for {CHANNEL}", "Recording stopped. Kept: nothing. {bye}"]


class TestAMomentCaught:
    @pytest.fixture(autouse=True)
    def into_the_stream(self, service, monkeypatch):
        async def stream_elapsed(channel, broadcaster_user_id):
            return 5025

        monkeypatch.setattr(service.main, "_stream_elapsed_seconds", stream_elapsed)

    async def test_a_clip_asked_for_is_said_where_that_is_switched_on(self, service, channel):
        await set_up(service, bot_writes=True, bot_tells_moments=True, clip_commands=["!clip"])

        await ask(service, "!clip")

        assert len(service.moments()) == 1
        assert written(service) == ["Clip asked for by the_streamer at 1:23:45 into the stream."]

    async def test_one_chat_set_off_too_in_the_channels_own_words(self, service, channel):
        service.warm_up(CHANNEL)
        await set_up(service, bot_writes=True, bot_tells_moments=True, bot_moment_message="LOL at {stream_time}{by}")

        await service.crowd_laughs(CHANNEL, people=12)
        await service.settle()

        assert written(service) == ["LOL at 1:23:45"]

    async def test_and_nowhere_else(self, service, channel):
        await set_up(service, bot_writes=True, clip_commands=["!clip"])

        await ask(service, "!clip")

        assert len(service.moments()) == 1
        assert service.api.chat_written == []


class TestTheCommands:
    async def test_asked_for_it_says_the_channels_commands(self, service, channel):
        await set_up(service, bot_writes=True, clip_commands=["!clip"], help_commands=["!commands"], **COMMANDS)

        await ask(service, "!commands", identity=MODERATOR, sender="a_mod")

        assert written(service) == [
            "Commands: !clip - a clip; !record start - start recording; !record stop - stop recording; "
            "!status - how things stand; !commands - these commands."
        ]


class TestWhatItSaysByItself:
    def live(self, since: datetime | None) -> None:
        recording_manager._looks[CHANNEL] = Look(at=datetime.now(timezone.utc), live_since=since)

    async def test_a_stream_that_starts_is_said_once_and_one_already_on_is_not(self, service, channel):
        await set_up(service, bot_writes=True, bot_tells_live=True)
        began = datetime.now(timezone.utc)
        self.live(began - timedelta(hours=1))
        service.main._bot_round(began)  # on when the service started
        self.live(None)
        service.main._bot_round(began)

        self.live(began)
        service.main._bot_round(began)
        service.main._bot_round(began + timedelta(minutes=1))
        await service.settle()

        assert written(service) == ["Watching chat for clips this stream."]

    async def test_its_own_message_comes_every_so_often_while_the_stream_is_on(self, service, channel):
        await set_up(service, bot_writes=True, bot_repeat_message="Follow {channel}!", bot_repeat_minutes=10)
        began = datetime.now(timezone.utc)
        self.live(None)
        service.main._bot_round(began + timedelta(minutes=30))
        self.live(began)

        for minutes in (5, 10, 15, 21):
            service.main._bot_round(began + timedelta(minutes=minutes))
            await service.settle()

        # Due ten minutes into the stream, and ten minutes after that - which
        # the bot, its clock running in seconds here, takes for the same
        # message written again just now and leaves out.
        assert written(service) == [f"Follow {CHANNEL}!"]
        assert service.main._repeated_at[USER_ID] == began + timedelta(minutes=21)

    async def test_nothing_is_said_by_itself_without_writing_in_chat(self, service, channel):
        await set_up(service, bot_tells_live=True, bot_repeat_message="Hi")
        began = datetime.now(timezone.utc)
        self.live(None)
        service.main._bot_round(began)
        self.live(began)

        service.main._bot_round(began + timedelta(hours=1))
        await service.settle()

        assert service.api.chat_written == []


class TestAClipSaved:
    @pytest.fixture
    def cut(self, service, monkeypatch):
        """Cuts the clip of a moment fired 1:23:45 into the stream, from a
        stand-in for the recorder."""

        async def create_clip(channel, start, end, name, **framing):
            path = recorder.CLIPS_DIR / channel / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"clip")
            return recorder.CutClip(path, BEGAN, BEGAN + timedelta(seconds=35))

        monkeypatch.setattr(service.main.recording_manager, "create_clip_for_moment", create_clip)

        async def cut() -> None:
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
            tuned = service.main._tuning_now(USER_ID)
            await service.main._create_clip_background(moment_id, CHANNEL, BEGAN, BEGAN, tuned)
            await service.settle()

        return cut

    async def test_it_is_said_where_that_is_asked_for(self, service, channel, cut):
        await set_up(service, bot_writes=True, bot_tells_clips=True)

        await cut()

        assert written(service) == ["Clip saved: 35 seconds, at 1:23:45 into the stream."]

    @pytest.mark.parametrize("values", [{"bot_writes": True}, {"bot_tells_clips": True}])
    async def test_and_nowhere_else(self, service, channel, cut, values):
        await set_up(service, **values)

        await cut()

        assert service.api.chat_written == []


class TestOnOtherPlatforms:
    def watch(self, platform: str, platform_user_id: str, slug: str) -> int:
        conn = db.get_connection()
        try:
            return db.add_platform_streamer(conn, platform, platform_user_id, slug)
        finally:
            conn.close()

    async def test_on_twitch_it_writes_as_the_account_chat_is_read_as(self, service, twitch_api, monkeypatch):
        monkeypatch.setattr(service.main, "settings", load_settings())
        owner = self.watch("twitch", "101", "someone@twitch")
        twitch_signed_in(twitch_api, FakeAccount("chat_reader", "777"))
        await set_up(service, "someone@twitch", bot_writes=True, **COMMANDS)

        await service.main._take_chats(
            [ChatMessage(owner, "someone@twitch", "t-1", "someone", "!record start", runs_channel=True)]
        )
        await service.settle()

        assert recording_manager.stream_recording_of("someone@twitch") is not None
        assert twitch_api.written_in("101") == ["Recording the stream from now on."]
        assert twitch_api.chat_written[0]["sender"] == "chat_reader"
        assert service.api.chat_written == []

    async def test_on_youtube_its_commands_are_carried_out_without_an_answer(self, service, youtube):
        owner = self.watch("youtube", "UCAAAAAAAAAAAAAAAAAAAAAA", "someone@youtube")
        await set_up(service, "someone@youtube", bot_writes=True, **COMMANDS)

        await service.main._take_chats(
            [ChatMessage(owner, "someone@youtube", "y-1", "someone", "!record start", runs_channel=True)]
        )
        await service.settle()

        assert recording_manager.stream_recording_of("someone@youtube") is not None
        assert service.api.chat_written == []
        assert service.main._bot().written == {}


class TestSigningInOnKick:
    async def sign_in(self, service, account: FakeKickAccount | None = None) -> httpx.Response:
        """Goes through what the browser does: to Kick and back."""
        sent = await service.client.get("/bot/kick/sign_in")
        assert sent.status_code == 303
        back = service.api.consent(sent.headers["location"], account)
        return await service.client.get(back.path, params=back.params)

    async def test_the_account_signs_in_on_kick_and_the_bot_writes_as_it(self, service):
        service.watch(CHANNEL, broadcaster_user_id=USER_ID)
        await set_up(service, bot_writes=True)

        response = await self.sign_in(service, FakeKickAccount("my_bot", 4242))

        assert response.status_code == 200
        page = parse(response.text)
        assert page.one("h1").text == "Signed in on Kick as my_bot"
        assert account_tokens.load(kick_auth.TOKEN_FILE).login == "my_bot"
        await service.client.post(f"/channels/{CHANNEL}/recording/start")
        await service.settle()
        assert service.api.chat_written[0]["sender"] == "my_bot"

    async def test_kick_is_asked_with_the_redirect_url_the_kick_app_has(self, service, monkeypatch):
        sent = await service.client.get("/bot/kick/sign_in")
        assert httpx.URL(sent.headers["location"]).params["redirect_uri"] == "http://localhost:8000/bot/kick/signed_in"

        monkeypatch.setenv("KICK_REDIRECT_URI", "http://localhost:8123/bot/kick/signed_in")
        monkeypatch.setattr(service.main, "settings", load_settings())
        sent = await service.client.get("/bot/kick/sign_in")
        location = httpx.URL(sent.headers["location"])
        assert location.params["redirect_uri"] == "http://localhost:8123/bot/kick/signed_in"

        back = service.api.consent(str(location))
        assert (await service.client.get(back.path, params=back.params)).status_code == 200

    async def test_another_sign_in_takes_the_place_of_the_last(self, service):
        service.watch(CHANNEL, broadcaster_user_id=USER_ID)
        await set_up(service, bot_writes=True)
        await self.sign_in(service, FakeKickAccount("first_bot", 1))

        await self.sign_in(service, FakeKickAccount("second_bot", 2))
        await service.client.post(f"/channels/{CHANNEL}/recording/start")
        await service.settle()

        assert [sent["sender"] for sent in service.api.chat_written] == ["second_bot"]

    async def test_turned_down_on_kick_nothing_is_kept(self, service):
        sent = await service.client.get("/bot/kick/sign_in")
        back = service.api.turn_down(sent.headers["location"])

        response = await service.client.get(back.path, params=back.params)

        assert response.status_code == 400
        page = parse(response.text)
        assert page.one("h1").text == "Not signed in on Kick"
        assert page.one("p", class_="section-lead").text == "Kick did not sign the account in (The user denied access)."
        assert page.one("a", class_="ch-btn-primary").attrs["href"] == "/bot/kick/sign_in"
        assert not kick_auth.TOKEN_FILE.exists()

    async def test_a_sign_in_that_was_not_started_here_is_refused(self, service):
        response = await service.client.get("/bot/kick/signed_in", params={"code": "code-1", "state": "made-up"})

        assert response.status_code == 400
        assert "not started here" in parse(response.text).one("p", class_="section-lead").text
        assert service.api.calls("POST", "id.kick.com/oauth/token") == []

    async def test_a_sign_in_comes_back_once(self, service):
        sent = await service.client.get("/bot/kick/sign_in")
        back = service.api.consent(sent.headers["location"])
        assert (await service.client.get(back.path, params=back.params)).status_code == 200

        again = await service.client.get(back.path, params=back.params)

        assert again.status_code == 400

    async def test_one_that_took_too_long_is_refused(self, service, monkeypatch):
        monkeypatch.setattr(kick_auth, "SIGN_IN_SECONDS", -1)
        sent = await service.client.get("/bot/kick/sign_in")
        back = service.api.consent(sent.headers["location"])

        response = await service.client.get(back.path, params=back.params)

        assert response.status_code == 400
        assert "took too long" in parse(response.text).one("p", class_="section-lead").text

    async def test_kick_sending_no_code_back_is_said(self, service):
        sent = await service.client.get("/bot/kick/sign_in")
        state = httpx.URL(sent.headers["location"]).params["state"]

        response = await service.client.get("/bot/kick/signed_in", params={"state": state})

        assert response.status_code == 400
        assert parse(response.text).one("p", class_="section-lead").text == "Kick sent no code back."

    async def test_kick_not_answering_is_said_and_can_be_tried_again(self, service):
        sent = await service.client.get("/bot/kick/sign_in")
        back = service.api.consent(sent.headers["location"])
        service.api.failures["oauth/token"] = 503

        response = await service.client.get(back.path, params=back.params)

        assert response.status_code == 400
        page = parse(response.text)
        assert page.one("p", class_="section-lead").text == "Kick could not be asked (HTTPStatusError). Try again."
        assert page.one("a", class_="ch-btn-primary").attrs["href"] == "/bot/kick/sign_in"

    async def test_kick_not_letting_it_say_who_signed_in_is_said(self, service):
        sent = await service.client.get("/bot/kick/sign_in")
        back = service.api.consent(sent.headers["location"], scopes=("chat:write",))

        response = await service.client.get(back.path, params=back.params)

        assert response.status_code == 400
        assert "did not say whose account signed in" in parse(response.text).one("p", class_="section-lead").text


async def page_at(service, path: str) -> Element:
    response = await service.client.get(path)
    assert response.status_code == 200, response.text
    return parse(response.text)


class TestOnThePages:
    async def test_the_health_page_says_as_whom_it_writes_and_where(self, service, channel):
        await set_up(service, bot_writes=True)
        await service.client.post(f"/channels/{CHANNEL}/recording/start")
        await service.settle()

        page = await page_at(service, "/dashboard/health")

        part = page.one("div", data_health_part="bot")
        assert part.one("p", class_="health-said").text == "The bot writes in the chat of 1 channel."
        assert "is-trouble" not in part.one("p", class_="health-said").classes
        assert part.find("p", class_="health-more")[0].text == "On Kick the bot writes as clip_bot."
        (row,) = part.one("tbody").find("tr")
        assert [cell.text for cell in row.find("td")][:2] == ["Kick", "Just now"]
        button = page.one("a", href="/bot/kick/sign_in")
        assert button.text == "Sign in another account"

    async def test_a_channel_it_cannot_write_in_is_trouble_on_every_page(self, service):
        service.watch(CHANNEL, broadcaster_user_id=USER_ID)
        await set_up(service, bot_writes=True)

        page = await page_at(service, "/dashboard/health")

        said = page.one("div", data_health_part="bot").one("p", class_="health-said")
        assert said.text == f"The bot cannot write in the chat of {CHANNEL}."
        assert "is-trouble" in said.classes
        assert page.one("a", href="/bot/kick/sign_in").text == "Sign in on Kick"
        bar = await page_at(service, "/dashboard/channels")
        assert bar.one(data_health_flag=True).text == "1 problem"

    async def test_a_message_a_channel_turned_down_is_its_trouble_until_one_goes_out(self, service, channel):
        await set_up(service, bot_writes=True)
        service.api.chat_refusals[USER_ID] = 403
        await service.client.post(f"/channels/{CHANNEL}/recording/start")
        await service.settle()

        page = await page_at(service, "/dashboard/health")

        (row,) = page.one("div", data_health_part="bot").one("tbody").find("tr")
        assert row.one("td", class_="health-note").text == "Kick did not take the message (403 You cannot chat here)."
        assert (await page_at(service, "/dashboard")).one(data_health_flag=True).text == "1 problem"

        del service.api.chat_refusals[USER_ID]
        await service.client.post(f"/channels/{CHANNEL}/recording/stop")
        await service.settle()

        page = await page_at(service, "/dashboard/health")
        (row,) = page.one("div", data_health_part="bot").one("tbody").find("tr")
        assert row.one("td", class_="health-note").text == ""
        assert page.one(data_health_flag=True).text == ""

    async def test_a_channels_settings_say_as_whom_the_bot_writes_there(self, service, channel):
        page = await page_at(service, f"/dashboard/settings?channel={CHANNEL}")

        note = page.one("p", class_="set-bot")
        assert note.text == "On Kick the bot writes as clip_bot. Sign in another account"
        assert note.one("a").attrs["href"] == "/bot/kick/sign_in"

    async def test_with_nobody_signed_in_they_say_where_one_signs_in(self, service):
        service.watch(CHANNEL, broadcaster_user_id=USER_ID)

        page = await page_at(service, f"/dashboard/settings?channel={CHANNEL}")

        note = page.one("p", class_="set-bot")
        assert note.text == "No Kick account has signed in for the bot to write as. Sign one in"

    async def test_a_youtube_channels_say_that_it_cannot_write_there(self, service):
        conn = db.get_connection()
        try:
            db.add_platform_streamer(conn, "youtube", "UCAAAAAAAAAAAAAAAAAAAAAA", "someone@youtube")
        finally:
            conn.close()

        page = await page_at(service, "/dashboard/settings?channel=someone@youtube")

        note = page.one("p", class_="set-bot")
        assert note.text == health.YOUTUBE_WORDS
        assert note.find("a") == []

    async def test_the_page_for_all_channels_has_its_switches_and_messages_but_no_commands(self, service, channel):
        page = await page_at(service, "/dashboard/settings")

        headings = [heading.text for heading in page.find("h2")]
        assert "The bot in chat" in headings
        assert "What the bot says" in headings
        assert page.find(data_setting="bot_writes") != []
        assert page.find(data_setting="record_commands") == []
        assert page.find("p", class_="set-bot") == []

    async def test_a_message_is_a_field_that_shows_the_bots_own_words_while_it_is_empty(self, service, channel):
        await set_up(service, bot_clip_message="Clipped at {stream_time}")

        page = await page_at(service, f"/dashboard/settings?channel={CHANNEL}")

        clip = page.one("input", data_setting="bot_clip_message")
        assert (clip.attrs["data-kind"], clip.attrs["value"]) == ("text", "Clipped at {stream_time}")
        live = page.one("input", data_setting="bot_live_message")
        assert (live.attrs["value"], live.attrs["placeholder"]) == ("", "Watching chat for clips this stream.")
        # A channel that goes by the settings for all channels can set them.
        pane = page.one("fieldset", data_section_pane="messages")
        assert "disabled" not in pane.attrs
        assert "disabled" in page.one("fieldset", data_section_pane="moments").attrs


class TestItsSettings:
    async def test_the_commands_are_each_channels_own(self, service, channel):
        response = await service.client.post("/tuning", json={"values": {"record_commands": ["!rec"]}})

        assert response.status_code == 400
        assert response.json()["detail"] == '"Messages that start a recording" is set for each channel by itself.'

    async def test_what_it_does_set_for_all_channels_holds_for_a_channel_with_none_of_its_own(self, service, channel):
        response = await service.client.post("/tuning", json={"values": {"bot_writes": True}})
        assert response.status_code == 200

        await service.client.post(f"/channels/{CHANNEL}/recording/start")
        await service.settle()

        assert written(service) == ["Recording the stream from now on."]

    async def test_a_channels_own_holds_over_it_whichever_way_its_own_settings_are_switched(self, service, channel):
        await service.client.post("/tuning", json={"values": {"bot_writes": True}})
        await set_up(service, bot_writes=False)
        service.watch("another_channel", broadcaster_user_id=42)

        await service.client.post(f"/channels/{CHANNEL}/recording/start")
        await service.client.post("/channels/another_channel/recording/start")
        await service.settle()

        assert service.api.written_in(42) == ["Recording the stream from now on."]
        assert written(service) == []

    async def test_a_message_with_a_word_it_does_not_fill_in_is_turned_down(self, service, channel):
        response = await service.client.post(
            f"/channels/{CHANNEL}/tuning", json={"values": {"bot_live_message": "Live: {streamer}"}}
        )

        assert response.status_code == 400
        assert response.json()["detail"] == (
            '"When the stream starts": {streamer} is not a word it fills in. It fills in {channel}.'
        )

    async def test_a_message_cannot_be_two_commands(self, service, channel):
        response = await service.client.post(
            f"/channels/{CHANNEL}/tuning", json={"values": {"clip_commands": ["!rec"], "record_commands": ["!rec"]}}
        )

        assert response.status_code == 400
        assert response.json()["detail"] == (
            '\'!rec\' cannot be in both "Messages that ask for a clip" and "Messages that start a recording".'
        )

    async def test_they_hold_whichever_way_the_channels_own_settings_are_switched(self, service, channel):
        await set_up(service, bot_writes=True, **COMMANDS)
        await service.client.post(f"/channels/{CHANNEL}/own_tuning?enabled=1")
        await service.client.post(f"/channels/{CHANNEL}/own_tuning?enabled=0")

        await ask(service, "!record start")

        assert recording() is not None
        assert written(service) == ["Recording the stream from now on."]
