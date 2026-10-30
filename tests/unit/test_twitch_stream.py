"""Where a live Twitch channel's stream is, as streamlink finds it: only its
master playlist is taken - the recorder follows the stream from there."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import ClassVar

import pytest
from streamlink.exceptions import NoPluginError, PluginError

from kick_clip_hunter import streams, twitch_stream
from kick_clip_hunter.platforms import NoVod, StreamUrlError, StreamUrls, Vod
from tests.support.twitch_api import past_broadcast

MASTER = "https://usher.example/api/v2/channel/hls/some_channel.m3u8?sig=abc&token=def"


class FakeStream:
    def __init__(self, master: str) -> None:
        self._master = master

    def to_manifest_url(self) -> str:
        return self._master


class FakeStreamlink:
    """Stands in for a streamlink session: what `streams` answers for each
    address, or raises."""

    answers: ClassVar[dict[str, object]] = {}
    asked: ClassVar[list[str]] = []

    def streams(self, url: str):
        FakeStreamlink.asked.append(url)
        answer = FakeStreamlink.answers.get(url, {})
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def streamlink(monkeypatch) -> type[FakeStreamlink]:
    FakeStreamlink.answers = {}
    FakeStreamlink.asked = []
    monkeypatch.setattr(twitch_stream, "Streamlink", FakeStreamlink)
    return FakeStreamlink


class TestFindingTheStream:
    def test_a_live_channels_master_playlist_is_its_streams_address(self, streamlink):
        streamlink.answers["https://www.twitch.tv/some_channel"] = {
            "best": FakeStream(MASTER),
            "worst": FakeStream("https://usher.example/other"),
        }

        assert twitch_stream.get_stream_urls("some_channel") == StreamUrls(live=MASTER, vod=None)

    def test_a_channel_that_is_not_live_has_no_stream(self, streamlink):
        with pytest.raises(StreamUrlError, match="is it live"):
            twitch_stream.get_stream_urls("some_channel")

    @pytest.mark.parametrize("error", [PluginError("integrity check failed"), NoPluginError()])
    def test_streamlink_failing_is_no_stream_found(self, streamlink, error):
        streamlink.answers["https://www.twitch.tv/some_channel"] = error

        with pytest.raises(StreamUrlError, match="Twitch did not say"):
            twitch_stream.get_stream_urls("some_channel")

    def test_a_twitch_channel_known_by_its_name_here_is_looked_for_on_twitch(self, streamlink):
        streamlink.answers["https://www.twitch.tv/some_channel"] = {"best": FakeStream(MASTER)}

        assert streams.stream_urls("some_channel@twitch").live == MASTER
        assert streamlink.asked == ["https://www.twitch.tv/some_channel"]


def test_streamlink_leaves_the_names_of_the_log_levels_as_the_service_writes_them():
    # Imported, it would have every logger in the process write "info".
    assert [logging.getLevelName(level) for level in (logging.INFO, logging.WARNING, logging.ERROR)] == [
        "INFO",
        "WARNING",
        "ERROR",
    ]


VOD_MASTER = "https://usher.example/vod/v2/555.m3u8?nauthsig=abc&nauth=def"


class TestFindingTheVod:
    """What an ad covered is in the stream's VOD: the recording Helix says
    is of the stream that is on, where the channel keeps one."""

    @pytest.fixture
    def live_stream(self, streamlink):
        streamlink.answers["https://www.twitch.tv/some_channel"] = {"best": FakeStream(MASTER)}
        return streamlink

    def test_the_vod_of_the_stream_that_is_on_goes_with_it(self, live_stream, twitch_api):
        twitch_api.add_channel("some_channel", "101", is_live=True, stream_id="9", archive_id="555")
        live_stream.answers["https://www.twitch.tv/videos/555"] = {"best": FakeStream(VOD_MASTER)}

        assert twitch_stream.get_stream_urls("some_channel") == StreamUrls(live=MASTER, vod=VOD_MASTER)
        assert twitch_api.calls("GET", "/helix/videos") == [{"user_id": "101", "type": "archive", "first": "5"}]

    def test_a_recording_of_a_stream_that_is_over_is_not_it(self, live_stream, twitch_api):
        twitch_api.add_channel("some_channel", "101", is_live=True, older_archives=["444"])

        assert twitch_stream.get_stream_urls("some_channel").vod is None
        assert "https://www.twitch.tv/videos/444" not in live_stream.asked

    def test_a_channel_that_keeps_no_past_broadcasts_has_none(self, live_stream, twitch_api):
        twitch_api.add_channel("some_channel", "101", is_live=True)

        assert twitch_stream.get_stream_urls("some_channel").vod is None

    def test_a_vod_streamlink_cannot_open_is_none_and_the_stream_is_recorded_all_the_same(
        self, live_stream, twitch_api, caplog
    ):
        # Kept for its subscribers only, for one.
        twitch_api.add_channel("some_channel", "101", is_live=True, archive_id="555")
        live_stream.answers["https://www.twitch.tv/videos/555"] = PluginError("subscribers only")

        with caplog.at_level(logging.INFO, logger="kick_clip_hunter"):
            urls = twitch_stream.get_stream_urls("some_channel")

        assert urls == StreamUrls(live=MASTER, vod=None)
        assert any("VOD of the stream could not be found" in record.getMessage() for record in caplog.records)

    def test_without_twitch_set_up_there_is_no_vod_to_find(self, live_stream, kick_api):
        assert twitch_stream.get_stream_urls("some_channel") == StreamUrls(live=MASTER, vod=None)


PAST_MASTER = "https://usher.example/vod/v2/777.m3u8?nauthsig=abc&nauth=def"
BEGAN = datetime(2026, 10, 16, 14, 38, 15, tzinfo=timezone.utc)


class TestFindingAPastBroadcast:
    """The recording of a stream that is over stays there for a while: the
    one a moment was in is found by its time, with when it began - its
    playlist says no times of its own."""

    @pytest.fixture
    def channel(self, twitch_api, streamlink):
        channel = twitch_api.add_channel("some_channel", "101")
        channel.past_broadcasts = [
            past_broadcast("777", "2026-10-16T14:38:15Z", "9h58m16s"),
            past_broadcast("666", "2026-10-15T08:39:13Z", "17h3m33s"),
        ]
        streamlink.answers["https://www.twitch.tv/videos/777"] = {"best": FakeStream(PAST_MASTER)}
        return channel

    def test_the_one_a_time_was_in_is_found_with_when_it_began(self, channel):
        vod = twitch_stream.vod_at("some_channel", BEGAN + timedelta(hours=6))

        assert vod == Vod(master=PAST_MASTER, started_at=BEGAN)

    def test_helix_is_asked_for_as_many_of_the_channels_as_it_gives_at_once(self, channel, twitch_api):
        twitch_stream.vod_at("some_channel", BEGAN + timedelta(hours=6))

        assert twitch_api.calls("GET", "/helix/videos") == [
            {"user_id": "101", "type": "archive", "first": str(twitch_stream.ARCHIVES_LOOKED_AT)}
        ]

    def test_the_one_of_the_stream_that_is_on_reaches_to_now(self, channel, streamlink):
        channel.is_live, channel.stream_id = True, "9"
        channel.past_broadcasts.insert(0, past_broadcast("888", "2026-10-22T09:00:00Z", "5m", stream_id="9"))
        streamlink.answers["https://www.twitch.tv/videos/888"] = {"best": FakeStream(VOD_MASTER)}

        vod = twitch_stream.vod_at("some_channel", datetime(2026, 10, 22, 11, 0, tzinfo=timezone.utc))

        assert vod.master == VOD_MASTER

    def test_a_time_no_past_broadcast_holds_has_none(self, channel):
        with pytest.raises(NoVod, match="Twitch lists no past broadcast of that stream"):
            twitch_stream.vod_at("some_channel", BEGAN - timedelta(hours=2))

    def test_one_kept_from_the_public_is_none_to_be_had(self, channel):
        channel.past_broadcasts[0]["viewable"] = "private"

        with pytest.raises(NoVod, match="from the public"):
            twitch_stream.vod_at("some_channel", BEGAN + timedelta(hours=6))

    def test_one_streamlink_cannot_open_is_said(self, channel, streamlink):
        streamlink.answers["https://www.twitch.tv/videos/777"] = PluginError("subscribers only")

        with pytest.raises(NoVod, match="subscribers only"):
            twitch_stream.vod_at("some_channel", BEGAN + timedelta(hours=6))

    def test_one_streamlink_finds_nothing_at_is_said(self, channel, streamlink):
        streamlink.answers["https://www.twitch.tv/videos/777"] = {}

        with pytest.raises(NoVod, match="would not say where"):
            twitch_stream.vod_at("some_channel", BEGAN + timedelta(hours=6))

    def test_a_channel_twitch_does_not_have_has_none(self, channel):
        with pytest.raises(NoVod, match=r"could not be asked .*ValueError"):
            twitch_stream.vod_at("gone_channel", BEGAN)

    def test_without_twitch_set_up_there_is_none(self, kick_api):
        with pytest.raises(NoVod, match="Twitch"):
            twitch_stream.vod_at("some_channel", BEGAN)


class TestWhereAPastBroadcastIsWatched:
    @pytest.mark.parametrize(
        ("seconds", "written"), [(3725.9, "1h2m5s"), (59, "0h0m59s"), (36000, "10h0m0s"), (-3, "0h0m0s")]
    )
    def test_the_time_is_written_as_twitch_writes_it(self, seconds, written):
        assert twitch_stream.watch_link("777", seconds) == f"https://www.twitch.tv/videos/777?t={written}"

    @pytest.mark.anyio
    async def test_the_one_a_time_was_in_is_linked_to_from_then(self, twitch_api):
        twitch_api.add_channel("some_channel", "101").past_broadcasts = [
            past_broadcast("777", "2026-10-16T14:38:15Z", "9h58m16s"),
            past_broadcast("666", "2026-10-15T08:39:13Z", "17h3m33s"),
        ]

        link = await twitch_stream.watch_link_at("some_channel", BEGAN + timedelta(hours=6, seconds=5))

        assert link == "https://www.twitch.tv/videos/777?t=6h0m5s"

    @pytest.mark.anyio
    async def test_a_time_no_past_broadcast_holds_has_none(self, twitch_api):
        twitch_api.add_channel("some_channel", "101").past_broadcasts = [
            past_broadcast("777", "2026-10-16T14:38:15Z", "1h")
        ]

        with pytest.raises(NoVod, match="Twitch lists no past broadcast of that stream"):
            await twitch_stream.watch_link_at("some_channel", BEGAN - timedelta(hours=2))

    @pytest.mark.anyio
    async def test_without_twitch_set_up_there_is_none(self, kick_api):
        with pytest.raises(NoVod, match="Twitch"):
            await twitch_stream.watch_link_at("some_channel", BEGAN)


@pytest.mark.anyio
class TestWhatKeepsAClipFromBeingCutFromAPastBroadcast:
    async def test_a_channel_keeping_public_past_broadcasts_has_nothing_in_the_way(self, twitch_api):
        twitch_api.add_channel("some_channel", "101").past_broadcasts = [
            past_broadcast("777", "2026-10-16T14:38:15Z", "1h")
        ]

        assert await twitch_stream.vod_trouble("some_channel") is None

    async def test_a_channel_keeping_none_is_said(self, twitch_api):
        twitch_api.add_channel("some_channel", "101")

        assert (await twitch_stream.vod_trouble("some_channel")).startswith(
            "Twitch lists no past broadcasts of this channel"
        )

    async def test_one_keeping_them_from_the_public_is_said(self, twitch_api):
        twitch_api.add_channel("some_channel", "101").past_broadcasts = [
            past_broadcast("777", "2026-10-16T14:38:15Z", "1h", viewable="private")
        ]

        assert "from the public" in await twitch_stream.vod_trouble("some_channel")

    async def test_twitch_not_set_up_says_nothing_either_way(self, kick_api):
        assert await twitch_stream.vod_trouble("some_channel") is None


@pytest.mark.parametrize(
    ("helix", "seconds"), [("9h58m16s", 35896), ("43m50s", 2630), ("59s", 59), ("1h", 3600), ("", 0), (None, 0)]
)
def test_how_long_helix_says_a_past_broadcast_is_is_read_in_seconds(helix, seconds):
    assert twitch_stream.length_seconds(helix) == seconds
