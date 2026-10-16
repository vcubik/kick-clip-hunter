"""What the service asks of TikTok (tiktok_client.py), against the fake
TikTok: who a channel is, whether it is live and since when, and where its
stream is in every quality TikTok serves it in - and which of those a
recorder follows."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from kick_clip_hunter import tiktok_client
from kick_clip_hunter.tiktok_client import Stream, TikTokChanged

pytestmark = pytest.mark.anyio

ORIGIN = "https://pull-flv.example/game/stream-1.flv?expire=1793298938&sign=abc"


class TestARoom:
    async def test_a_live_channel_is_live_since_tiktok_says_with_its_room(self, tiktok):
        tiktok.add_channel("Some.Channel", user_id="6800000000123", room_id="7696913392682683158").go_live(
            started_at=1792077342, streams={"origin": ("h265", ORIGIN)}
        )

        room = await tiktok_client.room("some.channel")

        assert room.user_id == "6800000000123" and room.handle == "Some.Channel"
        assert room.room_id == 7696913392682683158
        assert room.live and room.started_at == datetime.fromtimestamp(1792077342, timezone.utc)
        assert room.streams == (Stream("origin", "h265", ORIGIN),)

    async def test_a_channel_that_is_not_live_is_said_to_be_so_without_a_stream(self, tiktok):
        tiktok.add_channel("some_channel")

        room = await tiktok_client.room("some_channel")

        assert (room.live, room.started_at, room.streams) == (False, None, ())
        assert room.room_id == 7600000000000000001

    async def test_tiktok_is_asked_as_its_own_pages_ask(self, tiktok):
        tiktok.add_channel("some_channel")

        await tiktok_client.room("@Some_Channel")

        assert tiktok.requests == [{"aid": "1988", "sourceType": "54", "uniqueId": "some_channel"}]

    async def test_an_account_tiktok_does_not_have_is_said_to_be_none(self, tiktok):
        with pytest.raises(ValueError, match="no such TikTok account: 'gone_channel'"):
            await tiktok_client.room("gone_channel")

    @pytest.mark.parametrize("handle", ["", "has space", "x" * 25, "slash/es", "ü"])
    async def test_what_cannot_be_a_handle_is_not_asked_about(self, tiktok, handle):
        with pytest.raises(ValueError, match="can't be a TikTok handle"):
            await tiktok_client.room(handle)
        assert tiktok.requests == []

    async def test_an_account_that_was_never_live_has_no_room(self, tiktok):
        tiktok.add_channel("new_channel", room_id="")

        assert (await tiktok_client.room("new_channel")).room_id is None

    async def test_tiktok_failing_to_answer_is_said_as_it_is(self, tiktok):
        tiktok.add_channel("some_channel")
        tiktok.failures["api-live"] = 503

        with pytest.raises(httpx.HTTPStatusError):
            await tiktok_client.room("some_channel")

    @pytest.mark.parametrize(
        "answer",
        [
            ["not", "an", "object"],
            {"statusCode": 10011, "message": "params_error", "data": {}},
            {"statusCode": 0, "data": {"user": {"id": "1", "uniqueId": "x"}}},
            {"statusCode": 0, "data": {"user": {"uniqueId": "x"}, "liveRoom": {"status": 4}}},
            {"statusCode": 0, "data": {"user": {"id": "abc", "uniqueId": "x"}, "liveRoom": {"status": 4}}},
            {"statusCode": 0, "data": []},
        ],
    )
    async def test_an_answer_without_what_is_read_of_it_is_said_to_be_one(self, tiktok, answer):
        tiktok.answers["api-live"] = answer

        with pytest.raises(TikTokChanged):
            await tiktok_client.room("some_channel")

    async def test_an_answer_that_is_not_json_is_said_to_be_one(self, tiktok):
        tiktok.answers["api-live"] = "<html>Please wait...</html>"

        with pytest.raises(TikTokChanged, match="not JSON"):
            await tiktok_client.room("some_channel")

    async def test_a_client_that_is_held_open_is_asked_with(self, tiktok):
        tiktok.add_channel("some_channel")

        async with tiktok_client.new_client() as client:
            first = await tiktok_client.room("some_channel", client)
            second = await tiktok_client.room("some_channel", client)

        assert first == second and len(tiktok.requests) == 2


def room_with(**listed: dict) -> dict:
    """A live room whose answer lists these qualities, by the key they are
    listed under."""
    return {key: {"pull_data": {"stream_data": json.dumps({"data": qualities})}} for key, qualities in listed.items()}


def main(flv: str, codec: str | None = None, **more) -> dict:
    said = json.dumps({"VCodec": codec} if codec else {})
    return {"main": {"flv": flv, "hls": "", "sdk_params": said, **more}}


class TestTheStreamsOfARoom:
    def test_every_quality_is_listed_with_its_codec_h264_before_hevc(self):
        streams = tiktok_client.streams_of(
            room_with(
                streamData={"hd": main("https://a/hd.flv", "h264"), "ao": main("https://a/ao.flv", "h264")},
                hevcStreamData={"origin": main("https://a/origin.flv", "h265"), "hd": main("https://a/hd5.flv")},
            )
        )

        assert streams == (
            Stream("hd", "h264", "https://a/hd.flv"),
            Stream("ao", "h264", "https://a/ao.flv"),
            Stream("origin", "h265", "https://a/origin.flv"),
        )

    @pytest.mark.parametrize(
        ("said", "codec"), [("avc", "h264"), ("hevc", "h265"), ("bytevc1", "h265"), ("H264", "h264"), ("vp9", "vp9")]
    )
    def test_a_codec_is_named_as_ffprobe_names_it(self, said, codec):
        (stream,) = tiktok_client.streams_of(room_with(streamData={"hd": main("https://a/hd.flv", said)}))

        assert stream.codec == codec

    def test_a_quality_without_a_codec_is_taken_for_its_lists(self):
        streams = tiktok_client.streams_of(
            room_with(streamData={"hd": main("https://a/1.flv")}, hevcStreamData={"sd": main("https://a/2.flv")})
        )

        assert [stream.codec for stream in streams] == ["h264", "h265"]

    @pytest.mark.parametrize(
        "live_room",
        [
            {},
            {"streamData": None},
            {"streamData": {"pull_data": {"stream_data": "not json"}}},
            {"streamData": {"pull_data": {"stream_data": json.dumps({"data": "none"})}}},
            room_with(streamData={"hd": {"main": {"flv": ""}}}),
            room_with(streamData={"hd": {"main": {"flv": "http://insecure/hd.flv"}}}),
            room_with(streamData={"hd": "nothing"}),
            room_with(streamData={"hd": main("https://a/hd.flv") | {"main": None}}),
        ],
    )
    def test_what_does_not_say_where_a_stream_is_lists_nothing(self, live_room):
        assert tiktok_client.streams_of(live_room) == ()

    def test_sdk_params_that_cannot_be_read_leave_the_codec_to_the_list(self):
        listed = room_with(streamData={"hd": {"main": {"flv": "https://a/hd.flv", "sdk_params": "{oops"}}})

        assert tiktok_client.streams_of(listed) == (Stream("hd", "h264", "https://a/hd.flv"),)


class TestTheStreamFollowed:
    def test_the_quality_the_streamer_sends_is_followed_where_there_is_one(self):
        streams = (Stream("hd", "h264", "https://a/hd.flv"), Stream("origin", "h265", "https://a/o.flv"))

        assert tiktok_client.best_stream(streams).quality == "origin"

    def test_otherwise_the_best_tiktok_makes_of_it(self):
        streams = (
            Stream("sd", "h265", "https://a/sd.flv"),
            Stream("uhd_60", "h265", "https://a/uhd.flv"),
            Stream("hd", "h264", "https://a/hd.flv"),
        )

        assert tiktok_client.best_stream(streams).quality == "uhd_60"

    def test_sound_alone_and_qualities_not_known_are_never_followed(self):
        assert tiktok_client.best_stream((Stream("ao", "h264", "https://a/ao.flv"), Stream("4k", "h264", "x"))) is None
        assert tiktok_client.best_stream(()) is None


class TestWhenAnAddressRunsOut:
    def test_an_address_says_when_it_stops_working(self):
        assert tiktok_client.expires_at(ORIGIN) == datetime.fromtimestamp(1793298938, timezone.utc)
        assert tiktok_client.expires_at("https://a/x.flv?sign=abc&expire=1793298938") == datetime.fromtimestamp(
            1793298938, timezone.utc
        )

    @pytest.mark.parametrize("url", ["https://a/x.flv", "https://a/x.flv?expires=1793298938", "https://a/expire=1"])
    def test_one_that_does_not_say_has_no_end(self, url):
        assert tiktok_client.expires_at(url) is None
