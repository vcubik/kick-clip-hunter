"""The webhook receiver: what Kick sends in, what the service does with it.

Deliveries are signed with a real RSA key and verified by the service's real
verification code against a public key it fetches from the (fake) Kick API -
nothing about the signature path is stubbed.
"""

from __future__ import annotations

import json

import pytest

from kick_clip_hunter import db, detector
from tests.support.webhook import WebhookSigner, chat_payload

pytestmark = pytest.mark.anyio

CHANNEL = "some_channel"


class TestSignatureCheck:
    async def test_a_correctly_signed_delivery_is_accepted(self, service):
        response = await service.chat(CHANNEL, "alice", "hello")

        assert response.status_code == 200
        assert response.json() == {"status": "received"}

    async def test_a_delivery_signed_with_another_key_is_rejected(self, service):
        impostor = WebhookSigner()
        body, headers = impostor.delivery(chat_payload(CHANNEL, 1, "mallory", "xDDD"))

        response = await service.client.post("/webhooks/kick", content=body, headers=headers)

        assert response.status_code == 401
        assert service.chat_log() == []
        assert CHANNEL not in detector._entries

    async def test_a_body_changed_after_signing_is_rejected(self, service):
        body, headers = service.signer.delivery(chat_payload(CHANNEL, 1, "alice", "hello"))
        tampered = body.replace(b"hello", b"xDDDD")

        response = await service.client.post("/webhooks/kick", content=tampered, headers=headers)

        assert response.status_code == 401
        assert service.chat_log() == []

    @pytest.mark.parametrize("header", ["Kick-Event-Message-Id", "Kick-Event-Message-Timestamp"])
    async def test_a_signature_is_bound_to_its_message_id_and_timestamp(self, service, header):
        # Both are part of what Kick signs, so a captured signature can't be
        # replayed under a new id or a fresh timestamp.
        response = await service.deliver(chat_payload(CHANNEL, 1, "alice", "hello"), **{header: "something-else"})

        assert response.status_code == 401

    @pytest.mark.parametrize(
        "header",
        ["Kick-Event-Message-Id", "Kick-Event-Message-Timestamp", "Kick-Event-Signature", "Kick-Event-Type"],
    )
    async def test_a_delivery_missing_a_kick_header_is_refused(self, service, header):
        body, headers = service.signer.delivery(chat_payload(CHANNEL, 1, "alice", "hello"))
        del headers[header]

        response = await service.client.post("/webhooks/kick", content=body, headers=headers)

        assert response.status_code == 422
        assert service.chat_log() == []

    async def test_kicks_public_key_is_fetched_once_and_reused(self, service):
        for number in range(5):
            await service.chat(CHANNEL, "alice", f"message {number}")

        assert len(service.api.calls("GET", "/public-key")) == 1


class TestChatMessages:
    async def test_a_message_is_stored_as_received(self, service):
        user_id = service.watch(CHANNEL)
        payload = chat_payload(CHANNEL, user_id, "alice", "Hello KEKW", emote_positions=2)

        await service.deliver(payload)

        (row,) = service.rows("SELECT * FROM chat_messages")
        assert row["message_id"] == payload["message_id"]
        assert row["broadcaster_user_id"] == user_id
        assert (row["channel_slug"], row["sender_username"], row["content"]) == (CHANNEL, "alice", "Hello KEKW")
        assert json.loads(row["emotes"]) == payload["emotes"]
        assert row["created_at"] == payload["created_at"]

    async def test_a_message_reaches_the_detector_classified(self, service):
        service.watch(CHANNEL, keywords={"kekw": detector.EMOTE_MENTION_LAUGH_WEIGHT})

        await service.chat(CHANNEL, "alice", "xDDD KEKW [emote:7:emojiLol]", emote_positions=3)

        (entry,) = detector._entries[CHANNEL]
        _time, sender, content, emote_count, emote_weight, laugh_weight, mention_weight = entry
        assert sender == "alice"
        assert content == "xddd kekw [emote:7:emojilol]"
        assert emote_count == 3
        assert emote_weight == detector.EMOTE_LAUGH_WEIGHT
        assert laugh_weight == detector.LAUGH_STRONG_WEIGHT
        assert mention_weight == detector.EMOTE_MENTION_LAUGH_WEIGHT

    async def test_emote_names_only_count_on_the_channel_that_has_them(self, service):
        service.watch(CHANNEL, keywords={"kekw": 3.5})
        service.watch("other_channel")

        await service.chat(CHANNEL, "alice", "KEKW")
        await service.chat("other_channel", "alice", "KEKW")

        assert detector._entries[CHANNEL][0][6] == 3.5
        assert detector._entries["other_channel"][0][6] == 0.0

    async def test_a_redelivered_message_is_stored_only_once(self, service):
        payload = chat_payload(CHANNEL, 1, "alice", "hello")

        first = await service.deliver(payload)
        second = await service.deliver(payload)

        assert first.status_code == second.status_code == 200
        assert service.chat_log() == [(CHANNEL, "alice", "hello")]

    async def test_messages_of_several_channels_are_kept_apart(self, service):
        await service.chat("channel_a", "alice", "one")
        await service.chat("channel_b", "bob", "two")
        await service.chat("channel_a", "carol", "three")

        assert service.chat_log() == [
            ("channel_a", "alice", "one"),
            ("channel_b", "bob", "two"),
            ("channel_a", "carol", "three"),
        ]
        assert [entry[1] for entry in detector._entries["channel_a"]] == ["alice", "carol"]
        assert [entry[1] for entry in detector._entries["channel_b"]] == ["bob"]

    async def test_a_vote_is_stored_but_kept_out_of_detection(self, service):
        await service.chat(CHANNEL, "alice", "1")

        assert service.chat_log() == [(CHANNEL, "alice", "1")]
        assert len(detector._entries[CHANNEL]) == 0

    async def test_other_event_types_are_acknowledged_and_ignored(self, service):
        response = await service.deliver(
            {"broadcaster": {"user_id": 1}, "is_live": True}, event_type="livestream.status.updated"
        )

        assert response.status_code == 200
        assert response.json() == {"status": "received"}
        assert service.chat_log() == []
        assert not detector._entries


