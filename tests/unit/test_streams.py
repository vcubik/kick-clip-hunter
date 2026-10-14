"""What the service asks a channel's platform about its stream, sent to the
platform the channel's name says it is on."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kick_clip_hunter import kick_stream, streams
from kick_clip_hunter.platforms import LiveStatus, StreamUrlError, StreamUrls

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


class TestKickStream:
    def test_a_kick_channels_stream_is_found_on_kick(self, monkeypatch):
        asked = []
        monkeypatch.setattr(kick_stream, "get_stream_urls", lambda slug: asked.append(slug) or StreamUrls("x"))

        assert streams.stream_urls("some_channel") == StreamUrls("x")
        assert asked == ["some_channel"]


class TestUnknownWays:
    async def test_whether_a_channel_of_a_platform_without_a_way_to_ask_is_live_is_refused(self):
        with pytest.raises(ValueError):
            await streams.live_status("some_channel@twitch")

    def test_the_stream_of_a_channel_of_a_platform_without_a_way_to_find_it_is_not_found(self):
        with pytest.raises(StreamUrlError):
            streams.stream_urls("some_channel@twitch")
