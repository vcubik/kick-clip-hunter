"""tuning.py: what can be set from the dashboard's Settings page, and how
values set for all channels and for one are laid over what the code says.

The recommended value of every setting is a constant somewhere else, and
those constants are retuned often - so, like the detector's tests, these are
written against the constants, not against what they happen to be today.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from typing import ClassVar

import pytest

from kick_clip_hunter import detector, recorder, tuning


def setting(name: str) -> tuning.Setting:
    return tuning.BY_NAME[name]


class TestRecommended:
    def test_it_is_what_the_constants_say(self):
        recommended = tuning.recommended()

        assert recommended.detector == detector.recommended()
        assert recommended.quiet_seconds == tuning.MOMENT_SESSION_QUIET_SECONDS
        assert recommended.max_open_seconds == tuning.MOMENT_SESSION_MAX_SECONDS
        assert recommended.pre_roll_seconds == recorder.PRE_ROLL_SECONDS
        assert recommended.post_roll_seconds == tuning.DYNAMIC_POST_ROLL_SECONDS
        assert recommended.playback_delay_seconds == recorder.PLAYBACK_DELAY_SECONDS
        assert recommended.context_before_seconds == recorder.CONTEXT_BEFORE_SECONDS
        assert recommended.context_after_seconds == recorder.CONTEXT_AFTER_SECONDS

    def test_the_constants_are_read_when_it_is_asked_for(self, monkeypatch):
        # Not when the module was imported: a constant that was changed -
        # by a test, by replay_chat.py's --set - is what a channel gets.
        monkeypatch.setattr(recorder, "PRE_ROLL_SECONDS", 4)
        monkeypatch.setattr(detector, "COOLDOWN_SECONDS", 7)
        monkeypatch.setattr(tuning, "MOMENT_SESSION_MAX_SECONDS", 9)

        recommended = tuning.recommended()

        assert (recommended.pre_roll_seconds, recommended.max_open_seconds) == (4, 9)
        assert recommended.detector.cooldown_seconds == 7

    def test_the_clip_framing_is_what_the_recorder_takes(self):
        framing = tuning.recommended().framing()

        assert framing == {
            "pre_roll_seconds": recorder.PRE_ROLL_SECONDS,
            "post_roll_seconds": tuning.DYNAMIC_POST_ROLL_SECONDS,
            "playback_delay_seconds": recorder.PLAYBACK_DELAY_SECONDS,
            "context_before_seconds": recorder.CONTEXT_BEFORE_SECONDS,
            "context_after_seconds": recorder.CONTEXT_AFTER_SECONDS,
        }

    def test_a_clip_holds_the_reaction_widened_by_its_rolls_and_moved_back_onto_the_streams_clock(self):
        tuned = tuning.recommended()
        reacted = datetime(2026, 10, 30, 20, 0, 0, tzinfo=timezone.utc)

        start, end = tuned.clip_span(reacted, reacted + timedelta(seconds=25))

        assert start == reacted - timedelta(seconds=tuned.pre_roll_seconds + tuned.playback_delay_seconds)
        assert end == reacted + timedelta(seconds=25 + tuned.post_roll_seconds - tuned.playback_delay_seconds)


class TestTheSettingsOnOffer:
    def test_every_setting_has_a_name_of_its_own(self):
        assert len(tuning.BY_NAME) == len(tuning.SETTINGS)

    def test_every_setting_is_in_a_section_of_the_page(self):
        sections = {key for key, _title, _lead in tuning.GROUPS}

        assert {item.group for item in tuning.SETTINGS} == sections

    def test_everything_a_tuning_holds_can_be_set_by_exactly_one_setting(self):
        # A field added to Tuning or to the detector's settings and left out
        # here would be a constant again, silently.
        fields = [field.name for field in dataclasses.fields(tuning.Tuning) if field.name != "detector"]
        fields += [tuning.DETECTOR + field.name for field in dataclasses.fields(detector.Settings)]

        set_by_settings = [path for item in tuning.SETTINGS for path in item.sets]

        assert sorted(set_by_settings) == sorted(fields)

    @pytest.mark.parametrize("item", tuning.SETTINGS, ids=lambda item: item.name)
    def test_what_is_recommended_is_a_value_the_setting_takes(self, item):
        # A constant retuned to outside its setting's range would leave the
        # page unable to save the very value the code runs on.
        recommended = tuning.recommended_values()[item.name]

        assert tuning.clean(item.name, recommended) == recommended

    @pytest.mark.parametrize("item", tuning.SETTINGS, ids=lambda item: item.name)
    def test_a_setting_that_stands_for_two_constants_finds_them_equal(self, item):
        # The page shows one value; it can only do that while the code has
        # the same one in both places.
        recommended = tuning.recommended()

        assert len({tuning._field(recommended, path) for path in item.sets}) == 1

    @pytest.mark.parametrize("item", tuning.SETTINGS, ids=lambda item: item.name)
    def test_a_number_has_its_range_and_nothing_else_has(self, item):
        numeric = item.kind in (tuning.WHOLE, tuning.NUMBER)

        assert (item.least is not None, item.most is not None) == (numeric, numeric)
        if numeric:
            assert item.least < item.most

    def test_what_is_recommended_keeps_to_the_rules(self):
        tuning.check(tuning.recommended_values())

    def test_the_longest_file_a_moment_can_get_fits_in_what_a_recorder_keeps_before_it_is_told(self):
        most = {item.name: item.most for item in tuning.SETTINGS if item.most is not None}
        framing = (
            "longest_seconds",
            "pre_roll_seconds",
            "post_roll_seconds",
            "playback_delay_seconds",
            "context_before_seconds",
            "context_after_seconds",
        )

        longest = tuning.tuned({name: most[name] for name in framing})

        assert tuning.buffer_seconds(longest) <= recorder.BUFFER_RETENTION_SECONDS

    def test_the_lull_cannot_be_as_short_as_the_window_a_reaction_is_judged_in(self):
        # See test_detector_settings: the same laughs would fire twice.
        assert setting("lull_seconds").least > detector.SHORT_WINDOW_SECONDS
        assert setting("lull_seconds").least <= detector.COOLDOWN_SECONDS

    def test_a_list_starts_out_empty_but_the_accounts_to_ignore(self):
        recommended = tuning.recommended_values()
        lists = {item.name for item in tuning.SETTINGS if item.kind in tuning.LISTS}

        assert {recommended[name] for name in lists - {"ignored_senders"}} == {()}
        assert recommended["ignored_senders"] == tuple(sorted(detector.IGNORED_SENDERS))


class TestTakingAValue:
    def test_a_whole_number_within_its_range(self):
        assert tuning.clean("reacting_least", 5) == 5
        assert tuning.clean("reacting_least", 5.0) == 5
        assert isinstance(tuning.clean("reacting_least", 5.0), int)

    def test_a_number_with_decimals(self):
        assert tuning.clean("reacting_share", 0.125) == 0.125
        assert isinstance(tuning.clean("laugh_strong_weight", 4), float)

    def test_the_ends_of_the_range_are_in_it(self):
        item = setting("lull_seconds")

        assert tuning.clean(item.name, item.least) == item.least
        assert tuning.clean(item.name, item.most) == item.most

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("reacting_least", 2.5),
            ("reacting_least", 0),
            ("reacting_least", 10_000),
            ("reacting_least", "3"),
            ("reacting_least", None),
            ("reacting_least", True),
            ("reacting_share", 0),
            ("reacting_share", 1.5),
            ("reacting_share", float("nan")),
            ("reacting_share", float("inf")),
            ("reacting_share", [0.1]),
        ],
    )
    def test_anything_else_is_refused_in_words_that_name_the_setting_and_its_range(self, name, value):
        item = setting(name)

        with pytest.raises(ValueError) as refused:
            tuning.clean(name, value)

        words = str(refused.value)
        assert f'"{item.label}"' in words
        assert f"from {item.least:g} to {item.most:g}" in words

    def test_a_switch_is_on_or_off_and_nothing_like_it(self):
        assert tuning.clean("laugh_signal", False) is False
        for almost in (0, 1, "on", None):
            with pytest.raises(ValueError, match="on or off"):
                tuning.clean("laugh_signal", almost)

    def test_a_list_is_its_items_lowercased_and_put_in_order(self):
        assert tuning.clean("laugh_emote_words", ["Smich", " HAHA2 ", "smich", "omegA"]) == ("haha2", "omega", "smich")
        assert tuning.clean("laugh_emote_words", ("smich",)) == ("smich",)

    def test_a_message_to_ignore_is_kept_whole_commas_and_all(self):
        assert tuning.clean("ignored_messages", ["!join", " GG wp ", "!ENTER", "ok, go"]) == (
            "!enter",
            "!join",
            "gg wp",
            "ok, go",
        )

    def test_an_account_to_ignore_is_its_name_lowercased_without_a_handles_at(self):
        assert tuning.clean("ignored_senders", ["BotRix", "@Some Channel", "nightbot", " @nightbot "]) == (
            "botrix",
            "nightbot",
            "some channel",
        )

    def test_an_account_to_ignore_has_a_length_limit(self):
        with pytest.raises(ValueError, match="a name can be"):
            tuning.clean("ignored_senders", ["x" * (tuning.NAME_MAX_LENGTH + 1)])

    def test_the_accounts_to_ignore_reach_the_detector_as_a_set(self):
        chosen = tuning.tuned({"ignored_senders": ("botrix", "own_bot")})

        assert chosen.detector.ignored_senders == frozenset({"botrix", "own_bot"})
        assert tuning.tuned({"ignored_senders": ()}).detector.ignored_senders == frozenset()

    def test_an_empty_list_is_a_list(self):
        assert tuning.clean("laugh_emote_words", []) == ()
        assert tuning.clean("ignored_messages", ["  ", ""]) == ()

    @pytest.mark.parametrize("name", ["laugh_emote_words", "ignored_messages", "clip_commands"])
    def test_a_list_is_posted_as_its_items_not_as_text_to_split(self, name):
        # Each list once had a way of its own of being typed - spaces between
        # parts of emote names, commas between messages.
        with pytest.raises(ValueError, match="list of words"):
            tuning.clean(name, "kekw, lul")

    def test_a_part_of_an_emote_name_is_one_word(self):
        # No emote's name has a space in it, so such a part would never be in one.
        with pytest.raises(ValueError, match="'smich haha' has to be one word"):
            tuning.clean("laugh_emote_words", ["kekw", "smich haha"])

    def test_a_part_of_an_emote_name_too_short_to_tell_emotes_apart_is_refused(self):
        # "a" is in most emote names there are.
        with pytest.raises(ValueError, match="'ab' has to be"):
            tuning.clean("ignored_emote_words", ["kekw", "ab"])

    def test_the_patterns_built_in_would_all_be_taken(self):
        for pattern in (*detector.LAUGH_EMOTE_NAME_PATTERNS, *detector.DANCE_EMOTE_NAME_PATTERNS):
            assert tuning.clean("laugh_emote_words", [pattern]) == (pattern,)

    def test_a_list_has_its_limits(self):
        with pytest.raises(ValueError, match="at the most"):
            tuning.clean("laugh_emote_words", [f"emote{number}" for number in range(tuning.LIST_MAX_ITEMS + 1)])
        with pytest.raises(ValueError, match="characters long"):
            tuning.clean("ignored_messages", ["x" * (tuning.PHRASE_MAX_LENGTH + 1)])
        with pytest.raises(ValueError, match="list of words"):
            tuning.clean("laugh_emote_words", 7)
        with pytest.raises(ValueError, match="list of words"):
            tuning.clean("laugh_emote_words", ["kekw", 7])

    def test_a_setting_that_does_not_exist_is_refused(self):
        with pytest.raises(ValueError, match="no setting called 'self_destruct'"):
            tuning.clean("self_destruct", 1)

    def test_several_at_once_stop_at_the_first_that_cannot_be_taken(self):
        assert tuning.cleaned({"reacting_least": 4, "laugh_signal": False}) == {
            "reacting_least": 4,
            "laugh_signal": False,
        }
        with pytest.raises(ValueError, match="At least this many people"):
            tuning.cleaned({"reacting_share": 0.2, "reacting_least": -1})


class TestValuesThatContradictEachOther:
    def every(self, **changed) -> dict:
        return {**tuning.recommended_values(), **changed}

    def test_a_floor_above_its_ceiling(self):
        with pytest.raises(ValueError) as refused:
            tuning.check(self.every(reacting_least=20, reacting_most=10))

        assert str(refused.value) == (
            f'"{setting("reacting_least").label}" cannot be more than "{setting("reacting_most").label}".'
        )

    def test_a_floor_at_its_ceiling_is_fine(self):
        tuning.check(self.every(reacting_least=10, reacting_most=10))
        tuning.check(self.every(emote_weight_least=40, emote_weight_most=40))

    def test_a_lull_as_long_as_a_moment_may_stay_open(self):
        # Every moment would then run to its cap.
        with pytest.raises(ValueError) as refused:
            tuning.check(self.every(lull_seconds=60, longest_seconds=60))

        assert str(refused.value) == (
            f'"{setting("longest_seconds").label}" has to be more than "{setting("lull_seconds").label}".'
        )

    def test_every_rule_is_about_two_settings_that_exist(self):
        for lower, upper, _strictly in tuning.RULES:
            assert lower in tuning.BY_NAME and upper in tuning.BY_NAME

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            ("record_commands", "stop_commands"),
            ("clip_commands", "status_commands"),
            ("stop_commands", "status_commands"),
        ],
    )
    def test_a_message_that_would_be_two_commands(self, first, second):
        with pytest.raises(ValueError) as refused:
            tuning.check(self.every(**{first: ("!rec", "!go"), second: ("!rec",)}))

        assert str(refused.value) == (
            f'\'!rec\' cannot be in both "{setting(first).label}" and "{setting(second).label}".'
        )

    def test_every_list_of_commands_is_a_list_of_messages(self):
        assert {setting(name).kind for name in tuning.COMMAND_LISTS} == {tuning.PHRASES}


class TestLayingValuesOverEachOther:
    def test_nothing_set_is_what_is_recommended(self):
        assert tuning.tuned({}) == tuning.recommended()
        assert tuning.values({}) == tuning.recommended_values()

    def test_what_is_set_for_all_channels_replaces_what_is_recommended(self):
        tuned = tuning.tuned({"reacting_share": 0.25, "pre_roll_seconds": 20})

        assert tuned.detector.min_reaction_unique_fraction == 0.25
        assert tuned.pre_roll_seconds == 20
        # ...and nothing else.
        assert tuned.detector.min_absolute_unique == detector.MIN_ABSOLUTE_UNIQUE
        assert tuned.post_roll_seconds == tuning.DYNAMIC_POST_ROLL_SECONDS

    def test_a_channels_own_value_replaces_the_one_for_all_channels(self):
        for_all = {"reacting_share": 0.25, "pre_roll_seconds": 20}

        tuned = tuning.tuned(for_all, {"reacting_share": 0.5})

        assert tuned.detector.min_reaction_unique_fraction == 0.5
        assert tuned.pre_roll_seconds == 20
        assert tuning.values(for_all, {"reacting_share": 0.5})["reacting_share"] == 0.5
        assert tuning.values(for_all, {"reacting_share": 0.5})["pre_roll_seconds"] == 20

    def test_the_lull_sets_both_the_cooldown_and_the_quiet_that_closes_a_moment(self):
        tuned = tuning.tuned({"lull_seconds": 45})

        assert (tuned.detector.cooldown_seconds, tuned.quiet_seconds) == (45, 45)

    def test_left_alone_each_of_the_two_stays_what_its_own_constant_says(self, monkeypatch):
        monkeypatch.setattr(detector, "COOLDOWN_SECONDS", 11)
        monkeypatch.setattr(tuning, "MOMENT_SESSION_QUIET_SECONDS", 22)

        tuned = tuning.tuned({"reacting_share": 0.2})

        assert (tuned.detector.cooldown_seconds, tuned.quiet_seconds) == (11, 22)

    def test_one_weight_for_a_laughing_emote_whichever_way_it_arrives(self):
        tuned = tuning.tuned({"laugh_emote_weight": 6.0, "other_emote_weight": 0.0})

        assert (tuned.detector.emote_laugh_weight, tuned.detector.emote_mention_laugh_weight) == (6.0, 6.0)
        assert (tuned.detector.emote_other_weight, tuned.detector.emote_mention_other_weight) == (0.0, 0.0)

    def test_one_multiplier_for_a_signal_and_for_the_people_behind_it(self):
        tuned = tuning.tuned({"laugh_multiplier": 3.0, "mention_multiplier": 6.0, "emote_multiplier": 5.0})

        assert (tuned.detector.laugh_multiplier, tuned.detector.laugh_unique_sender_multiplier) == (3.0, 3.0)
        assert tuned.detector.emote_mention_multiplier == tuned.detector.emote_mention_unique_sender_multiplier == 6.0
        assert tuned.detector.emote_spike_multiplier == 5.0

    def test_switches_and_lists_reach_the_detector(self):
        tuned = tuning.tuned(
            {
                "mention_signal": False,
                "laugh_emote_words": ("smich",),
                "ignored_emote_words": ("clap", "hype"),
                "ignored_messages": ("!join", "gg wp"),
            }
        )

        assert tuned.detector.emote_mention_enabled is False
        assert tuned.detector.laugh_enabled is True
        assert tuned.detector.added_laugh_emote_patterns == ("smich",)
        assert tuned.detector.added_ignored_emote_patterns == ("clap", "hype")
        assert tuned.detector.added_ignored_messages == frozenset({"!join", "gg wp"})

    def test_clip_commands_and_who_may_post_them(self):
        tuned = tuning.tuned({}, {"clip_commands": ("!clip", "clip that"), "clip_command_from_anyone": True})

        assert tuned.clip_commands == frozenset({"!clip", "clip that"})
        assert tuned.clip_command_from_anyone is True

    def test_what_the_bot_does_in_chat_and_is_asked_there(self):
        tuned = tuning.tuned(
            {},
            {
                "bot_writes": True,
                "bot_tells_clips": True,
                "record_commands": ("!record start", "!rec"),
                "stop_commands": ("!record stop",),
                "status_commands": ("!status",),
            },
        )

        assert (tuned.bot_writes, tuned.bot_tells_clips) == (True, True)
        assert tuned.record_commands == frozenset({"!record start", "!rec"})
        assert tuned.stop_commands == frozenset({"!record stop"})
        assert tuned.status_commands == frozenset({"!status"})


class TestTheBuffer:
    """How far back a channel's recorder keeps its stream: from the first
    frame of a clip's file to when the file is cut."""

    def test_what_is_recommended_needs_less_than_a_recorder_keeps_before_it_is_told(self):
        assert tuning.buffer_seconds(tuning.recommended()) < recorder.BUFFER_RETENTION_SECONDS

    @pytest.mark.parametrize(
        "name",
        [
            "pre_roll_seconds",
            "post_roll_seconds",
            "playback_delay_seconds",
            "context_before_seconds",
            "context_after_seconds",
            "longest_seconds",
        ],
    )
    def test_every_second_more_of_a_clips_file_is_a_second_more_of_it(self, name):
        value = tuning.recommended_values()[name] + 7

        longer = tuning.buffer_seconds(tuning.tuned({name: value}))

        assert longer == tuning.buffer_seconds(tuning.recommended()) + 7

    def test_a_moment_that_closes_early_is_still_not_cut_before_the_laughs_are_counted(self):
        # So it is no shorter for a moment that may stay open for less.
        shortest = setting("longest_seconds").least
        assert shortest + tuning.MOMENT_SESSION_POLL_SECONDS < detector.KEEP_LAUGHING_SECONDS
        counted = detector.KEEP_LAUGHING_SECONDS - tuning.MOMENT_SESSION_POLL_SECONDS

        buffer = tuning.buffer_seconds(tuning.tuned({"longest_seconds": shortest}))

        assert buffer == tuning.buffer_seconds(tuning.tuned({"longest_seconds": counted}))

    def test_nothing_but_the_clips_framing_has_a_say_in_it(self):
        other = tuning.tuned({"reacting_share": 0.3, "lull_seconds": 60, "clip_commands": ("!clip",)})

        assert tuning.buffer_seconds(other) == tuning.buffer_seconds(tuning.recommended())


