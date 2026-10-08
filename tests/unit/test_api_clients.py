"""The two HTTP clients (Kick's public API, 7TV) and the watchlist operation
built on them.

The client code runs unmodified against `FakeKickApi` (an httpx transport),
so what is checked here is what goes over the wire: URLs, auth headers,
bodies, and how each kind of response is handled.
"""

from __future__ import annotations

import httpx
import pytest

from kick_clip_hunter import db, detector, kick_client, seventv_client, watchlist

pytestmark = pytest.mark.anyio


class TestAppAccessToken:
    async def test_is_requested_with_the_client_credentials_grant(self, kick_api):
        token = await kick_client.get_app_access_token("my-client-id", "my-client-secret")

        assert token == "app-token-1"
        assert kick_api.calls("POST", "id.kick.com/oauth/token") == [
            {"grant_type": "client_credentials", "client_id": "my-client-id", "client_secret": "my-client-secret"}
        ]

    async def test_is_reused_until_it_is_about_to_expire(self, kick_api, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(kick_client.time, "monotonic", lambda: clock[0])
        kick_api.token_lifetime_seconds = 3600

        first = await kick_client.get_app_access_token("id", "secret")
        clock[0] += 3600 - 31  # one second before the early-refresh margin
        still_first = await kick_client.get_app_access_token("id", "secret")

        assert still_first == first
        assert kick_api.tokens_issued == 1

    async def test_is_refreshed_a_little_before_it_expires(self, kick_api, monkeypatch):
        # Never hand out a token that could expire while a request is in flight.
        clock = [1000.0]
        monkeypatch.setattr(kick_client.time, "monotonic", lambda: clock[0])
        kick_api.token_lifetime_seconds = 3600

        first = await kick_client.get_app_access_token("id", "secret")
        clock[0] += 3600 - 29
        second = await kick_client.get_app_access_token("id", "secret")

        assert second != first
        assert kick_api.tokens_issued == 2

    async def test_a_refused_request_raises_and_caches_nothing(self, kick_api):
        kick_api.failures["/oauth/token"] = 401
        with pytest.raises(httpx.HTTPStatusError):
            await kick_client.get_app_access_token("id", "wrong-secret")

        kick_api.failures.clear()

        assert await kick_client.get_app_access_token("id", "secret") == "app-token-1"


class TestChannelLookup:
    async def test_returns_the_channel_kick_knows_under_that_slug(self, kick_api):
        kick_api.add_channel("some_channel", 4242, is_live=True, start_time="2026-03-01T19:00:00Z")
        token = await kick_client.get_app_access_token("id", "secret")

        channel = await kick_client.get_channel_by_slug("some_channel", token)

        assert channel["broadcaster_user_id"] == 4242
        assert channel["stream"] == {"is_live": True, "start_time": "2026-03-01T19:00:00Z"}
        assert kick_api.calls("GET", "/public/v1/channels") == [{"slug": "some_channel"}]

    async def test_is_made_with_the_app_token(self, kick_api):
        kick_api.add_channel("some_channel", 4242)

        with pytest.raises(httpx.HTTPStatusError) as refused:
            await kick_client.get_channel_by_slug("some_channel", "not-a-token-kick-issued")

        assert refused.value.response.status_code == 401

    async def test_a_slug_kick_rejects_is_an_http_error(self, kick_api):
        token = await kick_client.get_app_access_token("id", "secret")

        with pytest.raises(httpx.HTTPStatusError) as rejected:
            await kick_client.get_channel_by_slug("no_such_channel", token)

        assert rejected.value.response.status_code == 400

    async def test_an_empty_result_is_reported_as_no_such_channel(self, kick_api):
        kick_api.unknown_slug_status = None
        token = await kick_client.get_app_access_token("id", "secret")

        with pytest.raises(ValueError, match="No Kick channel found for slug 'no_such_channel'"):
            await kick_client.get_channel_by_slug("no_such_channel", token)


class TestEventSubscriptions:
    async def test_lists_what_is_currently_subscribed(self, kick_api):
        kick_api.subscribed.extend([11, 22])
        token = await kick_client.get_app_access_token("id", "secret")

        subscriptions = await kick_client.get_event_subscriptions(token)

        assert [subscription["broadcaster_user_id"] for subscription in subscriptions] == [11, 22]

    async def test_no_subscriptions_is_an_empty_list(self, kick_api):
        token = await kick_client.get_app_access_token("id", "secret")

        assert await kick_client.get_event_subscriptions(token) == []

    async def test_subscribing_asks_for_a_channels_chat_messages_by_webhook(self, kick_api):
        token = await kick_client.get_app_access_token("id", "secret")

        await kick_client.subscribe_chat_messages(4242, token)

        assert kick_api.calls("POST", "/public/v1/events/subscriptions") == [
            {"broadcaster_user_id": 4242, "events": [{"name": "chat.message.sent", "version": 1}], "method": "webhook"}
        ]
        assert kick_api.subscribed == [4242]

    async def test_a_refused_subscription_raises(self, kick_api):
        kick_api.subscribe_failures.add(4242)
        token = await kick_client.get_app_access_token("id", "secret")

        with pytest.raises(httpx.HTTPStatusError):
            await kick_client.subscribe_chat_messages(4242, token)


class TestSevenTvEmotes:
    async def test_returns_the_names_in_the_channels_emote_set(self, kick_api):
        kick_api.add_channel("some_channel", 4242, seventv_emotes=["KEKW", "catJAM", "Sadge"])

        assert await seventv_client.get_channel_emote_names(4242) == ["KEKW", "catJAM", "Sadge"]

    async def test_a_channel_without_a_7tv_account_has_none(self, kick_api):
        kick_api.add_channel("some_channel", 4242, seventv_emotes=None)

        assert await seventv_client.get_channel_emote_names(4242) == []

    async def test_an_account_without_an_emote_set_has_none(self, kick_api):
        kick_api.add_channel("some_channel", 4242, seventv_has_emote_set=False)

        assert await seventv_client.get_channel_emote_names(4242) == []

    async def test_a_server_error_is_not_mistaken_for_no_emotes(self, kick_api):
        kick_api.add_channel("some_channel", 4242, seventv_emotes=["KEKW"])
        kick_api.failures["7tv.io"] = 500

        with pytest.raises(httpx.HTTPStatusError):
            await seventv_client.get_channel_emote_names(4242)


class TestAddingToTheWatchlist:
    def stored(self) -> tuple[list, dict]:
        conn = db.get_connection()
        try:
            streamers = [(row["slug"], row["broadcaster_user_id"]) for row in db.get_streamers(conn)]
            return streamers, db.get_channel_keywords(conn, 4242)
        finally:
            conn.close()

    async def test_subscribes_fetches_emotes_and_stores_the_channel(self, kick_api):
        kick_api.add_channel("some_channel", 4242, seventv_emotes=["KEKW", "OMEGALUL", "Sadge", "catJAM", "re"])

        result = await watchlist.add_channel_to_watchlist("some_channel")

        assert result == {"slug": "some_channel", "broadcaster_user_id": 4242, "emote_count": 5, "laugh_emote_count": 2}
        assert kick_api.subscribed == [4242]
        assert self.stored() == (
            [("some_channel", 4242)],
            {
                "kekw": detector.EMOTE_MENTION_LAUGH_WEIGHT,
                "omegalul": detector.EMOTE_MENTION_LAUGH_WEIGHT,
                "sadge": detector.EMOTE_MENTION_OTHER_WEIGHT,
            },
        )

    async def test_uses_the_configured_credentials(self, kick_api):
        kick_api.add_channel("some_channel", 4242)

        await watchlist.add_channel_to_watchlist("some_channel")

        (token_request,) = kick_api.calls("POST", "/oauth/token")
        assert (token_request["client_id"], token_request["client_secret"]) == ("test-client-id", "test-client-secret")

    async def test_an_unknown_channel_stores_and_subscribes_nothing(self, kick_api):
        kick_api.unknown_slug_status = None

        with pytest.raises(ValueError):
            await watchlist.add_channel_to_watchlist("no_such_channel")

        assert self.stored() == ([], {})
        assert kick_api.subscribed == []

    async def test_a_channel_is_not_stored_if_subscribing_to_it_failed(self, kick_api):
        # Otherwise it would sit on the watchlist looking healthy while no
        # chat ever arrives for it.
        kick_api.add_channel("some_channel", 4242)
        kick_api.subscribe_failures.add(4242)

        with pytest.raises(httpx.HTTPStatusError):
            await watchlist.add_channel_to_watchlist("some_channel")

        assert self.stored() == ([], {})

    async def test_a_7tv_outage_leaves_nothing_behind_on_kicks_side(self, kick_api):
        # Subscribing used to come first: the add failed, but the
        # subscription it had already created stayed.
        kick_api.add_channel("some_channel", 4242, seventv_emotes=["KEKW"])
        kick_api.failures["7tv.io"] = 503

        with pytest.raises(httpx.HTTPStatusError):
            await watchlist.add_channel_to_watchlist("some_channel")

        assert kick_api.subscribed == []
        assert self.stored() == ([], {})

    async def test_a_channel_that_is_already_subscribed_is_not_subscribed_again(self, kick_api):
        kick_api.add_channel("some_channel", 4242)

        await watchlist.add_channel_to_watchlist("some_channel")
        await watchlist.add_channel_to_watchlist("some_channel")

        assert kick_api.subscribed == [4242]
        assert len(kick_api.calls("POST", "/events/subscriptions")) == 1

    async def test_a_watched_channel_whose_subscription_kick_dropped_is_subscribed_again(self, kick_api):
        kick_api.add_channel("some_channel", 4242)
        await watchlist.add_channel_to_watchlist("some_channel")
        kick_api.subscribed.clear()  # what Kick does every so often

        await watchlist.add_channel_to_watchlist("some_channel")

        assert kick_api.subscribed == [4242]

    async def test_adding_again_refreshes_the_keywords(self, kick_api):
        channel = kick_api.add_channel("some_channel", 4242, seventv_emotes=["KEKW"])
        await watchlist.add_channel_to_watchlist("some_channel")

        channel.seventv_emotes = ["OMEGALUL"]
        await watchlist.add_channel_to_watchlist("some_channel")

        streamers, keywords = self.stored()
        assert streamers == [("some_channel", 4242)]
        assert keywords == {"omegalul": detector.EMOTE_MENTION_LAUGH_WEIGHT}
