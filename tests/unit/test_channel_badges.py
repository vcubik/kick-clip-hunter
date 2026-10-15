"""A channel's own subscriber badges: what is kept of Kick's and Twitch's
lists of them, and which one a subscriber is drawn with.

An address ends up in a picture's `src`, so part of this is about what is
refused.
"""

from __future__ import annotations

import pytest

from kick_clip_hunter.channel_badges import Picture, from_kick, from_twitch, pick, twitch_tier

KICK = "https://files.kick.com/channel_subscriber_badges/{}/conversion/original-fullsize.png"
TWITCH = "https://static-cdn.jtvnw.net/badges/v1/{}/3"


def kick_badge(months: object, src: object) -> dict:
    return {"id": 1, "channel_id": 2, "months": months, "badge_image": {"srcset": "", "src": src}}


def twitch_set(*versions: tuple[object, object], set_id: str = "subscriber") -> dict:
    return {
        "set_id": set_id,
        "versions": [{"id": number, "image_url_4x": image, "title": "Subscriber"} for number, image in versions],
    }


class TestKick:
    def test_each_badge_by_the_months_it_is_shown_from(self):
        channel = {"subscriber_badges": [kick_badge(3, KICK.format(3)), kick_badge(1, KICK.format(1))]}

        assert from_kick(channel) == [Picture(1, 1, KICK.format(1)), Picture(1, 3, KICK.format(3))]

    @pytest.mark.parametrize("channel", [{}, {"subscriber_badges": None}, {"subscriber_badges": "x"}])
    def test_a_channel_without_a_list_has_none(self, channel):
        assert from_kick(channel) == []

    @pytest.mark.parametrize(
        "entry",
        [
            None,
            "badge",
            kick_badge(-1, KICK.format(1)),
            kick_badge("3", KICK.format(1)),
            kick_badge(True, KICK.format(1)),
            kick_badge(1, None),
            {"months": 1, "badge_image": "x"},
        ],
    )
    def test_one_that_is_not_what_kick_sends_is_left_out(self, entry):
        assert from_kick({"subscriber_badges": [entry, kick_badge(2, KICK.format(2))]}) == [
            Picture(1, 2, KICK.format(2))
        ]


class TestTwitch:
    def test_the_subscriber_sets_versions_by_tier_and_months(self):
        sets = [
            twitch_set(("0", TWITCH.format("a")), ("3", TWITCH.format("b")), ("2000", TWITCH.format("c"))),
            twitch_set(("100", TWITCH.format("bits")), set_id="bits"),
        ]

        assert from_twitch(sets) == [
            Picture(1, 0, TWITCH.format("a")),
            Picture(1, 3, TWITCH.format("b")),
            Picture(2, 0, TWITCH.format("c")),
        ]

    def test_the_largest_picture_and_failing_that_the_next(self):
        version = {"id": "0", "image_url_1x": TWITCH.format("x"), "image_url_2x": TWITCH.format("y")}

        assert from_twitch([{"set_id": "subscriber", "versions": [version]}]) == [Picture(1, 0, TWITCH.format("y"))]

    @pytest.mark.parametrize("number", ["", "abc", "-3", "1234567", 3, None])
    def test_a_version_that_is_no_number_is_left_out(self, number):
        assert from_twitch([twitch_set((number, TWITCH.format("x")))]) == []

    @pytest.mark.parametrize(("version", "tier"), [("0", (1, 0)), ("24", (1, 24)), ("2003", (2, 3)), ("3012", (3, 12))])
    def test_a_version_says_its_tier_and_months(self, version, tier):
        assert twitch_tier(version) == tier

    def test_sets_that_are_not_what_helix_sends_are_left_out(self):
        assert from_twitch([None, "subscriber", {"set_id": "subscriber", "versions": "x"}]) == []


class TestOnlyPicturesOfThePlatforms:
    @pytest.mark.parametrize(
        "src",
        [
            "http://files.kick.com/badge.png",
            "https://example.com/badge.png",
            "https://files.kick.com.example.com/badge.png",
            "javascript:alert(1)",
            '"https://files.kick.com/x.png" onerror="alert(1)',
            "https://files.kick.com/" + "x" * 400,
            "//files.kick.com/badge.png",
        ],
    )
    def test_another_address_is_refused(self, src):
        assert from_kick({"subscriber_badges": [kick_badge(1, src)]}) == []

    def test_either_platforms_host_is_taken_from_either(self):
        assert from_kick({"subscriber_badges": [kick_badge(1, TWITCH.format("x"))]}) == [
            Picture(1, 1, TWITCH.format("x"))
        ]


PICTURES = (
    Picture(1, 0, "t1-0"),
    Picture(1, 3, "t1-3"),
    Picture(1, 12, "t1-12"),
    Picture(2, 0, "t2-0"),
    Picture(2, 6, "t2-6"),
)


class TestPick:
    @pytest.mark.parametrize(("months", "image"), [(0, "t1-0"), (2, "t1-0"), (3, "t1-3"), (11, "t1-3"), (40, "t1-12")])
    def test_the_longest_subscription_reached(self, months, image):
        assert pick(PICTURES, 1, months) == image

    def test_of_the_subscribers_tier(self):
        assert pick(PICTURES, 2, 8) == "t2-6"

    def test_a_tier_the_channel_has_no_badges_for_gets_the_first_tiers(self):
        assert pick(PICTURES, 3, 8) == "t1-3"

    def test_without_months_or_before_the_first_badge_it_is_the_first(self):
        pictures = [Picture(1, 1, "one"), Picture(1, 6, "six")]

        assert pick(pictures, 1, None) == pick(pictures, 1, 0) == "one"

    def test_a_channel_without_badges_of_its_own_has_none(self):
        assert pick([], 1, 5) is None
        assert pick([Picture(2, 0, "t2")], 1, 5) is None
