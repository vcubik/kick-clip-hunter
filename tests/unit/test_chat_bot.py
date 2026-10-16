"""chat_bot.py: what the bot writes in a channel's chat, and how it writes
there - on Kick as the account signed in for it, on Twitch as the account
chat is read as - against the fake platforms, through the real clients.
"""

from __future__ import annotations

import httpx
import pytest

from kick_clip_hunter import chat_bot, kick_auth, stream_recording, twitch_auth
from kick_clip_hunter.chat_bot import CannotWrite, Channel, ChatBot, Status
from tests.support.bot import kick_bot_signed_in, twitch_signed_in
from tests.support.kick_api import CLIENT_ID, CLIENT_SECRET
from tests.support.twitch_api import FakeAccount

pytestmark = pytest.mark.anyio

KICK = Channel(broadcaster_user_id=41, slug="SomeChannel", platform="kick", platform_user_id="41")
TWITCH = Channel(broadcaster_user_id=-1, slug="someone@twitch", platform="twitch", platform_user_id="101")
TWITCH_CLIENT = "twitch-client-id"


class TestWhatItSays:
    @pytest.mark.parametrize(
        ("seconds", "words"),
        [
            (0, "0 seconds"),
            (1, "1 second"),
            (59.4, "59 seconds"),
            (60, "1 minute"),
            (3599, "59 minutes"),
            (3600, "1 hour"),
            (3600 + 23 * 60 + 40, "1 hour 23 minutes"),
            (2 * 3600 + 60, "2 hours 1 minute"),
        ],
    )
    def test_a_length_of_time_is_said_as_a_chat_says_it(self, seconds, words):
        assert chat_bot.length_words(seconds) == words

    def test_a_recording_that_starts_says_from_when(self):
        assert chat_bot.started_words(True) == "Recording the stream from now on."
        # Someone asking in chat is watching the stream.
        assert chat_bot.started_words(None) == "Recording the stream from now on."
        assert chat_bot.started_words(False) == "The stream will be recorded from when it starts."

    @pytest.mark.parametrize(
        ("ended_by", "words"),
        [
            (stream_recording.STOPPED, "Recording stopped. 1 hour 23 minutes of the stream recorded."),
            (stream_recording.STREAM_ENDED, "The stream is over. 1 hour 23 minutes of the stream recorded."),
            (
                stream_recording.DISK_FULL,
                "Recording stopped: the disk is nearly full. 1 hour 23 minutes of the stream recorded.",
            ),
            (
                stream_recording.WRITE_FAILED,
                "Recording stopped: its footage could not be written. 1 hour 23 minutes of the stream recorded.",
            ),
            (
                stream_recording.SHUTDOWN,
                "Recording stopped: the bot is shutting down. 1 hour 23 minutes of the stream recorded.",
            ),
        ],
    )
    def test_a_recording_that_ends_says_why_and_how_much_it_holds(self, ended_by, words):
        assert chat_bot.ended_words(ended_by, True, 3600 + 23 * 60) == words

    def test_one_that_holds_nothing_or_could_not_be_measured_says_so(self):
        assert (
            chat_bot.ended_words(stream_recording.STOPPED, False) == "Recording stopped. Nothing had been recorded yet."
        )
        assert chat_bot.ended_words(stream_recording.STOPPED, True, 0.0) == (
            "Recording stopped. What was recorded is being saved."
        )

    def test_every_way_a_running_recording_ends_has_words(self):
        ways = {
            stream_recording.STOPPED,
            stream_recording.STREAM_ENDED,
            stream_recording.DISK_FULL,
            stream_recording.WRITE_FAILED,
            stream_recording.SHUTDOWN,
        }

        assert ways <= chat_bot.ENDED_WORDS.keys()

    def test_asked_to_record_what_is_being_recorded(self):
        assert chat_bot.recording_already_words(125, waiting=False) == "Already recording: 2 minutes so far."
        assert chat_bot.recording_already_words(0, waiting=True) == "Already recording, from when the stream starts."

    @pytest.mark.parametrize(
        ("status", "words"),
        [
            (
                Status(watching=True, tracked=True, clips=3),
                "Looking for clips, 3 clips this stream so far. Not recording.",
            ),
            (
                Status(watching=True, tracked=True, clips=1),
                "Looking for clips, 1 clip this stream so far. Not recording.",
            ),
            (Status(watching=True, tracked=True, clips=0), "Looking for clips, none yet this stream. Not recording."),
            (Status(watching=True, tracked=True), "Looking for clips. Not recording."),
            (Status(watching=True, tracked=False, clips=3), "Not looking for clips here. Not recording."),
            (Status(watching=False, tracked=True), "Not looking for clips: watching is paused. Not recording."),
            (
                Status(watching=True, tracked=True, clips=2, recording=True, seconds=4000),
                "Looking for clips, 2 clips this stream so far. Recording: 1 hour 6 minutes so far.",
            ),
            (
                Status(watching=True, tracked=False, recording=True, waiting=True),
                "Not looking for clips here. Recording from when the stream starts.",
            ),
            (
                Status(watching=True, tracked=False, saving=True),
                "Not looking for clips here. Not recording - the last recording is being saved.",
            ),
        ],
    )
    def test_asked_how_things_stand(self, status, words):
        assert chat_bot.status_words(status) == words

    def test_a_clip_says_how_long_it_is_and_how_far_into_the_stream(self):
        assert chat_bot.clip_words(35.2, 5025) == "Clip saved: 35 seconds, at 1:23:45 into the stream."
        assert chat_bot.clip_words(95, None) == "Clip saved: 1 minute."

    def test_everything_it_says_fits_in_a_chat(self):
        longest = chat_bot.status_words(
            Status(watching=True, tracked=True, clips=999, recording=True, seconds=99 * 3600)
        )

        assert len(longest) < chat_bot.MAX_LENGTH


