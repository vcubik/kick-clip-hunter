"""The detector under a channel's own settings.

Everything in the other detector tests runs on what is recommended - the
constants. Here a channel is given something else for one setting at a time
(see `detector.Settings`), and the question is whether the detector does
what that setting says for that channel, and only for it.
"""

from __future__ import annotations

import dataclasses

import pytest

from kick_clip_hunter import detector
from tests.support.chat import ChatSim, native_emote, people

LAUGH = "xDDD"
LAUGH_EMOTE = native_emote("KEKW")
OTHER_EMOTE = native_emote("asmonSmash")
OWN_EMOTE = native_emote("pepeSmich")


def settings(**changed) -> detector.Settings:
    return dataclasses.replace(detector.recommended(), **changed)


def warm_chat(chatters: int = 10, *, interval: float = 1.0, **kwargs) -> ChatSim:
    """A channel with five minutes of ordinary conversation behind it, the
    odd laugh and emote from one person included - as in test_detector_firing."""
    chat = ChatSim(**kwargs)
    half = detector.BASELINE_WINDOW_SECONDS / 2
    chat.chatter(half, chatters=chatters, interval=interval)
    chat.say("viewer1", "xd")
    chat.say("viewer2", LAUGH_EMOTE)
    chat.say("viewer3", OTHER_EMOTE)
    for keyword in chat.keywords:
        chat.say("viewer4", keyword)
    chat.chatter(half, chatters=chatters, interval=interval)
    return chat


class TestRecommended:
    def test_it_is_the_constants_field_for_field(self):
        recommended = detector.recommended()

        for field in dataclasses.fields(detector.Settings):
            constant = getattr(detector, field.name.upper(), None)
            if constant is not None:
                assert getattr(recommended, field.name) == constant, field.name

    def test_every_signal_is_listened_for_and_nothing_is_added(self):
        recommended = detector.recommended()

        assert (recommended.laugh_enabled, recommended.emotes_enabled, recommended.emote_mention_enabled) == (
            True,
            True,
            True,
        )
        assert recommended.added_laugh_emote_patterns == ()
        assert recommended.added_ignored_emote_patterns == ()
        assert recommended.added_ignored_messages == frozenset()

    def test_a_channel_given_no_settings_runs_on_them(self):
        plain = warm_chat(channel="plain")
        explicit = warm_chat(channel="explicit", settings=detector.recommended())

        assert plain.reactions_needed("xd") == explicit.reactions_needed("xd")

    def test_the_constants_are_read_when_it_is_asked_for(self, monkeypatch):
        monkeypatch.setattr(detector, "COOLDOWN_SECONDS", detector.COOLDOWN_SECONDS + 100)

        assert detector.recommended().cooldown_seconds == detector.COOLDOWN_SECONDS


