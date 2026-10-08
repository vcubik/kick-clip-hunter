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
