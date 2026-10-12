"""tuning.py: what can be set from the dashboard's Settings page, and how
values set for all channels and for one are laid over what the code says.

The recommended value of every setting is a constant somewhere else, and
those constants are retuned often - so, like the detector's tests, these are
written against the constants, not against what they happen to be today.
"""

from __future__ import annotations

import dataclasses

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

    def test_the_longest_file_a_moment_can_get_fits_in_what_the_recorder_keeps(self):
        # From the first frame of the context before the clip to the moment
        # the file is cut: the footage has to be in the buffer all that time.
        most = {item.name: item.most for item in tuning.SETTINGS if item.most is not None}
        needed = (
            most["context_before_seconds"]
            + most["pre_roll_seconds"]
            + most["playback_delay_seconds"]
            + detector.SHORT_WINDOW_SECONDS
            + most["longest_seconds"]
            + tuning.MOMENT_SESSION_POLL_SECONDS
            + most["post_roll_seconds"]
            + most["context_after_seconds"]
            + recorder.CLIP_SETTLE_SECONDS
        )

        assert needed < recorder.BUFFER_RETENTION_SECONDS

    def test_the_lull_cannot_be_as_short_as_the_window_a_reaction_is_judged_in(self):
        # See test_detector_settings: the same laughs would fire twice.
        assert setting("lull_seconds").least > detector.SHORT_WINDOW_SECONDS
        assert setting("lull_seconds").least <= detector.COOLDOWN_SECONDS

    def test_a_list_starts_out_empty(self):
        recommended = tuning.recommended_values()

        assert {recommended[item.name] for item in tuning.SETTINGS if item.kind in tuning.LISTS} == {()}


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

    def test_words_are_split_lowercased_and_put_in_order(self):
        assert tuning.clean("laugh_emote_words", "Smich, HAHA2  smich\tomegA") == ("haha2", "omega", "smich")
        assert tuning.clean("laugh_emote_words", ["Smich", "haha2"]) == ("haha2", "smich")

    def test_a_message_to_ignore_keeps_its_spaces(self):
        assert tuning.clean("ignored_messages", "!join, GG wp\n !ENTER ,") == ("!enter", "!join", "gg wp")

    def test_an_empty_list_is_a_list(self):
        assert tuning.clean("laugh_emote_words", "  ") == ()
        assert tuning.clean("ignored_messages", []) == ()

    def test_a_part_of_an_emote_name_too_short_to_tell_emotes_apart_is_refused(self):
        # "a" is in most emote names there are.
        with pytest.raises(ValueError, match="'ab' has to be"):
            tuning.clean("ignored_emote_words", "kekw ab")

    def test_the_patterns_built_in_would_all_be_taken(self):
        for pattern in (*detector.LAUGH_EMOTE_NAME_PATTERNS, *detector.DANCE_EMOTE_NAME_PATTERNS):
            assert tuning.clean("laugh_emote_words", pattern) == (pattern,)

    def test_a_list_has_its_limits(self):
        with pytest.raises(ValueError, match="at the most"):
            tuning.clean("laugh_emote_words", " ".join(f"emote{number}" for number in range(tuning.LIST_MAX_ITEMS + 1)))
        with pytest.raises(ValueError, match="characters long"):
            tuning.clean("ignored_messages", "x" * (tuning.PHRASE_MAX_LENGTH + 1))
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
        assert tuning.words(setting("laugh_emote_words"), ("haha2", "smich")) == "haha2 smich"
        assert tuning.words(setting("ignored_messages"), ("!join", "gg wp")) == "!join, gg wp"

    def test_what_is_written_is_taken_back_as_the_same_value(self):
        given = {"laugh_emote_words": ("haha2", "smich"), "ignored_messages": ("!join", "gg wp")}

        for name, value in given.items():
            assert tuning.clean(name, tuning.words(setting(name), value)) == value

    def form(self, for_all: dict | None = None, own: dict | None = None) -> list[dict]:
        if own is None:
            return tuning.form(tuning.values(for_all or {}), tuning.recommended_values(), "Recommended")
        return tuning.form(tuning.values(for_all or {}, own), tuning.values(for_all or {}), "All channels")

    def fields(self, form: list[dict]) -> dict[str, dict]:
        return {field["name"]: field for section in form for field in [*section["fields"], *section["advanced"]]}

    def test_the_form_has_the_sections_in_order_with_every_setting_in_one(self):
        form = self.form()

        assert [section["title"] for section in form] == [title for _key, title, _lead in tuning.GROUPS]
        assert list(self.fields(form)) == [
            item.name for group, *_ in tuning.GROUPS for item in tuning.SETTINGS if item.group == group
        ]

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

        assert fields["ignored_messages"]["value"] == "!join"
        assert fields["ignored_messages"]["rest_words"] == "Recommended: none"
        assert fields["laugh_emote_words"]["value"] == ""

    def test_a_section_says_when_one_of_its_folded_away_settings_is_set(self):
        untouched = self.form({"reacting_share": 0.25})
        touched = self.form({"sustain_fraction": 0.5})

        assert [section["advanced_set"] for section in untouched] == [False, False, False]
        assert [section["advanced_set"] for section in touched] == [True, False, False]
