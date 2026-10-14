"""Parsing of the two kinds of HLS playlist the recorder reads.

The samples follow what Kick's player is actually served (an AWS IVS style
live playlist: 2-4 second MPEG-TS segments, a program date-time on each, a
title after the duration, vendor tags in between), with the hosts and signed
tokens replaced.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kick_clip_hunter import recorder
from kick_clip_hunter.recorder import RecorderError

MASTER_URL = "https://playlist.example/v1/playlist/master.m3u8?token=abc"

MASTER = """#EXTM3U
#EXT-X-SESSION-DATA:DATA-ID="NODE",VALUE="video-edge.example"
#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="720p60",NAME="720p60",AUTOSELECT=YES,DEFAULT=YES
#EXT-X-STREAM-INF:BANDWIDTH=3422999,RESOLUTION=1280x720,CODECS="avc1.4D401F,mp4a.40.2",VIDEO="720p60",FRAME-RATE=60.000
https://playlist.example/v1/playlist/720p60.m3u8
#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="1080p60",NAME="1080p60",AUTOSELECT=YES,DEFAULT=YES
#EXT-X-STREAM-INF:BANDWIDTH=8534030,RESOLUTION=1920x1080,CODECS="avc1.64002A,mp4a.40.2",VIDEO="1080p60",FRAME-RATE=60.000
https://playlist.example/v1/playlist/1080p60.m3u8
#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="160p30",NAME="160p",AUTOSELECT=YES,DEFAULT=YES
#EXT-X-STREAM-INF:BANDWIDTH=230000,RESOLUTION=284x160,CODECS="avc1.4D401F,mp4a.40.2",VIDEO="160p30",FRAME-RATE=30.000
https://playlist.example/v1/playlist/160p30.m3u8
"""

VARIANT_URL = "https://playlist.example/v1/playlist/1080p60.m3u8"

VARIANT = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:6
#EXT-X-MEDIA-SEQUENCE:1628
#EXT-X-NET-LIVE-VIDEO-LIVE-SEQUENCE:1628
#EXT-X-NET-LIVE-VIDEO-ELAPSED-SECS:3255.383
#EXT-X-DATERANGE:ID="playlist-creation-1",CLASS="timestamp",START-DATE="2026-10-08T12:27:00.621Z",END-ON-NEXT=YES
#EXT-X-DATERANGE:ID="source-1",CLASS="live-video-net-stream-source",START-DATE="2026-10-08T12:26:32.089Z",DURATION=6.000
#EXT-X-PROGRAM-DATE-TIME:2026-10-08T12:26:32.089Z
#EXTINF:2.000,live
https://segments.example/v1/segment/first.ts
#EXT-X-PROGRAM-DATE-TIME:2026-10-08T12:26:34.089Z
#EXTINF:2.000,live
https://segments.example/v1/segment/second.ts
#EXT-X-PROGRAM-DATE-TIME:2026-10-08T12:26:36.089Z
#EXTINF:4.167,live
https://segments.example/v1/segment/third.ts
#EXT-X-PREFETCH:https://segments.example/v1/segment/not-published-yet-1.ts
#EXT-X-PREFETCH:https://segments.example/v1/segment/not-published-yet-2.ts
"""


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def playlist(*lines: str) -> str:
    return "\n".join(("#EXTM3U", "#EXT-X-TARGETDURATION:6", *lines)) + "\n"


class TestMasterPlaylist:
    def test_picks_the_highest_bandwidth_variant_wherever_it_is_listed(self):
        assert recorder._pick_variant(MASTER, MASTER_URL) == "https://playlist.example/v1/playlist/1080p60.m3u8"

    def test_commas_inside_quoted_attributes_do_not_confuse_it(self):
        # CODECS="avc1...,mp4a..." sits between BANDWIDTH and the rest.
        attributes = recorder._attributes(
            '#EXT-X-STREAM-INF:BANDWIDTH=8534030,RESOLUTION=1920x1080,CODECS="avc1.64002A,mp4a.40.2",VIDEO="1080p60"'
        )
        assert attributes == {
            "BANDWIDTH": "8534030",
            "RESOLUTION": "1920x1080",
            "CODECS": "avc1.64002A,mp4a.40.2",
            "VIDEO": "1080p60",
        }

    def test_relative_variant_urls_are_resolved_against_the_master(self):
        master = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100\nvariants/only.m3u8\n"

        assert recorder._pick_variant(master, MASTER_URL) == "https://playlist.example/v1/playlist/variants/only.m3u8"

    def test_a_variant_without_a_bandwidth_is_still_usable(self):
        master = "#EXTM3U\n#EXT-X-STREAM-INF:RESOLUTION=1280x720\nhttps://playlist.example/only.m3u8\n"

        assert recorder._pick_variant(master, MASTER_URL) == "https://playlist.example/only.m3u8"

    def test_a_master_without_variants_is_an_error(self):
        with pytest.raises(RecorderError, match="no variants"):
            recorder._pick_variant("#EXTM3U\n#EXT-X-VERSION:3\n", MASTER_URL)

    def test_windows_line_endings_are_tolerated(self):
        assert recorder._pick_variant(MASTER.replace("\n", "\r\n"), MASTER_URL).endswith("/1080p60.m3u8")