class TestHowBigAReactionHasToBe:
    def test_a_bigger_share_of_chat_takes_a_bigger_crowd(self):
        # With the ceiling out of the way, so that the share is what holds.
        share = detector.MIN_REACTION_UNIQUE_FRACTION * 2
        mine = settings(min_reaction_unique_fraction=share, min_reaction_unique_ceiling=1000)
        usual = warm_chat(chatters=100, interval=0.5, channel="usual")
        strict = warm_chat(chatters=100, interval=0.5, channel="strict", settings=mine)

        needed_usual, needed_strict = usual.reactions_needed("xd"), strict.reactions_needed("xd")

        assert needed_strict == round(100 * share)
        assert needed_usual is not None and needed_usual < needed_strict

    def test_the_floor_under_it_is_the_channels_own(self):
        chat = warm_chat(settings=settings(min_absolute_unique=detector.MIN_ABSOLUTE_UNIQUE + 2))

        assert chat.reactions_needed("xd") == detector.MIN_ABSOLUTE_UNIQUE + 2

    def test_so_is_the_ceiling_over_it(self):
        # Half the usual ceiling: a busy chat that would need more is let off
        # at that.
        ceiling = detector.MIN_REACTION_UNIQUE_CEILING // 2
        capped = settings(min_reaction_unique_ceiling=ceiling, min_reaction_unique_fraction=0.5)

        assert detector._dynamic_min_reaction_unique(100_000, capped) == ceiling
        assert detector._dynamic_min_reaction_unique(100_000) == detector.MIN_REACTION_UNIQUE_CEILING

    def test_the_smallest_burst_of_emotes_is_the_channels_own_too(self):
        mine = settings(min_absolute_count_floor=2, min_absolute_count_fraction=0.5, min_absolute_count_ceiling=9)

        assert detector._dynamic_min_count(0, mine) == 2
        assert detector._dynamic_min_count(10, mine) == 5
        assert detector._dynamic_min_count(1000, mine) == 9

    def test_a_higher_multiplier_takes_more_people(self):
        # A baseline with laughing in it, so that the ratio is what holds.
        def laughing_chat(channel: str, **changed) -> ChatSim:
            chat = ChatSim(channel=channel, settings=settings(**changed) if changed else None)
            for _ in range(10):
                chat.chatter(detector.BASELINE_WINDOW_SECONDS / 10)
                chat.burst(people(2, prefix="regular"), "xd", spacing=4.0)
            chat.wait(detector.SHORT_WINDOW_SECONDS + 1)  # the regulars' last laughs are history too
            return chat

        usual = laughing_chat("usual").reactions_needed("xd")
        demanding = laughing_chat(
            "demanding", laugh_multiplier=detector.LAUGH_MULTIPLIER * 4, laugh_unique_sender_multiplier=1.0
        ).reactions_needed("xd")

        assert usual is not None and demanding is not None
        assert demanding > usual

    def test_one_channels_settings_are_not_anothers(self):
        strict = warm_chat(channel="strict", settings=settings(min_absolute_unique=9))
        usual = warm_chat(channel="usual")

        assert strict.burst(people(8), LAUGH) is None
        assert usual.burst(people(8), LAUGH) is not None


class TestTheCooldown:
    def test_a_channel_with_a_shorter_one_fires_again_sooner(self):
        # Shorter, but still longer than the window the first reaction is in.
        short = detector.SHORT_WINDOW_SECONDS + 5
        assert short < detector.COOLDOWN_SECONDS
        chat = warm_chat(settings=settings(cooldown_seconds=short))
        assert chat.burst(people(8), LAUGH) is not None

        chat.chatter(short + 2)

        assert chat.burst(people(8, prefix="latecomer"), LAUGH) is not None

    def test_one_no_longer_than_the_window_fires_again_on_the_same_laughs(self):
        """Why the Settings page does not offer a lull that short: the
        people who laughed right after the moment fired are still in the
        window when the cooldown runs out, and any message then fires a
        second moment on them."""
        chat = warm_chat(settings=settings(cooldown_seconds=detector.SHORT_WINDOW_SECONDS))
        assert chat.burst(people(8), LAUGH) is not None

        chat.chatter(detector.SHORT_WINDOW_SECONDS + 1)  # nobody laughs

        assert len(chat.moments) == 2

    def test_one_with_a_longer_one_does_not(self):
        chat = warm_chat(settings=settings(cooldown_seconds=detector.COOLDOWN_SECONDS * 3))
        assert chat.burst(people(8), LAUGH) is not None

        chat.chatter(detector.COOLDOWN_SECONDS + 5)

        assert chat.burst(people(8, prefix="latecomer"), LAUGH) is None


