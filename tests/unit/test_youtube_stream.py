"""Where a live YouTube channel's stream is: the video it is streaming, as
YouTube says, and that video's master playlist, as streamlink finds it."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import ClassVar

import pytest
from streamlink.exceptions import PluginError

from kick_clip_hunter import streams, youtube_stream
from kick_clip_hunter.platforms import StreamUrlError, StreamUrls

MASTER = "https://manifest.example/api/manifest/hls_variant/expire/1792091778/ei/abc/id/livevideo01.1/file/index.m3u8"
EXPIRES = datetime.fromtimestamp(1792091778, timezone.utc)
WATCH = "https://www.youtube.com/watch?v=livevideo01"


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
    monkeypatch.setattr(youtube_stream, "Streamlink", FakeStreamlink)
    return FakeStreamlink


class TestFindingTheStream:
    def test_the_video_streamed_is_asked_for_its_master_playlist(self, youtube, streamlink):
        youtube.add_channel("some_channel").go_live("livevideo01")
        streamlink.answers[WATCH] = {"best": FakeStream(MASTER), "worst": FakeStream("https://manifest.example/x")}

        assert youtube_stream.get_stream_urls("some_channel") == StreamUrls(live=MASTER, expires_at=EXPIRES)
        assert streamlink.asked == [WATCH]

    def test_a_channel_that_is_not_live_has_no_stream(self, youtube, streamlink):
        youtube.add_channel("some_channel").schedule()

        with pytest.raises(StreamUrlError, match="is it live"):
            youtube_stream.get_stream_urls("some_channel")
        # A stream only scheduled is not asked about at all.
        assert streamlink.asked == []

    def test_a_channel_youtube_no_longer_has_has_no_stream(self, youtube, streamlink):
        with pytest.raises(StreamUrlError, match="YouTube did not say what 'gone_channel' is streaming"):
            youtube_stream.get_stream_urls("gone_channel")

    def test_youtube_refusing_to_say_is_no_stream_found(self, youtube, streamlink):
        youtube.add_channel("some_channel").go_live("livevideo01")
        youtube.failures["resolve_url"] = 429

        with pytest.raises(StreamUrlError, match="did not say what"):
            youtube_stream.get_stream_urls("some_channel")

    def test_streamlink_failing_is_no_stream_found(self, youtube, streamlink):
        youtube.add_channel("some_channel").go_live("livevideo01")
        streamlink.answers[WATCH] = PluginError("could not get video info")

        with pytest.raises(StreamUrlError, match="YouTube did not say where"):
            youtube_stream.get_stream_urls("some_channel")

    def test_streamlink_finding_nothing_is_no_stream_found(self, youtube, streamlink):
        youtube.add_channel("some_channel").go_live("livevideo01")

        with pytest.raises(StreamUrlError, match="YouTube did not say where"):
            youtube_stream.get_stream_urls("some_channel")

    def test_a_youtube_channel_known_by_its_name_here_is_looked_for_on_youtube(self, youtube, streamlink):
        youtube.add_channel("Some.Channel").go_live("livevideo01")
        streamlink.answers[WATCH] = {"best": FakeStream(MASTER)}

        assert streams.stream_urls("some.channel@youtube").live == MASTER


class TestWhenAnAddressRunsOut:
    @pytest.mark.parametrize(
        "url",
        [
            "https://manifest.example/api/manifest/hls_variant/expire/1792091778/ei/abc/file/index.m3u8",
            "https://manifest.example/api/manifest/hls_variant/expire/1792091778",
            "https://segments.example/videoplayback?id=abc&expire=1792091778&ip=1.2.3.4",
        ],
    )
    def test_an_address_says_when_it_stops_working(self, url):
        assert youtube_stream.expires_at(url) == EXPIRES

    @pytest.mark.parametrize(
        "url",
        [
            "https://manifest.example/api/manifest/hls_variant/ei/abc/file/index.m3u8",
            "https://manifest.example/expires/1792091778/x",
            "https://manifest.example/expire/soon/x",
        ],
    )
    def test_one_that_does_not_say_has_no_end(self, url):
        assert youtube_stream.expires_at(url) is None
