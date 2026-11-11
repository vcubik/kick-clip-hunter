"""Where a live Twitch channel's stream is, as streamlink finds it: only its
master playlist is taken - the recorder follows the stream from there."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import ClassVar

import httpx
import pytest
from streamlink.exceptions import NoPluginError, PluginError

from kick_clip_hunter import streams, twitch_client, twitch_stream
from kick_clip_hunter.platforms import (
    VODS_FOR_SUBSCRIBERS,
    VODS_HIDDEN,
    VODS_NONE,
    VODS_PUBLIC,
    WATCH_LATER_SECONDS,
    NoVod,
    StreamUrlError,
    StreamUrls,
    Vod,
)
from tests.support.twitch_api import past_broadcast

MASTER = "https://usher.example/api/v2/channel/hls/some_channel.m3u8?sig=abc&token=def"


class FakeStream:
    def __init__(self, master: str) -> None:
        self._master = master

    def to_manifest_url(self) -> str:
        return self._master


class FakeStreamlink:
    """Stands in for a streamlink session: what `streams` answers for each
    address, or raises - and the headers its Twitch plugin was told to send
    with each ask (`viewers`). One asked with a header Twitch refuses
    (`refused_headers`) raises as streamlink does."""

    answers: ClassVar[dict[str, object]] = {}
    asked: ClassVar[list[str]] = []
    viewers: ClassVar[list[dict[str, str]]] = []
    refused_headers: ClassVar[list[dict[str, str]]] = []

    def streams(self, url: str, options=None):
        headers = dict(options.get("api-header") or []) if options is not None else {}
        FakeStreamlink.asked.append(url)
        FakeStreamlink.viewers.append(headers)
        if headers and headers in FakeStreamlink.refused_headers:
            raise PluginError("Unable to validate response text: 401 Unauthorized")
        answer = FakeStreamlink.answers.get(url, {})
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def streamlink(monkeypatch) -> type[FakeStreamlink]:
    FakeStreamlink.answers = {}
    FakeStreamlink.asked = []
    FakeStreamlink.viewers = []
    FakeStreamlink.refused_headers = []
    monkeypatch.setattr(twitch_stream, "Streamlink", FakeStreamlink)
    return FakeStreamlink


AUTH_TOKEN = "subscriber-auth-token"
AS_THE_ACCOUNT = {"Authorization": f"OAuth {AUTH_TOKEN}"}


@pytest.fixture
def auth_token(monkeypatch) -> str:
    """The auth-token of an account signed in on twitch.tv set, as .env sets
    it."""
    monkeypatch.setenv("TWITCH_AUTH_TOKEN", AUTH_TOKEN)
    return AUTH_TOKEN


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


class TestAskingAsASignedInAccount:
    """With the auth-token of an account signed in on twitch.tv set, Twitch
    is asked for a stream as that account - a subscriber's gets what only
    subscribers do - and as a visitor where it will not answer that."""

    def test_without_one_twitch_is_asked_as_a_visitor(self, streamlink):
        streamlink.answers["https://www.twitch.tv/some_channel"] = {"best": FakeStream(MASTER)}

        twitch_stream.get_stream_urls("some_channel")

        assert streamlink.viewers == [{}]

    def test_with_one_streamlink_sends_it_to_twitch(self, streamlink, auth_token):
        streamlink.answers["https://www.twitch.tv/some_channel"] = {"best": FakeStream(MASTER)}

        assert twitch_stream.get_stream_urls("some_channel").live == MASTER
        assert streamlink.viewers == [AS_THE_ACCOUNT]

    def test_one_twitch_refuses_is_let_go_of_and_a_visitor_asks(self, streamlink, auth_token, caplog):
        streamlink.answers["https://www.twitch.tv/some_channel"] = {"best": FakeStream(MASTER)}
        streamlink.refused_headers = [AS_THE_ACCOUNT]

        with caplog.at_level(logging.WARNING, logger="kick_clip_hunter"):
            assert twitch_stream.get_stream_urls("some_channel").live == MASTER

        assert streamlink.viewers == [AS_THE_ACCOUNT, {}]
        assert "TWITCH_AUTH_TOKEN" in caplog.text
        assert AUTH_TOKEN not in caplog.text

    def test_a_vod_kept_for_subscribers_is_had_as_one(self, streamlink, auth_token, twitch_api):
        channel = twitch_api.add_channel("some_channel", "101")
        channel.past_broadcasts = [past_broadcast("777", "2026-10-16T14:38:15Z", "9h58m16s")]
        twitch_api.subscriber_only_videos.add("777")
        # Twitch plays it to the account only.
        streamlink.answers["https://www.twitch.tv/videos/777"] = {"best": FakeStream(VOD_MASTER)}

        assert twitch_stream.vod_at("some_channel", BEGAN + timedelta(hours=6)).master == VOD_MASTER
        assert streamlink.viewers == [AS_THE_ACCOUNT]


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

    @pytest.mark.parametrize("refused", [{}, PluginError("Manifest is restricted")])
    def test_one_kept_for_subscribers_says_so_once_its_playlist_is_refused(
        self, channel, streamlink, twitch_api, refused
    ):
        # Helix lists it as "public"; only its playback token tells.
        twitch_api.subscriber_only_videos.add("777")
        streamlink.answers["https://www.twitch.tv/videos/777"] = refused

        with pytest.raises(NoVod, match="for the channel's subscribers only") as refusal:
            twitch_stream.vod_at("some_channel", BEGAN + timedelta(hours=6))
        assert "TWITCH_AUTH_TOKEN" in str(refusal.value)

    def test_one_refused_to_an_account_that_is_no_subscriber_says_no_more(
        self, channel, streamlink, twitch_api, auth_token
    ):
        twitch_api.web_tokens.add(auth_token)
        twitch_api.subscriber_only_videos.add("777")
        streamlink.answers["https://www.twitch.tv/videos/777"] = {}

        with pytest.raises(NoVod, match="for the channel's subscribers only") as refusal:
            twitch_stream.vod_at("some_channel", BEGAN + timedelta(hours=6))
        assert "TWITCH_AUTH_TOKEN" not in str(refusal.value)

    def test_one_kept_for_subscribers_that_still_plays_is_had(self, channel, twitch_api):
        twitch_api.subscriber_only_videos.add("777")

        assert twitch_stream.vod_at("some_channel", BEGAN + timedelta(hours=6)).master == PAST_MASTER
        assert twitch_api.calls("POST", "gql.twitch.tv/gql") == []

    def test_a_refused_playlist_twitch_says_nothing_more_of_stays_unexplained(self, channel, streamlink, twitch_api):
        twitch_api.failures["gql.twitch.tv"] = 500
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

        # A little later than asked: nearer to what chat reacted to.
        assert link == twitch_stream.watch_link("777", 6 * 3600 + 5 + WATCH_LATER_SECONDS)

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
class TestWhatTwitchListsOfAChannelsPastBroadcasts:
    async def test_a_channel_keeping_public_past_broadcasts_has_nothing_in_the_way(self, twitch_api):
        twitch_api.add_channel("some_channel", "101").past_broadcasts = [
            past_broadcast("777", "2026-10-16T14:38:15Z", "1h")
        ]

        assert await twitch_stream.vod_access("some_channel") == VODS_PUBLIC
        assert VODS_PUBLIC not in twitch_stream.VOD_TROUBLE

    async def test_a_channel_keeping_none_has_none(self, twitch_api):
        twitch_api.add_channel("some_channel", "101")

        assert await twitch_stream.vod_access("some_channel") == VODS_NONE
        assert twitch_stream.VOD_TROUBLE[VODS_NONE].startswith("Twitch lists no past broadcasts of this channel")

    async def test_one_keeping_them_from_the_public_has_them_hidden(self, twitch_api):
        twitch_api.add_channel("some_channel", "101").past_broadcasts = [
            past_broadcast("777", "2026-10-16T14:38:15Z", "1h", viewable="private")
        ]

        assert await twitch_stream.vod_access("some_channel") == VODS_HIDDEN
        assert "from the public" in twitch_stream.VOD_TROUBLE[VODS_HIDDEN]

    async def test_one_keeping_them_for_its_subscribers_has_them_for_subscribers(self, twitch_api):
        # Helix lists them as "public" all the same.
        twitch_api.add_channel("some_channel", "101").past_broadcasts = [
            past_broadcast("777", "2026-10-16T14:38:15Z", "1h")
        ]
        twitch_api.subscriber_only_videos.add("777")

        assert await twitch_stream.vod_access("some_channel") == VODS_FOR_SUBSCRIBERS
        assert "subscribers only" in twitch_stream.VOD_TROUBLE[VODS_FOR_SUBSCRIBERS]
        assert (
            streams.vod_trouble("some_channel@twitch", VODS_FOR_SUBSCRIBERS)
            == (twitch_stream.VOD_TROUBLE[VODS_FOR_SUBSCRIBERS])
        )

    async def test_the_newest_of_a_stream_that_is_over_is_the_one_that_tells(self, twitch_api):
        channel = twitch_api.add_channel("some_channel", "101", is_live=True, stream_id="9")
        channel.past_broadcasts = [
            past_broadcast("888", "2026-10-22T09:00:00Z", "5m", stream_id="9"),
            past_broadcast("777", "2026-10-16T14:38:15Z", "1h"),
        ]
        twitch_api.subscriber_only_videos.add("777")

        assert await twitch_stream.vod_access("some_channel") == VODS_FOR_SUBSCRIBERS

    async def test_with_only_the_stream_that_is_on_listed_it_is_the_one_that_tells(self, twitch_api):
        twitch_api.add_channel("some_channel", "101", is_live=True, stream_id="9").past_broadcasts = [
            past_broadcast("888", "2026-10-22T09:00:00Z", "5m", stream_id="9")
        ]

        assert await twitch_stream.vod_access("some_channel") == VODS_PUBLIC

    async def test_what_helix_lists_stands_where_the_playback_token_is_not_had(self, twitch_api):
        twitch_api.add_channel("some_channel", "101").past_broadcasts = [
            past_broadcast("777", "2026-10-16T14:38:15Z", "1h")
        ]
        twitch_api.failures["gql.twitch.tv"] = 500

        assert await twitch_stream.vod_access("some_channel") == VODS_PUBLIC

    async def test_twitch_not_set_up_says_nothing_either_way(self, kick_api):
        assert await twitch_stream.vod_access("some_channel") is None


@pytest.mark.parametrize(
    ("helix", "seconds"), [("9h58m16s", 35896), ("43m50s", 2630), ("59s", 59), ("1h", 3600), ("", 0), (None, 0)]
)
def test_how_long_helix_says_a_past_broadcast_is_is_read_in_seconds(helix, seconds):
    assert twitch_stream.length_seconds(helix) == seconds


@pytest.mark.anyio
class TestWhoMayWatchAPastBroadcast:
    """What the playback token Twitch hands a visitor says of a past
    broadcast: the qualities only the channel's subscribers may watch."""

    async def test_it_is_asked_as_twitchs_own_site_asks_it(self, twitch_api):
        assert await twitch_client.video_restricted_qualities("777") == []

        [query] = twitch_api.calls("POST", "gql.twitch.tv/gql")
        assert query["operationName"] == "PlaybackAccessToken"
        assert query["variables"]["vodID"] == "777"
        assert query["variables"]["isVod"] is True

    async def test_without_an_auth_token_a_visitor_asks(self, twitch_api):
        await twitch_client.video_restricted_qualities("777")

        assert twitch_api.gql_viewers == [None]

    async def test_a_subscriber_may_watch_one_kept_for_subscribers_in_full(self, twitch_api, auth_token):
        twitch_api.web_tokens.add(auth_token)
        twitch_api.subscribing_tokens.add(auth_token)
        twitch_api.subscriber_only_videos.add("777")

        assert await twitch_client.video_restricted_qualities("777") == []
        assert await twitch_stream.kept_for_subscribers("777") is False
        assert twitch_api.gql_viewers == [auth_token, auth_token]

    async def test_an_account_that_is_no_subscriber_may_not(self, twitch_api, auth_token):
        twitch_api.web_tokens.add(auth_token)
        twitch_api.subscriber_only_videos.add("777")

        assert await twitch_stream.kept_for_subscribers("777") is True

    async def test_a_token_twitch_refuses_is_let_go_of_and_a_visitor_asks(self, twitch_api, auth_token, caplog):
        twitch_api.subscriber_only_videos.add("777")

        with caplog.at_level(logging.WARNING, logger="kick_clip_hunter"):
            assert twitch_client.SOURCE_QUALITY in await twitch_client.video_restricted_qualities("777")

        assert twitch_api.gql_viewers == [auth_token, None]
        assert "TWITCH_AUTH_TOKEN" in caplog.text
        assert auth_token not in caplog.text

    async def test_one_kept_for_subscribers_has_its_source_among_them(self, twitch_api):
        twitch_api.subscriber_only_videos.add("777")

        assert twitch_client.SOURCE_QUALITY in await twitch_client.video_restricted_qualities("777")
        assert await twitch_stream.kept_for_subscribers("777") is True
        assert await twitch_stream.kept_for_subscribers("666") is False

    @pytest.mark.parametrize(
        "answer",
        [
            {"errors": [{"message": "PersistedQueryNotFound"}]},
            {"data": {"videoPlaybackAccessToken": None}},
            {"data": {"videoPlaybackAccessToken": {"value": "not json", "signature": "s"}}},
            {"data": {"videoPlaybackAccessToken": {"value": '{"chansub": {"restricted_bitrates": "x"}}'}}},
        ],
    )
    async def test_an_answer_that_does_not_say_is_not_taken_for_one(self, twitch_api, monkeypatch, answer):
        monkeypatch.setattr(twitch_api, "_gql", lambda request, body: httpx.Response(200, json=answer))

        with pytest.raises(ValueError, match="did not say"):
            await twitch_client.video_restricted_qualities("777")
        assert await twitch_stream.kept_for_subscribers("777") is None
