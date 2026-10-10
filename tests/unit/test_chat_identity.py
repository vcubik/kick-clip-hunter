"""A chatter's name colour and badges: what is taken from Kick's `sender`,
and how it is drawn.

Everything in an identity is typed or chosen by a viewer and ends up in the
page - the colour in a style attribute, a badge's type in a file name - so
most of this is about what is refused.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from kick_clip_hunter import chat_identity

STATIC = Path(chat_identity.__file__).parent / "static"


def sender(identity: object) -> dict:
    return {"user_id": 1, "username": "alice", "identity": identity}


def stored_badges(identity: dict) -> list[dict]:
    stored = chat_identity.from_sender(sender(identity))
    assert stored is not None
    return json.loads(stored[1])


class TestWhatIsKept:
    def test_the_colour_and_the_badges_as_kick_sends_them(self):
        identity = {
            "username_color": "#FF5733",
            "badges": [
                {"text": "Moderator", "type": "moderator"},
                {"text": "Sub Gifter", "type": "sub_gifter", "count": 5},
                {"text": "Subscriber", "type": "subscriber", "count": 3},
            ],
        }

        colour, badges = chat_identity.from_sender(sender(identity))

        assert colour == "#ff5733"
        assert json.loads(badges) == [
            {"text": "Moderator", "type": "moderator"},
            {"text": "Sub Gifter", "type": "sub_gifter", "count": 5},
            {"text": "Subscriber", "type": "subscriber", "count": 3},
        ]

    def test_the_same_identity_is_always_stored_the_same_way(self):
        # It is compared with what was stored last to decide whether to write.
        first = {"username_color": "#FF5733", "badges": [{"text": "VIP", "type": "vip"}]}
        second = {"badges": [{"type": "vip", "text": "VIP"}], "username_color": "#ff5733"}

        assert chat_identity.from_sender(sender(first)) == chat_identity.from_sender(sender(second))

    def test_someone_with_neither_is_still_someone_known(self):
        assert chat_identity.from_sender(sender({"username_color": None, "badges": []})) == (None, "[]")
        assert chat_identity.from_sender(sender({})) == (None, chat_identity.NO_BADGES)

    @pytest.mark.parametrize("identity", [None, "moderator", 7, ["x"]])
    def test_a_message_without_an_identity_says_nothing_about_the_chatter(self, identity):
        assert chat_identity.from_sender(sender(identity)) is None
        assert chat_identity.from_sender({"username": "alice"}) is None

    @pytest.mark.parametrize(
        "colour",
        ["red", "#FFF", "#FF57331", "FF5733", "#GG5733", "#FF5733; background: url(//evil)", "", 16734003, None],
    )
    def test_only_a_six_digit_hex_colour_is_a_colour(self, colour):
        stored = chat_identity.from_sender(sender({"username_color": colour, "badges": []}))

        assert stored == (None, "[]")

    @pytest.mark.parametrize(
        "badge",
        [
            {"text": "Moderator"},
            {"text": "x", "type": "../../secret"},
            {"text": "x", "type": "Moderator"},
            {"text": "x", "type": 'mod" onerror="alert(1)'},
            {"text": "x", "type": ""},
            {"text": "x", "type": "a" * 33},
            {"text": "x", "type": 5},
            "moderator",
            None,
        ],
    )
    def test_a_badge_without_a_plain_type_is_left_out(self, badge):
        # The type picks the picture's file.
        good = {"text": "VIP", "type": "vip"}

        assert stored_badges({"badges": [badge, good]}) == [good]

    def test_badges_that_are_not_a_list_are_no_badges(self):
        assert stored_badges({"badges": {"type": "vip"}}) == []
        assert stored_badges({"badges": "vip"}) == []

    @pytest.mark.parametrize("count", [0, -3, "5", 2.5, True, None])
    def test_a_count_is_a_positive_whole_number_or_not_there(self, count):
        assert stored_badges({"badges": [{"text": "Subscriber", "type": "subscriber", "count": count}]}) == [
            {"text": "Subscriber", "type": "subscriber"}
        ]

    def test_a_badges_text_is_kept_short_and_is_text(self):
        (long,) = stored_badges({"badges": [{"text": "x" * 500, "type": "vip"}]})
        (odd,) = stored_badges({"badges": [{"text": {"a": 1}, "type": "vip"}]})

        assert long["text"] == "x" * chat_identity.MAX_BADGE_TEXT
        assert odd["text"] == ""

    def test_only_so_many_badges_are_kept(self):
        many = [{"text": f"Badge {number}", "type": f"badge_{number}"} for number in range(50)]

        assert stored_badges({"badges": many}) == many[: chat_identity.MAX_BADGES]


class TestNameColour:
    def test_the_background_it_is_measured_against_is_the_one_chat_is_on(self):
        stylesheet = (STATIC / "dashboard.css").read_text(encoding="utf-8")

        (field,) = re.findall(r"--field:\s*(#[0-9A-Fa-f]{6})", stylesheet)

        assert chat_identity.CHAT_BACKGROUND.lower() == field.lower()

    def test_contrast_is_the_usual_ratio(self):
        assert chat_identity.contrast("#ffffff", "#000000") == pytest.approx(21)
        assert chat_identity.contrast("#585858", "#585858") == pytest.approx(1)
        assert chat_identity.contrast("#000000", "#ffffff") == chat_identity.contrast("#ffffff", "#000000")

    @pytest.mark.parametrize("colour", ["#ffffff", "#ffeb57", "#9ad8ff", "#53fc18"])
    def test_a_colour_that_can_be_read_is_left_as_it_is(self, colour):
        assert chat_identity.readable(colour) == colour

    @pytest.mark.parametrize("colour", ["#000000", "#0000ff", "#1b263b", "#7a0000", "#585858", "#ff0000"])
    def test_one_that_cannot_is_lightened_until_it_can(self, colour):
        drawn = chat_identity.readable(colour)

        assert chat_identity.contrast(colour) < chat_identity.MIN_NAME_CONTRAST
        assert chat_identity.contrast(drawn) >= chat_identity.MIN_NAME_CONTRAST
        assert re.fullmatch(r"#[0-9a-f]{6}", drawn)

    def test_it_is_lightened_no_further_than_it_has_to_be(self):
        # Still blue, not white.
        red, green, blue = (int(chat_identity.readable("#0000ff")[index : index + 2], 16) for index in (1, 3, 5))

        assert blue == 255 and red == green < 200

    def test_any_colour_comes_out_readable(self):
        for value in range(0, 0x1000000, 0x030509):
            assert chat_identity.contrast(chat_identity.readable(f"#{value:06x}")) >= chat_identity.MIN_NAME_CONTRAST


class TestBadges:
    def test_a_badge_says_what_it_is(self):
        assert chat_identity.badge_words({"type": "moderator", "text": "Moderator"}) == "Moderator"

    def test_a_subscribers_says_for_how_long(self):
        assert chat_identity.badge_words({"type": "subscriber", "text": "Subscriber", "count": 3}) == (
            "Subscriber, 3 months"
        )
        assert chat_identity.badge_words({"type": "subscriber", "text": "Subscriber", "count": 1}) == (
            "Subscriber, 1 month"
        )

    def test_a_gifters_says_how_many(self):
        assert chat_identity.badge_words({"type": "sub_gifter", "text": "Sub Gifter", "count": 5}) == (
            "Sub Gifter, 5 gifted"
        )

    def test_another_badge_with_a_count_just_gives_it(self):
        assert chat_identity.badge_words({"type": "new_thing", "text": "New thing", "count": 2}) == "New thing, 2"

    def test_one_kick_did_not_name_is_named_for_its_type(self):
        assert chat_identity.badge_words({"type": "sub_gifter", "text": ""}) == "Sub gifter"

    def test_stored_badges_are_drawn_in_the_order_kick_listed_them(self):
        stored = json.dumps(
            [
                {"type": "moderator", "text": "Moderator"},
                {"type": "subscriber", "text": "Subscriber", "count": 7},
            ]
        )

        assert chat_identity.badge_parts(stored) == [
            {"icon": "moderator", "words": "Moderator"},
            {"icon": "subscriber", "words": "Subscriber, 7 months"},
        ]

    def test_a_type_there_is_no_picture_for_gets_the_general_one_and_keeps_its_name(self):
        stored = json.dumps([{"type": "brand_new_badge", "text": "Brand new"}])

        assert chat_identity.badge_parts(stored) == [{"icon": chat_identity.OTHER_BADGE, "words": "Brand new"}]

    def test_no_badges(self):
        assert chat_identity.badge_parts(chat_identity.NO_BADGES) == []
        assert chat_identity.badge_parts("") == []

    def test_every_badge_that_can_be_drawn_has_its_picture_and_no_picture_is_left_over(self):
        pictures = {path.stem for path in (STATIC / "badges").glob("*.svg")}

        assert pictures == chat_identity.BADGE_ICONS | {chat_identity.OTHER_BADGE}
