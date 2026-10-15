"""What a message of YouTube's chat is, as the service takes chat in: its
emoji as tokens like any platform's emotes, its sender's badges in the form
Kick's are kept in (see youtube_chat.chat_message)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import pytest

from kick_clip_hunter import chat_identity, detector
from kick_clip_hunter.youtube_chat import Channel, chat_message, messages
from tests.support.youtube import (
    MODERATOR,
    OWNER,
    SENT_USEC,
    VERIFIED,
    custom_emoji,
    emoji,
    member_badge,
    message,
    removed,
    super_chat,
    welcome,
)

CHANNEL = Channel(-1, "some_channel@youtube")
RECEIVED = datetime(2026, 3, 1, 20, 0, 5, tzinfo=timezone.utc)


def taken(action: dict):
    return chat_message(action["addChatItemAction"]["item"], CHANNEL, RECEIVED)


class TestAMessage:
    def test_a_plain_message_is_taken_in_as_written(self):
        got = taken(message("m-1", "alice", "that was ", "close"))

        assert (got.broadcaster_user_id, got.channel, got.message_id) == (-1, "some_channel@youtube", "m-1")
        assert (got.sender, got.content, got.emotes) == ("alice", "that was close", [])
        assert got.received_at == RECEIVED

    def test_the_sender_is_named_without_the_at_youtube_writes_a_handle_with(self):
        assert taken(message("m-1", "Some.Viewer-1", "hi")).sender == "Some.Viewer-1"

    def test_when_youtube_says_it_was_sent_is_kept(self):
        got = taken(message("m-1", "alice", "hi", sent_usec=SENT_USEC + 500_000))

        assert datetime.fromisoformat(got.created_at) == datetime(2026, 3, 1, 20, 0, 0, 500000, tzinfo=timezone.utc)

    def test_a_channels_name_on_youtube_is_its_handle(self):
        assert CHANNEL.login == "some_channel"

    def test_a_message_that_does_not_say_when_it_was_sent_has_no_such_time(self):
        action = message("m-1", "alice", "hi")
        del action["addChatItemAction"]["item"]["liveChatTextMessageRenderer"]["timestampUsec"]

        assert taken(action).created_at == ""

    def test_a_part_of_a_message_it_cannot_read_is_left_out(self):
        action = message("m-1", "alice", "still ", "here")
        action["addChatItemAction"]["item"]["liveChatTextMessageRenderer"]["message"]["runs"].insert(1, "odd")

        assert taken(action).content == "still here"


class TestEmoji:
    def test_an_emoji_everyone_has_stands_as_a_token_by_its_code_points_and_name(self):
        got = taken(message("m-1", "alice", "no way ", emoji("😂", "face_with_tears_of_joy", "1f602")))

        assert got.content == "no way [emote:emoji.1f602:face_with_tears_of_joy]"
        assert got.emotes == [{"emote_id": "emoji.1f602", "positions": [{"s": 7, "e": 48}]}]

    def test_an_emoji_of_several_code_points_keeps_them_all(self):
        got = taken(message("m-1", "alice", emoji("😮‍💨", "face_exhaling", "1f62e_200d_1f4a8")))

        assert got.content == "[emote:emoji.1f62e_200d_1f4a8:face_exhaling]"

    def test_a_channels_own_emoji_stands_as_a_token_by_its_picture(self):
        got = taken(message("m-1", "alice", custom_emoji("_kekw", "Pic-ture_1")))

        assert got.content == "[emote:youtube.Pic-ture_1:_kekw]"

    def test_every_place_an_emoji_stands_is_listed(self):
        laugh = emoji("🤣", "rolling_on_the_floor_laughing", "1f923")

        got = taken(message("m-1", "alice", laugh, " ", laugh))

        (listed,) = got.emotes
        token = "[emote:emoji.1f923:rolling_on_the_floor_laughing]"
        assert listed["positions"] == [{"s": 0, "e": len(token) - 1}, {"s": len(token) + 1, "e": 2 * len(token)}]
        assert got.emote_count == 2

    def test_the_detector_weighs_youtubes_laughing_emoji_as_laughing(self):
        got = taken(message("m-1", "alice", emoji("😂", "face_with_tears_of_joy", "1f602")))

        assert detector.classify_native_emotes(got.content) == detector.EMOTE_LAUGH_WEIGHT

    def test_an_emoji_whose_picture_is_somewhere_else_is_kept_as_typed(self):
        odd = emoji("😂", "face_with_tears_of_joy", "1f602")
        odd["emoji"]["image"]["thumbnails"][0]["url"] = "https://elsewhere.example/1f602.png"
        own = custom_emoji("_kekw", "picture")
        own["emoji"]["image"]["thumbnails"] = [{"url": "https://elsewhere.example/kekw.png"}]

        got = taken(message("m-1", "alice", odd, " ", own))

        assert (got.content, got.emotes) == ("😂 :_kekw:", [])

    def test_an_emoji_without_a_name_fit_for_a_token_is_kept_as_typed(self):
        nameless = emoji("😂", "face_with_tears_of_joy", "1f602")
        nameless["emoji"]["shortcuts"] = []

        assert taken(message("m-1", "alice", nameless)).content == "😂"

    def test_what_a_chatter_typed_that_reads_as_a_token_does_not(self):
        got = taken(message("m-1", "alice", "[emote:1:KEKW]"))

        assert got.emotes == []
        assert detector.classify_native_emotes(got.content) == 0.0


class TestWhoWroteIt:
    def identity(self, *badges: dict):
        got = taken(message("m-1", "alice", "hi", badges=badges))
        return got.identity, json.loads(got.identity[1]), got.runs_channel

    def test_a_name_has_no_colour_of_its_own_on_youtube(self):
        (colour, _), badges, runs = self.identity()

        assert (colour, badges, runs) == (None, [], False)

    def test_the_owners_and_moderators_badges_say_they_run_the_channel(self):
        _, owner, runs_as_owner = self.identity(OWNER)
        _, moderator, runs_as_moderator = self.identity(MODERATOR)

        assert owner == [{"type": "broadcaster", "text": "Owner"}]
        assert moderator == [{"type": "moderator", "text": "Moderator"}]
        assert runs_as_owner and runs_as_moderator

    def test_a_verified_badge_does_not(self):
        _, badges, runs = self.identity(VERIFIED)

        assert (badges, runs) == ([{"type": "verified", "text": "Verified"}], False)

    @pytest.mark.parametrize(
        ("tooltip", "months"),
        [("Member (6 months)", 6), ("Member (1 year)", 12), ("Member (2 years, 3 months)", 27), ("New member", None)],
    )
    def test_a_members_badge_says_for_how_long(self, tooltip, months):
        _, (badge,), runs = self.identity(member_badge(tooltip))

        assert (badge["type"], badge["text"], badge.get("count"), runs) == ("subscriber", tooltip, months, False)

    def test_badges_it_cannot_read_are_left_out(self):
        _, badges, _ = self.identity({"liveChatAuthorBadgeRenderer": {"tooltip": "?"}}, {"other": 1}, OWNER)

        assert [badge["type"] for badge in badges] == ["broadcaster"]

    def test_its_identity_is_one_the_dashboard_draws(self):
        identity, _, _ = self.identity(member_badge(), MODERATOR)

        assert chat_identity.from_youtube([member_badge(), MODERATOR]) == identity


class TestWhatIsNotAMessage:
    def test_a_super_chat_with_words_is_a_message(self):
        got = taken(super_chat("m-1", "alice", "take my money"))

        assert (got.message_id, got.content) == ("m-1", "take my money")

    def test_one_without_words_is_not(self):
        assert taken(super_chat("m-1", "alice")) is None

    def test_what_the_chat_says_of_itself_is_not(self):
        assert taken(welcome()) is None

    def test_a_message_without_an_id_is_not_taken(self):
        action = message("m-1", "alice", "hi")
        del action["addChatItemAction"]["item"]["liveChatTextMessageRenderer"]["id"]

        assert taken(action) is None

    def test_a_message_of_nothing_but_spaces_is_not_taken(self):
        assert taken(message("m-1", "alice", "   ")) is None


class TestALookAtTheChat:
    def test_the_messages_among_what_a_look_brought_are_taken_in_order(self):
        actions = [
            welcome(),
            message("m-1", "alice", "first"),
            removed("m-0"),
            {"addBannerToLiveChatCommand": {}},
            super_chat("m-2", "bob", "second"),
            message("m-3", "carol", "third"),
        ]

        got = messages(actions, CHANNEL, RECEIVED)

        assert [(m.message_id, m.content) for m in got] == [("m-1", "first"), ("m-2", "second"), ("m-3", "third")]
        assert {m.received_at for m in got} == {RECEIVED}

    def test_one_that_cannot_be_read_is_left_out_and_said(self, caplog, monkeypatch):
        from kick_clip_hunter import youtube_chat

        real = youtube_chat.chat_message

        def failing(item, channel, received_at=None):
            if "m-1" in json.dumps(item):
                raise ValueError("unreadable")
            return real(item, channel, received_at)

        monkeypatch.setattr(youtube_chat, "chat_message", failing)

        with caplog.at_level(logging.WARNING, logger="kick_clip_hunter"):
            got = messages([message("m-1", "alice", "x"), message("m-2", "bob", "y")], CHANNEL)

        assert [m.message_id for m in got] == ["m-2"]
        assert any("could not be read" in record.getMessage() for record in caplog.records)
