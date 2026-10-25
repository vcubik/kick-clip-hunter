"""The webhook receiver: what Kick sends in, what the service does with it.

Deliveries are signed with a real RSA key and verified by the service's real
verification code against a public key it fetches from the (fake) Kick API -
nothing about the signature path is stubbed.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from kick_clip_hunter import db, detector, webhook_security
from kick_clip_hunter.chat_message import ChatMessage
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

    @pytest.mark.parametrize("garbage", ["abc", "not base64 at all!!!"])
    async def test_a_garbage_signature_header_is_rejected_rather_than_crashing_the_request(self, service, garbage):
        payload = chat_payload(CHANNEL, 1, "mallory", "xDDD")

        response = await service.deliver(payload, **{"Kick-Event-Signature": garbage})

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

    async def test_after_a_restart_with_kick_out_of_reach_the_key_kept_from_before_verifies(self, service):
        await service.chat(CHANNEL, "alice", "before the restart")
        webhook_security._public_key_cache = None
        webhook_security._kept_copy_until = None
        service.api.failures["/public-key"] = 503

        response = await service.chat(CHANNEL, "alice", "after the restart")

        assert response.status_code == 200
        assert [content for _, _, content in service.chat_log()] == ["before the restart", "after the restart"]

    async def test_a_key_that_was_never_fetched_and_cannot_be_is_a_refusal_not_a_crash(self, service):
        service.api.failures["/public-key"] = 503

        response = await service.chat(CHANNEL, "alice", "hello")

        assert response.status_code == 503
        assert service.chat_log() == []


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
        assert emote_weight == 0.0  # emojiLol is a laugh, and "xDDD" the stronger one
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

    async def test_a_redelivered_message_is_counted_once_by_the_detector(self, service):
        # Kick redelivers what it didn't see acknowledged in time - which is
        # when the service is busiest. A retry must not look like more chat.
        payload = chat_payload(CHANNEL, 1, "alice", "xDDD")

        await service.deliver(payload)
        again = await service.deliver(payload)

        assert again.status_code == 200
        assert again.json() == {"status": "duplicate"}
        assert len(detector._entries[CHANNEL]) == 1

    async def test_a_storm_of_redeliveries_cannot_pass_for_a_crowd(self, service):
        service.watch(CHANNEL)
        service.warm_up(CHANNEL)
        few = detector.MIN_ABSOLUTE_UNIQUE - 1
        payloads = [chat_payload(CHANNEL, service.user_id(CHANNEL), f"fan{n}", "xDDD") for n in range(few)]

        for _ in range(30):
            for payload in payloads:
                await service.deliver(payload)

        assert service.moments() == []
        assert (
            len([entry for entry in detector._entries[CHANNEL] if entry[5] > 0 and entry[1].startswith("fan")]) == few
        )

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


MOD = {"username_color": "#FF5733", "badges": [{"text": "Moderator", "type": "moderator"}]}


class TestChatIdentities:
    """Who a chatter is - name colour, badges - comes with every message
    and is kept once per chatter, not once per message."""

    def identities(self, service) -> dict[tuple[str, str], tuple]:
        rows = service.rows(
            """
            SELECT s.slug, i.username, i.colour, i.badges FROM chat_identities i
            LEFT JOIN streamers s ON s.broadcaster_user_id = i.broadcaster_user_id
            """
        )
        return {(row[0], row[1]): (row[2], json.loads(row[3])) for row in rows}

    @pytest.fixture
    def writes(self, service, monkeypatch) -> list[str]:
        """Names an identity was written for, in order."""
        written = []
        real = service.main.set_chat_identities

        def counting(conn, identities):
            identities = list(identities)
            written.extend(username for _, username, _, _ in identities)
            real(conn, identities)

        monkeypatch.setattr(service.main, "set_chat_identities", counting)
        return written

    async def test_a_chatters_colour_and_badges_are_kept(self, service):
        service.watch(CHANNEL)

        await service.chat(CHANNEL, "alice", "hello", identity=MOD)

        assert self.identities(service) == {
            (CHANNEL, "alice"): ("#ff5733", [{"text": "Moderator", "type": "moderator"}])
        }

    async def test_they_are_written_once_however_much_someone_talks(self, service, writes):
        service.watch(CHANNEL)

        for number in range(20):
            await service.chat(CHANNEL, "alice", f"message {number}", identity=MOD)
            await service.chat(CHANNEL, "bob", f"reply {number}", identity={"username_color": "#00FF00", "badges": []})

        assert writes == ["alice", "bob"]
        assert len(service.rows("SELECT * FROM chat_messages")) == 40

    async def test_a_change_is_written(self, service, writes):
        service.watch(CHANNEL)
        subscribed = {**MOD, "badges": [*MOD["badges"], {"text": "Subscriber", "type": "subscriber", "count": 1}]}

        await service.chat(CHANNEL, "alice", "one", identity=MOD)
        await service.chat(CHANNEL, "alice", "two", identity=subscribed)
        await service.chat(CHANNEL, "alice", "three", identity=subscribed)

        assert writes == ["alice", "alice"]
        assert self.identities(service)[(CHANNEL, "alice")][1] == [
            {"text": "Moderator", "type": "moderator"},
            {"count": 1, "text": "Subscriber", "type": "subscriber"},
        ]

    async def test_a_message_without_an_identity_changes_nothing(self, service, writes):
        service.watch(CHANNEL)

        await service.chat(CHANNEL, "alice", "one", identity=MOD)
        await service.chat(CHANNEL, "alice", "two")
        await service.chat(CHANNEL, "bob", "three")

        assert writes == ["alice"]
        assert set(self.identities(service)) == {(CHANNEL, "alice")}

    async def test_the_same_person_is_kept_per_channel(self, service):
        service.watch("channel_a")
        service.watch("channel_b")

        await service.chat("channel_a", "alice", "hello", identity=MOD)
        await service.chat("channel_b", "alice", "hello", identity={"username_color": "#00FF00", "badges": []})

        assert self.identities(service) == {
            ("channel_a", "alice"): ("#ff5733", [{"text": "Moderator", "type": "moderator"}]),
            ("channel_b", "alice"): ("#00ff00", []),
        }

    async def test_what_is_already_stored_is_not_written_again_after_a_restart_unless_spoken(self, service, writes):
        # The memory of what was stored does not outlive the process: the
        # first message of each chatter after a start costs one write.
        service.watch(CHANNEL)
        await service.chat(CHANNEL, "alice", "one", identity=MOD)
        service.main._chat_identity_cache.clear()

        await service.chat(CHANNEL, "alice", "two", identity=MOD)
        await service.chat(CHANNEL, "alice", "three", identity=MOD)

        assert writes == ["alice", "alice"]
        assert len(self.identities(service)) == 1

    async def test_the_memory_of_what_was_stored_does_not_grow_without_end(self, service, monkeypatch):
        monkeypatch.setattr(service.main, "CHAT_IDENTITY_CACHE_LIMIT", 3)
        service.watch(CHANNEL)

        for number in range(10):
            await service.chat(CHANNEL, f"viewer{number}", "hello", identity=MOD)

        assert len(service.main._chat_identity_cache) <= 3
        assert len(self.identities(service)) == 10

    async def test_a_redelivered_message_writes_nothing(self, service, writes):
        service.watch(CHANNEL)
        payload = chat_payload(CHANNEL, service.user_id(CHANNEL), "alice", "hello", identity=MOD)
        changed = {**payload, "sender": {**payload["sender"], "identity": {"username_color": "#000000", "badges": []}}}

        await service.deliver(payload)
        await service.deliver(changed)

        assert writes == ["alice"]

    async def test_a_colour_that_is_not_one_is_not_stored_as_one(self, service):
        service.watch(CHANNEL)
        nasty = {"username_color": "red; background: url(//evil.example)", "badges": [{"type": "../x", "text": "x"}]}

        response = await service.chat(CHANNEL, "mallory", "hello", identity=nasty)

        assert response.status_code == 200
        assert self.identities(service) == {(CHANNEL, "mallory"): (None, [])}

    async def test_with_tracking_off_nothing_is_kept_of_the_chatter_either(self, service):
        service.watch(CHANNEL)
        await service.client.post(f"/channels/{CHANNEL}/tracking", params={"what": "off"})

        await service.chat(CHANNEL, "alice", "hello", identity=MOD)

        assert self.identities(service) == {}


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
        await service.client.post("/channels/paused_channel/tracking?what=off")

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


class TestChatAlone:
    """A channel tracked for its chat alone has every message kept, as a
    tracked channel's is - and nothing looked for in it."""

    async def track(self, service, what: str, channel: str = CHANNEL) -> None:
        response = await service.client.post(f"/channels/{channel}/tracking", params={"what": what})
        assert response.status_code == 200, response.text

    async def test_its_chat_is_kept_with_who_wrote_it(self, service):
        service.watch(CHANNEL)
        await self.track(service, "chat")

        response = await service.chat(CHANNEL, "alice", "hello", identity=MOD)

        assert response.json() == {"status": "stored"}
        assert service.chat_log() == [(CHANNEL, "alice", "hello")]
        assert [row["username"] for row in service.rows("SELECT username FROM chat_identities")] == ["alice"]

    async def test_nobody_looks_for_moments_in_it(self, service):
        service.watch(CHANNEL)
        service.warm_up(CHANNEL)
        entries = len(detector._entries[CHANNEL])
        await self.track(service, "chat")

        await service.crowd_laughs(CHANNEL, people=20)

        assert service.moments() == []
        assert len(detector._entries[CHANNEL]) == entries
        assert len(service.chat_log()) == 20

    async def test_with_watching_off_it_is_dropped_like_any_other(self, service):
        service.watch(CHANNEL)
        await self.track(service, "chat")
        await service.client.post("/settings/watching?enabled=0")

        response = await service.chat(CHANNEL, "alice", "hello")

        assert response.json() == {"status": "watching disabled"}
        assert service.chat_log() == []

    async def test_tracked_for_clips_again_it_is_looked_for_moments_in_again(self, service):
        service.watch(CHANNEL)
        await self.track(service, "chat")
        await self.track(service, "clips")

        response = await service.chat(CHANNEL, "alice", "hello")

        assert response.json() == {"status": "received"}
        assert CHANNEL in detector._entries

    async def test_each_message_of_a_batch_goes_by_what_its_channel_is_tracked_for(self, service):
        tracked = service.watch("tracked_channel")
        chatty = service.watch("chatty_channel")
        paused = service.watch("paused_channel")
        await self.track(service, "chat", "chatty_channel")
        await self.track(service, "off", "paused_channel")

        statuses = await service.main._take_chats(
            [
                batch_message("tracked_channel", tracked, "alice", "one", 1),
                batch_message("chatty_channel", chatty, "bob", "two", 2),
                batch_message("paused_channel", paused, "carol", "three", 3),
            ]
        )

        assert statuses == ["received", "stored", "channel tracking disabled"]
        assert [channel for channel, _, _ in service.chat_log()] == ["tracked_channel", "chatty_channel"]
        assert "chatty_channel" not in detector._entries


