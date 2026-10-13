"""The Settings page and what stands behind it: saving values for all
channels and for one, switching a channel to settings of its own, and
trying what is in the form on stored chat.

What a setting may be and how values are laid over each other is covered
where it is decided (tests/unit/test_tuning.py), and what the detector does
with each of them in tests/unit/test_detector_settings.py. Here the question
is whether what is saved reaches the running service: the next chat message,
the moment that fires, the clip that is cut for it, the pages.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from kick_clip_hunter import db, detector, recorder, tuning
from tests.support.chat import StoredStream, typical_evening
from tests.support.data import T0, add_chat, add_moment
from tests.support.html import Element, parse

pytestmark = pytest.mark.anyio

CHANNEL = "channel_a"
OTHER = "channel_b"
RECOMMENDED = tuning.recommended_values()


def crowd_needed() -> int:
    """The smallest crowd that fires on the 10-chatter history `warm_up` builds."""
    return detector._dynamic_min_reaction_unique(10)


async def save(service, channel: str | None = None, **values):
    address = f"/channels/{channel}/tuning" if channel else "/tuning"
    return await service.client.post(address, json={"values": values})


async def use_own(service, channel: str, enabled: bool = True):
    return await service.client.post(f"/channels/{channel}/own_tuning?enabled={int(enabled)}")


def stored(owner: int = db.TUNING_FOR_ALL) -> dict:
    conn = db.get_connection()
    try:
        return tuning.stored(db.get_tuning(conn, owner))
    finally:
        conn.close()


def in_force(service, broadcaster_user_id: int) -> tuning.Tuning:
    conn = db.get_connection()
    try:
        return service.main._tuning(conn, broadcaster_user_id)
    finally:
        conn.close()


class TestSavingForAllChannels:
    async def test_a_value_is_stored_and_named_in_the_answer(self, service):
        response = await save(service, reacting_share=0.2, laugh_signal=False)

        assert response.status_code == 200
        assert response.json() == {"channel": None, "set": ["laugh_signal", "reacting_share"]}
        assert stored() == {"reacting_share": 0.2, "laugh_signal": False}

    async def test_what_is_recommended_anyway_is_not_stored(self, service):
        # The page posts every setting, the untouched ones too.
        response = await save(service, **{**RECOMMENDED, "reacting_share": 0.2})

        assert response.json()["set"] == ["reacting_share"]
        assert stored() == {"reacting_share": 0.2}

    async def test_a_setting_put_back_goes_back_to_following_the_code(self, service):
        await save(service, reacting_share=0.2)

        await save(service, reacting_share=RECOMMENDED["reacting_share"])

        assert stored() == {}

    async def test_a_setting_that_is_not_mentioned_stays_as_it_is(self, service):
        await save(service, reacting_share=0.2)

        await save(service, lull_seconds=40)

        assert stored() == {"reacting_share": 0.2, "lull_seconds": 40}

    async def test_lists_are_taken_as_they_are_typed(self, service):
        await save(service, laugh_emote_words="Smich HAHA2", ignored_messages="!join, GG wp")

        assert stored() == {"laugh_emote_words": ("haha2", "smich"), "ignored_messages": ("!join", "gg wp")}

    async def test_the_service_goes_by_it_at_once_and_after_a_restart(self, service):
        user_id = service.watch(CHANNEL)
        assert in_force(service, user_id) == tuning.recommended()

        await save(service, reacting_share=0.2, pre_roll_seconds=20)

        assert in_force(service, user_id).detector.min_reaction_unique_fraction == 0.2
        service.main._tuning_values = None  # what a fresh process starts with
        assert in_force(service, user_id).pre_roll_seconds == 20

    async def test_the_next_message_is_judged_by_it(self, service):
        # One more person than fires a moment as recommended is asked for.
        service.watch(CHANNEL)
        service.warm_up(CHANNEL)
        needed = crowd_needed() + 1
        await save(service, reacting_least=needed)

        await service.crowd_laughs(CHANNEL, people=needed - 1)
        assert service.moments() == []

        await service.chat(CHANNEL, "one_more", "xDDD")
        assert len(service.moments()) == 1

    async def test_a_signal_switched_off_fires_nothing(self, service):
        service.watch(CHANNEL)
        service.warm_up(CHANNEL)
        await save(service, laugh_signal=False)

        await service.crowd_laughs(CHANNEL, people=12)

        assert service.moments() == []

    @pytest.mark.parametrize(
        ("values", "about"),
        [
            ({"reacting_share": 7}, "Share of chat that has to react"),
            ({"reacting_least": 2.5}, "At least this many people"),
            ({"laugh_signal": "off"}, "Typed laughs"),
            ({"laugh_emote_words": "ab"}, "More laughing emotes"),
            ({"self_destruct": 1}, "self_destruct"),
            ({"reacting_share": None}, "Share of chat that has to react"),
        ],
    )
    async def test_a_value_that_cannot_be_taken_is_refused_in_words_and_nothing_is_stored(self, service, values, about):
        await save(service, lull_seconds=40)

        response = await save(service, pre_roll_seconds=20, **values)

        assert response.status_code == 400
        assert about in response.json()["detail"]
        assert stored() == {"lull_seconds": 40}

    async def test_values_that_contradict_each_other_are_refused(self, service):
        response = await save(service, reacting_least=20, reacting_most=10)

        assert response.status_code == 400
        assert "cannot be more than" in response.json()["detail"]
        assert stored() == {}

    async def test_so_is_one_that_contradicts_what_was_saved_before(self, service):
        await save(service, reacting_most=5)

        response = await save(service, reacting_least=9)

        assert response.status_code == 400
        assert stored() == {"reacting_most": 5}

    @pytest.mark.parametrize("body", [b"not json", b"[]", b"{}", b'{"values": [1, 2]}', b'{"values": "all"}'])
    async def test_a_request_that_is_not_values_by_name_is_refused(self, service, body):
        response = await service.client.post("/tuning", content=body, headers={"Content-Type": "application/json"})

        assert response.status_code == 400
        assert "expected the settings' values" in response.json()["detail"]

    async def test_a_value_that_would_leave_a_channels_own_settings_contradicting_is_refused(self, service):
        user_id = service.watch(CHANNEL)
        await save(service, CHANNEL, reacting_most=5)
        await use_own(service, CHANNEL)
        assert in_force(service, user_id).detector.min_reaction_unique_ceiling == 5

        response = await save(service, reacting_least=9)

        assert response.status_code == 400
        assert response.json()["detail"].endswith(f"That is what {CHANNEL}'s own settings would then come to.")
        assert stored() == {}

    async def test_a_channel_that_does_not_go_by_its_own_is_not_in_the_way(self, service):
        service.watch(CHANNEL)
        await save(service, CHANNEL, reacting_most=5)

        response = await save(service, reacting_least=9)

        assert response.status_code == 200


class TestAChannelsOwnSettings:
    async def test_they_are_kept_under_the_channel(self, service):
        user_id = service.watch(CHANNEL)

        response = await save(service, CHANNEL, reacting_share=0.3)

        assert response.json() == {"channel": CHANNEL, "set": ["reacting_share"]}
        assert stored(user_id) == {"reacting_share": 0.3}
        assert stored() == {}

    async def test_they_hold_only_once_the_channel_goes_by_them(self, service):
        user_id, other_id = service.watch(CHANNEL), service.watch(OTHER)
        await save(service, reacting_least=5)
        await save(service, CHANNEL, reacting_share=0.3)
        assert in_force(service, user_id).detector.min_reaction_unique_fraction == RECOMMENDED["reacting_share"]

        response = await use_own(service, CHANNEL)

        assert response.json() == {"slug": CHANNEL, "own_tuning": True}
        assert in_force(service, user_id).detector.min_reaction_unique_fraction == 0.3
        # What it has no value of its own for it takes from all channels...
        assert in_force(service, user_id).detector.min_absolute_unique == 5
        # ...and no other channel takes anything from it.
        assert in_force(service, other_id).detector.min_reaction_unique_fraction == RECOMMENDED["reacting_share"]

    async def test_switched_off_again_the_channel_follows_all_channels_and_keeps_its_values(self, service):
        user_id = service.watch(CHANNEL)
        await save(service, CHANNEL, reacting_share=0.3)
        await use_own(service, CHANNEL)

        response = await use_own(service, CHANNEL, enabled=False)

        assert response.json() == {"slug": CHANNEL, "own_tuning": False}
        assert in_force(service, user_id) == tuning.recommended()
        assert stored(user_id) == {"reacting_share": 0.3}

    async def test_what_all_channels_have_anyway_is_not_stored_for_the_channel(self, service):
        user_id = service.watch(CHANNEL)
        await save(service, reacting_share=0.2)

        await save(service, CHANNEL, **{**RECOMMENDED, "reacting_share": 0.2, "reacting_least": 6})

        assert stored(user_id) == {"reacting_least": 6}

    async def test_a_value_put_back_to_what_all_channels_have_follows_them_again(self, service):
        user_id = service.watch(CHANNEL)
        await save(service, CHANNEL, reacting_share=0.3, reacting_least=6)

        await save(service, CHANNEL, reacting_share=RECOMMENDED["reacting_share"])

        assert stored(user_id) == {"reacting_least": 6}

    async def test_its_values_must_not_contradict_the_ones_it_takes_from_all_channels(self, service):
        user_id = service.watch(CHANNEL)
        await save(service, reacting_most=5)

        response = await save(service, CHANNEL, reacting_least=9)

        assert response.status_code == 400
        assert stored(user_id) == {}

    async def test_the_same_reaction_is_a_moment_on_one_channel_and_not_on_the_other(self, service):
        service.watch(CHANNEL)
        service.watch(OTHER)
        service.warm_up(CHANNEL)
        service.warm_up(OTHER)
        await save(service, CHANNEL, reacting_least=crowd_needed() + 4)
        await use_own(service, CHANNEL)

        await service.crowd_laughs(CHANNEL, people=crowd_needed() + 2)
        await service.crowd_laughs(OTHER, people=crowd_needed() + 2)

        assert [moment["channel_slug"] for moment in service.moments()] == [OTHER]

    @pytest.mark.parametrize("address", ["/channels/nobody/tuning", "/channels/nobody/tuning/trial"])
    async def test_a_channel_that_is_not_on_the_watchlist_has_no_settings(self, service, address):
        response = await service.client.post(address, json={"values": {"reacting_share": 0.3}})

        assert response.status_code == 404
        assert "nobody" in response.json()["detail"]

    async def test_nor_can_it_be_switched_to_its_own(self, service):
        response = await use_own(service, "nobody")

        assert response.status_code == 404


def naming_crowd() -> int:
    """How many people typing an ordinary emote's name fire a moment on the
    history `warm_up` builds, in which nobody has typed one: with nothing to
    compare against, the floors have to be cleared the multiplier's number
    of times over (the quirk described in test_detector_firing)."""
    by_weight = detector._dynamic_min_count(10) * detector.EMOTE_MENTION_MULTIPLIER
    by_people = detector.MIN_ABSOLUTE_UNIQUE * detector.EMOTE_MENTION_UNIQUE_SENDER_MULTIPLIER
    return math.ceil(max(by_weight / detector.EMOTE_MENTION_OTHER_WEIGHT, by_people))


class TestKeywordsUnderTheSettings:
    async def test_an_emote_name_the_channel_is_told_to_ignore_stops_counting_at_once(self, service):
        # The weights of a channel's emote names are kept in memory once
        # they have been used; saving has to drop them.
        service.watch(CHANNEL, keywords={"sadge": detector.EMOTE_MENTION_OTHER_WEIGHT})
        service.warm_up(CHANNEL)
        await service.chat(CHANNEL, "viewer1", "Sadge")
        await save(service, ignored_emote_words="sadge")

        for number in range(naming_crowd()):
            await service.chat(CHANNEL, f"fan{number}", "Sadge")

        assert service.moments() == []

    async def test_without_that_the_same_crowd_is_a_moment(self, service):
        service.watch(CHANNEL, keywords={"sadge": detector.EMOTE_MENTION_OTHER_WEIGHT})
        service.warm_up(CHANNEL)
        await service.chat(CHANNEL, "viewer1", "Sadge")

        for number in range(naming_crowd()):
            await service.chat(CHANNEL, f"fan{number}", "Sadge")

        (moment,) = service.moments()
        assert moment["reason"] == "emote_mention"


class TestAMomentAndItsSettings:
    @pytest.fixture
    def framings(self, service, monkeypatch) -> list[dict]:
        """How each clip was asked to be cut, in place of cutting it."""
        asked: list[dict] = []

        async def create_clip(channel, start, end, name, **framing):
            asked.append(framing)
            raise recorder.RecorderError("nothing is being recorded in this test")

        monkeypatch.setattr(service.main.recording_manager, "create_clip_for_moment", create_clip)
        return asked

    async def test_its_clip_is_framed_the_way_the_channel_is_set_up(self, service, framings):
        service.watch(CHANNEL)
        service.warm_up(CHANNEL)
        await save(service, pre_roll_seconds=20, post_roll_seconds=3, context_after_seconds=0)
        await save(service, CHANNEL, playback_delay_seconds=4)
        await use_own(service, CHANNEL)

        await service.crowd_laughs(CHANNEL, people=crowd_needed())
        await service.settle()

        assert framings == [
            {
                "pre_roll_seconds": 20,
                "post_roll_seconds": 3,
                "playback_delay_seconds": 4,
                "context_before_seconds": recorder.CONTEXT_BEFORE_SECONDS,
                "context_after_seconds": 0,
            }
        ]

    async def test_a_moment_that_is_open_keeps_the_settings_it_fired_with(self, service, framings):
        # Long enough that the moment is certainly still open when saved over.
        service.quiet_seconds = 0.5
        user_id = service.watch(CHANNEL)
        service.warm_up(CHANNEL)
        await service.crowd_laughs(CHANNEL, people=crowd_needed())
        assert list(service.main._moment_sessions) == [CHANNEL]

        await save(service, pre_roll_seconds=20)

        assert in_force(service, user_id).pre_roll_seconds == 20
        assert service.main._moment_sessions[CHANNEL].tuned.pre_roll_seconds == recorder.PRE_ROLL_SECONDS
        await service.settle()
        assert framings[0]["pre_roll_seconds"] == recorder.PRE_ROLL_SECONDS

    async def test_how_long_a_clip_may_be_on_its_way_follows_the_settings(self, service):
        recommended = tuning.recommended()
        longer = tuning.tuned({"longest_seconds": 120, "post_roll_seconds": 40, "context_after_seconds": 100})

        pending = service.main._clip_pending_seconds
        added = (120 - recommended.max_open_seconds) + (40 - recommended.post_roll_seconds)
        added += 100 - recommended.context_after_seconds

        assert pending(recommended) > recommended.max_open_seconds + recommended.context_after_seconds
        assert pending(longer) == pending(recommended) + added


class TestTryingOnStoredChat:
    @pytest.fixture
    def evening(self, service) -> int:
        """A watched channel with a stream's chat stored: two moments' worth
        as recommended (see `typical_evening`)."""
        user_id = service.watch(CHANNEL)
        conn = db.get_connection()
        try:
            start = datetime(2026, 3, 1, 18, 0, tzinfo=timezone.utc).timestamp()
            typical_evening(StoredStream(channel=CHANNEL, now=start, conn=conn, broadcaster_user_id=user_id))
        finally:
            conn.close()
        return user_id

    async def trial(self, service, channel: str | None = None, chat: str = CHANNEL, **values):
        address = f"/channels/{channel}/tuning/trial" if channel else f"/tuning/trial?chat={chat}"
        return await service.client.post(address, json={"values": values})

    def moments(self, response) -> list[tuple[str, str]]:
        return [(row["label"], row["moments"]) for row in response.json()["rows"]]

    async def test_says_what_the_settings_as_saved_and_as_typed_would_each_have_made(self, service, evening):
        response = await self.trial(service, reacting_least=50, reacting_most=50)

        assert response.status_code == 200
        assert self.moments(response) == [("As saved", "2"), ("With these values", "0")]
        saved, typed = response.json()["rows"]
        assert set(saved) == {"label", "moments", "per_hour", "not_extended", "median", "longest"}
        assert (saved["not_extended"], typed["median"], typed["longest"]) == ("1", "-", "-")
        assert saved["median"].endswith(" s") and float(saved["per_hour"]) > 0

    async def test_says_how_much_chat_it_was_tried_on(self, service, evening):
        response = await self.trial(service)

        summary = response.json()["summary"]
        assert summary.startswith(f"{len(service.chat_log())} messages over ")
        assert " hours of chat, from " in summary

    async def test_nothing_typed_is_the_same_twice(self, service, evening):
        response = await self.trial(service)

        assert self.moments(response) == [("As saved", "2"), ("With these values", "2")]

    async def test_nothing_is_saved_by_it(self, service, evening):
        await self.trial(service, reacting_least=50, reacting_most=50)

        assert stored() == {} and stored(evening) == {}
        assert in_force(service, evening) == tuning.recommended()

    async def test_as_saved_is_what_was_saved(self, service, evening):
        await save(service, laugh_signal=False)

        response = await self.trial(service, laugh_signal=True)

        assert self.moments(response) == [("As saved", "0"), ("With these values", "2")]

    async def test_a_channels_own_settings_are_tried_on_its_own_chat(self, service, evening):
        response = await self.trial(service, channel=CHANNEL, laugh_signal=False)

        assert self.moments(response) == [("As saved", "2"), ("With these values", "0")]
        assert stored(evening) == {}

    async def test_for_all_channels_it_is_tried_on_top_of_what_the_channel_has_of_its_own(self, service, evening):
        await save(service, CHANNEL, laugh_signal=False)
        await use_own(service, CHANNEL)

        response = await self.trial(service, reacting_share=0.05)

        assert self.moments(response) == [("As saved", "0"), ("With these values", "0")]

    async def test_only_the_newest_chat_is_replayed(self, service, evening, monkeypatch):
        monkeypatch.setattr(service.main, "TRIAL_MESSAGE_LIMIT", 100)

        response = await self.trial(service)

        assert response.json()["summary"].startswith("100 messages over ")

    async def test_values_that_cannot_be_taken_are_refused_like_when_saving(self, service, evening):
        out_of_range = await self.trial(service, reacting_share=7)
        contradicting = await self.trial(service, reacting_least=20, reacting_most=10)

        assert (out_of_range.status_code, contradicting.status_code) == (400, 400)
        assert "Share of chat that has to react" in out_of_range.json()["detail"]

    async def test_a_channel_without_stored_chat_says_so(self, service):
        service.watch(OTHER)

        response = await self.trial(service, chat=OTHER)

        assert response.status_code == 409
        assert response.json()["detail"] == f"No chat is stored for {OTHER} yet."

    async def test_a_channel_that_is_not_watched_cannot_be_tried_on(self, service, evening):
        response = await self.trial(service, chat="nobody")

        assert response.status_code == 404

    async def test_whose_chat_has_to_be_said(self, service, evening):
        response = await service.client.post("/tuning/trial", json={"values": {}})

        assert response.status_code == 422

    async def test_one_trial_at_a_time(self, service, evening, monkeypatch):
        monkeypatch.setattr(service.main, "_trial_running", True)

        response = await self.trial(service)

        assert response.status_code == 409
        assert "still running" in response.json()["detail"]

    async def test_the_next_one_can_run_once_it_is_over_however_it_ended(self, service, evening):
        await self.trial(service)
        assert service.main._trial_running is False

        await self.trial(service, chat=OTHER)  # not watched: refused before anything runs
        assert (await self.trial(service)).status_code == 200


# -- the page -----------------------------------------------------------------


async def settings_page(service, channel: str | None = None) -> Element:
    response = await service.client.get("/dashboard/settings", params={"channel": channel} if channel else {})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    return parse(response.text)


def field(page: Element, name: str) -> Element:
    """The control a setting is set with: an input, or a switch's two halves."""
    return page.one(data_setting=name)