class TestWhatIsAChannelsAlone:
    COMMANDS: ClassVar = {"clip_commands": ("!clip",), "clip_command_from_anyone": True}

    def test_the_commands_are_and_nothing_else_is(self):
        assert tuning.PER_CHANNEL == {
            "clip_commands",
            "clip_command_from_anyone",
            "record_commands",
            "stop_commands",
            "status_commands",
            "help_commands",
        }
        assert {item.name for item in tuning.SETTINGS if item.per_channel} == tuning.PER_CHANNEL

    def test_what_the_bot_does_and_says_holds_of_a_channels_own_either_way_too(self):
        bots = {item.name for item in tuning.SETTINGS if item.group in (tuning.BOT, tuning.MESSAGES)}

        assert tuning.EITHER_WAY == tuning.PER_CHANNEL | bots
        assert tuning.either_way({"bot_writes": True, "record_commands": ("!rec",), "reacting_share": 0.2}) == {
            "bot_writes": True,
            "record_commands": ("!rec",),
        }

    def test_what_the_bot_does_and_says_can_be_set_for_all_channels_and_a_channels_own_holds_over_it(self):
        for_all = {"bot_writes": True, "bot_clip_message": "Clipped!"}

        assert tuning.shared(for_all) == for_all
        assert (tuning.tuned(for_all).bot_writes, tuning.tuned(for_all).bot_clip_message) == (True, "Clipped!")
        mine = tuning.tuned(for_all, {"bot_writes": False, "bot_clip_message": ""})
        assert (mine.bot_writes, mine.bot_clip_message) == (False, "")

    def test_given_values_are_told_apart(self):
        given = {**self.COMMANDS, "reacting_share": 0.2}

        assert tuning.per_channel(given) == self.COMMANDS
        assert tuning.shared(given) == {"reacting_share": 0.2}

    def test_one_stored_for_all_channels_holds_for_none(self):
        # Left over from when it could be set there.
        for_all = {**self.COMMANDS, "reacting_share": 0.2}

        tuned = tuning.tuned(for_all)

        assert (tuned.clip_commands, tuned.clip_command_from_anyone) == (frozenset(), False)
        assert tuned.detector.min_reaction_unique_fraction == 0.2
        assert tuning.values(for_all) == {**tuning.recommended_values(), "reacting_share": 0.2}

    def test_a_channels_own_holds_over_that(self):
        tuned = tuning.tuned({"clip_commands": ("!old",)}, {"clip_commands": ("!clip",)})

        assert tuned.clip_commands == frozenset({"!clip"})