class TestSignalsSwitchedOff:
    def test_typed_laughs_switched_off_never_fire(self):
        chat = warm_chat(settings=settings(laugh_enabled=False))

        assert chat.burst(people(30), LAUGH) is None

    def test_the_other_signals_still_do(self):
        chat = warm_chat(settings=settings(laugh_enabled=False))

        spike = chat.burst(people(8), LAUGH_EMOTE)

        assert spike is not None and spike.reasons == ["emotes"]

    def test_kick_emotes_switched_off_never_fire(self):
        chat = warm_chat(settings=settings(emotes_enabled=False))

        assert chat.burst(people(30), LAUGH_EMOTE) is None

    def test_emote_names_switched_off_never_fire(self):
        keywords = detector.classify_emote_names(["KEKW"])
        listening = warm_chat(channel="listening", keywords=keywords)
        deaf = warm_chat(channel="deaf", keywords=keywords, settings=settings(emote_mention_enabled=False))

        assert listening.burst(people(8), "KEKW") is not None
        assert deaf.burst(people(30), "KEKW") is None

    def test_a_message_that_is_two_signals_fires_on_the_one_that_is_on(self):
        keywords = detector.classify_emote_names(["KEKW"])
        chat = warm_chat(keywords=keywords, settings=settings(laugh_enabled=False))

        spike = chat.burst(people(8), "xDDD KEKW")

        assert spike is not None and spike.reasons == ["emote_mention"]

    def laughing_after_an_emote_moment(self, **changed) -> bool:
        """A moment fired by emotes, and a crowd that piles in afterwards
        only laughing: is the reaction still going?"""
        chat = warm_chat(settings=settings(**changed))
        assert chat.burst(people(8), LAUGH_EMOTE) is not None
        fired_at = chat.now
        chat.wait(2)
        chat.burst(people(8, prefix="latecomer"), LAUGH)
        return chat.reaction_active(since=fired_at)

    def test_a_signal_that_is_off_does_not_hold_a_moment_open_either(self):
        assert self.laughing_after_an_emote_moment(laugh_enabled=False) is False

    def test_the_same_crowd_does_where_it_is_on(self):
        assert self.laughing_after_an_emote_moment() is True


class TestWeights:
    def test_the_laughs_weigh_what_the_channel_says(self):
        mine = settings(laugh_strong_weight=9.0, laugh_weak_weight=0.5)

        assert detector.classify_message("xDDDD", settings=mine)[0] == 9.0
        assert detector.classify_message("xd", settings=mine)[0] == 0.5

    def test_so_do_kick_emotes(self):
        mine = settings(emote_laugh_weight=8.0, emote_other_weight=0.25)

        assert detector.classify_native_emotes(LAUGH_EMOTE, mine) == 8.0
        assert detector.classify_native_emotes(OTHER_EMOTE, mine) == 0.25
        assert detector.classify_native_emotes(native_emote("catJAM"), mine) == 0.0

    def test_with_other_emotes_at_nothing_only_laughing_ones_fire(self):
        mine = settings(emote_other_weight=0.0)

        assert warm_chat(channel="unrelated", settings=mine).burst(people(45), OTHER_EMOTE) is None
        assert warm_chat(channel="laughing", settings=mine).burst(people(8), LAUGH_EMOTE) is not None