def shown(page: Element, name: str) -> str:
    """What the page has a setting at."""
    control = field(page, name)
    if control.tag == "input":
        return control.attrs["value"]
    (lit,) = [half.text for half in control.find("button") if half.attrs["aria-pressed"] == "true"]
    return lit


def row_of(page: Element, name: str) -> Element:
    (row,) = [row for row in page.find("div", class_="set-row") if row.find(data_setting=name)]
    return row


def rest_button(page: Element, name: str) -> Element:
    return row_of(page, name).one("button", class_="set-rest")


class TestTheSettingsPage:
    async def test_it_is_one_of_the_pages_in_the_bar(self, service):
        page = await settings_page(service)

        links = page.one("nav", class_="bar-nav").find("a")
        assert [link.text for link in links] == ["Review", "Recordings", "Channels", "Settings"]
        assert [link.text for link in links if link.attrs.get("aria-current") == "page"] == ["Settings"]

    async def test_with_nothing_saved_every_setting_is_at_what_is_recommended(self, service):
        page = await settings_page(service)

        assert page.one("h2", id="whose-title").text == "Settings for all channels"
        assert [control.attrs["data-setting"] for control in page.find(data_setting=True)] == [
            item.name
            for section in tuning.form(RECOMMENDED, RECOMMENDED, "Recommended")
            for item in [tuning.BY_NAME[field["name"]] for field in [*section["fields"], *section["advanced"]]]
        ]
        for item in tuning.SETTINGS:
            assert shown(page, item.name) == tuning.words(item, RECOMMENDED[item.name]), item.name
            assert "is-set" not in row_of(page, item.name).classes
            assert "disabled" in rest_button(page, item.name).attrs

    async def test_every_setting_says_what_it_does_and_what_is_recommended(self, service):
        page = await settings_page(service)

        row = row_of(page, "reacting_share")
        item = tuning.BY_NAME["reacting_share"]
        assert row.one("label").text == item.label
        assert row.one("label").attrs["for"] == field(page, "reacting_share").attrs["id"]
        assert row.one("p", class_="ch-cap").text == item.caption
        assert rest_button(page, "reacting_share").text == f"Recommended: {RECOMMENDED['reacting_share']:g}"

    async def test_a_number_is_typed_into_a_field_that_says_what_it_takes(self, service):
        page = await settings_page(service)

        whole, decimal = field(page, "lull_seconds"), field(page, "reacting_share")
        item = tuning.BY_NAME["lull_seconds"]
        # A text field: a number field would be written the browser's way,
        # with a comma where the rest of the page has a point.
        assert (whole.attrs["type"], whole.attrs["inputmode"], whole.attrs["data-kind"]) == ("text", "numeric", "whole")
        assert whole.attrs["title"] == f"A whole number from {item.least:g} to {item.most:g}"
        assert (decimal.attrs["inputmode"], decimal.attrs["data-kind"]) == ("decimal", "number")
        assert decimal.attrs["title"] == "A number from 0.01 to 1"
        assert row_of(page, "lull_seconds").one("span", class_="ch-cap").text == "seconds"
        assert row_of(page, "reacting_share").one("span", class_="ch-cap").text == ""

    async def test_a_saved_value_is_shown_marked_and_can_be_put_back(self, service):
        await save(service, reacting_share=0.2, mention_signal=False, ignored_messages="!join, gg wp")

        page = await settings_page(service)

        assert shown(page, "reacting_share") == "0.2"
        assert "is-set" in row_of(page, "reacting_share").classes
        button = rest_button(page, "reacting_share")
        assert "disabled" not in button.attrs
        assert button.attrs["data-rest"] == f"{RECOMMENDED['reacting_share']:g}"
        assert shown(page, "mention_signal") == "Off"
        assert (rest_button(page, "mention_signal").text, rest_button(page, "mention_signal").attrs["data-rest"]) == (
            "Recommended: On",
            "1",
        )
        assert shown(page, "ignored_messages") == "!join, gg wp"
        assert rest_button(page, "ignored_messages").text == "Recommended: none"
        # ...and what was left alone still is.
        assert "is-set" not in row_of(page, "reacting_least").classes

    async def test_the_settings_few_will_touch_are_folded_away_until_one_is_set(self, service):
        folded = (await settings_page(service)).find("details", class_="set-more")
        await save(service, sustain_fraction=0.5)
        unfolded = (await settings_page(service)).find("details", class_="set-more")

        assert folded and all("open" not in details.attrs for details in folded)
        assert ["open" in details.attrs for details in unfolded] == [True, False]
        assert unfolded[0].find(data_setting="sustain_fraction")

    async def test_the_form_is_saved_to_and_tried_at_the_settings_for_all_channels(self, service):
        form = (await settings_page(service)).one("form", data_tuning=True)

        assert (form.attrs["data-tuning"], form.attrs["data-trial"]) == ("/tuning", "/tuning/trial")
        assert "disabled" not in form.one("fieldset").attrs
        assert form.one("button", type="submit").text == "Save"
        assert form.one("button", data_rest_all=True).text == "Put back what is recommended"

    async def test_whose_settings_are_shown_can_be_chosen_among_the_watchlist(self, service):
        service.watch(CHANNEL)
        service.watch(OTHER)
        await use_own(service, OTHER)

        picker = (await settings_page(service, CHANNEL)).one("select", name="channel")

        assert [(option.attrs["value"], option.text) for option in picker.find("option")] == [
            ("", "All channels"),
            (CHANNEL, CHANNEL),
            (OTHER, f"{OTHER} - has its own"),
        ]
        assert [option.attrs["value"] for option in picker.find("option", selected=True)] == [CHANNEL]
        assert "data-submit-on-change" in picker.attrs

    async def test_the_page_for_all_channels_names_the_ones_with_settings_of_their_own(self, service):
        service.watch(CHANNEL)
        service.watch(OTHER)
        nobody_yet = (await settings_page(service)).one("p", class_="set-whose").text
        await use_own(service, OTHER)

        page = await settings_page(service)

        assert "settings of" not in nobody_yet
        assert page.one("p", class_="set-whose").text.endswith(f"{OTHER} has settings of its own on top of these.")

    async def test_a_channel_that_goes_by_all_channels_settings_is_shown_those_and_cannot_change_them(self, service):
        service.watch(CHANNEL)
        await save(service, reacting_share=0.2)
        await save(service, CHANNEL, reacting_share=0.4)  # kept, but not gone by

        page = await settings_page(service, CHANNEL)

        assert page.one("h2", id="whose-title").text == f"Settings of {CHANNEL}"
        assert "goes by the settings for all channels" in page.one("p", class_="set-whose").text
        assert "disabled" in page.one("form", data_tuning=True).one("fieldset").attrs
        assert shown(page, "reacting_share") == "0.2"
        assert "is-set" not in row_of(page, "reacting_share").classes
        assert page.find("button", data_trial_go=True) == []

    async def test_its_switch_turns_its_own_settings_on(self, service):
        service.watch(CHANNEL)

        switch = (await settings_page(service, CHANNEL)).one("span", aria_label=f"Own settings for {CHANNEL}")

        assert switch.attrs["data-switch"] == f"/channels/{CHANNEL}/own_tuning"
        assert "data-switch-reloads" in switch.attrs
        assert [half.text for half in switch.find("button") if half.attrs["aria-pressed"] == "true"] == ["Off"]

    async def test_a_channel_that_goes_by_its_own_is_shown_them_against_all_channels(self, service):
        service.watch(CHANNEL)
        await save(service, reacting_share=0.2)
        await save(service, CHANNEL, reacting_least=6)
        await use_own(service, CHANNEL)

        page = await settings_page(service, CHANNEL)

        form = page.one("form", data_tuning=True)
        assert (form.attrs["data-tuning"], form.attrs["data-trial"]) == (
            f"/channels/{CHANNEL}/tuning",
            f"/channels/{CHANNEL}/tuning/trial",
        )
        assert "disabled" not in form.one("fieldset").attrs
        assert form.one("button", data_rest_all=True).text == "Put back what all channels have"
        # Its own value, marked; and one it takes from all channels, at rest.
        assert (shown(page, "reacting_least"), "is-set" in row_of(page, "reacting_least").classes) == ("6", True)
        assert rest_button(page, "reacting_least").text == f"All channels: {RECOMMENDED['reacting_least']}"
        assert (shown(page, "reacting_share"), "is-set" in row_of(page, "reacting_share").classes) == ("0.2", False)
        assert rest_button(page, "reacting_share").text == "All channels: 0.2"
        assert f"{CHANNEL} goes by the settings below" in page.one("p", class_="set-whose").text

    async def test_a_channel_that_is_not_on_the_watchlist_has_no_page(self, service):
        response = await service.client.get("/dashboard/settings", params={"channel": "nobody"})

        assert response.status_code == 404

    async def test_what_is_in_the_form_can_be_tried_on_a_watched_channels_chat(self, service):
        nothing_to_try_on = await settings_page(service)
        service.watch(CHANNEL)
        service.watch(OTHER)

        page = await settings_page(service)

        assert nothing_to_try_on.find("button", data_trial_go=True) == []
        assert [option.text for option in page.one("select", data_trial_chat=True).find("option")] == [CHANNEL, OTHER]
        assert page.one("button", data_trial_go=True).text == "Try on stored chat"
        result = page.one("table", data_trial_result=True)
        assert "hidden" in result.attrs
        assert [heading.text for heading in result.one("thead").find("th")] == [
            "Settings", "Moments", "An hour", "Not extended", "Median clip", "Longest clip",
        ]  # fmt: skip

    async def test_a_channels_own_are_tried_on_its_own_chat_without_asking_whose(self, service):
        service.watch(CHANNEL)
        await use_own(service, CHANNEL)

        page = await settings_page(service, CHANNEL)

        assert page.find("select", data_trial_chat=True) == []
        assert len(page.find("button", data_trial_go=True)) == 1