class TestClipCommands:
    def test_there_are_none_until_one_is_set_and_then_only_those_who_run_the_channel_may_ask(self):
        recommended = tuning.recommended()

        assert recommended.clip_commands == frozenset()
        assert recommended.clip_command_from_anyone is False
        assert not recommended.is_clip_command("!clip")
        assert not recommended.is_clip_command("")

    def test_a_command_is_the_whole_message_whatever_its_case(self):
        tuned = tuning.tuned({}, {"clip_commands": tuning.clean("clip_commands", ["!Clip", "clip that"])})

        assert tuned.is_clip_command("!clip")
        assert tuned.is_clip_command("  !CLIP ")
        assert tuned.is_clip_command("Clip That")
        assert not tuned.is_clip_command("type !clip to get a clip")
        assert not tuned.is_clip_command("!clips")


class TestTheBot:
    def test_it_writes_nothing_and_takes_no_command_until_it_is_set_to(self):
        recommended = tuning.recommended()

        assert (recommended.bot_writes, recommended.bot_tells_clips) == (False, False)
        assert recommended.record_commands == recommended.stop_commands == recommended.status_commands == frozenset()
        assert [recommended.bot_command(said) for said in ("!record", "!stop", "!status", "")] == [None] * 4

    def test_a_command_is_the_whole_message_whatever_its_case_and_says_which_it_is(self):
        tuned = tuning.tuned(
            {},
            {
                "record_commands": tuning.clean("record_commands", ["!Record Start", "!rec"]),
                "stop_commands": tuning.clean("stop_commands", ["!record stop"]),
                "status_commands": tuning.clean("status_commands", ["!status"]),
            },
        )

        assert tuned.bot_command("!record start") == tuning.RECORD
        assert tuned.bot_command("  !REC ") == tuning.RECORD
        assert tuned.bot_command("!Record Stop") == tuning.STOP
        assert tuned.bot_command("!status") == tuning.STATUS
        assert tuned.bot_command("type !status to see") is None
        assert tuned.bot_command("!statuses") is None

    def test_a_clip_command_is_not_one_of_the_bots(self):
        tuned = tuning.tuned({}, {"clip_commands": ("!clip",), "status_commands": ("!status",)})

        assert tuned.bot_command("!clip") is None
        assert not tuned.is_clip_command("!status")

    def test_it_tells_of_recordings_and_nothing_else_by_itself_and_has_no_words_of_its_own_until_set_to(self):
        recommended = tuning.recommended()

        assert recommended.bot_tells_recordings is True
        assert (recommended.bot_tells_live, recommended.bot_tells_moments) == (False, False)
        messages = [item.name for item in tuning.SETTINGS if item.kind == tuning.TEXT]
        assert [getattr(recommended, name) for name in messages] == [""] * len(messages)
        assert recommended.bot_repeat_minutes == tuning.BOT_REPEAT_MINUTES

    def test_a_message_asking_for_the_commands_is_one_of_them(self):
        tuned = tuning.tuned({}, {"help_commands": tuning.clean("help_commands", ["!Commands"])})

        assert tuned.bot_command(" !commands ") == tuning.HELP

    def test_it_cannot_be_another_command_too(self):
        every = tuning.values({}, {"help_commands": ("!status",), "status_commands": ("!status",)})

        with pytest.raises(ValueError, match="!status"):
            tuning.check(every)


