"""A Twitch channel's emotes beyond Twitch's own: 7TV's, BetterTTV's and
FrankerFaceZ's, fetched from each service and kept together - with the
service before each id, so that the dashboard knows where its picture is."""

from __future__ import annotations

import httpx
import pytest

from kick_clip_hunter import dashboard_view, db, detector, twitch_emotes, watchlist
from kick_clip_hunter.seventv_client import Emote
from tests.support.kick_api import seventv_emote_id
from tests.support.twitch_api import bttv_emote_id, ffz_emote_id

pytestmark = pytest.mark.anyio


class TestBetterTtv:
    async def test_a_channels_own_and_shared_emotes(self, twitch_api):
        twitch_api.add_channel("some_channel", "101", bttv_emotes=["OMEGALUL", "monkaS"])

        emotes = await twitch_emotes.get_bttv_emotes("101")

        assert emotes == [
            Emote("OMEGALUL", f"bttv.{bttv_emote_id('OMEGALUL')}"),
            Emote("monkaS", f"bttv.{bttv_emote_id('monkaS')}"),
        ]

    async def test_a_channel_without_an_account_there_has_none(self, twitch_api):
        twitch_api.add_channel("some_channel", "101", bttv_emotes=None)

        assert await twitch_emotes.get_bttv_emotes("101") == []

    async def test_a_server_error_is_not_mistaken_for_no_emotes(self, twitch_api):
        twitch_api.failures["api.betterttv.net"] = 503

        with pytest.raises(httpx.HTTPStatusError):
            await twitch_emotes.get_bttv_emotes("101")


class TestFrankerFaceZ:
    async def test_the_emotes_of_a_channels_room_with_their_shape(self, twitch_api):
        twitch_api.add_channel("some_channel", "101", ffz_emotes=["KEKW"])

        emotes = await twitch_emotes.get_ffz_emotes("101")

        assert emotes == [Emote("KEKW", f"ffz.{ffz_emote_id('KEKW')}", 64, 32)]

    async def test_a_channel_without_a_room_there_has_none(self, twitch_api):
        twitch_api.add_channel("some_channel", "101", ffz_emotes=None)

        assert await twitch_emotes.get_ffz_emotes("101") == []


class TestWhatIsKept:
    @pytest.mark.parametrize(
        "entry",
        [
            {"id": "../x", "code": "KEKW"},
            {"id": "abc", "code": ""},
            {"id": "abc"},
            "not an emote",
        ],
    )
    def test_an_entry_that_cannot_be_drawn_safely_is_left_out(self, entry):
        assert twitch_emotes._emote("bttv", entry, "code") is None

    def test_a_shape_that_is_not_one_is_unknown(self):
        assert twitch_emotes._emote("ffz", {"id": 1, "name": "x", "width": -3, "height": 32}, "name") == Emote(
            "x", "ffz.1", 0, 0
        )


class TestAllOfAChannelsEmotes:
    async def test_the_three_sets_are_kept_together_and_7tv_wins_a_name(self, twitch_api):
        twitch_api.add_channel(
            "some_channel",
            "101",
            seventv_emotes=["KEKW"],
            bttv_emotes=["KEKW", "OMEGALUL"],
            ffz_emotes=["OMEGALUL", "LULW"],
        )

        result = await watchlist.add_twitch_channel("some_channel")

        conn = db.get_connection()
        try:
            pictures = db.get_channel_emotes(conn, result["broadcaster_user_id"])
            keywords = db.get_channel_keywords(conn, result["broadcaster_user_id"])
        finally:
            conn.close()
        assert {name: emote_id for name, (emote_id, _, _) in pictures.items()} == {
            "KEKW": seventv_emote_id("KEKW"),
            "OMEGALUL": f"bttv.{bttv_emote_id('OMEGALUL')}",
            "LULW": f"ffz.{ffz_emote_id('LULW')}",
        }
        assert set(keywords) == {"kekw", "omegalul", "lulw"}
        assert set(keywords.values()) == {detector.EMOTE_MENTION_LAUGH_WEIGHT}

    async def test_startup_refreshes_all_three(self, twitch_api):
        twitch_api.add_channel("some_channel", "101", bttv_emotes=["OMEGALUL"])

        count = await watchlist.refresh_emote_pictures(-1, "twitch", "101")

        assert count == 1


class TestDrawingThem:
    def test_each_is_drawn_from_its_own_service(self):
        emotes = {
            "KEKW": ("01ABC", 32, 32),
            "OMEGALUL": ("bttv.5f1b0186cf6d2144653d2970", 0, 0),
            "LULW": ("ffz.139407", 64, 32),
        }

        parts = dashboard_view.chat_parts("KEKW OMEGALUL LULW", emotes)

        assert [part["image"] for part in parts if "emote" in part] == [
            "https://cdn.7tv.app/emote/01ABC/2x.webp",
            "https://cdn.betterttv.net/emote/5f1b0186cf6d2144653d2970/2x",
            "https://cdn.frankerfacez.com/emote/139407/2",
        ]
        assert [part["width"] for part in parts if "emote" in part] == [20, 20, 40]

    def test_an_emote_of_a_service_there_is_no_address_for_stays_a_word(self):
        assert dashboard_view.chat_parts("KEKW", {"KEKW": ("elsewhere.1", 32, 32)}) == [{"text": "KEKW"}]

    @pytest.mark.parametrize(
        ("picture", "source"),
        [
            ("https://cdn.betterttv.net/emote/5f1b/2x", "BTTV"),
            ("https://cdn.frankerfacez.com/emote/1/2", "FFZ"),
            ("https://cdn.7tv.app/emote/01ABC/2x.webp", "7TV"),
        ],
    )
    def test_whose_it_is(self, picture, source):
        assert dashboard_view.emote_source(picture) == source
