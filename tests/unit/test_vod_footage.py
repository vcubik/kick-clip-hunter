"""The pure parts of making a moment's file longer from its broadcast's VOD:
which segments of the recording there are and when each begins, which hold
a stretch, and in what order to look for the file in them."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kick_clip_hunter import recorder, vod_footage
from kick_clip_hunter.vod_footage import FootageError, Piece

T0 = datetime(2026, 10, 21, 19, 0, tzinfo=timezone.utc)
URL = "https://vod.example/media/hls/1080p60/playlist.m3u8"


def playlist(*lines: str) -> recorder._Playlist:
    return recorder._parse_playlist("\n".join(["#EXTM3U", *lines]), URL)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def piece(start: float, duration: float = 5.0, name: str = "x.ts") -> Piece:
    return Piece(f"https://vod.example/{name}", at(start), duration)


class TestTheRecordingsSegments:
    def test_each_begins_when_its_program_date_time_says(self):
        found = vod_footage.pieces(
            playlist(
                "#EXT-X-PROGRAM-DATE-TIME:2026-10-21T19:00:00.000Z",
                "#EXTINF:12.5,",
                "0.ts",
                "#EXT-X-PROGRAM-DATE-TIME:2026-10-21T19:00:14.000Z",
                "#EXTINF:12.5,",
                "1.ts",
            ),
            began=None,
        )

        assert [(p.url, p.started_at, p.duration) for p in found] == [
            ("https://vod.example/media/hls/1080p60/0.ts", at(0), 12.5),
            ("https://vod.example/media/hls/1080p60/1.ts", at(14), 12.5),
        ]

    def test_without_one_they_are_counted_on_from_when_the_broadcast_began(self):
        found = vod_footage.pieces(
            playlist("#EXTINF:10.0,", "0.ts", "#EXTINF:10.5,", "1.ts", "#EXTINF:10,", "2.ts"), T0
        )

        assert [p.started_at for p in found] == [at(0), at(10), at(20.5)]

    def test_with_neither_there_is_no_telling_where_footage_is(self):
        with pytest.raises(FootageError, match="says nothing of when"):
            vod_footage.pieces(playlist("#EXTINF:10.0,", "0.ts"), None)

    def test_a_fragment_keeps_its_initialization_segment(self):
        (found,) = vod_footage.pieces(playlist('#EXT-X-MAP:URI="init-0.mp4"', "#EXTINF:10.0,", "0.m4s"), T0)

        assert found.init_url == "https://vod.example/media/hls/1080p60/init-0.mp4"

    @pytest.mark.parametrize(("name", "muted"), [("12-muted.ts", True), ("12-unmuted.ts", False), ("12.ts", False)])
    def test_a_segment_twitch_silenced_says_so_in_its_address(self, name, muted):
        assert piece(0, name=name).muted is muted


class TestAStreamWoundBackByNumber:
    """YouTube lists the newest segments of a stream that is on, with one
    date-time at the top; older ones are counted back from the first."""

    LIVE = (
        "#EXT-X-MEDIA-SEQUENCE:519",
        "#EXT-X-PROGRAM-DATE-TIME:2026-10-21T19:08:39.000Z",
        "#EXTINF:1.0,",
        "https://rr1.googlevideo.example/videoplayback/id/abc.1/itag/301/sq/519/goap/x",
        "#EXTINF:1.0,",
        "https://rr1.googlevideo.example/videoplayback/id/abc.1/itag/301/sq/520/goap/x",
        "#EXTINF:1.0,",
        "https://rr1.googlevideo.example/videoplayback/id/abc.1/itag/301/sq/521/goap/x",
    )

    def test_older_segments_are_had_by_their_number_counted_back_from_the_first_listed(self):
        found = vod_footage.numbered(playlist(*self.LIVE), at(10), at(12.5))

        assert [p.url.split("/sq/")[1] for p in found] == ["10/goap/x", "11/goap/x", "12/goap/x"]
        assert [p.started_at for p in found] == [at(10), at(11), at(12)]

    def test_none_before_the_streams_first(self):
        found = vod_footage.numbered(playlist(*self.LIVE), at(-30), at(1.5))

        assert [p.started_at for p in found] == [at(0), at(1)]

    def test_none_newer_than_the_newest_listed(self):
        found = vod_footage.numbered(playlist(*self.LIVE), at(519), at(600))

        assert [p.url.split("/sq/")[1].split("/")[0] for p in found] == ["519", "520", "521"]

    def test_a_playlist_without_numbers_in_its_addresses_cannot_be_wound_back(self):
        with pytest.raises(FootageError, match="does not say how earlier footage"):
            vod_footage.numbered(
                playlist("#EXT-X-PROGRAM-DATE-TIME:2026-10-21T19:08:39.000Z", "#EXTINF:1.0,", "seg/519.ts"),
                at(0),
                at(5),
            )

    def test_one_without_a_date_time_cannot_either(self):
        with pytest.raises(FootageError, match="says nothing of when"):
            vod_footage.numbered(playlist("#EXT-X-MEDIA-SEQUENCE:519", "#EXTINF:1.0,", "a/sq/519/x"), at(0), at(5))


class TestChoosingSegments:
    PIECES = (piece(0), piece(5), piece(10), piece(15))

    def test_the_segments_that_hold_a_stretch(self):
        assert vod_footage.covering(list(self.PIECES), at(4), at(10)) == [piece(0), piece(5)]

    def test_a_stretch_that_ends_where_a_segment_begins_does_not_take_it(self):
        assert vod_footage.covering(list(self.PIECES), at(5), at(10)) == [piece(5)]

    def test_the_file_is_looked_for_in_the_segment_it_should_be_in_and_then_outwards(self):
        order = vod_footage.nearest_first(list(self.PIECES), at(11))

        assert order == [piece(10), piece(5), piece(15), piece(0)]


class TestTellingPicturesApart:
    def test_a_picture_the_file_shows_more_than_once_says_nothing_of_where_it_is(self):
        marks = [(0.0, "a"), (2.0, "still"), (4.0, "b"), (6.0, "still")]

        assert vod_footage.unique_marks(marks) == {"a": 0.0, "b": 4.0}

    def test_the_offset_most_agree_on_wins(self):
        assert vod_footage.agreed([2.001, 2.0, 7.5]) == (2.0, 2)

    def test_nothing_to_agree_on_is_no_offset(self):
        assert vod_footage.agreed([]) is None