class TestTheBotsMessages:
    def test_one_is_kept_as_one_line_as_it_was_typed(self):
        assert tuning.clean("bot_live_message", "  We are LIVE,\n  {channel}!  ") == "We are LIVE, {channel}!"
        assert tuning.clean("bot_live_message", "") == ""

    def test_it_may_fill_in_the_words_it_names_and_braces_written_twice(self):
        typed = "{reason} {length} on {channel} {{not a word}}"

        assert tuning.clean("bot_ended_message", typed) == typed

    @pytest.mark.parametrize(
        "typed",
        ["Clip of {lenght}", "At {stream_time} by {by}", "{}", "{0}", "{channel!r}", "{channel:>9}", "{channel.x}"],
    )
    def test_a_word_it_does_not_fill_in_is_turned_down_saying_which_it_does(self, typed):
        with pytest.raises(ValueError, match=r"It fills in \{channel\}, \{length\}, \{stream_time\}\."):
            tuning.clean("bot_clip_message", typed)

    @pytest.mark.parametrize("typed", ["a { b", "a } b", "{channel"])
    def test_a_brace_by_itself_is_turned_down(self, typed):
        with pytest.raises(ValueError, match="written twice"):
            tuning.clean("bot_live_message", typed)

    def test_one_too_long_for_a_chat_or_that_is_no_text_is_turned_down(self):
        with pytest.raises(ValueError, match=str(tuning.TEXT_MAX_LENGTH)):
            tuning.clean("bot_repeat_message", "x" * (tuning.TEXT_MAX_LENGTH + 1))
        with pytest.raises(ValueError, match="has to be a message"):
            tuning.clean("bot_repeat_message", 5)

    def test_one_survives_being_stored_and_read_back(self):
        given = {"bot_moment_message": 'Caught at {stream_time} "{by}"', "bot_repeat_minutes": 15}

        assert tuning.stored(tuning.encoded(given)) == given

    def test_every_one_names_what_it_fills_in_and_has_its_own_words_to_show(self):
        for item in tuning.SETTINGS:
            if item.kind == tuning.TEXT:
                assert "channel" in item.fills
                assert item.built_in
                assert item.either_way