class TestOtherPagesUnderTheSettings:
    async def moment_article(self, service) -> Element:
        response = await service.client.get("/dashboard")
        return parse(response.text).one("article", class_="moment")

    async def test_an_emote_the_channel_added_is_drawn_as_laughing(self, service):
        # A 40 second clip chat saw from T0-25; two messages in its second 17.
        service.watch(CHANNEL, 77)
        add_moment(
            CHANNEL,
            detected_at=T0,
            broadcaster_user_id=77,
            clip_path=f"{CHANNEL}/moment_1.mp4",
            clip_start=(T0 - timedelta(seconds=25)).isoformat(),
            clip_duration=40.0,
        )
        add_chat(CHANNEL, "alice", "[emote:1:pepeSmich]", T0 - timedelta(seconds=8))
        add_chat(CHANNEL, "bob", "[emote:2:asmonSmash]", T0 - timedelta(seconds=8))

        def laughing(article: Element) -> int:
            strip = article.one("figure", class_="trace").one("div", class_="ch-strip")
            return sum(map(int, strip.attrs["data-laugh"].split(",")))

        before = laughing(await self.moment_article(service))
        await save(service, laugh_emote_words="smich")
        after = laughing(await self.moment_article(service))

        assert (before, after) == (0, 1)

    async def test_a_clip_is_waited_for_as_long_as_the_channels_settings_can_take(self, service):
        user_id = service.watch(CHANNEL)
        waited = service.main._clip_pending_seconds(in_force(service, user_id)) + 60
        add_moment(
            CHANNEL, detected_at=datetime.now(timezone.utc) - timedelta(seconds=waited), broadcaster_user_id=user_id
        )
        given_up = (await self.moment_article(service)).one("p", class_="no-clip").text

        await save(service, CHANNEL, context_after_seconds=recorder.CONTEXT_AFTER_SECONDS + 60, longest_seconds=110)
        await use_own(service, CHANNEL)
        still_waiting = (await self.moment_article(service)).one("p", class_="no-clip").text

        assert given_up == "No clip was saved for this moment."
        assert still_waiting.startswith("The clip is still being cut.")
