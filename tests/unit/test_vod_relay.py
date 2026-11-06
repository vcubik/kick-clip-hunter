"""The relay of a VOD whose host keeps a page elsewhere from fetching it:
only addresses the service handed out are fetched, and every address in a
playlist it hands on is one of those."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from kick_clip_hunter import vod_relay

BASE = "https://vod.example/abc/chunked/index-dvr.m3u8"


def relayed(line: str) -> str:
    """The address a relay address stands for, checking it is signed."""
    parts = urlsplit(line)
    assert parts.path == vod_relay.ROUTE
    query = parse_qs(parts.query)
    url, given = query["u"][0], query["s"][0]
    assert vod_relay.signed(url, given)
    return url


class TestSigning:
    def test_an_address_it_handed_out_is_signed(self):
        url = "https://vod.example/abc/0.ts"

        assert relayed(vod_relay.address(url)) == url

    def test_another_address_with_that_signature_is_not(self):
        signature = vod_relay.signature("https://vod.example/abc/0.ts")

        assert not vod_relay.signed("http://169.254.169.254/latest/meta-data", signature)

    def test_a_made_up_signature_is_not(self):
        assert not vod_relay.signed("https://vod.example/abc/0.ts", "made-up")


class TestRewritingAPlaylist:
    def test_a_masters_variants_are_fetched_through_the_relay(self):
        master = (
            "#EXTM3U\n"
            '#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="chunked",NAME="1080p60 (source)"\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=6000000,CODECS="avc1.64002A,mp4a.40.2",VIDEO="chunked"\n'
            "https://cdn.example/abc/chunked/index-dvr.m3u8\n"
        )

        lines = vod_relay.rewritten(master, "https://usher.example/vod/777.m3u8?sig=x").splitlines()

        # The tags stay as they are: none of them carries an address.
        assert lines[:3] == master.splitlines()[:3]
        assert relayed(lines[3]) == "https://cdn.example/abc/chunked/index-dvr.m3u8"

    def test_footage_listed_relative_to_the_playlist_is_resolved_against_it(self):
        playlist = "#EXTM3U\n#EXTINF:10.000,\n0.ts\n\n#EXTINF:10.000,\n1-muted.ts\n#EXT-X-ENDLIST\n"

        lines = vod_relay.rewritten(playlist, BASE).splitlines()

        assert relayed(lines[2]) == "https://vod.example/abc/chunked/0.ts"
        assert lines[3] == ""
        assert relayed(lines[5]) == "https://vod.example/abc/chunked/1-muted.ts"
        assert lines[6] == "#EXT-X-ENDLIST"

    def test_an_address_in_a_tag_is_fetched_through_the_relay_too(self):
        playlist = '#EXTM3U\n#EXT-X-MAP:URI="init-0.mp4"\n#EXTINF:2.000,\n0.mp4\n'

        line = vod_relay.rewritten(playlist, BASE).splitlines()[1]

        assert line.startswith('#EXT-X-MAP:URI="') and line.endswith('"')
        assert relayed(line.removeprefix('#EXT-X-MAP:URI="').removesuffix('"')) == (
            "https://vod.example/abc/chunked/init-0.mp4"
        )
