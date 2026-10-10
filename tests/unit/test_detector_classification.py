"""How a single chat message is classified before it reaches the rolling
window: laughs, native Kick emotes, 7TV emote names typed as text, votes."""

from __future__ import annotations

import pytest

from kick_clip_hunter import detector
from tests.support.chat import native_emote

STRONG = detector.LAUGH_STRONG_WEIGHT
WEAK = detector.LAUGH_WEAK_WEIGHT


class TestLaughs:
    @pytest.mark.parametrize("content", ["xDD", "xDDDDDD", "XDDD", "xddd", "to je konec xDDD", "xDDD!!!", "(xDD)"])
    def test_exaggerated_laugh_is_the_strong_form(self, content):
        assert detector.classify_message(content)[0] == STRONG

    @pytest.mark.parametrize("content", ["xd", "xD", "XD", "no to snad ne xD", "xd.", "xD xD"])
    def test_bare_laugh_is_the_weak_form(self, content):
        assert detector.classify_message(content)[0] == WEAK

    @pytest.mark.parametrize(
        "content",
        ["", "ahoj", "maxdps build", "taxdd", "xdrive", "x d", "lol", "haha", "xDDDrive"],
    )
    def test_laugh_has_to_stand_as_its_own_word(self, content):
        assert detector.classify_message(content)[0] == 0.0

    def test_strong_form_wins_when_both_appear(self):
        assert detector.classify_message("xd ... xDDDD")[0] == STRONG

    def test_strong_form_outweighs_weak_form(self):
        assert STRONG > WEAK > 0


# A channel's 7TV emote names with their mention weights, as stored per channel.
CHANNEL_KEYWORDS = {"kekw": 3.5, "pogchamp": 1.0, "sadge": 1.0}


class TestEmoteMentions:
    KEYWORDS = CHANNEL_KEYWORDS

    def test_no_keywords_means_no_mention(self):
        assert detector.classify_message("KEKW") == (0.0, 0.0)

    @pytest.mark.parametrize("content", ["KEKW", "kekw", "KeKw", "that was KEKW honestly", "KEKWait"])
    def test_keywords_match_case_insensitively_anywhere_in_the_text(self, content):
        assert detector.classify_message(content, self.KEYWORDS)[1] == 3.5

    def test_strongest_matching_keyword_decides_the_weight(self):
        assert detector.classify_message("Sadge ... KEKW", self.KEYWORDS)[1] == 3.5
        assert detector.classify_message("Sadge PogChamp", self.KEYWORDS)[1] == 1.0

    def test_text_without_a_keyword_is_not_a_mention(self):
        assert detector.classify_message("just a normal sentence", self.KEYWORDS)[1] == 0.0

    def test_laugh_and_mention_are_independent(self):
        assert detector.classify_message("xDDD KEKW", self.KEYWORDS) == (STRONG, 3.5)


class TestNativeEmotes:
    def test_message_without_emote_tokens_weighs_nothing(self):
        assert detector.classify_native_emotes("no emotes here") == 0.0

    @pytest.mark.parametrize("name", ["emojiLol", "KEKW", "OMEGALUL", "pepeLaugh", "collectibleswideomelaugh", "LULW"])
    def test_laugh_emotes_weigh_the_most(self, name):
        assert detector.classify_native_emotes(native_emote(name)) == detector.EMOTE_LAUGH_WEIGHT

    @pytest.mark.parametrize("name", ["asmonSmash", "resttD", "shoulderRoll", "collectiblesGoldenLOOT"])
    def test_unrelated_emotes_weigh_little(self, name):
        assert detector.classify_native_emotes(native_emote(name)) == detector.EMOTE_OTHER_WEIGHT

    @pytest.mark.parametrize("name", ["catJAM", "beeBobble", "headBang", "EDMusiC", "pepeDance", "GooseJAM"])
    def test_dance_emotes_weigh_nothing(self, name):
        assert detector.classify_native_emotes(native_emote(name)) == 0.0

    def test_several_emotes_in_one_message_count_as_the_strongest_one(self):
        content = f"{native_emote('asmonSmash')} {native_emote('KEKW', 2)} {native_emote('catJAM', 3)}"
        assert detector.classify_native_emotes(content) == detector.EMOTE_LAUGH_WEIGHT

    def test_stacking_the_same_emote_does_not_add_up(self):
        assert detector.classify_native_emotes(native_emote("KEKW") * 8) == detector.EMOTE_LAUGH_WEIGHT

    def test_laugh_outranks_dance_when_a_name_matches_both(self):
        # "LULdance" style names exist; laughing is the stronger reading.
        assert detector.classify_native_emotes(native_emote("LULdance")) == detector.EMOTE_LAUGH_WEIGHT

    def test_weights_are_ordered_laugh_over_other_over_dance(self):
        assert detector.EMOTE_LAUGH_WEIGHT > detector.EMOTE_OTHER_WEIGHT > 0.0


