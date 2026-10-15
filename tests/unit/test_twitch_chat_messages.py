"""What a Twitch chat message (a `channel.chat.message` event) is made into
as the service takes chat in: its own emotes as tokens in the text, like
Kick's, its sender's colour and badges in the form they are kept in."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from kick_clip_hunter import detector
from kick_clip_hunter.twitch_chat import Channel, chat_message
from tests.support.eventsub import chat_event

CHANNEL = Channel(broadcaster_user_id=-1, channel="some_channel@twitch", twitch_id="101")


def emote(code: str, emote_id: str) -> dict:
    return {
        "type": "emote",
        "text": code,
        "emote": {"id": emote_id, "emote_set_id": "0", "owner_id": "0", "format": ["static"]},
    }


def text(words: str) -> dict:
    return {"type": "text", "text": words}


def message_of(event: dict):
    taken = chat_message(event, CHANNEL)
    assert taken is not None
    return taken


class TestWhatIsSaid:
    def test_plain_text_is_taken_as_it_is(self):
        taken = message_of(chat_event("101", "some_channel", "Alice", "what was that"))

        assert (taken.content, taken.emote_count, taken.sender) == ("what was that", 0, "Alice")
        assert (taken.broadcaster_user_id, taken.channel) == (-1, "some_channel@twitch")

    def test_twitchs_own_emotes_stand_in_the_text_as_tokens(self):
        event = chat_event("101", "some_channel", "alice", fragments=[text("he fell "), emote("LUL", "425618")])

        taken = message_of(event)

        assert taken.content == "he fell [emote:twitch.425618:LUL]"
        assert detector.NATIVE_EMOTE_TOKEN_PATTERN.findall(taken.content) == ["LUL"]
        assert taken.emotes == [{"emote_id": "twitch.425618", "positions": [{"s": 8, "e": 32}]}]

    def test_an_emote_used_twice_is_listed_once_with_both_places(self):
        event = chat_event("101", "some_channel", "alice", fragments=[emote("LUL", "1"), text(" "), emote("LUL", "1")])

        taken = message_of(event)

        assert taken.emote_count == 2
        assert [len(where["positions"]) for where in taken.emotes] == [2]

    def test_newer_emote_ids_are_kept_whole(self):
        taken = message_of(chat_event("101", "x", "alice", fragments=[emote("pepeLaugh", "emotesv2_dcd06b30a5c24f6e")]))

        assert taken.content == "[emote:twitch.emotesv2_dcd06b30a5c24f6e:pepeLaugh]"

    def test_an_emote_whose_id_or_code_could_not_stand_in_a_token_is_its_text(self):
        event = chat_event("101", "x", "alice", fragments=[emote("bad]code", "1"), text(" "), emote("LUL", "../1")])

        taken = message_of(event)

        assert taken.content == "bad]code LUL"
        assert taken.emote_count == 0

    def test_a_token_typed_by_a_viewer_does_not_read_as_an_emote(self):
        taken = message_of(chat_event("101", "x", "mallory", "[emote:twitch.1:LUL] haha"))

        assert detector.NATIVE_EMOTE_TOKEN_PATTERN.findall(taken.content) == []
        assert "LUL] haha" in taken.content

    def test_without_fragments_the_text_is_taken(self):
        event = chat_event("101", "x", "alice", "just text")
        event["message"]["fragments"] = None

        assert message_of(event).content == "just text"

    def test_mentions_and_cheers_are_their_text(self):
        event = chat_event(
            "101",
            "x",
            "alice",
            fragments=[
                {"type": "mention", "text": "@bob", "mention": {"user_id": "5", "user_login": "bob"}},
                text(" "),
                {"type": "cheermote", "text": "Cheer100", "cheermote": {"prefix": "cheer", "bits": 100, "tier": 1}},
            ],
        )

        assert message_of(event).content == "@bob Cheer100"


class TestWhoSaidIt:
    def test_the_name_twitch_shows_is_the_senders(self):
        event = chat_event("101", "x", "Alice")
        assert message_of(event).sender == "Alice"

        event["chatter_user_name"] = ""
        assert message_of(event).sender == "alice"

    def test_colour_and_badges_are_kept_as_kick_s_are(self):
        badges = [
            {"set_id": "moderator", "id": "1", "info": ""},
            {"set_id": "subscriber", "id": "12", "info": "14"},
            {"set_id": "sub-gifter", "id": "50", "info": ""},
            {"set_id": "partner", "id": "1", "info": ""},
            {"set_id": "hype-train", "id": "2", "info": ""},
        ]

        colour, stored = message_of(chat_event("101", "x", "alice", color="#1E90FF", badges=badges)).identity

        assert colour == "#1e90ff"
        assert json.loads(stored) == [
            {"type": "moderator", "text": "Moderator"},
            {"type": "subscriber", "text": "Subscriber", "count": 14},
            {"type": "sub_gifter", "text": "Sub gifter", "count": 50},
            {"type": "verified", "text": "Partner"},
            {"type": "hype_train", "text": "Hype train"},
        ]

    @pytest.mark.parametrize(("version", "tier"), [("2003", 2), ("3012", 3), ("12", None), ("x", None)])
    def test_a_subscription_above_the_first_tier_keeps_its_tier(self, version, tier):
        badges = [{"set_id": "subscriber", "id": version, "info": "4"}]

        (badge,) = json.loads(message_of(chat_event("101", "x", "alice", badges=badges)).identity[1])

        assert badge.get("tier") == tier and badge["count"] == 4

    def test_someone_who_never_chose_a_colour_has_none(self):
        assert message_of(chat_event("101", "x", "alice", color="")).identity == (None, "[]")

    def test_a_moderator_runs_the_channel(self):
        mod = [{"set_id": "moderator", "id": "1", "info": ""}]

        assert message_of(chat_event("101", "x", "alice", badges=mod)).runs_channel
        assert not message_of(chat_event("101", "x", "alice", badges=[{"set_id": "vip", "id": "1"}])).runs_channel

    def test_the_streamer_runs_their_channel_badge_or_not(self):
        assert message_of(chat_event("101", "x", "streamer", chatter_id="101")).runs_channel


class TestWhichChat:
    def test_another_channels_chat_shown_here_in_a_shared_session_is_left_out(self):
        assert chat_message(chat_event("101", "x", "alice", "hi", source_broadcaster_id="999"), CHANNEL) is None

    def test_the_channels_own_chat_in_a_shared_session_is_kept(self):
        assert chat_message(chat_event("101", "x", "alice", "hi", source_broadcaster_id="101"), CHANNEL) is not None

    def test_when_it_was_sent_and_when_it_came_go_with_it(self):
        arrived = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)

        taken = chat_message(
            chat_event("101", "x", "alice", "hi", message_id="m-1"), CHANNEL, "2026-10-14T11:59:59.5Z", arrived
        )

        assert (taken.message_id, taken.created_at, taken.received_at) == ("m-1", "2026-10-14T11:59:59.5Z", arrived)
