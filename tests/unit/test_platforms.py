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

    @pytest.mark.parametrize(
        ("name", "platform", "login"),
        [
            ("some_channel", "kick", "some_channel"),
            ("Some_Channel", "kick", "Some_Channel"),
            ("some-channel", "kick", "some-channel"),
            ("some_channel@twitch", "twitch", "some_channel"),
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
        ],
    )
    def test_a_name_or_a_link_says_which_channel_is_meant(self, typed, meant):
        assert platforms.parse(typed) == meant
