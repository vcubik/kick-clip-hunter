"""notify.py: what a notification says, and how it gets to Telegram and to a
Discord channel - against the fake Telegram and Discord, through the real
clients. What the service tells of, and when, is tested through the running
service (tests/integration/test_api_notifications.py).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

import telegram_chats
from kick_clip_hunter import health, notify, stream_recording
from kick_clip_hunter.notify import Discord, HealthWatch, Note, Notifier, Telegram
from tests.support.notify import DISCORD_WEBHOOK_URL, TELEGRAM_CHAT, TELEGRAM_TOKEN, read_telegram_html

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 10, 16, 21, 0, tzinfo=timezone.utc)
NOTE = Note(notify.CLIP, "Clip saved", ("35 seconds, 1:23:45 into the stream.",), channel="some_channel")


class Sleeps:
    """Stands in for asyncio.sleep: the waits asked for, without waiting."""

    def __init__(self) -> None:
        self.asked: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.asked.append(seconds)


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


def video(tmp_path: Path, size: int, name: str = "moment_7.mp4") -> Path:
    path = tmp_path / name
    path.write_bytes(b"v" * size)
    return path


class TestWhatItSays:
    def test_a_clip_says_how_long_it_is_how_far_into_the_stream_and_what_chat_said(self):
        note = notify.clip_note("some_channel", 35.2, 5025, said="KEKW x31", link="http://pc:8000/dashboard?moment=7")

        assert note == Note(
            notify.CLIP,
            "Clip saved",
            ("35 seconds, 1:23:45 into the stream.", "Chat: KEKW x31"),
            channel="some_channel",
            link="http://pc:8000/dashboard?moment=7",
        )

    def test_a_clip_chat_said_nothing_in_particular_about_says_what_set_it_off(self):
        note = notify.clip_note("some_channel", 61, None, reasons=["laughing", "a laugh emote"])

        assert note.lines == ("1 minute.", "Set off by laughing and a laugh emote.")

    def test_a_clip_asked_for_in_chat_says_so(self):
        note = notify.clip_note("some_channel", 30, 60, said="!clip x2", commanded=True)

        assert note.lines == ("30 seconds, 0:01:00 into the stream.", "Asked for in chat.")

    @pytest.mark.parametrize(
        ("started_by", "asked_by", "live", "lines"),
        [
            (notify.DASHBOARD, None, True, ("Recording the stream from now on.", "Started from the dashboard.")),
            (notify.DASHBOARD, None, None, ("Recording the stream from now on.", "Started from the dashboard.")),
            (
                notify.DASHBOARD,
                None,
                False,
                ("Recorded from when the stream starts.", "Started from the dashboard."),
            ),
            (notify.CHAT, "a_mod", True, ("Recording the stream from now on.", "Asked for in chat by a_mod.")),
            (
                notify.ALWAYS,
                None,
                True,
                ("Recording the stream from now on.", "Started by itself: the channel is recorded always."),
            ),
        ],
    )
    def test_a_recording_that_starts_says_who_started_it(self, started_by, asked_by, live, lines):
        note = notify.recording_started_note("some_channel", started_by, live=live, asked_by=asked_by)

        assert (note.title, note.lines, note.trouble) == ("Recording started", lines, False)

    def test_a_recording_that_was_saved_says_why_it_ended_and_what_it_holds(self):
        note = notify.recording_saved_note(
            "some_channel", stream_recording.STREAM_ENDED, 3 * 3600 + 12 * 60, 9_800 * 1024**2, 3
        )

        assert note.title == "Recording saved"
        assert note.lines == ("The stream is over.", "3 hours 12 minutes of the stream, 9.6 GB, with 2 breaks in it.")
        assert not note.trouble

    def test_one_nobody_asked_to_end_is_trouble(self):
        note = notify.recording_saved_note("some_channel", stream_recording.DISK_FULL, 600, 2 * 1024**3)

        assert note.lines == ("It was stopped: the disk is nearly full.", "10 minutes of the stream, 2 GB.")
        assert note.trouble

    def test_every_way_a_recording_ends_has_words(self):
        ways = {
            stream_recording.STOPPED,
            stream_recording.STREAM_ENDED,
            stream_recording.DISK_FULL,
            stream_recording.WRITE_FAILED,
            stream_recording.SHUTDOWN,
            stream_recording.INTERRUPTED,
        }

        assert set(notify.ENDED_WORDS) == ways
        # One of an unknown reason is said to have been stopped.
        assert notify.recording_saved_note("x", None, 60, 1).lines[0] == "It was stopped."

    def test_a_recording_that_could_not_be_saved_or_kept_nothing_says_so(self):
        failed = notify.recording_failed_note("some_channel", link="http://pc/dashboard/recordings?recording=3")
        empty = notify.recording_empty_note("some_channel", stream_recording.STOPPED)

        assert failed.title == "Recording not saved" and failed.trouble
        assert "the Recordings page can try again" in failed.lines[0]
        assert (empty.title, empty.lines) == ("Recording ended", ("It was stopped. Nothing had been recorded yet.",))

    @pytest.mark.parametrize(
        ("words", "plural", "trouble", "title"),
        [
            ("the tunnel", False, True, "The tunnel needs a look"),
            ("webhooks", True, True, "Webhooks need a look"),
            ("the tunnel", False, False, "The tunnel looks fine again"),
            ("the recorders", True, False, "The recorders look fine again"),
        ],
    )
    def test_a_part_of_the_health_page_is_said_as_the_page_says_it(self, words, plural, trouble, title):
        note = notify.health_note(words, plural, "What the page says.", trouble)

        assert (note.title, note.lines, note.trouble) == (title, ("What the page says.",), trouble)

    def test_a_link_leads_where_the_dashboard_is_reached_from_and_nowhere_without_one(self):
        assert notify.dashboard_link("http://pc.tail.ts.net:8000/", "/dashboard?moment=7") == (
            "http://pc.tail.ts.net:8000/dashboard?moment=7"
        )
        assert notify.dashboard_link("", "/dashboard") is None

    def test_a_clip_too_big_to_send_says_how_big(self):
        assert notify.too_big_words(52 * 1024**2, "Telegram", 50 * 1024**2) == (
            "The clip (52 MB) is more than Telegram takes (50 MB)."
        )
        assert notify.too_big_words(None, "Discord", 1) == "The clip could not be read to be sent."


class TestSetUp:
    def test_telegram_needs_a_bot_and_a_chat_discord_a_webhook(self):
        assert notify.destinations() == ()
        assert notify.destinations("token", "") == ()
        assert [found.name for found in notify.destinations("token", "42", DISCORD_WEBHOOK_URL)] == [
            "Telegram",
            "Discord",
        ]
        assert [found.name for found in notify.destinations(discord_webhook_url=DISCORD_WEBHOOK_URL)] == ["Discord"]

    def test_where_each_sends_is_said_without_its_secret(self):
        telegram, discord = notify.destinations(TELEGRAM_TOKEN, TELEGRAM_CHAT, DISCORD_WEBHOOK_URL)

        assert telegram.where() == f"chat {TELEGRAM_CHAT}"
        assert discord.where() == "the webhook's channel"

    @pytest.mark.parametrize(
        "url",
        [
            DISCORD_WEBHOOK_URL,
            "https://discordapp.com/api/webhooks/1/a-b_c",
            "https://ptb.discord.com/api/v10/webhooks/1/abc",
            f"{DISCORD_WEBHOOK_URL}?thread_id=5",
        ],
    )
    def test_a_discord_webhook_is_taken_for_one(self, url):
        assert notify.setup_trouble("", "", url) == []

    def test_what_is_wrong_with_the_set_up_is_said(self):
        assert notify.setup_trouble("token", "", "https://example.com/hook") == [
            "TELEGRAM_BOT_TOKEN is set but TELEGRAM_CHAT_ID is not - scripts/telegram_chats.py finds it",
            "DISCORD_WEBHOOK_URL does not look like a Discord webhook's address",
        ]
        assert notify.setup_trouble("", "42", "") == ["TELEGRAM_CHAT_ID is set but TELEGRAM_BOT_TOKEN is not"]


class TestSetUpOnTheDashboard:
    ENV = notify.Setup(
        telegram_bot_token=TELEGRAM_TOKEN, telegram_chat_id=TELEGRAM_CHAT, dashboard_url="http://pc:8000"
    )

    def test_what_was_saved_holds_and_dotenv_has_the_rest(self):
        assert notify.in_force(self.ENV, {}) == self.ENV
        assert notify.in_force(self.ENV, {"telegram_chat_id": "77", "discord_webhook_url": DISCORD_WEBHOOK_URL}) == (
            notify.Setup(TELEGRAM_TOKEN, "77", "", DISCORD_WEBHOOK_URL, "http://pc:8000")
        )

    def test_an_empty_value_saved_says_there_is_none(self):
        assert notify.in_force(self.ENV, {"dashboard_url": ""}).dashboard_url == ""

    def test_what_is_kept_of_a_field_there_is_no_longer_is_left_alone(self):
        assert notify.in_force(self.ENV, {"gone_since": "x"}) == self.ENV

    @pytest.mark.parametrize(
        "setup",
        [
            notify.Setup(),
            notify.Setup(TELEGRAM_TOKEN, TELEGRAM_CHAT),
            notify.Setup(TELEGRAM_TOKEN, "-1001234567890"),
            notify.Setup(TELEGRAM_TOKEN, "@clip_channel", "http://localhost:8081"),
            notify.Setup(discord_webhook_url=f"{DISCORD_WEBHOOK_URL}?thread_id=5"),
            notify.Setup(dashboard_url="http://pc.tail1234.ts.net:8000/"),
        ],
    )
    def test_a_setup_that_works_is_taken(self, setup):
        notify.check_setup(setup)

    @pytest.mark.parametrize(
        ("setup", "about"),
        [
            (notify.Setup(TELEGRAM_TOKEN), '"Chat" is needed as well'),
            (notify.Setup(telegram_chat_id=TELEGRAM_CHAT), '"Bot token" is needed as well'),
            (notify.Setup("abc", TELEGRAM_CHAT), '"Bot token" is not a bot'),
            (notify.Setup(TELEGRAM_TOKEN, "@ab"), '"Chat" has to be'),
            (notify.Setup(TELEGRAM_TOKEN, TELEGRAM_CHAT, "ftp://server"), '"Bot API server" has to be'),
            (notify.Setup(discord_webhook_url="https://discord.com/channels/1/2"), '"Webhook" is not'),
            (notify.Setup(dashboard_url="pc:8000"), '"Dashboard address" has to be'),
        ],
    )
    def test_one_that_could_not_work_is_refused_in_words_that_name_the_field(self, setup, about):
        with pytest.raises(ValueError, match=about):
            notify.check_setup(setup)


class TestOnTelegram:
    def telegram(self, sleeps: Sleeps, chat: str = TELEGRAM_CHAT, token: str = TELEGRAM_TOKEN) -> Telegram:
        return Telegram(token, chat, sleep=sleeps)

    async def test_a_note_is_a_message_in_the_chat(self, telegram, sleeps):
        note = NOTE.plus("Chat: KEKW x31")

        await self.telegram(sleeps).send(note)

        (message,) = telegram.sent
        assert message["method"] == "sendMessage" and message["chat_id"] == TELEGRAM_CHAT
        assert message["shown"] == "some_channel · Clip saved\n35 seconds, 1:23:45 into the stream.\nChat: KEKW x31"
        assert message["body"]["link_preview_options"] == {"is_disabled": True}

    async def test_its_link_leads_to_the_dashboard(self, telegram, sleeps):
        await self.telegram(sleeps).send(Note(notify.TEST, "Test", link="http://pc:8000/dashboard?a=1&b=2"))

        (message,) = telegram.sent
        assert message["links"] == ["http://pc:8000/dashboard?a=1&b=2"]
        assert message["shown"].endswith("Open on the dashboard")

    async def test_what_chat_wrote_is_shown_as_it_was_written(self, telegram, sleeps):
        note = Note(notify.CLIP, "Clip saved", ('<b>bold</b> & <script> "hi"',), channel="<a>_channel")

        await self.telegram(sleeps).send(note)

        assert telegram.shown() == ['<a>_channel · Clip saved\n<b>bold</b> & <script> "hi"']

    async def test_a_clip_goes_as_a_video_with_the_note_under_it(self, telegram, sleeps, tmp_path):
        await self.telegram(sleeps).send(NOTE, video(tmp_path, 1000))

        (message,) = telegram.sent
        assert message["method"] == "sendVideo"
        assert message["video"] == ("moment_7.mp4", b"v" * 1000)
        assert message["shown"] == "some_channel · Clip saved\n35 seconds, 1:23:45 into the stream."
        assert message["body"]["supports_streaming"] == "true"

    async def test_one_that_came_too_soon_is_sent_again_after_the_wait_asked_for(self, telegram, sleeps):
        telegram.refusals.append((429, {"parameters": {"retry_after": 7}}))

        await self.telegram(sleeps).send(NOTE)

        assert sleeps.asked == [7]
        assert len(telegram.sent) == 1

    async def test_no_wait_is_longer_than_the_longest_and_none_is_waited_twice(self, telegram, sleeps):
        telegram.refusals += [(429, {"parameters": {"retry_after": 3600}})] * 2

        with pytest.raises(notify.CannotSend) as refused:
            await self.telegram(sleeps).send(NOTE)

        assert sleeps.asked == [notify.RETRY_AFTER_MAX_SECONDS]
        assert refused.value.words == "Telegram did not take it (429 Too Many Requests)."
        assert telegram.sent == []

    async def test_a_token_that_is_not_a_bots_says_where_it_is_set(self, telegram, sleeps):
        with pytest.raises(notify.CannotSend) as refused:
            await self.telegram(sleeps, token="1:wrong").send(NOTE)

        assert refused.value.words == (
            "Telegram did not take it (401 Unauthorized). TELEGRAM_BOT_TOKEN in .env is not a bot's token."
        )
        assert "wrong" not in refused.value.words

    @pytest.mark.parametrize(
        ("status", "description"),
        [(400, "Bad Request: chat not found"), (403, "Forbidden: bot can't initiate conversation with a user")],
    )
    async def test_a_chat_it_cannot_write_in_says_which_chat_it_can(self, telegram, sleeps, status, description):
        telegram.refusals.append((status, {"description": description}))

        with pytest.raises(notify.CannotSend) as refused:
            await self.telegram(sleeps).send(NOTE)

        assert refused.value.words == (
            f"Telegram did not take it ({status} {description}). "
            "TELEGRAM_CHAT_ID in .env has to be a chat that has written to the bot, or a group it is in."
        )

    async def test_a_video_too_big_for_it_is_told_apart(self, telegram, sleeps, tmp_path):
        telegram.video_max_bytes = 10

        with pytest.raises(notify.TooBig):
            await self.telegram(sleeps).send(NOTE, video(tmp_path, 11))

    async def test_one_it_could_not_reach_says_so_without_the_address(self, kick_api, sleeps, monkeypatch):
        def unreachable(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"cannot connect to {request.url}")

        monkeypatch.setattr(kick_api, "_handle", unreachable)

        with pytest.raises(notify.CannotSend) as refused:
            await self.telegram(sleeps).send(NOTE)

        assert refused.value.words == "Telegram could not be reached (ConnectError)."
        assert refused.value.again


class TestOnDiscord:
    def discord(self, sleeps: Sleeps, url: str = DISCORD_WEBHOOK_URL) -> Discord:
        return Discord(url, sleep=sleeps)

    async def test_a_note_is_an_embed_that_mentions_nobody(self, discord, sleeps):
        note = Note(
            notify.CLIP, "Clip saved", ("Chat: @everyone", "*KEKW* x31"), channel="some_channel", link="http://pc/x"
        )

        await self.discord(sleeps).send(note)

        (message,) = discord.posted
        assert message["wait"] == "true"
        assert message["payload"]["allowed_mentions"] == {"parse": []}
        assert message["payload"]["username"] == notify.DISCORD_NAME
        assert discord.embeds() == [
            {
                "title": "Clip saved",
                "description": "Chat: @everyone\n\\*KEKW\\* x31",
                "color": notify.COLOURS[notify.CLIP],
                "author": {"name": "some_channel"},
                "url": "http://pc/x",
            }
        ]

    async def test_trouble_is_in_red(self, discord, sleeps):
        await self.discord(sleeps).send(notify.health_note("the disk", False, "Only 2 GB is free.", True))

        (embed,) = discord.embeds()
        assert embed["color"] == notify.TROUBLE_COLOUR
        assert "author" not in embed and "url" not in embed

    async def test_a_clip_goes_as_a_file_with_the_embed(self, discord, sleeps, tmp_path):
        await self.discord(sleeps).send(NOTE, video(tmp_path, 1000))

        (message,) = discord.posted
        assert message["file"] == ("moment_7.mp4", b"v" * 1000)
        assert message["payload"]["embeds"][0]["title"] == "Clip saved"

    async def test_one_that_came_too_soon_is_sent_again_after_the_wait_asked_for(self, discord, sleeps):
        discord.refusals.append((429, {"retry_after": 0.25, "global": False}))

        await self.discord(sleeps).send(NOTE)

        assert sleeps.asked == [0.25]
        assert len(discord.posted) == 1

    async def test_a_webhook_that_is_gone_says_where_it_is_set(self, discord, sleeps):
        discord.gone = True

        with pytest.raises(notify.CannotSend) as refused:
            await self.discord(sleeps).send(NOTE)

        assert refused.value.words == (
            "Discord did not take it (404 Unknown Webhook). "
            "The webhook notifications go to has to be one the channel still has."
        )

    async def test_a_file_too_big_for_the_server_is_told_apart(self, discord, sleeps, tmp_path):
        discord.file_max_bytes = 10

        with pytest.raises(notify.TooBig):
            await self.discord(sleeps).send(NOTE, video(tmp_path, 11))

    @pytest.mark.parametrize(
        ("text", "shown"),
        [
            ("a_b_c", "a\\_b\\_c"),
            ("> quoted", "\\> quoted"),
            ("# heading", "\\# heading"),
            ("- item", "\\- item"),
            ("[link](http://x)", "\\[link\\](http://x)"),
            ("<@123> `code` ~~gone~~ ||spoiler||", "\\<@123> \\`code\\` \\~\\~gone\\~\\~ \\|\\|spoiler\\|\\|"),
            ("1:23:45 - 2:00", "1:23:45 - 2:00"),
        ],
    )
    def test_what_chat_wrote_is_shown_as_it_was_written(self, text, shown):
        assert notify.discord_escape(text) == shown

    def test_nothing_is_longer_than_discord_takes(self):
        payload = notify.discord_payload(Note(notify.CLIP, "T" * 300, tuple("x" * 290 for _ in range(20))))

        (embed,) = payload["embeds"]
        assert len(embed["title"]) == notify.DISCORD_TITLE_MAX
        assert len(embed["description"]) == notify.DISCORD_DESCRIPTION_MAX
        json.dumps(payload)


class TestTheNotifier:
    async def test_a_note_goes_everywhere_and_what_became_of_it_is_kept(self, telegram, discord, sleeps):
        notifier = Notifier(notify.destinations(TELEGRAM_TOKEN, TELEGRAM_CHAT, DISCORD_WEBHOOK_URL, sleep=sleeps))

        sent = await notifier.notify(NOTE)

        assert sent == 2
        assert len(telegram.sent) == len(discord.posted) == 1
        assert {name: (kept.sent_at is not None, kept.trouble) for name, kept in notifier.sent.items()} == {
            "Telegram": (True, None),
            "Discord": (True, None),
        }

    async def test_one_that_cannot_be_sent_is_kept_until_the_next_goes_out(self, telegram, discord, sleeps):
        notifier = Notifier(notify.destinations(TELEGRAM_TOKEN, TELEGRAM_CHAT, DISCORD_WEBHOOK_URL, sleep=sleeps))
        discord.gone = True

        assert await notifier.notify(NOTE) == 1
        assert notifier.sent["Discord"].trouble.startswith("Discord did not take it (404 Unknown Webhook).")
        assert notifier.sent["Telegram"].trouble is None

        discord.gone = False
        assert await notifier.notify(NOTE) == 2
        assert notifier.sent["Discord"].trouble is None

    async def test_one_that_could_not_be_reached_is_tried_once_more_a_while_later(self, telegram, sleeps):
        notifier = Notifier(notify.destinations(TELEGRAM_TOKEN, TELEGRAM_CHAT, sleep=sleeps), sleep=sleeps)
        tried = []
        real = telegram.handle

        def first_unreachable(request: httpx.Request) -> httpx.Response:
            tried.append(request.url.path)
            if len(tried) == 1:
                raise httpx.ConnectTimeout("timed out")
            return real(request)

        telegram.handle = first_unreachable

        assert await notifier.notify(NOTE) == 1
        assert sleeps.asked == [notify.RETRY_SECONDS]
        assert len(telegram.sent) == 1

    async def test_one_that_still_cannot_be_reached_is_given_up(self, telegram, sleeps, monkeypatch):
        notifier = Notifier(notify.destinations(TELEGRAM_TOKEN, TELEGRAM_CHAT, sleep=sleeps), sleep=sleeps)

        def unreachable(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no")

        telegram.handle = unreachable

        assert await notifier.notify(NOTE) == 0
        assert sleeps.asked == [notify.RETRY_SECONDS]
        assert notifier.sent["Telegram"].trouble == "Telegram could not be reached (ConnectError)."

    async def test_a_note_refused_for_what_it_says_is_not_sent_again(self, telegram, sleeps):
        notifier = Notifier(notify.destinations(TELEGRAM_TOKEN, TELEGRAM_CHAT, sleep=sleeps), sleep=sleeps)
        telegram.refusals.append((400, {"description": "Bad Request: chat not found"}))

        assert await notifier.notify(NOTE) == 0
        assert sleeps.asked == [] and telegram.requests == 1

    async def test_a_clip_goes_where_it_fits_and_the_note_says_why_not_elsewhere(
        self, telegram, discord, sleeps, tmp_path
    ):
        notifier = Notifier(notify.destinations(TELEGRAM_TOKEN, TELEGRAM_CHAT, DISCORD_WEBHOOK_URL, sleep=sleeps))
        clip = video(tmp_path, 30 * 1024**2)

        assert await notifier.notify(NOTE, clip) == 2

        assert telegram.sent[0]["video"][1] == clip.read_bytes()
        (message,) = discord.posted
        assert message["file"] is None
        assert discord.embeds()[0]["description"].endswith("The clip (30 MB) is more than Discord takes (20 MB).")

    async def test_a_clip_a_destination_turns_down_for_its_size_goes_as_words(self, discord, sleeps, tmp_path):
        notifier = Notifier(notify.destinations(discord_webhook_url=DISCORD_WEBHOOK_URL, sleep=sleeps))
        # Less than a server without boosts takes: the server's own limit.
        discord.file_max_bytes = 5 * 1024**2

        assert await notifier.notify(NOTE, video(tmp_path, 6 * 1024**2)) == 1

        (message,) = discord.posted
        assert message["file"] is None
        assert discord.embeds()[0]["description"].endswith("The clip (6 MB) is more than Discord takes.")

    async def test_a_clip_that_is_gone_goes_as_words(self, telegram, sleeps, tmp_path):
        notifier = Notifier(notify.destinations(TELEGRAM_TOKEN, TELEGRAM_CHAT, sleep=sleeps))

        assert await notifier.notify(NOTE, tmp_path / "gone.mp4") == 1

        assert telegram.sent[0]["method"] == "sendMessage"
        assert telegram.shown()[0].endswith("The clip could not be read to be sent.")

    async def test_notes_go_to_a_destination_in_the_order_they_were_made(self, telegram, sleeps):
        import asyncio

        notifier = Notifier(notify.destinations(TELEGRAM_TOKEN, TELEGRAM_CHAT, sleep=sleeps))
        telegram.refusals.append((429, {"parameters": {"retry_after": 1}}))

        await asyncio.gather(*(notifier.notify(Note(notify.TEST, f"Note {number}")) for number in range(5)))

        assert telegram.shown() == [f"Note {number}" for number in range(5)]

    async def test_whatever_goes_wrong_it_never_raises(self, telegram, sleeps, caplog):
        notifier = Notifier(notify.destinations(TELEGRAM_TOKEN, TELEGRAM_CHAT, sleep=sleeps))

        def broken(request: httpx.Request) -> httpx.Response:
            raise ZeroDivisionError

        telegram.handle = broken

        assert await notifier.notify(NOTE) == 0
        assert notifier.sent["Telegram"].trouble == "Something went wrong. The server log says what."

    async def test_with_nowhere_to_go_nothing_is_sent(self):
        assert await Notifier().notify(NOTE) == 0


class TestTheChatsABotCanWriteIn:
    async def test_who_the_bot_is_and_the_chats_that_wrote_to_it(self, telegram):
        telegram.updates = [
            {"update_id": 1, "message": {"chat": {"id": 42, "type": "private", "first_name": "Viktor"}}},
            {"update_id": 2, "message": {"chat": {"id": -100, "type": "supergroup", "title": "Clips"}}},
            {
                "update_id": 3,
                "my_chat_member": {"chat": {"id": 7, "type": "private", "first_name": "A", "username": "someone"}},
            },
            # The same chat again is listed once, where it last wrote.
            {"update_id": 4, "edited_message": {"chat": {"id": 42, "type": "private", "first_name": "Viktor"}}},
            {"update_id": 5, "channel_post": {"chat": {"id": -200, "type": "channel", "username": "news"}}},
            {"update_id": 6, "poll": {"id": "1"}},
            "not an update",
        ]

        assert await notify.telegram_bot(TELEGRAM_TOKEN) == "clip_hunter_bot"
        assert await notify.telegram_chats(TELEGRAM_TOKEN) == [
            notify.TelegramChat(-100, "supergroup", "Clips"),
            notify.TelegramChat(7, "private", "A (@someone)"),
            notify.TelegramChat(42, "private", "Viktor"),
            notify.TelegramChat(-200, "channel", "@news"),
        ]

    async def test_a_token_telegram_does_not_know_says_so(self, telegram):
        with pytest.raises(notify.CannotSend) as refused:
            await notify.telegram_chats("1:wrong")

        assert refused.value.words == "Telegram did not answer (401 Unauthorized)."


class TestABotApiServerOfOnesOwn:
    async def test_telegram_is_asked_there_and_takes_files_of_up_to_2000_mb(self, telegram, sleeps):
        telegram.hosts = {"bots.example"}
        (found,) = notify.destinations(TELEGRAM_TOKEN, TELEGRAM_CHAT, telegram_api_url="http://bots.example:8081/")

        await found.send(NOTE)

        assert len(telegram.sent) == 1
        assert found.video_max_bytes == notify.TELEGRAM_LOCAL_VIDEO_MAX_BYTES == 2000 * 1024**2
        assert found.where() == f"chat {TELEGRAM_CHAT} through http://bots.example:8081"

    def test_without_one_telegrams_own_is_asked(self):
        (found,) = notify.destinations(TELEGRAM_TOKEN, TELEGRAM_CHAT, telegram_api_url="  ")

        assert found.api_url == notify.TELEGRAM_API
        assert found.video_max_bytes == notify.TELEGRAM_VIDEO_MAX_BYTES

    @pytest.mark.parametrize("url", ["localhost:8081", "ftp://bots.example", "http://"])
    def test_one_that_is_no_web_address_is_said(self, url):
        assert notify.setup_trouble("t", "1", "", url) == ["TELEGRAM_API_URL is not an http:// or https:// address"]
        assert notify.setup_trouble("t", "1", "", "http://localhost:8081") == []

    async def test_the_chats_are_asked_of_it_too(self, telegram, capsys, monkeypatch):
        telegram.hosts = {"localhost"}
        monkeypatch.setenv("TELEGRAM_API_URL", "http://localhost:8081")

        await telegram_chats.main()

        assert "Nobody has written to @clip_hunter_bot lately." in capsys.readouterr().out


class TestTheChatsScript:
    async def test_it_lists_the_chats_with_their_ids(self, telegram, capsys):
        telegram.updates = [{"update_id": 1, "message": {"chat": {"id": 4242, "type": "private", "first_name": "V"}}}]

        await telegram_chats.main()

        output = capsys.readouterr().out
        assert "Chats that wrote to @clip_hunter_bot lately" in output
        assert output.splitlines()[1].split() == ["4242", "private", "V"]

    async def test_with_none_it_says_to_write_to_the_bot(self, telegram, capsys):
        await telegram_chats.main()

        assert "Nobody has written to @clip_hunter_bot lately." in capsys.readouterr().out

    async def test_without_a_token_it_says_where_one_goes(self, kick_api):
        with pytest.raises(SystemExit, match=r"TELEGRAM_BOT_TOKEN in \.env is empty"):
            await telegram_chats.main()

    async def test_a_token_telegram_does_not_know_is_said(self, telegram, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:wrong")

        with pytest.raises(SystemExit, match="401 Unauthorized"):
            await telegram_chats.main()


class TestTheFakeReadsHtmlAsTelegramDoes:
    def test_text_with_a_tag_it_does_not_know_is_turned_down(self):
        assert read_telegram_html("<b>a</b>")[2] is None
        assert read_telegram_html("a <script> b")[2] is not None
        assert read_telegram_html("a < b")[2] is not None


REPORT_FINE = {"disk": {"trouble": False, "said": "500 GB free."}}
REPORT_FULL = {"disk": {"trouble": True, "said": "Only 2 GB is free."}}


class TestTheHealthPageWatched:
    PARTS = (("disk", "the disk", False),)

    def test_a_part_is_told_of_once_it_has_been_trouble_for_a_while(self):
        watch = HealthWatch(after_seconds=120)

        assert watch.look(REPORT_FULL, self.PARTS, NOW) == []
        assert watch.look(REPORT_FULL, self.PARTS, NOW + timedelta(seconds=119)) == []
        assert watch.look(REPORT_FULL, self.PARTS, NOW + timedelta(seconds=120)) == [
            notify.Change("disk", "the disk", False, "Only 2 GB is free.", True)
        ]
        # Once.
        assert watch.look(REPORT_FULL, self.PARTS, NOW + timedelta(seconds=600)) == []

    def test_and_again_once_it_is_fine(self):
        watch = HealthWatch(after_seconds=0)
        watch.look(REPORT_FULL, self.PARTS, NOW)

        assert watch.look(REPORT_FINE, self.PARTS, NOW) == [
            notify.Change("disk", "the disk", False, "500 GB free.", False)
        ]
        assert watch.look(REPORT_FINE, self.PARTS, NOW) == []

    def test_trouble_that_does_not_last_is_never_told_of(self):
        watch = HealthWatch(after_seconds=120)

        for second in range(0, 600, 60):
            report = REPORT_FULL if second % 120 else REPORT_FINE
            assert watch.look(report, self.PARTS, NOW + timedelta(seconds=second)) == []

    def test_a_part_no_longer_on_the_page_is_over(self):
        watch = HealthWatch(after_seconds=0)
        watch.look(
            {"twitch": {"trouble": True, "said": "Lost.", "shown": True}}, [("twitch", "Twitch chat", False)], NOW
        )

        changes = watch.look({"twitch": {"trouble": True, "shown": False}}, [("twitch", "Twitch chat", False)], NOW)

        assert [change.trouble for change in changes] == [False]

    def test_forgotten_what_is_trouble_is_told_of_as_it_is_found(self):
        watch = HealthWatch(after_seconds=0)
        watch.look(REPORT_FULL, self.PARTS, NOW)

        watch.forget()

        assert [change.trouble for change in watch.look(REPORT_FULL, self.PARTS, NOW)] == [True]

    def test_every_part_of_the_page_can_be_told_of(self):
        report = health.report(health.Facts(now=NOW, started_at=NOW), NOW.date())
        watch = HealthWatch(after_seconds=0)

        assert watch.look(report, health.PARTS, NOW) == []
        for name, _, _ in health.PARTS:
            assert {"trouble", "said"} <= report[name].keys()