class TestEmotesOfTheChannelsOwn:
    def test_an_added_pattern_makes_an_emote_a_laughing_one(self):
        mine = settings(added_laugh_emote_patterns=("smich",))

        assert detector.classify_native_emotes(OWN_EMOTE) == detector.EMOTE_OTHER_WEIGHT
        assert detector.classify_native_emotes(OWN_EMOTE, mine) == detector.EMOTE_LAUGH_WEIGHT

    def test_fewer_people_are_then_enough_as_with_any_laughing_emote(self):
        needed_before = warm_chat(channel="before").reactions_needed(OWN_EMOTE)
        needed_after = warm_chat(
            channel="after", settings=settings(added_laugh_emote_patterns=("smich",))
        ).reactions_needed(OWN_EMOTE)

        assert needed_before is not None and needed_after is not None
        assert needed_after < needed_before

    def test_the_patterns_built_in_still_hold(self):
        mine = settings(added_laugh_emote_patterns=("smich",))

        assert detector.classify_native_emotes(LAUGH_EMOTE, mine) == detector.EMOTE_LAUGH_WEIGHT
        assert detector.is_laugh_emote_name("KEKW", ("smich",))
        assert not detector.is_laugh_emote_name("Sadge", ("smich",))

    def test_an_emote_the_channel_was_told_to_ignore_is_no_reaction(self):
        mine = settings(added_ignored_emote_patterns=("smash",))
        chat = warm_chat(settings=mine)

        assert detector.classify_native_emotes(OTHER_EMOTE, mine) == 0.0
        assert chat.burst(people(45), OTHER_EMOTE) is None

    def test_being_told_to_ignore_it_comes_before_what_its_name_says(self):
        # "KEKWait" spammed for no reason: it is a laughing emote by name.
        mine = settings(added_ignored_emote_patterns=("kekwait",))

        assert detector.classify_native_emotes(native_emote("KEKWait"), mine) == 0.0
        assert detector.classify_native_emotes(native_emote("KEKW"), mine) == detector.EMOTE_LAUGH_WEIGHT

    def test_among_several_emotes_the_others_still_count(self):
        mine = settings(added_ignored_emote_patterns=("smash",))

        assert detector.classify_native_emotes(f"{OTHER_EMOTE} {LAUGH_EMOTE}", mine) == detector.EMOTE_LAUGH_WEIGHT


class TestKeywordWeights:
    """What a channel's stored 7TV emote names weigh under its settings."""

    STORED = detector.classify_emote_names(["KEKW", "Sadge", "pepeSmich"])

    def test_as_recommended_they_weigh_what_they_were_stored_with(self):
        assert detector.keyword_weights(self.STORED) == self.STORED

    def test_the_channels_weights_are_used(self):
        mine = settings(emote_mention_laugh_weight=7.0, emote_mention_other_weight=0.5)

        assert detector.keyword_weights(self.STORED, mine) == {"kekw": 7.0, "sadge": 0.5, "pepesmich": 0.5}

    def test_an_added_pattern_makes_a_stored_name_a_laughing_one(self):
        mine = settings(added_laugh_emote_patterns=("smich",))

        weights = detector.keyword_weights(self.STORED, mine)

        assert weights["pepesmich"] == detector.EMOTE_MENTION_LAUGH_WEIGHT
        assert weights["sadge"] == detector.EMOTE_MENTION_OTHER_WEIGHT

    def test_a_name_the_channel_was_told_to_ignore_is_not_listened_for(self):
        mine = settings(added_ignored_emote_patterns=("sadge",))

        assert set(detector.keyword_weights(self.STORED, mine)) == {"kekw", "pepesmich"}

    def test_which_names_are_listened_for_at_all_is_not_decided_again(self):
        # A name stored before dance emotes were left out is still stored,
        # and still listened for: that changes when the channel's emotes are
        # fetched again, not behind a settings page.
        stored = {"catjam": detector.EMOTE_MENTION_OTHER_WEIGHT, "kekw": detector.EMOTE_MENTION_LAUGH_WEIGHT}

        assert detector.keyword_weights(stored) == stored

    def test_a_crowd_typing_a_name_the_channel_ignores_is_not_a_moment(self):
        keywords = detector.classify_emote_names(["KEKW", "Sadge"])
        chat = warm_chat(keywords=keywords, settings=settings(added_ignored_emote_patterns=("kekw",)))

        assert chat.burst(people(30), "KEKW") is None