class TestStoredTimes:
    async def test_received_at_is_when_the_service_got_it_not_what_the_payload_claims(self, service):
        payload = chat_payload(CHANNEL, 1, "alice", "hello")
        payload["created_at"] = "2020-01-01T00:00:00+00:00"

        await service.deliver(payload)

        (row,) = service.rows("SELECT created_at, received_at FROM chat_messages")
        assert row["created_at"] == "2020-01-01T00:00:00+00:00"
        assert row["received_at"] > "2026"

    async def test_chat_is_found_again_by_when_it_was_received(self, service):
        before = datetime.now(timezone.utc)
        await service.chat(CHANNEL, "alice", "first")
        await service.chat(CHANNEL, "bob", "second")
        after = datetime.now(timezone.utc)

        conn = db.get_connection()
        try:
            chat = db.get_chat_between(conn, CHANNEL, before, after)
            earlier = db.get_chat_between(conn, CHANNEL, before - timedelta(hours=1), before - timedelta(seconds=1))
        finally:
            conn.close()

        assert [(row["sender_username"], row["content"]) for row in chat] == [("alice", "first"), ("bob", "second")]
        assert earlier == []


def batch_message(channel: str, user_id: int, sender: str, content: str, number: int, **fields) -> ChatMessage:
    return ChatMessage(
        broadcaster_user_id=user_id,
        channel=channel,
        message_id=f"batch-{channel}-{number}",
        sender=sender,
        content=content,
        **fields,
    )