class TestWritingOnKick:
    def writer(self) -> chat_bot.KickWriter:
        return chat_bot.KickWriter(kick_auth.TokenKeeper(CLIENT_ID, CLIENT_SECRET))

    async def test_it_writes_in_the_channels_chat_as_the_account_signed_in_for_it(self, kick_api):
        kick_bot_signed_in(kick_api)

        await self.writer().write(KICK, "Recording the stream from now on.")

        assert kick_api.chat_written == [
            {"broadcaster_user_id": 41, "content": "Recording the stream from now on.", "sender": "clip_bot"}
        ]

    async def test_without_an_account_it_cannot_and_says_where_one_signs_in(self, kick_api):
        with pytest.raises(CannotWrite) as refused:
            await self.writer().write(KICK, "hello")

        assert refused.value.sign_in is True
        assert refused.value.words == (
            "No Kick account has signed in for the bot to write as. Sign one in on the Health page."
        )
        assert kick_api.chat_written == []

    async def test_an_account_that_did_not_let_it_write_is_not_asked_to(self, kick_api):
        kick_bot_signed_in(kick_api, scopes=("user:read",))

        with pytest.raises(CannotWrite, match="clip_bot did not let the bot write in chat") as refused:
            await self.writer().write(KICK, "hello")

        assert refused.value.sign_in is True
        assert kick_api.calls("POST", "/public/v1/chat") == []

    async def test_a_token_kick_no_longer_takes_is_renewed_once_and_the_message_written(self, kick_api):
        token = kick_bot_signed_in(kick_api)
        del kick_api.user_tokens[token.access_token]  # taken back, its refresh token still good

        await self.writer().write(KICK, "hello")

        assert kick_api.written_in(41) == ["hello"]
        assert len(kick_api.calls("POST", "/public/v1/chat")) == 2

    async def test_a_channel_that_turns_it_down_is_said_with_what_kick_said(self, kick_api):
        kick_bot_signed_in(kick_api)
        kick_api.chat_refusals[41] = 403

        with pytest.raises(CannotWrite) as refused:
            await self.writer().write(KICK, "hello")

        assert refused.value.words == "Kick did not take the message (403 You cannot chat here)."
        assert refused.value.sign_in is False

    async def test_a_message_kick_took_but_did_not_send_is_not_written(self, kick_api):
        kick_bot_signed_in(kick_api)
        kick_api.chat_unsent.add(41)

        with pytest.raises(CannotWrite, match="Kick did not send the message"):
            await self.writer().write(KICK, "hello")


