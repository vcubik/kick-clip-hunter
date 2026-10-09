"""When the detector decides that chat is reacting to something.

Every test scripts a chat on a simulated clock (see tests/support/chat.py)
and checks whether, when and why a moment fires. Expectations are phrased
against the detector's own constants wherever a number is involved, so
retuning a threshold doesn't break tests that are about behaviour.
"""

from __future__ import annotations

import math

import pytest

from kick_clip_hunter import detector
from tests.support.chat import ChatSim, native_emote, people

WINDOW = detector.SHORT_WINDOW_SECONDS
LAUGH = "xDDD"
LAUGH_EMOTE = native_emote("KEKW")
OTHER_EMOTE = native_emote("asmonSmash")
DANCE_EMOTE = native_emote("catJAM")


def warm_chat(chatters: int = 10, *, interval: float = 1.0, stray_reactions: bool = True, **kwargs) -> ChatSim:
    """A channel with five minutes of ordinary conversation behind it.

    With `stray_reactions` the baseline also holds what any real chat does:
    the odd laugh and emote from one person, long before anything happens.
    """
    chat = ChatSim(**kwargs)
    half = detector.BASELINE_WINDOW_SECONDS / 2
    chat.chatter(half, chatters=chatters, interval=interval)
    if stray_reactions:
        chat.say("viewer1", "xd")
        chat.say("viewer2", LAUGH_EMOTE)
        chat.say("viewer3", OTHER_EMOTE)
        for keyword in chat.keywords:
            chat.say("viewer4", keyword)
    chat.chatter(half, chatters=chatters, interval=interval)
    return chat


class TestWarmUp:
    def test_nothing_fires_before_there_is_a_baseline_to_compare_against(self):
        chat = ChatSim()
        chat.chatter(detector.BASELINE_WINDOW_SECONDS / 3)

        assert chat.burst(people(30), LAUGH) is None

    def test_the_same_reaction_fires_once_the_baseline_exists(self):
        chat = ChatSim()
        chat.warm_up()

        assert chat.burst(people(30), LAUGH) is not None

    def test_a_long_silence_means_warming_up_again(self):
        # The window only reaches back five minutes: after a longer gap the
        # channel has no history left, exactly as after a restart.
        chat = warm_chat()
        chat.wait(detector.BASELINE_WINDOW_SECONDS + 60)

        assert chat.burst(people(30), LAUGH) is None


class TestLaughReaction:
    def test_a_crowd_laughing_is_a_moment(self):
        chat = warm_chat()

        spike = chat.burst(people(8), LAUGH)

        assert spike is not None
        assert spike.reasons == ["laugh"]

    def test_fires_at_the_first_instant_the_crowd_is_big_enough(self):
        chat = warm_chat(chatters=10)

        assert chat.reactions_needed("xd") == detector._dynamic_min_reaction_unique(10)

    def test_fewer_people_than_the_absolute_floor_never_fire(self):
        chat = warm_chat()
        too_few = people(detector.MIN_ABSOLUTE_UNIQUE - 1)

        for _ in range(10):  # however enthusiastically they keep at it
            assert chat.burst(too_few, LAUGH) is None

    def test_one_account_spamming_is_not_a_crowd(self):
        chat = warm_chat()

        assert chat.burst(["spammer"] * 40, LAUGH) is None

    def test_a_busy_chat_needs_a_bigger_crowd_than_a_small_one(self):
        small = warm_chat(chatters=10, channel="small")
        busy = warm_chat(chatters=100, interval=0.5, channel="busy")

        needed_small = small.reactions_needed("xd")
        needed_busy = busy.reactions_needed("xd")

        assert needed_small == detector._dynamic_min_reaction_unique(10)
        assert needed_busy == detector._dynamic_min_reaction_unique(100)
        assert needed_busy > needed_small

    def test_a_baseline_with_no_laughs_at_all_needs_twice_the_floor(self):
        """Characterisation of a quirk, not an endorsement of it.

        With no laugh anywhere in the last five minutes there is no baseline
        rate to compare against, so the detector falls back to "how many
        times over the floor" - and that has to reach the same multiplier.
        One stray "xd" in the baseline removes the effect (see the test
        above), which makes a perfectly laugh-free chat *harder* to trigger
        than one with background noise. Worth knowing when tuning.
        """
        chat = warm_chat(stray_reactions=False)

        expected = math.ceil(detector.MIN_ABSOLUTE_UNIQUE * detector.LAUGH_UNIQUE_SENDER_MULTIPLIER)
        assert chat.reactions_needed("xd") == expected

    def test_exaggerated_laughs_count_for_more_than_bare_ones(self):
        bare = warm_chat(stray_reactions=False, channel="bare").burst(people(6), "xd")
        exaggerated = warm_chat(stray_reactions=False, channel="exaggerated").burst(people(6), "xDDDD")

        assert bare is not None and exaggerated is not None
        assert exaggerated.score > bare.score