class TestWhatIsStored:
    def test_only_what_differs_from_what_it_would_be_anyway(self):
        rest = tuning.recommended_values()
        given = {**rest, "reacting_share": 0.2, "laugh_signal": False, "ignored_messages": ("!join",)}

        assert tuning.differing(given, rest) == {
            "reacting_share": 0.2,
            "laugh_signal": False,
            "ignored_messages": ("!join",),
        }

    def test_values_survive_being_stored_and_read_back(self):
        given = {
            "reacting_share": 0.2,
            "reacting_least": 5,
            "laugh_signal": False,
            "laugh_emote_words": ("haha2", "smich"),
            "ignored_messages": ("!join", "gg wp"),
        }

        texts = tuning.encoded(given)

        assert all(isinstance(text, str) for text in texts.values())
        assert tuning.stored(texts) == given

    def test_what_can_no_longer_be_taken_is_left_out(self):
        # A setting that was removed, a value from when the range was wider,
        # something that is not JSON at all: the setting falls back on what
        # it would be without it, and the rest still holds.
        texts = {
            "reacting_share": "0.2",
            "gone_since": "1",
            "lull_seconds": "9999",
            "laugh_signal": '"yes"',
            "reacting_least": "{not json",
        }

        assert tuning.stored(texts) == {"reacting_share": 0.2}


