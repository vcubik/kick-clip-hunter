"""Where a live Twitch channel's stream is, as streamlink finds it: only its
master playlist is taken - the recorder follows the stream from there."""

from __future__ import annotations

import logging
from typing import ClassVar

import pytest
from streamlink.exceptions import NoPluginError, PluginError

from kick_clip_hunter import streams, twitch_stream
from kick_clip_hunter.platforms import StreamUrlError, StreamUrls

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
