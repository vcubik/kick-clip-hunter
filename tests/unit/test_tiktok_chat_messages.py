"""A TikTok comment as the service takes chat in (tiktok_chat.chat_message):
made from what TikTokLive hands over - its own CommentEvent, so that the
names read here are the library's own."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from kick_clip_hunter import chat_identity
from kick_clip_hunter.tiktok_chat import Channel, chat_message
from tests.support.tiktok import comment

CHANNEL = Channel(broadcaster_user_id=-7, channel="some.channel@tiktok")
RECEIVED = datetime(2026, 10, 15, 19, 0, 12, tzinfo=timezone.utc)


def badges(message) -> list[str]:
    return [badge["type"] for badge in json.loads(message.identity[1])]


class TestAComment:
    def test_it_is_a_message_of_its_channel_by_the_name_tiktok_shows(self):
        message = chat_message(
            comment(7696971407893056289, "Pepa 🔥", "to je hustý 😂", handle="pepa.real", sent_at_ms=1792090811314),
            CHANNEL,
            RECEIVED,
        )

        assert message.broadcaster_user_id == -7 and message.channel == "some.channel@tiktok"
        assert message.message_id == "7696971407893056289"
        assert message.sender == "Pepa 🔥"
        assert message.content == "to je hustý 😂"
        assert message.created_at == "2026-10-15T19:00:11.314000+00:00"
        assert message.received_at == RECEIVED
        assert message.emotes == () and message.identity == (None, "[]") and not message.runs_channel

    def test_a_writer_without_a_nickname_goes_by_their_handle(self):
        message = chat_message(comment(1, "", "hello", handle="pepa.real"), CHANNEL)

        assert message.sender == "pepa.real"

    def test_what_would_read_as_an_emote_is_kept_so_it_does_not(self):
        message = chat_message(comment(1, "pepa", "look [emote:123:KEKW]"), CHANNEL)

        assert message.content == "look [emote​:123:KEKW]" and message.emote_count == 0

    @pytest.mark.parametrize(
        ("roles", "said", "runs"),
        [
            ({"streamer": True}, ["broadcaster"], True),
            ({"moderator": True}, ["moderator"], True),
            ({"subscriber": True}, ["subscriber"], False),
            ({"moderator": True, "subscriber": True}, ["moderator", "subscriber"], True),
        ],
    )
    def test_who_the_writer_is_in_the_channel_is_kept_as_badges(self, roles, said, runs):
        message = chat_message(comment(1, "pepa", "hi", **roles), CHANNEL)

        assert badges(message) == said
        assert message.runs_channel is runs

    @pytest.mark.parametrize("content", ["", "   "])
    def test_a_comment_with_no_words_is_no_message(self, content):
        assert chat_message(comment(1, "pepa", content), CHANNEL) is None

    def test_a_comment_without_an_id_is_no_message(self):
        assert chat_message(comment(0, "pepa", "hi"), CHANNEL) is None

    def test_a_comment_without_a_writer_is_no_message(self):
        event = comment(1, "pepa", "hi")
        event.user = None

        assert chat_message(event, CHANNEL) is None

    def test_a_comment_without_a_time_keeps_none(self):
        assert chat_message(comment(1, "pepa", "hi", sent_at_ms=0), CHANNEL).created_at == ""

    def test_a_comment_that_says_nothing_of_its_writer_has_no_badges(self):
        event = comment(1, "pepa", "hi")
        event.user_identity = None

        message = chat_message(event, CHANNEL)

        assert badges(message) == [] and not message.runs_channel


class TestATikTokChattersIdentity:
    def test_it_has_no_colour_and_kicks_badges_for_what_tiktok_says(self):
        assert chat_identity.from_tiktok(streamer=True, moderator=True, subscriber=True) == (
            None,
            '[{"text":"","type":"broadcaster"},{"text":"","type":"moderator"},{"text":"","type":"subscriber"}]',
        )

    def test_one_who_is_none_of_them_has_none(self):
        assert chat_identity.from_tiktok(streamer=False, moderator=False, subscriber=False) == (None, "[]")

    def test_its_badges_are_drawn_like_any_others(self):
        parts = chat_identity.badge_parts(
            chat_identity.from_tiktok(streamer=False, moderator=True, subscriber=False)[1]
        )

        assert parts == [{"icon": "moderator", "image": None, "words": "Moderator"}]