class TestEmoteReaction:
    def test_a_crowd_posting_a_laugh_emote_is_a_moment(self):
        chat = warm_chat()

        spike = chat.burst(people(8), LAUGH_EMOTE)

        assert spike is not None
        assert spike.reasons == ["emotes"]

    def test_unrelated_emotes_need_more_people_than_laugh_emotes(self):
        needed_laugh = warm_chat(channel="laugh").reactions_needed(LAUGH_EMOTE)
        needed_other = warm_chat(channel="other").reactions_needed(OTHER_EMOTE)

        assert needed_laugh is not None and needed_other is not None
        assert needed_other > needed_laugh

    def test_everyone_dancing_to_a_song_is_not_a_moment(self):
        chat = warm_chat()

        assert chat.burst(people(45), DANCE_EMOTE) is None

    def test_one_person_repeating_an_emote_does_not_stand_in_for_others(self):
        # Seen live: one account posting the same emote five times in a row
        # used to carry five people's worth of weight.
        chat = warm_chat()
        needed = chat.reactions_needed(OTHER_EMOTE)
        assert needed is not None and needed > 2

        spammy = warm_chat(channel="spammy")
        for _ in range(needed * 3):
            assert spammy.say("spammer", OTHER_EMOTE) is None
            spammy.wait(0.05)
        assert spammy.burst(people(needed - 2), OTHER_EMOTE) is None

    def test_reports_how_many_emotes_were_in_the_window(self):
        chat = warm_chat()
        chat.wait(WINDOW + 1)  # only the reaction itself in the window

        spike = chat.burst(people(8), LAUGH_EMOTE * 2)

        assert spike is not None
        assert spike.emote_count == 2 * spike.message_count


class TestEmoteMentions:
    KEYWORDS = detector.classify_emote_names(["KEKW", "Sadge"])

    def test_a_crowd_typing_a_laugh_emote_name_is_a_moment(self):
        chat = warm_chat(keywords=self.KEYWORDS)

        spike = chat.burst(people(8), "KEKW")

        assert spike is not None
        assert spike.reasons == ["emote_mention"]

    def test_the_same_text_means_nothing_on_a_channel_without_that_emote(self):
        chat = warm_chat()

        assert chat.burst(people(30), "KEKW") is None

    def test_a_message_can_be_a_laugh_and_a_mention_at_once(self):
        chat = warm_chat(keywords=self.KEYWORDS)

        spike = chat.burst(people(8), "xDDD KEKW")

        assert spike is not None
        assert spike.reasons == ["laugh", "emote_mention"]


class TestMessageVolume:
    """Chat simply getting busier is never a moment by itself."""

    def flood(self, chat: ChatSim, count: int) -> None:
        for number, sender in enumerate(people(count, prefix="passerby")):
            assert chat.say(sender, f"something entirely different {number}") is None
            chat.wait(0.05)

    def test_a_flood_of_ordinary_messages_is_not_a_moment(self):
        chat = warm_chat()

        self.flood(chat, 120)

    def test_a_copy_pasted_raid_is_not_a_moment(self):
        chat = warm_chat()

        assert chat.burst(people(120, prefix="raider"), "HELLO FROM THE RAID", spacing=0.05) is None

    def test_it_is_reported_alongside_a_real_reaction(self):
        chat = warm_chat()
        self.flood(chat, 80)

        spike = chat.burst(people(8), LAUGH)

        assert spike is not None
        assert spike.reasons == ["message_rate", "laugh"]

    def test_it_is_not_reported_when_only_the_reaction_stands_out(self):
        chat = warm_chat()

        spike = chat.burst(people(8), LAUGH)

        assert spike is not None
        assert "message_rate" not in spike.reasons


class TestVotes:
    def test_a_vote_never_enters_the_window(self):
        chat = warm_chat()
        before = len(detector._entries[chat.channel])

        for voter in people(50, prefix="voter"):
            assert chat.say(voter, "1") is None
            assert chat.say(voter, " 2 ") is None

        assert len(detector._entries[chat.channel]) == before

    def test_a_poll_does_not_make_the_chat_look_bigger(self):
        # 200 extra "chatters" would otherwise raise the crowd a reaction needs.
        chat = ChatSim()
        half = detector.BASELINE_WINDOW_SECONDS / 2
        chat.chatter(half)
        chat.say("viewer1", "xd")
        for voter in people(200, prefix="voter"):
            chat.say(voter, "1")
        chat.chatter(half)
        assert detector._dynamic_min_reaction_unique(210) > detector._dynamic_min_reaction_unique(10)

        assert chat.reactions_needed("xd") == detector._dynamic_min_reaction_unique(10)

    def test_a_poll_is_not_message_volume(self):
        chat = warm_chat()
        for voter in people(150, prefix="voter"):
            chat.say(voter, "2")

        spike = chat.burst(people(8), LAUGH)

        assert spike is not None
        assert spike.reasons == ["laugh"]


