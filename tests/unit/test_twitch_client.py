"""Twitch's API as the service asks it (Helix, with an app token), and a
Twitch channel put on the watchlist.

The client code runs unmodified against `FakeTwitchApi` (an httpx
transport), so what is checked is what goes over the wire and what is made
of each answer.
"""

from __future__ import annotations

import httpx
import pytest

from kick_clip_hunter import db, detector, twitch_client, watchlist
from kick_clip_hunter.config import load_settings
from tests.support.kick_api import seventv_emote_id

pytestmark = pytest.mark.anyio

CLIENT_ID = "twitch-client-id"


class TestAppAccessToken:
    async def test_is_requested_with_the_client_credentials_grant(self, twitch_api):
        token = await twitch_client.get_app_access_token("my-client-id", "my-secret")

        assert token == "twitch-app-token-1"
        assert twitch_api.calls("POST", "id.twitch.tv/oauth2/token") == [
            {"client_id": "my-client-id", "client_secret": "my-secret", "grant_type": "client_credentials"}
        ]

    async def test_is_reused_until_it_is_about_to_expire(self, twitch_api, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(twitch_client.time, "monotonic", lambda: clock[0])

        first = await twitch_client.get_app_access_token("id", "secret")
        clock[0] += twitch_api.token_lifetime_seconds - 31
        again = await twitch_client.get_app_access_token("id", "secret")
        clock[0] += 2
        renewed = await twitch_client.get_app_access_token("id", "secret")

        assert again == first != renewed
        assert twitch_api.tokens_issued == 2

    async def test_is_the_configured_applications(self, twitch_api):
        await twitch_client.app_token(load_settings())

        assert twitch_api.calls("POST", "/oauth2/token")[0]["client_id"] == CLIENT_ID

    async def test_without_an_application_there_is_none(self, kick_api):
        with pytest.raises(twitch_client.NotSetUp):
            await twitch_client.app_token(load_settings())


class TestUsers:
    async def test_a_channel_is_found_by_its_name(self, twitch_api):
        twitch_api.add_channel("some_channel", "123", display_name="Some_Channel")

        user = await twitch_client.get_user("some_channel", CLIENT_ID, "token")

        assert (user["id"], user["login"], user["display_name"]) == ("123", "some_channel", "Some_Channel")
        assert twitch_api.calls("GET", "/helix/users") == [{"login": "some_channel"}]

    async def test_a_name_is_looked_up_as_twitch_writes_it(self, twitch_api):
        twitch_api.add_channel("some_channel", "123")

        user = await twitch_client.get_user("  Some_Channel ", CLIENT_ID, "token")

        assert user["login"] == "some_channel"

    async def test_a_name_twitch_does_not_know_is_no_such_channel(self, twitch_api):
        with pytest.raises(ValueError, match="No Twitch channel found"):
            await twitch_client.get_user("nobody", CLIENT_ID, "token")

    @pytest.mark.parametrize("name", ["", "some channel", "a/b", "x" * 26, "../x"])
    async def test_what_cannot_be_a_twitch_name_is_not_even_asked_about(self, twitch_api, name):
        with pytest.raises(ValueError):
            await twitch_client.get_user(name, CLIENT_ID, "token")

        assert twitch_api.requests == []

    async def test_a_refused_request_raises(self, twitch_api):
        twitch_api.failures["/helix/users"] = 401

        with pytest.raises(httpx.HTTPStatusError):
            await twitch_client.get_user("some_channel", CLIENT_ID, "token")


class TestStreams:
    async def test_a_live_channels_stream(self, twitch_api):
        twitch_api.add_channel("some_channel", "123", is_live=True, started_at="2026-03-01T20:00:00Z", stream_id="9")

        stream = await twitch_client.get_stream("some_channel", CLIENT_ID, "token")

        assert (stream["id"], stream["started_at"]) == ("9", "2026-03-01T20:00:00Z")

    async def test_an_offline_channel_has_none(self, twitch_api):
        twitch_api.add_channel("some_channel", "123", is_live=False)

        assert await twitch_client.get_stream("some_channel", CLIENT_ID, "token") is None


class TestAddingToTheWatchlist:
    def streamer(self, slug: str):
        conn = db.get_connection()
        try:
            return db.get_streamer_by_slug(conn, slug)
        finally:
            conn.close()

    async def test_a_twitch_channel_is_kept_under_its_name_and_twitchs(self, twitch_api):
        twitch_api.add_channel("some_channel", "123", seventv_emotes=["KEKW", "catJAM"])

        result = await watchlist.add_twitch_channel("some_channel")

        assert result == {
            "slug": "some_channel@twitch",
            "broadcaster_user_id": -1,
            "emote_count": 2,
            "laugh_emote_count": 1,
        }
        row = self.streamer("some_channel@twitch")
        assert (row["platform"], row["platform_user_id"], row["broadcaster_user_id"]) == ("twitch", "123", -1)

    async def test_its_7tv_emotes_are_what_the_detector_listens_for_and_the_pictures(self, twitch_api):
        twitch_api.add_channel("some_channel", "123", seventv_emotes=["KEKW"])

        await watchlist.add_twitch_channel("some_channel")

        conn = db.get_connection()
        try:
            assert db.get_channel_keywords(conn, -1) == {"kekw": detector.EMOTE_MENTION_LAUGH_WEIGHT}
            assert db.get_channel_emotes(conn, -1) == {"KEKW": (seventv_emote_id("KEKW"), 0, 0)}
        finally:
            conn.close()
        assert twitch_api.calls("GET", "7tv.io/v3/users/twitch/123") == [{}]

    async def test_a_channel_without_a_7tv_account_is_added_all_the_same(self, twitch_api):
        twitch_api.add_channel("some_channel", "123", seventv_emotes=None)

        result = await watchlist.add_twitch_channel("some_channel")

        assert result["emote_count"] == 0
        assert self.streamer("some_channel@twitch") is not None

    async def test_a_channel_twitch_does_not_know_is_not_added(self, twitch_api):
        with pytest.raises(ValueError):
            await watchlist.add_twitch_channel("nobody")

        assert self.streamer("nobody@twitch") is None

    async def test_without_a_twitch_application_nothing_is_added(self, kick_api):
        with pytest.raises(twitch_client.NotSetUp):
            await watchlist.add_twitch_channel("some_channel")

        assert self.streamer("some_channel@twitch") is None

    @pytest.mark.parametrize(
        "typed", ["https://www.twitch.tv/some_channel", "twitch.tv/some_channel/videos", "some_channel@twitch"]
    )
    async def test_what_is_typed_says_it_is_a_twitch_channel(self, twitch_api, typed):
        twitch_api.add_channel("some_channel", "123")

        result = await watchlist.add_to_watchlist(typed)

        assert result["slug"] == "some_channel@twitch"

    @pytest.mark.parametrize("typed", ["some_channel", "https://kick.com/some_channel"])
    async def test_a_bare_name_or_a_kick_link_is_a_kick_channel(self, twitch_api, kick_api, typed):
        kick_api.add_channel("some_channel", 555)

        result = await watchlist.add_to_watchlist(typed)

        assert (result["slug"], result["broadcaster_user_id"]) == ("some_channel", 555)