class TestPausing:
    async def test_with_watching_off_messages_are_dropped(self, service):
        await service.client.post("/settings/watching?enabled=0")

        response = await service.chat(CHANNEL, "alice", "xDDD")

        assert response.json() == {"status": "watching disabled"}
        assert service.chat_log() == []
        assert CHANNEL not in detector._entries

    async def test_turning_watching_back_on_resumes(self, service):
        await service.client.post("/settings/watching?enabled=0")
        await service.chat(CHANNEL, "alice", "dropped")
        await service.client.post("/settings/watching?enabled=1")

        await service.chat(CHANNEL, "alice", "kept")

        assert service.chat_log() == [(CHANNEL, "alice", "kept")]

    async def test_a_channel_with_tracking_off_is_dropped_while_others_carry_on(self, service):
        service.watch("paused_channel")
        service.watch("active_channel")
        await service.client.post("/channels/paused_channel/tracking?enabled=0")

        paused = await service.chat("paused_channel", "alice", "xDDD")
        active = await service.chat("active_channel", "bob", "xDDD")

        assert paused.json() == {"status": "channel tracking disabled"}
        assert active.json() == {"status": "received"}
        assert service.chat_log() == [("active_channel", "bob", "xDDD")]
        assert "paused_channel" not in detector._entries

    async def test_a_channel_missing_from_the_watchlist_is_still_processed(self, service):
        # Kick only delivers what was subscribed to, so an unknown channel
        # means the local watchlist is behind - not that the message is junk.
        response = await service.chat("not_on_the_watchlist", "alice", "hello")

        assert response.json() == {"status": "received"}
        assert service.chat_log() == [("not_on_the_watchlist", "alice", "hello")]

    async def test_once_shutdown_is_requested_messages_are_dropped(self, service, process_exits):
        await service.client.post("/shutdown")

        response = await service.chat(CHANNEL, "alice", "xDDD")

        assert response.json() == {"status": "watching disabled"}
        assert service.chat_log() == []


class TestStoredTimes:
    async def test_received_at_is_when_the_service_got_it_not_what_the_payload_claims(self, service):
        payload = chat_payload(CHANNEL, 1, "alice", "hello")
        payload["created_at"] = "2020-01-01T00:00:00+00:00"

        await service.deliver(payload)

        (row,) = service.rows("SELECT created_at, received_at FROM chat_messages")
        assert row["created_at"] == "2020-01-01T00:00:00+00:00"
        assert row["received_at"] > "2026"

    async def test_the_snippet_query_finds_messages_by_when_they_were_received(self, service):
        await service.chat(CHANNEL, "alice", "first")
        await service.chat(CHANNEL, "bob", "second")
        (start,), (end,) = (
            service.rows("SELECT MIN(received_at) FROM chat_messages")[0],
            service.rows("SELECT MAX(received_at) FROM chat_messages")[0],
        )

        conn = db.get_connection()
        try:
            snippet = db.get_chat_snippet(conn, CHANNEL, start, end)
        finally:
            conn.close()

        assert [(row["sender_username"], row["content"]) for row in snippet] == [("alice", "first"), ("bob", "second")]
