"""Where a live TikTok channel's stream is: TikTok says it itself, as an FLV
address in every quality it serves - of which the one the streamer sends is
followed - and whether a TikTok channel is live, as the recording loop asks."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kick_clip_hunter import streams, tiktok_stream
from kick_clip_hunter.platforms import LiveStatus, StreamUrlError, StreamUrls

ORIGIN = "https://pull-flv.example/game/stream-1.flv?expire=1793298938&sign=abc"
HD = "https://pull-flv.example/game/stream-1_hd.flv?expire=1793298938&sign=abc"
EXPIRES = datetime.fromtimestamp(1793298938, timezone.utc)


class TestFindingTheStream:
    def test_the_quality_the_streamer_sends_is_followed_as_flv(self, tiktok):
        tiktok.add_channel("some_channel").go_live(streams={"hd": ("h264", HD), "origin": ("h265", ORIGIN)})

        assert tiktok_stream.get_stream_urls("some_channel") == StreamUrls(live=ORIGIN, expires_at=EXPIRES, flv=True)

    def test_without_it_the_best_tiktok_makes_is_followed(self, tiktok):
        tiktok.add_channel("some_channel").go_live(streams={"hd": ("h264", HD), "sd": ("h265", ORIGIN)})

        assert tiktok_stream.get_stream_urls("some_channel").live == HD

    def test_a_channel_that_is_not_live_has_no_stream(self, tiktok):
        tiktok.add_channel("some_channel")

        with pytest.raises(StreamUrlError, match="is it live"):
            tiktok_stream.get_stream_urls("some_channel")

    def test_a_live_room_that_says_nowhere_has_no_stream(self, tiktok):
        tiktok.add_channel("some_channel").go_live(streams={})

        with pytest.raises(StreamUrlError, match="TikTok did not say where"):
            tiktok_stream.get_stream_urls("some_channel")

    def test_an_account_tiktok_no_longer_has_has_no_stream(self, tiktok):
        with pytest.raises(StreamUrlError, match="TikTok did not say whether 'gone_channel' is live"):
            tiktok_stream.get_stream_urls("gone_channel")

    def test_tiktok_failing_to_answer_is_no_stream_found(self, tiktok):
        tiktok.add_channel("some_channel").go_live()
        tiktok.failures["api-live"] = 429

        with pytest.raises(StreamUrlError, match="did not say whether"):
            tiktok_stream.get_stream_urls("some_channel")

    def test_a_tiktok_channel_known_by_its_name_here_is_looked_for_on_tiktok(self, tiktok):
        tiktok.add_channel("Some.Channel").go_live(streams={"origin": ("h264", ORIGIN)})

        assert streams.stream_urls("some.channel@tiktok") == StreamUrls(live=ORIGIN, expires_at=EXPIRES, flv=True)


pytestmark = pytest.mark.anyio


class TestTikTokLiveStatus:
    async def test_a_live_tiktok_channel_is_live_since_tiktok_says(self, tiktok):
        tiktok.add_channel("some_channel").go_live(started_at=1792077342)

        assert await streams.live_status("some_channel@tiktok") == LiveStatus(
            live=True, started_at=datetime.fromtimestamp(1792077342, timezone.utc)
        )

    async def test_a_tiktok_channel_that_is_not_live_is_not(self, tiktok):
        tiktok.add_channel("some_channel")

        assert await streams.live_status("some_channel@tiktok") == LiveStatus(live=False)
