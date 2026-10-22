"""Where the recording of a Kick channel's stream is (its VOD): kick.com's
list of the channel's streams, asked over plain HTTP, and the one a time was
in. The browser that finds a live stream's address is not tested here (see
docs/testing.md)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kick_clip_hunter import kick_stream
from kick_clip_hunter.platforms import NoVod, Vod
from tests.support.kick_api import kick_video

T0 = datetime(2026, 10, 21, 19, 2, 25, tzinfo=timezone.utc)
SOURCE = "https://stream.kick.com/ivs/v1/1/abc/2026/10/21/19/2/xyz/media/hls/master.m3u8"


def minutes(count: float) -> timedelta:
    return timedelta(minutes=count)


class TestWhichStreamATimeWasIn:
    def test_the_stream_whose_time_it_lies_in(self):
        earlier = kick_video(T0 - minutes(600), 3600, "earlier")
        stream = kick_video(T0, 4 * 3600, "this")

        assert kick_stream.covering([stream, earlier], T0 + minutes(90)) == stream

    def test_one_that_is_on_reaches_to_now(self):
        stream = kick_video(T0, 0, "this", live=True)

        assert kick_stream.covering([stream], T0 + minutes(500)) == stream

    def test_a_moment_just_outside_a_streams_time_is_still_its(self):
        # The VOD begins a few seconds before the start kick.com gives, and
        # the length it gives is whole seconds.
        stream = kick_video(T0, 3600, "this")

        assert kick_stream.covering([stream], T0 - timedelta(seconds=10)) == stream
        assert kick_stream.covering([stream], T0 + minutes(60) + timedelta(seconds=10)) == stream

    def test_of_two_near_streams_the_one_it_is_in_wins(self):
        first = kick_video(T0, 90, "first")
        second = kick_video(T0 + timedelta(seconds=120), 3600, "second")

        assert kick_stream.covering([second, first], T0 + timedelta(seconds=80)) == first
        assert kick_stream.covering([second, first], T0 + timedelta(seconds=125)) == second

    def test_a_time_far_from_every_stream_is_in_none(self):
        stream = kick_video(T0, 3600, "this")

        assert kick_stream.covering([stream], T0 + minutes(90)) is None
        assert kick_stream.covering([stream], T0 - minutes(5)) is None

    def test_an_entry_that_says_no_start_is_passed_over(self):
        stream = kick_video(T0, 3600, "this")

        assert kick_stream.covering([{"start_time": None}, "junk", stream], T0 + minutes(1)) == stream


class TestFindingTheVod:
    def test_the_vod_of_the_stream_a_time_was_in_is_found_by_its_address(self, kick_api):
        channel = kick_api.add_channel("Some_Channel", 1)
        channel.videos = [kick_video(T0, 4 * 3600, SOURCE), kick_video(T0 - minutes(1440), 3600, "older")]

        assert kick_stream.vod_at("Some_Channel", T0 + minutes(60)) == Vod(master=SOURCE)
        # kick.com's own list, by the slug in small letters; no token.
        assert kick_api.calls("GET", "kick.com/api/v2/channels/some_channel/videos") == [{}]

    def test_the_vod_of_the_stream_that_is_on_is_found_too(self, kick_api):
        kick_api.add_channel("some_channel", 1).videos = [kick_video(T0, 0, SOURCE, live=True)]

        assert kick_stream.vod_at("some_channel", T0 + minutes(5)).master == SOURCE

    def test_a_vod_for_subscribers_only_is_none_to_be_had(self, kick_api):
        kick_api.add_channel("some_channel", 1).videos = [kick_video(T0, 3600, SOURCE, status="subscriber_only")]

        with pytest.raises(NoVod, match="for the channel's subscribers only"):
            kick_stream.vod_at("some_channel", T0 + minutes(5))

    def test_a_stream_kick_lists_without_an_address_has_none(self, kick_api):
        kick_api.add_channel("some_channel", 1).videos = [kick_video(T0, 3600, "", status="public")]

        with pytest.raises(NoVod, match="no address"):
            kick_stream.vod_at("some_channel", T0 + minutes(5))

    def test_a_stream_kick_does_not_list_has_none(self, kick_api):
        kick_api.add_channel("some_channel", 1).videos = [kick_video(T0, 3600, SOURCE)]

        with pytest.raises(NoVod, match="Kick lists no VOD of that stream"):
            kick_stream.vod_at("some_channel", T0 - minutes(600))

    def test_kick_not_answering_is_said(self, kick_api):
        kick_api.add_channel("some_channel", 1).videos = [kick_video(T0, 3600, SOURCE)]
        kick_api.failures["/videos"] = 503

        with pytest.raises(NoVod, match=r"could not be asked .*HTTPStatusError"):
            kick_stream.vod_at("some_channel", T0 + minutes(5))