class TestChatInBatches:
    """A platform that delivers chat in bulk hands it over a batch at a
    time: stored in one go, then each message counted as if it had come
    alone."""

    async def test_a_batch_is_stored_in_one_go(self, service, monkeypatch):
        user_id = service.watch(CHANNEL)
        stores = []
        real = service.main.insert_chat_messages

        def counting(conn, rows):
            rows = list(rows)
            stores.append(len(rows))
            return real(conn, rows)

        monkeypatch.setattr(service.main, "insert_chat_messages", counting)
        batch = [batch_message(CHANNEL, user_id, f"viewer{n}", f"message {n}", n) for n in range(5)]

        statuses = await service.main._take_chats(batch)

        assert statuses == ["received"] * 5
        assert stores == [5]
        assert [entry[1] for entry in detector._entries[CHANNEL]] == [f"viewer{n}" for n in range(5)]

    async def test_a_message_twice_in_one_batch_is_counted_once(self, service):
        user_id = service.watch(CHANNEL)
        message = batch_message(CHANNEL, user_id, "alice", "xDDD", 1)

        statuses = await service.main._take_chats([message, message])

        assert statuses == ["received", "duplicate"]
        assert len(detector._entries[CHANNEL]) == 1

    async def test_each_message_of_a_batch_is_judged_by_its_own_channel(self, service):
        tracked = service.watch("tracked_channel")
        paused = service.watch("paused_channel")
        await service.client.post("/channels/paused_channel/tracking?what=off")

        statuses = await service.main._take_chats(
            [
                batch_message("tracked_channel", tracked, "alice", "one", 1),
                batch_message("paused_channel", paused, "bob", "two", 2),
                batch_message("tracked_channel", tracked, "carol", "three", 3),
            ]
        )

        assert statuses == ["received", "channel tracking disabled", "received"]
        assert service.chat_log() == [("tracked_channel", "alice", "one"), ("tracked_channel", "carol", "three")]

    async def test_a_message_is_stored_as_arrived_when_it_did(self, service):
        user_id = service.watch(CHANNEL)
        arrived = datetime(2026, 10, 14, 12, 0, 0, 250000, tzinfo=timezone.utc)

        await service.main._take_chats([batch_message(CHANNEL, user_id, "alice", "hi", 1, received_at=arrived)])

        (row,) = service.rows("SELECT received_at FROM chat_messages")
        assert row["received_at"] == arrived.isoformat()

    async def test_who_wrote_a_batch_is_kept_in_one_go_with_their_latest_identity(self, service, monkeypatch):
        user_id = service.watch(CHANNEL)
        writes = []
        real = service.main.set_chat_identities

        def counting(conn, identities):
            identities = list(identities)
            writes.append(sorted(name for _, name, _, _ in identities))
            real(conn, identities)

        monkeypatch.setattr(service.main, "set_chat_identities", counting)
        green, red = ("#00ff00", "[]"), ("#ff0000", "[]")

        await service.main._take_chats(
            [
                batch_message(CHANNEL, user_id, "alice", "one", 1, identity=green),
                batch_message(CHANNEL, user_id, "bob", "two", 2, identity=green),
                batch_message(CHANNEL, user_id, "alice", "three", 3, identity=red),
            ]
        )

        assert writes == [["alice", "bob"]]
        (row,) = service.rows("SELECT colour FROM chat_identities WHERE username = 'alice'")
        assert row["colour"] == "#ff0000"

    async def test_a_crowd_laughing_in_one_batch_makes_a_moment(self, service):
        user_id = service.watch(CHANNEL)
        service.warm_up(CHANNEL)

        await service.main._take_chats(
            [batch_message(CHANNEL, user_id, f"fan{n}", "xDDD", n) for n in range(detector.MIN_ABSOLUTE_UNIQUE + 5)]
        )
        await service.settle()

        assert [moment["reason"] for moment in service.moments()] == ["laugh"]