class TestChannelEmoteNames:
    def test_laugh_names_get_the_laugh_weight_and_the_rest_the_default(self):
        weights = detector.classify_emote_names(["KEKW", "OMEGALUL", "Sadge", "PogChamp"])
        assert weights == {
            "kekw": detector.EMOTE_MENTION_LAUGH_WEIGHT,
            "omegalul": detector.EMOTE_MENTION_LAUGH_WEIGHT,
            "sadge": detector.EMOTE_MENTION_OTHER_WEIGHT,
            "pogchamp": detector.EMOTE_MENTION_OTHER_WEIGHT,
        }

    @pytest.mark.parametrize("name", ["lo", "re", "xd", "bla", "LUL"])
    def test_names_too_short_to_be_meaningful_substrings_are_dropped(self, name):
        # "lo" would match inside "clovek", "re" inside "treba", ...
        assert len(name) < detector.MIN_EMOTE_NAME_LENGTH
        assert detector.classify_emote_names([name]) == {}

    @pytest.mark.parametrize("name", ["catJAM", "headBang", "beeBobble", "pepeDance"])
    def test_dance_names_are_dropped(self, name):
        assert detector.classify_emote_names([name]) == {}

    def test_keys_are_lowercased_to_match_how_messages_are_compared(self):
        weights = detector.classify_emote_names(["PepeLaugh"])
        assert list(weights) == ["pepelaugh"]
        assert detector.classify_message("PEPELAUGH", weights)[1] == detector.EMOTE_MENTION_LAUGH_WEIGHT

    def test_empty_emote_set(self):
        assert detector.classify_emote_names([]) == {}


class TestIsLaughing:
    """The plain yes or no the dashboard's trace is drawn from."""

    @pytest.mark.parametrize("content", ["xd", "XD", "to je konec xDDDD", "xD!", "ne xd ne"])
    def test_the_typed_laugh_in_any_form(self, content):
        assert detector.is_laughing(content)

    @pytest.mark.parametrize("content", ["[emote:37226:KEKW]", "no way [emote:1:emojiLol]", "[emote:2:pepeLaugh] ok"])
    def test_a_native_laugh_emote(self, content):
        assert detector.is_laughing(content)

    def test_one_of_the_channels_laugh_emotes_named_in_the_text(self):
        assert detector.is_laughing("KEKW", ["kekw"])
        assert detector.is_laughing("on spadl OMEGALUL", ["kekw", "omegalul"])

    def test_the_same_word_means_nothing_on_a_channel_that_has_no_such_emote(self):
        assert not detector.is_laughing("KEKW")

    @pytest.mark.parametrize(
        "content", ["", "what was that", "extra dobry", "[emote:5:catJAM]", "[emote:6:asmonSmash]", "GG", "???"]
    )
    def test_anything_else_is_not_laughing(self, content):
        assert not detector.is_laughing(content, ["kekw"])

    def test_the_channels_laugh_emotes_are_picked_out_of_all_of_its_emotes(self):
        weights = detector.classify_emote_names(["KEKW", "Sadge", "OMEGALUL", "peepoHappy", "LULW"])

        assert detector.laugh_emote_names(weights) == ["kekw", "omegalul", "lulw"]
        assert detector.laugh_emote_names([]) == []


class TestVoteMessages:
    @pytest.mark.parametrize("content", ["1", "2", " 1", "2 ", "  1  ", "\t2\n"])
    def test_a_bare_1_or_2_is_a_vote(self, content):
        assert detector.is_vote_message(content)

    @pytest.mark.parametrize("content", ["", "3", "0", "12", "11", "1 xd", "2!", "1.", "one", "1 1"])
    def test_anything_else_is_not(self, content):
        assert not detector.is_vote_message(content)