class TestWritingOnTwitch:
    def writer(self) -> chat_bot.TwitchWriter:
        return chat_bot.TwitchWriter(twitch_auth.TokenKeeper(TWITCH_CLIENT, "secret"), TWITCH_CLIENT)

    async def test_it_writes_as_the_account_chat_is_read_as(self, twitch_api):
        twitch_signed_in(twitch_api, FakeAccount("chat_reader", "777"))

        await self.writer().write(TWITCH, "Recording the stream from now on.")

        assert twitch_api.chat_written == [
            {"broadcaster_id": "101", "message": "Recording the stream from now on.", "sender": "chat_reader"}
        ]
        ((asked,),) = [twitch_api.calls("POST", "/helix/chat/messages")]
        assert asked["sender_id"] == "777"

    async def test_an_account_signed_in_only_to_read_is_not_asked_to_write(self, twitch_api):
        twitch_signed_in(twitch_api, scopes=("user:read:chat",))

        with pytest.raises(CannotWrite) as refused:
            await self.writer().write(TWITCH, "hello")

        assert refused.value.sign_in is True
        assert refused.value.words == (
            "chat_reader did not let the bot write in chat when it signed in. "
            "Sign it in again with scripts/twitch_login.py."
        )
        assert twitch_api.calls("POST", "/helix/chat/messages") == []

    async def test_one_signed_in_before_the_bot_could_write_learns_that_it_may_not(self, twitch_api):
        # What it let the service do was not kept with it: Twitch turns the
        # message down, and the renewed token says why.
        twitch_signed_in(twitch_api, scopes=("user:read:chat",), known=False)
        keeper = twitch_auth.TokenKeeper(TWITCH_CLIENT, "secret")

        with pytest.raises(CannotWrite) as refused:
            await chat_bot.TwitchWriter(keeper, TWITCH_CLIENT).write(TWITCH, "hello")

        assert refused.value.sign_in is True
        assert "did not let the bot write in chat" in refused.value.words
        assert keeper.account.may(twitch_auth.WRITE_SCOPE) is False
        assert len(twitch_api.calls("POST", "/helix/chat/messages")) == 1

    async def test_a_token_twitch_does_not_take_even_renewed_is_said(self, twitch_api, monkeypatch):
        twitch_signed_in(twitch_api)

        def refuse(request, body):
            return httpx.Response(401, json={"status": 401, "message": "Invalid OAuth token"})

        monkeypatch.setattr(twitch_api, "_write", refuse)

        with pytest.raises(CannotWrite) as refused:
            await self.writer().write(TWITCH, "hello")

        assert refused.value.words == (
            "Twitch does not let chat_reader write in chat (401 Invalid OAuth token). "
            "Sign it in again with scripts/twitch_login.py."
        )
        assert refused.value.sign_in is False

    async def test_a_message_twitch_dropped_is_said_with_why(self, twitch_api):
        twitch_signed_in(twitch_api)
        twitch_api.chat_drops["101"] = "msg_duplicate"

        with pytest.raises(CannotWrite) as refused:
            await self.writer().write(TWITCH, "hello")

        assert refused.value.words == "Twitch did not send the message: Dropped: msg_duplicate."

    async def test_a_channel_that_does_not_let_it_chat_is_said(self, twitch_api):
        twitch_signed_in(twitch_api)
        twitch_api.chat_refusals["101"] = 403

        with pytest.raises(CannotWrite) as refused:
            await self.writer().write(TWITCH, "hello")

        assert refused.value.words == "Twitch did not take the message (403 The sender may not chat here)."