class TestCooldown:
    def test_a_second_reaction_right_after_is_the_same_moment(self):
        chat = warm_chat()
        assert chat.burst(people(8), LAUGH) is not None

        chat.wait(detector.COOLDOWN_SECONDS / 2)

        assert chat.burst(people(8, prefix="latecomer"), LAUGH) is None

    def test_after_the_cooldown_a_new_reaction_is_a_new_moment(self):
        chat = warm_chat()
        assert chat.burst(people(8), LAUGH) is not None

        chat.chatter(detector.COOLDOWN_SECONDS + 5)

        assert chat.burst(people(8, prefix="latecomer"), LAUGH) is not None
        assert len(chat.moments) == 2

    def test_a_channel_with_no_moment_yet_is_not_in_cooldown_whatever_the_clock_reads(self):
        # The detector's clock is monotonic time, which starts near zero when
        # the machine boots: a service started right after a boot must not
        # treat "no moment yet" as "a moment at time zero".
        chat = ChatSim(now=-detector.BASELINE_WINDOW_SECONDS)
        chat.warm_up()
        assert abs(chat.now) < detector.COOLDOWN_SECONDS

        assert chat.burst(people(30), LAUGH) is not None

    def test_cooldown_and_history_are_per_channel(self):
        # Two channels living through the same five minutes.
        first = warm_chat(channel="first")
        second = warm_chat(channel="second")
        assert first.now == second.now

        assert first.burst(people(8), LAUGH) is not None

        # The first channel's moment (and its cooldown) is not the second's.
        assert second.burst(people(8), LAUGH) is not None
        assert list(detector._last_moment_at) == ["first", "second"]


class TestReportedNumbers:
    def quiet_window_reaction(self, laughers: int):
        chat = warm_chat()
        chat.wait(WINDOW + 1)  # nothing but the reaction in the short window
        return chat, chat.burst(people(laughers), LAUGH)

    def test_counts_describe_the_short_window(self):
        needed = detector._dynamic_min_reaction_unique(10)
        _chat, spike = self.quiet_window_reaction(needed + 3)

        assert spike is not None
        assert spike.message_count == needed  # it fired on the last one needed
        assert spike.keyword_hits == needed
        assert spike.current_message_rate == pytest.approx(needed / WINDOW)

    def test_baseline_rate_is_the_ordinary_pace_of_the_chat(self):
        _chat, spike = self.quiet_window_reaction(8)

        assert spike is not None
        assert spike.baseline_message_rate == pytest.approx(1.0, abs=0.1)  # warm_chat talks once a second

    @pytest.mark.parametrize("laughers", [3, 8, 20, 45])
    def test_score_never_exceeds_the_cap(self, laughers):
        chat = warm_chat()

        spike = chat.burst(people(laughers), LAUGH)

        assert spike is not None
        assert 0 < spike.score <= detector.MAX_RATIO_SCORE

    def test_a_reaction_backed_by_little_volume_scores_lower(self):
        # Same crowd, same laughs - but in one case nothing else was said in
        # the window. A handful of messages must not present like an eruption.
        _chat, thin = self.quiet_window_reaction(8)
        busy_chat = warm_chat(channel="busy")
        busy = busy_chat.burst(people(8), LAUGH)

        assert thin is not None and busy is not None
        assert thin.message_count < detector.VOLUME_DAMPEN_REFERENCE <= busy.message_count
        assert thin.score < busy.score


class TestBookkeeping:
    def test_messages_older_than_the_baseline_window_are_forgotten(self):
        chat = ChatSim()

        chat.chatter(detector.BASELINE_WINDOW_SECONDS * 4, interval=0.5)

        kept = detector._entries[chat.channel]
        assert len(kept) <= detector.BASELINE_WINDOW_SECONDS / 0.5 + 1
        assert chat.now - kept[0][0] <= detector.BASELINE_WINDOW_SECONDS + 0.5

    def test_content_is_compared_case_and_whitespace_insensitively(self):
        # A raid pasting the same phrase with different capitalisation is
        # still one phrase as far as content diversity goes.
        chat = warm_chat()

        for number, raider in enumerate(people(120, prefix="raider")):
            text = "Hello From The Raid" if number % 2 else "  hello from the raid "
            assert chat.say(raider, text) is None
            chat.wait(0.05)

        window = [e for e in detector._entries[chat.channel] if e[1].startswith("raider")]
        assert {e[2] for e in window} == {"hello from the raid"}