class TestVariantPlaylist:
    def test_lists_every_published_segment_and_nothing_else(self):
        parsed = recorder._parse_playlist(VARIANT, VARIANT_URL)

        # The two prefetch hints are not segments yet.
        assert [segment.url for segment in parsed.segments] == [
            "https://segments.example/v1/segment/first.ts",
            "https://segments.example/v1/segment/second.ts",
            "https://segments.example/v1/segment/third.ts",
        ]

    def test_numbers_segments_from_the_media_sequence(self):
        parsed = recorder._parse_playlist(VARIANT, VARIANT_URL)

        assert [segment.sequence for segment in parsed.segments] == [1628, 1629, 1630]

    def test_reads_duration_and_title(self):
        parsed = recorder._parse_playlist(VARIANT, VARIANT_URL)

        assert [segment.duration for segment in parsed.segments] == [2.0, 2.0, 4.167]
        assert {segment.title for segment in parsed.segments} == {"live"}

    def test_reads_each_segments_program_time_as_utc(self):
        parsed = recorder._parse_playlist(VARIANT, VARIANT_URL)

        assert [segment.started_at for segment in parsed.segments] == [
            utc(2026, 10, 8, 12, 26, 32, 89000),
            utc(2026, 10, 8, 12, 26, 34, 89000),
            utc(2026, 10, 8, 12, 26, 36, 89000),
        ]

    def test_collects_the_date_range_classes(self):
        parsed = recorder._parse_playlist(VARIANT, VARIANT_URL)

        assert parsed.daterange_classes == {"timestamp", "live-video-net-stream-source"}

    def test_a_live_playlist_has_not_ended(self):
        assert recorder._parse_playlist(VARIANT, VARIANT_URL).ended is False

    def test_an_endlist_tag_marks_the_stream_as_over(self):
        assert recorder._parse_playlist(VARIANT + "#EXT-X-ENDLIST\n", VARIANT_URL).ended is True

    def test_relative_segment_urls_are_resolved_against_the_playlist(self):
        parsed = recorder._parse_playlist(playlist("#EXTINF:2.000,live", "../segment/1.ts"), VARIANT_URL)

        assert parsed.segments[0].url == "https://playlist.example/v1/segment/1.ts"

    def test_a_program_time_is_carried_forward_to_segments_without_their_own(self):
        parsed = recorder._parse_playlist(
            playlist(
                "#EXT-X-PROGRAM-DATE-TIME:2026-10-08T12:00:00.000Z",
                "#EXTINF:2.000,live",
                "a.ts",
                "#EXTINF:4.000,live",
                "b.ts",
                "#EXTINF:2.000,live",
                "c.ts",
            ),
            VARIANT_URL,
        )

        start = utc(2026, 10, 8, 12, 0, 0)
        assert [segment.started_at for segment in parsed.segments] == [
            start,
            start + timedelta(seconds=2),
            start + timedelta(seconds=6),
        ]

    def test_without_any_program_time_segments_have_no_start(self):
        parsed = recorder._parse_playlist(
            playlist("#EXTINF:2.000,live", "a.ts", "#EXTINF:2.000,live", "b.ts"), VARIANT_URL
        )

        assert [segment.started_at for segment in parsed.segments] == [None, None]

    def test_a_program_time_without_a_zone_is_taken_as_utc(self):
        parsed = recorder._parse_playlist(
            playlist("#EXT-X-PROGRAM-DATE-TIME:2026-10-08T12:00:00.500", "#EXTINF:2.000,live", "a.ts"), VARIANT_URL
        )

        assert parsed.segments[0].started_at == utc(2026, 10, 8, 12, 0, 0, 500000)

    def test_a_program_time_with_an_offset_keeps_its_instant(self):
        parsed = recorder._parse_playlist(
            playlist("#EXT-X-PROGRAM-DATE-TIME:2026-10-08T14:00:00.000+02:00", "#EXTINF:2.000,live", "a.ts"),
            VARIANT_URL,
        )

        assert parsed.segments[0].started_at == utc(2026, 10, 8, 12, 0, 0)

    def test_a_discontinuity_marks_only_the_segment_that_follows_it(self):
        parsed = recorder._parse_playlist(
            playlist(
                "#EXTINF:2.000,live",
                "a.ts",
                "#EXT-X-DISCONTINUITY",
                "#EXTINF:2.000,live",
                "b.ts",
                "#EXTINF:2.000,live",
                "c.ts",
            ),
            VARIANT_URL,
        )

        assert [segment.discontinuity for segment in parsed.segments] == [False, True, False]

    def test_a_segment_without_a_title_has_an_empty_one(self):
        parsed = recorder._parse_playlist(playlist("#EXTINF:4.167,", "a.ts", "#EXTINF:2", "b.ts"), VARIANT_URL)

        assert [(segment.duration, segment.title) for segment in parsed.segments] == [(4.167, ""), (2.0, "")]

    def test_a_playlist_without_a_media_sequence_counts_from_zero(self):
        parsed = recorder._parse_playlist(
            playlist("#EXTINF:2.000,live", "a.ts", "#EXTINF:2.000,live", "b.ts"), VARIANT_URL
        )

        assert [segment.sequence for segment in parsed.segments] == [0, 1]

    def test_an_empty_playlist_has_no_segments(self):
        parsed = recorder._parse_playlist("#EXTM3U\n#EXT-X-TARGETDURATION:6\n", VARIANT_URL)

        assert parsed.segments == []
        assert parsed.daterange_classes == set()

    def test_windows_line_endings_and_blank_lines_are_tolerated(self):
        parsed = recorder._parse_playlist(VARIANT.replace("\n", "\r\n\r\n"), VARIANT_URL)

        assert len(parsed.segments) == 3
        assert parsed.segments[0].started_at == utc(2026, 10, 8, 12, 26, 32, 89000)