class Writes:
    """A writer that writes nowhere, and remembers what it was handed."""

    def __init__(self, fails: Exception | None = None) -> None:
        self.written: list[tuple[str, str]] = []
        self.fails = fails

    async def write(self, channel: Channel, text: str) -> None:
        if self.fails is not None:
            raise self.fails
        self.written.append((channel.slug, text))


class TestTheBot:
    def bot(self, writer, clock=None) -> ChatBot:
        return ChatBot(lambda platform: writer if platform != "youtube" else None, clock=clock or (lambda: 0.0))

    async def test_what_it_says_is_written_and_kept_as_written(self):
        writer = Writes()
        bot = self.bot(writer)

        assert await bot.say(KICK, "Recording   the stream\nfrom now on.")

        assert writer.written == [("SomeChannel", "Recording the stream from now on.")]
        assert bot.written[41].sent_at is not None
        assert bot.written[41].trouble is None

    async def test_the_same_words_in_the_same_chat_are_not_written_twice_in_a_row(self):
        writer, now = Writes(), [0.0]
        bot = self.bot(writer, clock=lambda: now[0])

        await bot.say(KICK, "Not recording.")
        now[0] = chat_bot.REPEAT_SECONDS - 1
        assert not await bot.say(KICK, "Not recording.")
        assert await bot.say(KICK, "Recording the stream from now on.")
        assert await bot.say(TWITCH, "Not recording.")
        now[0] = chat_bot.REPEAT_SECONDS
        assert await bot.say(KICK, "Not recording.")

        assert writer.written == [
            ("SomeChannel", "Not recording."),
            ("SomeChannel", "Recording the stream from now on."),
            ("someone@twitch", "Not recording."),
            ("SomeChannel", "Not recording."),
        ]

    async def test_where_it_cannot_write_it_says_nothing(self):
        bot = self.bot(Writes())
        youtube = Channel(-2, "someone@youtube", "youtube", "UC123")

        assert not await bot.say(youtube, "Not recording.")
        assert bot.written == {}

    async def test_a_message_that_is_too_long_for_a_chat_is_cut(self):
        writer = Writes()

        await self.bot(writer).say(KICK, "x" * 600)

        assert writer.written == [("SomeChannel", "x" * chat_bot.MAX_LENGTH)]

    async def test_a_channels_trouble_is_kept_until_a_message_goes_out_there(self):
        writer = Writes(fails=CannotWrite("Kick did not take the message (403 You cannot chat here)."))
        bot = self.bot(writer)

        assert not await bot.say(KICK, "one")
        assert bot.written[41].trouble == "Kick did not take the message (403 You cannot chat here)."

        writer.fails = None
        assert await bot.say(KICK, "two")
        assert bot.written[41].trouble is None

    async def test_the_accounts_trouble_is_left_to_the_account(self):
        # Said for as long as there is no account, by its keeper - not past
        # the sign-in that mends it.
        bot = self.bot(Writes(fails=CannotWrite("No Kick account has signed in.", sign_in=True)))

        assert not await bot.say(KICK, "one")

        assert bot.written[41].trouble is None

    async def test_whatever_goes_wrong_it_never_raises(self, caplog):
        bot = self.bot(Writes(fails=KeyError("is_sent")))

        assert not await bot.say(KICK, "one")

        assert bot.written[41].trouble == "Something went wrong. The server log says what."
        assert "the bot could not write in chat" in caplog.text


class TestCarryingOutOnce:
    def test_a_message_that_comes_again_is_not_new(self):
        recent = chat_bot.Recent()

        assert recent.first_time("a")
        assert not recent.first_time("a")
        assert recent.first_time("b")

    def test_only_so_many_are_remembered(self):
        recent = chat_bot.Recent(size=2)
        for message_id in ("a", "b", "c"):
            recent.first_time(message_id)

        assert recent.first_time("a")
        assert not recent.first_time("c")
