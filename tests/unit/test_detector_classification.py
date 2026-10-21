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

    @pytest.mark.parametrize(
        "name", ["emojiLol", "KEKW", "OMEGALUL", "pepeLaugh", "collectibleswideomelaugh", "LULW", "collectiblesLMAO"]
    )
    def test_a_laugh_emote_is_a_laugh_not_an_emote(self, name):
        assert detector.classify_message(native_emote(name))[0] == detector.EMOTE_LAUGH_WEIGHT
        assert detector.classify_native_emotes(native_emote(name)) == 0.0

    @pytest.mark.parametrize("name", ["POGGERS", "PogChamp", "peepoPog"])
    def test_pog_anywhere_in_the_name_is_laughing(self, name):
        assert detector.native_laugh_weight(native_emote(name)) == detector.EMOTE_LAUGH_WEIGHT

    @pytest.mark.parametrize("name", ["lo", "LO", "Lo"])
    def test_an_emote_called_just_lo_is_laughing_in_any_case(self, name):
        assert detector.native_laugh_weight(native_emote(name)) == detector.EMOTE_LAUGH_WEIGHT

    @pytest.mark.parametrize("name", ["asmonSmash", "catJAM", "Love"])
    def test_any_other_emote_is_no_laugh(self, name):
        assert detector.native_laugh_weight(native_emote(name)) == 0.0

    def test_a_typed_laugh_outweighs_a_laugh_emote_next_to_it(self):
        assert detector.LAUGH_STRONG_WEIGHT > detector.EMOTE_LAUGH_WEIGHT
        assert detector.classify_message(f"xDDD {native_emote('KEKW')}")[0] == detector.LAUGH_STRONG_WEIGHT

    @pytest.mark.parametrize("name", ["asmonSmash", "resttD", "shoulderRoll", "collectiblesGoldenLOOT", "Glow", "Love"])
    def test_unrelated_emotes_weigh_little(self, name):
        assert detector.classify_native_emotes(native_emote(name)) == detector.EMOTE_OTHER_WEIGHT

    @pytest.mark.parametrize("name", ["catJAM", "beeBobble", "headBang", "EDMusiC", "pepeDance", "GooseJAM"])
    def test_dance_emotes_weigh_nothing(self, name):
        assert detector.classify_native_emotes(native_emote(name)) == 0.0

    def test_a_laugh_emote_among_others_is_a_laugh_and_the_others_still_emotes(self):
        content = f"{native_emote('asmonSmash')} {native_emote('KEKW', 2)} {native_emote('catJAM', 3)}"
        assert detector.classify_native_emotes(content) == detector.EMOTE_OTHER_WEIGHT
        assert detector.native_laugh_weight(content) == detector.EMOTE_LAUGH_WEIGHT

    def test_stacking_the_same_emote_does_not_add_up(self):
        assert detector.native_laugh_weight(native_emote("KEKW") * 8) == detector.EMOTE_LAUGH_WEIGHT
        assert detector.classify_native_emotes(native_emote("asmonSmash") * 8) == detector.EMOTE_OTHER_WEIGHT

    def test_laugh_outranks_dance_when_a_name_matches_both(self):
        # "LULdance" style names exist; laughing is the stronger reading.
        assert detector.native_laugh_weight(native_emote("LULdance")) == detector.EMOTE_LAUGH_WEIGHT

    def test_weights_are_ordered_laugh_over_other_over_dance(self):
        assert detector.EMOTE_LAUGH_WEIGHT > detector.EMOTE_OTHER_WEIGHT > 0.0


