"""What the service asks a channel's platform about its stream, sent to the
platform the channel's name says it is on."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kick_clip_hunter import kick_stream, streams, twitch_client
from kick_clip_hunter.platforms import LiveStatus, StreamUrls

pytestmark = pytest.mark.anyio


class TestKickLiveStatus:
    async def test_a_live_kick_channel_is_live_since_kick_says(self, kick_api):
        kick_api.add_channel("some_channel", 1, is_live=True, start_time="2026-03-01T20:00:00Z")

        assert await streams.live_status("some_channel") == LiveStatus(
            live=True, started_at=datetime(2026, 3, 1, 20, 0, tzinfo=timezone.utc)
        )

    async def test_an_offline_kick_channel_is_not_live(self, kick_api):
        kick_api.add_channel("some_channel", 1, is_live=False)

        assert await streams.live_status("some_channel") == LiveStatus(live=False)

    async def test_a_live_channel_kick_gives_no_start_for_is_live_since_nobody_knows(self, kick_api):
        kick_api.add_channel("some_channel", 1, is_live=True, start_time=None)

        assert await streams.live_status("some_channel") == LiveStatus(live=True, started_at=None)

    async def test_the_watchlists_spelling_of_a_kick_name_is_asked_about_as_it_is(self, kick_api):
        kick_api.add_channel("Some_Channel", 1, is_live=True)

        await streams.live_status("Some_Channel")

        assert kick_api.calls("GET", "/public/v1/channels") == [{"slug": "Some_Channel"}]

    async def test_a_kick_channel_whose_id_is_given_is_asked_for_by_it(self, kick_api):
        # A channel that renamed itself is found by its id whichever of its
        # names the caller had.
        kick_api.add_channel("new_name", 7, is_live=True, start_time="2026-03-01T20:00:00Z")

        status = await streams.live_status("old_name", 7)

        assert status == LiveStatus(live=True, started_at=datetime(2026, 3, 1, 20, 0, tzinfo=timezone.utc))
        assert kick_api.calls("GET", "/public/v1/channels") == [{"broadcaster_user_id": "7"}]


class TestKickStream:
    def test_a_kick_channels_stream_is_found_on_kick(self, monkeypatch):
        asked = []
        monkeypatch.setattr(kick_stream, "get_stream_urls", lambda slug: asked.append(slug) or StreamUrls("x"))

        assert streams.stream_urls("some_channel") == StreamUrls("x")
        assert asked == ["some_channel"]


class TestTwitchLiveStatus:
    async def test_a_live_twitch_channel_is_live_since_twitch_says(self, twitch_api):
        twitch_api.add_channel("some_channel", "123", is_live=True, started_at="2026-03-01T20:00:00Z")

        assert await streams.live_status("some_channel@twitch") == LiveStatus(
            live=True, started_at=datetime(2026, 3, 1, 20, 0, tzinfo=timezone.utc)
        )

    async def test_an_offline_twitch_channel_is_not_live(self, twitch_api):
        twitch_api.add_channel("some_channel", "123", is_live=False)

        assert await streams.live_status("some_channel@twitch") == LiveStatus(live=False)

    async def test_twitch_is_asked_by_the_channels_name_there_with_the_apps_token(self, twitch_api):
        twitch_api.add_channel("some_channel", "123")

        await streams.live_status("some_channel@twitch")

        assert twitch_api.calls("GET", "/helix/streams") == [{"user_login": "some_channel", "type": "live"}]
        (token_request,) = twitch_api.calls("POST", "/oauth2/token")
        assert (token_request["client_id"], token_request["grant_type"]) == ("twitch-client-id", "client_credentials")

    async def test_without_a_twitch_application_nothing_can_be_asked(self, kick_api):
        with pytest.raises(twitch_client.NotSetUp):
            await streams.live_status("some_channel@twitch")


class TestYouTubeLiveStatus:
    async def test_a_youtube_channel_streaming_is_live_since_youtube_says(self, youtube):
        youtube.add_channel("Some.Channel").go_live(started_at="2026-03-01T20:00:00+00:00")

        assert await streams.live_status("some.channel@youtube") == LiveStatus(
            live=True, started_at=datetime(2026, 3, 1, 20, 0, tzinfo=timezone.utc)
        )

    async def test_a_youtube_channel_streaming_nothing_is_not_live(self, youtube):
        youtube.add_channel("some.channel").schedule()

        assert await streams.live_status("some.channel@youtube") == LiveStatus(live=False)

    async def test_youtube_is_asked_by_the_channels_handle(self, youtube):
        youtube.add_channel("some.channel")

        await streams.live_status("some.channel@youtube")

        assert youtube.calls("POST", "resolve_url")[0]["url"] == "https://www.youtube.com/@some.channel/live"