class TestMessagesToIgnore:
    IGNORING = frozenset({"!join", "gg wp"})

    @pytest.mark.parametrize("content", ["!join", "!JOIN", "  !join  ", "GG wp", "1", " 2 "])
    def test_a_whole_message_that_is_on_the_list_or_a_vote(self, content):
        assert detector.is_ignored_message(content, self.IGNORING)

    @pytest.mark.parametrize("content", ["!join now", "everyone !join", "gg", "wp", "", "3"])
    def test_anything_else_is_not(self, content):
        assert not detector.is_ignored_message(content, self.IGNORING)

    def test_without_a_list_only_votes_are(self):
        assert detector.is_ignored_message("1")
        assert not detector.is_ignored_message("!join")

    def test_such_a_message_never_enters_the_window(self):
        chat = warm_chat(settings=settings(added_ignored_messages=self.IGNORING))
        before = len(detector._entries[chat.channel])

        for entrant in people(80, prefix="entrant"):
            assert chat.say(entrant, "!join") is None

        assert len(detector._entries[chat.channel]) == before

    def test_so_a_giveaway_does_not_make_the_chat_look_bigger(self):
        # 200 one-off entrants would otherwise raise the crowd a reaction needs.
        def chat_with_a_giveaway(channel: str, mine: detector.Settings | None) -> ChatSim:
            chat = ChatSim(channel=channel, settings=mine)
            half = detector.BASELINE_WINDOW_SECONDS / 2
            chat.chatter(half)
            chat.say("viewer1", "xd")
            for entrant in people(200, prefix="entrant"):
                chat.say(entrant, "!join")
            chat.chatter(half)
            return chat

        ignoring = chat_with_a_giveaway("ignoring", settings(added_ignored_messages=self.IGNORING))
        counting = chat_with_a_giveaway("counting", None)

        assert ignoring.reactions_needed("xd") == detector._dynamic_min_reaction_unique(10)
        assert counting.reactions_needed("xd") == detector._dynamic_min_reaction_unique(210)


class TestExtendingAMoment:
    def chat_with_a_moment(self, **changed) -> tuple[ChatSim, float]:
        """A warmed-up channel under its own settings, on which a moment has
        fired this very instant."""
        chat = ChatSim(settings=settings(**changed))
        chat.warm_up()
        assert chat.reactions_needed(LAUGH) is not None
        return chat, chat.now

    def a_couple_of_latecomers(self) -> list[str]:
        """Fewer people than fire a moment on this chat, but as many as can
        ever keep one open."""
        assert detector.MIN_SUSTAIN_UNIQUE < detector._dynamic_min_reaction_unique(10)
        return people(detector.MIN_SUSTAIN_UNIQUE, prefix="latecomer")

    def test_at_the_full_bar_a_couple_of_latecomers_do_not_keep_it_open(self):
        chat, fired_at = self.chat_with_a_moment(sustain_fraction=1.0)
        chat.wait(2)

        chat.burst(self.a_couple_of_latecomers(), LAUGH)

        assert chat.reaction_active(since=fired_at) is False

    def test_at_a_third_of_it_they_do(self):
        chat, fired_at = self.chat_with_a_moment(sustain_fraction=1 / 3)
        chat.wait(2)

        chat.burst(self.a_couple_of_latecomers(), LAUGH)

        assert chat.reaction_active(since=fired_at) is True

    def test_a_higher_floor_raises_what_it_takes_to_keep_it_open_too(self):
        floor = detector.MIN_ABSOLUTE_UNIQUE + 3
        chat, fired_at = self.chat_with_a_moment(min_absolute_unique=floor)
        chat.wait(2)

        chat.burst(people(floor - 1, prefix="latecomer"), LAUGH)
        assert chat.reaction_active(since=fired_at) is False

        chat.say("one_more", LAUGH)
        assert chat.reaction_active(since=fired_at) is True


class TestLaughingOnTheTrace:
    """`is_laughing`, the yes or no the dashboard's trace is drawn from."""

    def test_an_emote_the_channel_added_counts_as_laughing(self):
        assert not detector.is_laughing(OWN_EMOTE)
        assert detector.is_laughing(OWN_EMOTE, added_patterns=("smich",))

    def test_so_does_its_name_typed_on_a_channel_that_has_it_as_an_emote(self):
        names = detector.laugh_emote_names(["kekw", "sadge", "pepesmich"], ("smich",))

        assert names == ["kekw", "pepesmich"]
        assert detector.is_laughing("to je pepeSmich", names, ("smich",))
        assert not detector.is_laughing("to je Sadge", names, ("smich",))