class TestChannelEmoteNames:
    def test_laugh_names_get_the_laugh_weight_and_the_rest_the_default(self):
        weights = detector.classify_emote_names(["KEKW", "OMEGALUL", "PogChamp", "Sadge", "monkaS"])
        assert weights == {
            "kekw": detector.EMOTE_MENTION_LAUGH_WEIGHT,
            "omegalul": detector.EMOTE_MENTION_LAUGH_WEIGHT,
            "pogchamp": detector.EMOTE_MENTION_LAUGH_WEIGHT,
            "sadge": detector.EMOTE_MENTION_OTHER_WEIGHT,
            "monkas": detector.EMOTE_MENTION_OTHER_WEIGHT,
        }

    @pytest.mark.parametrize("name", ["re", "xd", "bla", "LUL"])
    def test_names_too_short_to_be_meaningful_substrings_are_dropped(self, name):
        # "re" would match inside "treba", "bla" inside "blazen", ...
        assert len(name) < detector.MIN_EMOTE_NAME_LENGTH
        assert detector.classify_emote_names([name]) == {}

    @pytest.mark.parametrize("name", ["lo", "LO", "Lo"])
    def test_a_short_laughing_name_is_kept_and_found_as_a_word_of_its_own(self, name):
        assert name.lower() in detector.LAUGH_EMOTE_WHOLE_NAMES
        weights = detector.classify_emote_names([name])
        assert weights == {"lo": detector.EMOTE_MENTION_LAUGH_WEIGHT}
        assert detector.classify_message("LO LO LO", weights)[1] == detector.EMOTE_MENTION_LAUGH_WEIGHT
        assert detector.classify_message("to je lo!", weights)[1] == detector.EMOTE_MENTION_LAUGH_WEIGHT

    @pytest.mark.parametrize("content", ["clovek", "napadlo", "lol", "slow"])
    def test_a_short_laughing_name_is_not_found_inside_other_words(self, content):
        assert detector.classify_message(content, detector.classify_emote_names(["lo"]))[1] == 0.0

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

    @pytest.mark.parametrize(
        "content",
        [
            "[emote:37226:KEKW]",
            "no way [emote:1:emojiLol]",
            "[emote:2:pepeLaugh] ok",
            "[emote:twitch.425618:LUL]",
            "[emote:twitch.emotesv2_abc123:pepeLaugh] ok",
        ],
    )
    def test_a_native_laugh_emote(self, content):
        assert detector.is_laughing(content)

    def test_a_twitch_emote_is_weighed_like_a_kick_one(self):
        assert detector.native_laugh_weight("[emote:twitch.425618:LUL]") == detector.EMOTE_LAUGH_WEIGHT
        assert detector.classify_native_emotes("[emote:twitch.25:Kappa]") == detector.EMOTE_OTHER_WEIGHT
        assert detector.classify_native_emotes("[emote:twitch.1:catJAM]") == 0.0

    @pytest.mark.parametrize(
        ("content", "laugh", "emote"),
        [
            ("[emote:emoji.1f602:face_with_tears_of_joy]", detector.EMOTE_LAUGH_WEIGHT, 0.0),
            ("[emote:emoji.1f923:rolling_on_the_floor_laughing]", detector.EMOTE_LAUGH_WEIGHT, 0.0),
            ("[emote:emoji.2764_fe0f:red_heart]", 0.0, detector.EMOTE_OTHER_WEIGHT),
            ("[emote:youtube.KOxdr_z3A5h-1Gb7k:face-purple-laughing]", detector.EMOTE_LAUGH_WEIGHT, 0.0),
            ("[emote:youtube.tRnrCQtE-KlTM9YL:face-purple-sweating]", 0.0, detector.EMOTE_OTHER_WEIGHT),
        ],
    )
    def test_a_youtube_emoji_is_weighed_by_its_name_like_any_platforms_emote(self, content, laugh, emote):
        assert detector.native_laugh_weight(content) == laugh
        assert detector.classify_native_emotes(content) == emote

    def test_one_of_the_channels_laugh_emotes_named_in_the_text(self):
        assert detector.is_laughing("KEKW", ["kekw"])
        assert detector.is_laughing("on spadl OMEGALUL", ["kekw", "omegalul"])
        assert detector.is_laughing("LO LO", ["lo"])
        assert not detector.is_laughing("clovek napadlo", ["lo"])

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
