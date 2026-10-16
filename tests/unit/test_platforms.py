"""How a channel's name says which platform it is on - the name everything
of the channel is kept and shown under."""

from __future__ import annotations

import pytest

from kick_clip_hunter import platforms


class TestChannelNames:
    def test_a_kick_channel_is_called_by_its_slug(self):
        assert platforms.channel_name(platforms.KICK, "some_channel") == "some_channel"

    def test_a_channel_elsewhere_is_called_by_its_name_and_its_platforms(self):
        assert platforms.channel_name(platforms.TWITCH, "some_channel") == "some_channel@twitch"
        assert platforms.channel_name(platforms.YOUTUBE, "some.channel") == "some.channel@youtube"
        assert platforms.channel_name(platforms.TIKTOK, "some.channel") == "some.channel@tiktok"

    @pytest.mark.parametrize(
        ("name", "platform", "login"),
        [
            ("some_channel", "kick", "some_channel"),
            ("Some_Channel", "kick", "Some_Channel"),
            ("some-channel", "kick", "some-channel"),
            ("some_channel@twitch", "twitch", "some_channel"),
            ("some.channel@youtube", "youtube", "some.channel"),
            ("český-kanál@youtube", "youtube", "český-kanál"),
            ("some.channel_1@tiktok", "tiktok", "some.channel_1"),
        ],
    )
    def test_the_name_says_where_the_channel_is(self, name, platform, login):
        assert platforms.split(name) == (platform, login)
        assert (platforms.platform_of(name), platforms.login_of(name)) == (platform, login)

    @pytest.mark.parametrize("name", ["some_channel@nowhere", "@twitch", "some_channel@", "some_channel@kick"])
    def test_what_names_no_platform_it_knows_is_taken_for_a_kick_name(self, name):
        assert platforms.split(name) == ("kick", name)

    def test_a_name_and_its_platform_come_apart_again(self):
        for platform in platforms.PLATFORMS:
            assert platforms.split(platforms.channel_name(platform, "some_channel")) == (platform, "some_channel")

    def test_every_platform_has_a_name_for_people(self):
        assert set(platforms.TITLES) == set(platforms.PLATFORMS)

    def test_only_tiktok_channels_are_recorded_and_never_tracked(self):
        assert platforms.RECORDED_ONLY == {platforms.TIKTOK}


class TestWhatWasTyped:
    @pytest.mark.parametrize(
        ("typed", "meant"),
        [
            ("some_channel", ("kick", "some_channel")),
            ("  some_channel  ", ("kick", "some_channel")),
            ("some_channel@twitch", ("twitch", "some_channel")),
            ("https://kick.com/some_channel", ("kick", "some_channel")),
            ("kick.com/some_channel", ("kick", "some_channel")),
            ("https://www.twitch.tv/some_channel", ("twitch", "some_channel")),
            ("https://m.twitch.tv/some_channel/", ("twitch", "some_channel")),
            ("twitch.tv/some_channel?sr=a", ("twitch", "some_channel")),
            ("HTTPS://WWW.TWITCH.TV/Some_Channel", ("twitch", "Some_Channel")),
            ("http://twitch.tv/some_channel/videos", ("twitch", "some_channel")),
            ("some.channel@youtube", ("youtube", "some.channel")),
            ("@Some.Channel@youtube", ("youtube", "Some.Channel")),
            ("https://www.youtube.com/@Some.Channel", ("youtube", "Some.Channel")),
            ("youtube.com/@some-channel/live", ("youtube", "some-channel")),
            ("https://m.youtube.com/@some_channel/streams?view=2", ("youtube", "some_channel")),
            ("HTTPS://WWW.YOUTUBE.COM/@Some_Channel#about", ("youtube", "Some_Channel")),
            ("https://www.youtube.com/@%C4%8Desk%C3%BD-kan%C3%A1l", ("youtube", "český-kanál")),
            ("some.channel@tiktok", ("tiktok", "some.channel")),
            ("@Some.Channel@tiktok", ("tiktok", "Some.Channel")),
            ("https://www.tiktok.com/@some.channel", ("tiktok", "some.channel")),
            ("https://www.tiktok.com/@some.channel/live", ("tiktok", "some.channel")),
            ("tiktok.com/@Some_Channel?lang=cs-CZ", ("tiktok", "Some_Channel")),
            ("HTTPS://M.TIKTOK.COM/@some_channel/video/7600000000000000000", ("tiktok", "some_channel")),
        ],
    )
    def test_a_name_or_a_link_says_which_channel_is_meant(self, typed, meant):
        assert platforms.parse(typed) == meant

    @pytest.mark.parametrize(
        ("typed", "address"),
        [
            (
                "https://www.youtube.com/channel/UCAAAAAAAAAAAAAAAAAAAAAA",
                "https://www.youtube.com/channel/UCAAAAAAAAAAAAAAAAAAAAAA",
            ),
            ("youtube.com/c/SomeChannel/videos", "https://www.youtube.com/c/SomeChannel/videos"),
            ("https://www.youtube.com/user/SomeChannel", "https://www.youtube.com/user/SomeChannel"),
            ("https://www.youtube.com/live/abcdefghijk?si=x", "https://www.youtube.com/live/abcdefghijk?si=x"),
            ("youtube.com", "https://www.youtube.com/"),
        ],
    )
    def test_a_youtube_link_without_a_handle_is_kept_whole_for_youtube_to_say_whose_it_is(self, typed, address):
        assert platforms.parse(typed) == ("youtube", address)

    @pytest.mark.parametrize(
        "typed",
        [
            "https://www.youtube.com/watch?v=abcdefghijk&t=42s",
            "https://m.youtube.com/watch/?feature=share&v=abcdefghijk",
            "https://youtu.be/abcdefghijk?si=x",
            "youtu.be/abcdefghijk",
        ],
    )
    def test_a_link_to_a_video_comes_down_to_which_video_it_is(self, typed):
        assert platforms.parse(typed) == ("youtube", "https://www.youtube.com/watch?v=abcdefghijk")

    def test_a_site_that_only_begins_like_youtube_is_not_youtube(self):
        assert platforms.parse("youtube.community/@some_channel")[0] == "kick"

    def test_a_link_to_tiktok_that_names_no_channel_is_not_taken_for_one(self):
        assert platforms.parse("https://www.tiktok.com/live")[0] == "kick"