class TestAdSegments:
    """A stitched ad is announced by a stream-source date range naming
    something other than the broadcast; it lasts until the next one."""

    @staticmethod
    def source(name: str) -> str:
        return (
            f'#EXT-X-DATERANGE:ID="source-{name}",CLASS="live-video-net-stream-source",'
            f'X-NET-LIVE-VIDEO-STREAM-SOURCE="{name}"'
        )

    def ads(self, *lines: str) -> list[bool]:
        return [segment.ad for segment in recorder._parse_playlist(playlist(*lines), VARIANT_URL).segments]

    def test_segments_of_the_broadcast_are_not_ads(self):
        flags = self.ads(self.source("live"), "#EXTINF:4.167,live", "a.ts", "#EXTINF:4.167,live", "b.ts")

        assert flags == [False, False]

    def test_segments_after_another_source_is_announced_are_ads_until_the_broadcast_is_back(self):
        flags = self.ads(
            self.source("live"),
            "#EXTINF:4.167,live",
            "before.ts",
            self.source("1ac39748-2160"),
            "#EXT-X-DISCONTINUITY",
            "#EXTINF:2.000,1ac39748-2160",
            "ad-1.ts",
            "#EXTINF:2.000,1ac39748-2160",
            "ad-2.ts",
            self.source("live"),
            "#EXT-X-DISCONTINUITY",
            "#EXTINF:4.167,live",
            "after.ts",
        )

        assert flags == [False, True, True, False]

    def test_a_playlist_that_names_no_source_has_no_ads(self):
        assert [segment.ad for segment in recorder._parse_playlist(VARIANT, VARIANT_URL).segments] == [False] * 3


# What Twitch's player is served (the same IVS make as Kick's, with Twitch's
# own names for its tags), with the hosts and signed tokens replaced.
TWITCH_MASTER = """#EXTM3U
#EXT-X-SESSION-DATA:DATA-ID="NODE",VALUE="video-edge.example"
#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="chunked",NAME="1080p60 (source)",AUTOSELECT=YES,DEFAULT=YES
#EXT-X-STREAM-INF:BANDWIDTH=6624734,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2",VIDEO="chunked",FRAME-RATE=60.000
https://playlist.example/v1/playlist/chunked.m3u8
#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="1080p60_portrait",NAME="1080p60",AUTOSELECT=YES,DEFAULT=YES
#EXT-X-STREAM-INF:BANDWIDTH=7000000,RESOLUTION=1080x1920,CODECS="avc1.640028,mp4a.40.2",VIDEO="1080p60_portrait",IVS-GROUPS="portrait"
https://playlist.example/v1/playlist/portrait.m3u8
#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="audio_only",NAME="audio_only",AUTOSELECT=NO,DEFAULT=NO
#EXT-X-STREAM-INF:BANDWIDTH=160000,CODECS="mp4a.40.2",VIDEO="audio_only"
https://playlist.example/v1/playlist/audio_only.m3u8
"""

