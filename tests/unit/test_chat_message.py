"""A chat message as the service takes it in, and what Kick's webhook makes
of one - the form every platform's chat is turned into before it is stored
and counted."""

from __future__ import annotations

import json

import pytest

from kick_clip_hunter import chat_identity, kick_chat
from kick_clip_hunter.chat_message import ChatMessage
from tests.support.webhook import chat_payload

MOD = {"username_color": "#FF5733", "badges": [{"text": "Moderator", "type": "moderator"}]}


class TestChatMessage:
    def test_its_emotes_are_counted_by_where_they_stand(self):
        message = ChatMessage(
            broadcaster_user_id=1,
            channel="some_channel",
            message_id="m1",
            sender="alice",
            content="[emote:1:a] [emote:1:a] [emote:2:b]",
            emotes=[
                {"emote_id": "1", "positions": [{"s": 0, "e": 10}, {"s": 12, "e": 22}]},
                {"emote_id": "2", "positions": [{"s": 24, "e": 34}]},
            ],
        )

        assert message.emote_count == 3

    def test_without_emotes_there_are_none_to_count(self):
        message = ChatMessage(broadcaster_user_id=1, channel="c", message_id="m1", sender="alice", content="hi")

        assert message.emote_count == 0
        assert message.emotes_json() == "[]"

    def test_an_emote_listed_without_positions_counts_for_nothing(self):
        message = ChatMessage(
            broadcaster_user_id=1,
            channel="c",
            message_id="m1",
            sender="alice",
            content="hi",
            emotes=[{"emote_id": "1", "positions": None}, {"emote_id": "2"}],
        )

        assert message.emote_count == 0

    def test_its_emotes_are_stored_as_they_are_listed(self):
        emotes = [{"emote_id": "7", "positions": [{"s": 0, "e": 3}]}]
        message = ChatMessage(1, "c", "m1", "alice", "[emote:7:x]", emotes=emotes)

        assert json.loads(message.emotes_json()) == emotes


class TestFromKick:
    def test_everything_the_service_reads_is_taken_from_the_delivery(self):
        payload = chat_payload("some_channel", 42, "alice", "Hello [emote:7:emojiLol]", emote_positions=1)

        message = kick_chat.chat_message(payload)

        assert message.broadcaster_user_id == 42
        assert message.channel == "some_channel"
        assert message.message_id == payload["message_id"]
        assert message.sender == "alice"
        assert message.content == "Hello [emote:7:emojiLol]"
        assert message.created_at == payload["created_at"]
        assert list(message.emotes) == payload["emotes"]
        assert message.emote_count == 1

    def test_who_sent_it_is_taken_as_chat_identity_keeps_it(self):
        payload = chat_payload("some_channel", 42, "alice", "hi", identity=MOD)

        message = kick_chat.chat_message(payload)

        assert message.identity == chat_identity.from_sender(payload["sender"])
        assert message.runs_channel

    def test_a_viewer_does_not_run_the_channel(self):
        payload = chat_payload("some_channel", 42, "alice", "hi", identity={"username_color": None, "badges": []})

        assert not kick_chat.chat_message(payload).runs_channel

    def test_the_streamer_runs_their_own_channel_badge_or_not(self):
        payload = chat_payload("some_channel", 42, "streamer", "hi")
        payload["sender"]["user_id"] = 42

        assert kick_chat.chat_message(payload).runs_channel

    def test_a_message_without_a_sender_identity_has_none(self):
        payload = chat_payload("some_channel", 42, "alice", "hi")

        assert kick_chat.chat_message(payload).identity is None

    def test_what_kick_left_out_is_taken_as_empty(self):
        payload = {"message_id": "m1", "broadcaster": {"user_id": 42}}

        message = kick_chat.chat_message(payload)

        assert (message.channel, message.sender, message.content, message.created_at) == ("?", "", "", "")
        assert message.emotes_json() == "[]"

    def test_a_delivery_that_does_not_say_whose_chat_it_is_is_refused(self):
        with pytest.raises(KeyError):
            kick_chat.chat_message({"message_id": "m1", "content": "hi"})