class TestOnThePage:
    def test_a_value_is_written_the_way_it_is_typed(self):
        assert tuning.words(setting("reacting_share"), 0.1) == "0.1"
        assert tuning.words(setting("laugh_strong_weight"), 5.0) == "5"
        assert tuning.words(setting("lull_seconds"), 30) == "30"
        assert tuning.words(setting("laugh_signal"), True) == "On"
        assert tuning.words(setting("laugh_signal"), False) == "Off"
        # Every list the same way, whatever it holds.
        assert tuning.words(setting("laugh_emote_words"), ("haha2", "smich")) == "haha2, smich"
        assert tuning.words(setting("ignored_messages"), ("!join", "gg wp")) == "!join, gg wp"

    def form(self, for_all: dict | None = None, own: dict | None = None) -> list[dict]:
        if own is None:
            return tuning.form(tuning.values(for_all or {}), tuning.recommended_values(), "Recommended")
        every, rest = tuning.values(for_all or {}, own), tuning.values(for_all or {})
        return tuning.form(every, rest, "All channels", for_channel=True)

    def fields(self, form: list[dict]) -> dict[str, dict]:
        return {field["name"]: field for section in form for field in [*section["fields"], *section["advanced"]]}

    def test_a_channels_form_has_the_sections_in_order_with_every_setting_in_one(self):
        form = self.form(own={})

        assert [section["title"] for section in form] == [title for _key, title, _lead in tuning.GROUPS]
        assert list(self.fields(form)) == [
            item.name for group, *_ in tuning.GROUPS for item in tuning.SETTINGS if item.group == group
        ]

    def test_what_holds_of_a_channels_own_either_way_comes_first_in_it_as_sections_of_their_own(self):
        commands, bot, messages, *others = self.form(own={})

        assert (commands["key"], commands["title"]) == (tuning.COMMANDS, "A clip on command")
        assert [field["name"] for field in commands["fields"]] == ["clip_commands", "clip_command_from_anyone"]
        assert (bot["key"], bot["title"]) == (tuning.BOT, "The bot in chat")
        assert [field["name"] for field in bot["fields"]] == [
            "bot_writes",
            "bot_tells_live",
            "bot_tells_recordings",
            "bot_tells_moments",
            "bot_tells_clips",
            "record_commands",
            "stop_commands",
            "status_commands",
            "help_commands",
        ]
        assert (messages["key"], messages["title"]) == (tuning.MESSAGES, "What the bot says")
        assert [field["name"] for field in messages["fields"]] == [
            "bot_live_message",
            "bot_started_message",
            "bot_ended_message",
            "bot_moment_message",
            "bot_clip_message",
            "bot_repeat_message",
            "bot_repeat_minutes",
        ]
        assert commands["either_way"] is bot["either_way"] is messages["either_way"] is True
        assert [section["either_way"] for section in others] == [False] * len(others)

    def test_what_is_a_channels_alone_is_not_in_the_form_for_all_channels_at_all(self):
        form = self.form()

        # The bot's sections come after detection's there.
        assert [section["key"] for section in form] == [
            *(key for key, _title, _lead in tuning.GROUPS[3:]),
            tuning.BOT,
            tuning.MESSAGES,
        ]
        assert not tuning.PER_CHANNEL & self.fields(form).keys()
        assert self.fields(form).keys() == {item.name for item in tuning.SETTINGS if not item.per_channel}
        assert [field["name"] for field in form[-2]["fields"]] == [
            "bot_writes",
            "bot_tells_live",
            "bot_tells_recordings",
            "bot_tells_moments",
            "bot_tells_clips",
        ]

    def test_a_message_is_its_words_and_left_empty_says_it_is_the_bots_own(self):
        fields = self.fields(self.form({"bot_clip_message": "Clipped at {stream_time}!"}, own={}))

        clip, live = fields["bot_clip_message"], fields["bot_live_message"]
        assert (clip["kind"], clip["value"], clip["at_rest"]) == (tuning.TEXT, "Clipped at {stream_time}!", True)
        assert clip["rest_words"] == "All channels: Clipped at {stream_time}!"
        assert (live["value"], live["rest_words"]) == ("", "All channels: its own words")
        assert live["prompt"] == setting("bot_live_message").built_in
        assert live["longest"] == tuning.TEXT_MAX_LENGTH
        assert clip["caption"].endswith("It fills in {channel}, {length}, {stream_time}.")

    def test_left_alone_it_is_what_is_recommended_whatever_all_channels_were_given(self):
        fields = self.fields(self.form({"clip_commands": ("!old",)}, own={"clip_command_from_anyone": True}))

        assert (fields["clip_commands"]["entries"], fields["clip_commands"]["at_rest"]) == ([], True)
        assert fields["clip_commands"]["rest_words"] == "Recommended: none"
        assert (fields["clip_command_from_anyone"]["on"], fields["clip_command_from_anyone"]["at_rest"]) == (
            True,
            False,
        )
        assert fields["clip_command_from_anyone"]["rest_words"] == "Recommended: Off"

    def test_advanced_settings_are_listed_apart(self):
        form = self.form()

        for section in form:
            assert all(not field["advanced"] for field in section["fields"])
            assert all(field["advanced"] for field in section["advanced"])
        assert any(section["advanced"] for section in form)

    def test_a_setting_left_alone_is_at_rest_and_says_what_is_recommended(self):
        field = self.fields(self.form())["reacting_share"]

        recommended = f"{detector.MIN_REACTION_UNIQUE_FRACTION:g}"
        assert (field["value"], field["rest"], field["at_rest"]) == (recommended, recommended, True)
        assert field["rest_words"] == f"Recommended: {recommended}"
        assert field["range_words"] == "A number from 0.01 to 1"

    def test_one_that_was_set_is_not_and_still_says_what_it_would_be(self):
        field = self.fields(self.form({"reacting_share": 0.25}))["reacting_share"]

        assert (field["value"], field["at_rest"]) == ("0.25", False)
        assert field["rest_words"] == f"Recommended: {detector.MIN_REACTION_UNIQUE_FRACTION:g}"

    def test_for_a_channel_rest_is_what_all_channels_have(self):
        fields = self.fields(self.form({"reacting_share": 0.25}, own={"reacting_least": 6}))

        assert (fields["reacting_share"]["value"], fields["reacting_share"]["at_rest"]) == ("0.25", True)
        assert fields["reacting_share"]["rest_words"] == "All channels: 0.25"
        assert (fields["reacting_least"]["value"], fields["reacting_least"]["at_rest"]) == ("6", False)

    def test_a_number_says_what_may_be_typed_and_nothing_else_does(self):
        fields = self.fields(self.form())
        lull = setting("lull_seconds")

        assert fields["lull_seconds"]["range_words"] == f"A whole number from {lull.least:g} to {lull.most:g}"
        assert (fields["lull_seconds"]["unit"], fields["reacting_share"]["unit"]) == ("seconds", "")
        assert fields["laugh_signal"]["range_words"] is None
        assert fields["ignored_messages"]["range_words"] is None

    def test_a_switch_says_which_way_it_is(self):
        fields = self.fields(self.form({"mention_signal": False}))

        assert (fields["mention_signal"]["on"], fields["mention_signal"]["at_rest"]) == (False, False)
        assert fields["mention_signal"]["rest_words"] == "Recommended: On"
        assert fields["laugh_signal"]["on"] is True

    def test_an_empty_list_is_said_in_a_word(self):
        fields = self.fields(self.form({"ignored_messages": ("!join",)}))

        assert fields["ignored_messages"]["entries"] == ["!join"]
        assert fields["ignored_messages"]["rest_words"] == "Recommended: none"
        assert fields["laugh_emote_words"]["entries"] == []

    def test_a_list_is_drawn_as_its_items_with_what_it_would_hold_if_left_alone(self):
        fields = self.fields(self.form({"ignored_messages": ("!join", "gg wp")}, own={"ignored_messages": ("!enter",)}))

        field = fields["ignored_messages"]
        assert (field["entries"], field["rest_entries"], field["at_rest"]) == (["!enter"], ["!join", "gg wp"], False)
        assert field["rest_words"] == "All channels: !join, gg wp"

    def test_a_list_says_what_may_be_added_to_it(self):
        fields = self.fields(self.form())

        words, messages = fields["laugh_emote_words"], fields["ignored_messages"]
        assert (words["shortest"], words["longest"]) == (tuning.WORD_MIN_LENGTH, tuning.WORD_MAX_LENGTH)
        assert (messages["shortest"], messages["longest"]) == (1, tuning.PHRASE_MAX_LENGTH)
        assert words["most_items"] == messages["most_items"] == tuning.LIST_MAX_ITEMS
        assert (words["prompt"], messages["prompt"]) == ("Part of an emote's name", "A whole message")
        # ...and nothing else is a list.
        assert "entries" not in fields["reacting_share"]
        assert "entries" not in fields["laugh_signal"]

    def test_a_section_says_when_one_of_its_folded_away_settings_is_set(self):
        untouched = self.form({"reacting_share": 0.25})
        touched = self.form({"sustain_fraction": 0.5})

        assert [section["advanced_set"] for section in untouched] == [False] * len(untouched)
        assert {section["key"] for section in touched if section["advanced_set"]} == {tuning.MOMENTS}

    def test_a_section_counts_its_settings_that_are_set_folded_away_or_not(self):
        form = self.form({"reacting_share": 0.25, "sustain_fraction": 0.5, "ignored_messages": ("!join",)})

        assert {section["key"]: section["set_count"] for section in form} == {
            tuning.BOT: 0,
            tuning.MESSAGES: 0,
            tuning.MOMENTS: 2,
            tuning.SIGNALS: 1,
            tuning.CLIPS: 0,
        }