TWITCH_VARIANT = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:6
#EXT-X-MEDIA-SEQUENCE:8878
#EXT-X-TWITCH-LIVE-SEQUENCE:8878
#EXT-X-TWITCH-ELAPSED-SECS:17756.000
#EXT-X-TWITCH-TOTAL-SECS:17788.000
#EXT-X-DATERANGE:ID="playlist-creation-1",CLASS="timestamp",START-DATE="2026-10-14T12:17:34.822Z",END-ON-NEXT=YES
#EXT-X-DATERANGE:ID="playlist-session-1",CLASS="twitch-session",START-DATE="2026-10-14T12:17:34.822Z",END-ON-NEXT=YES
#EXT-X-DATERANGE:ID="source-1",CLASS="twitch-stream-source",START-DATE="2026-10-14T12:17:04.434Z",END-ON-NEXT=YES,X-TV-TWITCH-STREAM-SOURCE="live"
#EXT-X-PROGRAM-DATE-TIME:2026-10-14T12:17:04.434Z
#EXTINF:2.000,live
https://segments.example/v1/segment/first
#EXT-X-PROGRAM-DATE-TIME:2026-10-14T12:17:06.434Z
#EXTINF:2.000,live
https://segments.example/v1/segment/second
#EXT-X-TWITCH-PREFETCH:https://segments.example/v1/segment/not-published-yet-1
#EXT-X-TWITCH-PREFETCH:https://segments.example/v1/segment/not-published-yet-2
"""


class TestTwitchPlaylists:
    def test_the_source_variant_is_picked(self):
        assert recorder._pick_variant(TWITCH_MASTER, MASTER_URL).endswith("/chunked.m3u8")

    def test_an_upright_variant_is_not_picked_however_good_it_is(self):
        upright_only = (
            '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=7000000,IVS-GROUPS="portrait"\nhttps://playlist.example/p.m3u8\n'
        )

        assert recorder._pick_variant(TWITCH_MASTER, MASTER_URL) != "https://playlist.example/v1/playlist/portrait.m3u8"
        # ...unless there is no other.
        assert recorder._pick_variant(upright_only, MASTER_URL) == "https://playlist.example/p.m3u8"

    def test_the_published_segments_are_read_with_their_program_times(self):
        parsed = recorder._parse_playlist(TWITCH_VARIANT, VARIANT_URL)

        assert [(segment.sequence, segment.started_at, segment.title, segment.ad) for segment in parsed.segments] == [
            (8878, utc(2026, 10, 14, 12, 17, 4, 434000), "live", False),
            (8879, utc(2026, 10, 14, 12, 17, 6, 434000), "live", False),
        ]
        assert "twitch-stream-source" in parsed.daterange_classes


class TestTwitchAdSegments:
    """Twitch marks a stitched ad in each of three ways: a stream source
    other than the broadcast, a date range spanning the ad, and the ad's
    segment titles. Any one is enough."""

    def ads(self, *lines: str) -> list[bool]:
        return [segment.ad for segment in recorder._parse_playlist(playlist(*lines), VARIANT_URL).segments]

    def test_a_stream_source_other_than_the_broadcast(self):
        flags = self.ads(
            '#EXT-X-DATERANGE:ID="source-1",CLASS="twitch-stream-source",X-TV-TWITCH-STREAM-SOURCE="live"',
            "#EXTINF:2.000,live",
            "before.ts",
            '#EXT-X-DATERANGE:ID="source-2",CLASS="twitch-stream-source",X-TV-TWITCH-STREAM-SOURCE="ad"',
            "#EXTINF:2.000,live",
            "ad.ts",
            '#EXT-X-DATERANGE:ID="source-3",CLASS="twitch-stream-source",X-TV-TWITCH-STREAM-SOURCE="live"',
            "#EXTINF:2.000,live",
            "after.ts",
        )

        assert flags == [False, True, False]

    def test_a_date_range_spanning_a_stitched_ad(self):
        flags = self.ads(
            '#EXT-X-DATERANGE:ID="stitched-ad-1",CLASS="twitch-stitched-ad",START-DATE="2026-10-14T12:00:02.000Z",'
            "DURATION=4.000",
            "#EXT-X-PROGRAM-DATE-TIME:2026-10-14T12:00:00.000Z",
            "#EXTINF:2.000,live",
            "before.ts",
            "#EXT-X-DISCONTINUITY",
            "#EXTINF:2.000,live",
            "ad-1.ts",
            "#EXTINF:2.000,live",
            "ad-2.ts",
            "#EXT-X-DISCONTINUITY",
            "#EXTINF:2.000,live",
            "after.ts",
        )

        assert flags == [False, True, True, False]

    def test_a_date_range_is_known_by_its_id_too_and_may_come_after_the_segments(self):
        flags = self.ads(
            "#EXT-X-PROGRAM-DATE-TIME:2026-10-14T12:00:00.000Z",
            "#EXTINF:2.000,live",
            "ad.ts",
            "#EXTINF:2.000,live",
            "after.ts",
            '#EXT-X-DATERANGE:ID="stitched-ad-1",START-DATE="2026-10-14T12:00:00.000Z",END-DATE="2026-10-14T12:00:02Z"',
        )

        assert flags == [True, False]

    def test_a_segment_titled_for_the_advertiser(self):
        flags = self.ads("#EXTINF:2.000,live", "before.ts", "#EXTINF:2.000,Amazon|123456", "ad.ts")

        assert flags == [False, True]

    def test_a_date_range_that_does_not_say_how_long_it_runs_marks_nothing(self):
        flags = self.ads(
            '#EXT-X-DATERANGE:ID="stitched-ad-1",CLASS="twitch-stitched-ad",START-DATE="2026-10-14T12:00:00.000Z"',
            "#EXT-X-PROGRAM-DATE-TIME:2026-10-14T12:00:00.000Z",
            "#EXTINF:2.000,live",
            "a.ts",
        )

        assert flags == [False]

    def test_a_date_range_that_cannot_be_read_marks_nothing(self):
        flags = self.ads(
            '#EXT-X-DATERANGE:ID="stitched-ad-1",CLASS="twitch-stitched-ad",START-DATE="tomorrow",DURATION=4',
            "#EXT-X-PROGRAM-DATE-TIME:2026-10-14T12:00:00.000Z",
            "#EXTINF:2.000,live",
            "a.ts",
        )

        assert flags == [False]


class TestFragmentedMp4:
    """Twitch's VOD is fragmented MP4: its segments play only after the
    initialization segment the playlist names (EXT-X-MAP)."""

    VOD = playlist(
        '#EXT-X-MAP:URI="init-0.mp4"',
        "#EXT-X-PROGRAM-DATE-TIME:2026-10-13T15:43:01.893Z",
        "#EXTINF:10.000,",
        "0.mp4",
        "#EXT-X-PROGRAM-DATE-TIME:2026-10-13T15:43:11.893Z",
        "#EXTINF:10.000,",
        "1.mp4",
    )

    def test_each_segment_knows_the_initialization_segment_it_plays_after(self):
        parsed = recorder._parse_playlist(self.VOD, "https://vod.example/abc/chunked/index-dvr.m3u8?t=1")

        assert {segment.init_url for segment in parsed.segments} == {"https://vod.example/abc/chunked/init-0.mp4"}
        assert [segment.url.rsplit("/", 1)[1] for segment in parsed.segments] == ["0.mp4", "1.mp4"]

    def test_an_mpeg_ts_segment_has_none(self):
        assert {segment.init_url for segment in recorder._parse_playlist(VARIANT, VARIANT_URL).segments} == {None}

    @pytest.mark.parametrize(
        ("init_url", "group"),
        [
            (None, "vod"),
            ("https://vod.example/abc/chunked/init-0.mp4", "vod-init-0"),
            ("https://vod.example/abc/chunked/init-1.mp4?x=1", "vod-init-1"),
            ("https://vod.example/abc/chunked/../weird name.mp4", "vod-weird-name"),
        ],
    )
    def test_a_vod_segment_is_kept_in_the_group_of_its_initialization_segment(self, init_url, group):
        segment = recorder._PlaylistSegment(0, "x", 10.0, "", None, False, init_url=init_url)

        assert recorder._vod_group(segment) == group
        assert recorder._is_vod_group(group)
