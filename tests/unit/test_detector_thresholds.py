"""The building blocks the firing decision is made of: floors that scale with
how many people are chatting, the baseline-relative ratio, and the per-sender
weighting that keeps one account from counting as a crowd.

The detector's constants are tuned often, so these tests state the *shape*
each helper has to keep (clamped, monotonic, proportional) in terms of those
constants instead of pinning today's numbers.
"""

from __future__ import annotations

import pytest

from kick_clip_hunter import detector

CHAT_SIZES = [0, 1, 5, 10, 19, 20, 35, 50, 80, 100, 150, 400, 5000]


class TestMinimumVolume:
    """`_dynamic_min_count`: how much has to happen in the window at all."""

    @pytest.mark.parametrize("chatters", CHAT_SIZES)
    def test_stays_between_its_floor_and_ceiling(self, chatters):
        minimum = detector._dynamic_min_count(chatters)
        assert detector.MIN_ABSOLUTE_COUNT_FLOOR <= minimum <= detector.MIN_ABSOLUTE_COUNT_CEILING

    def test_never_decreases_as_the_chat_grows(self):
        minimums = [detector._dynamic_min_count(chatters) for chatters in range(0, 500)]
        assert minimums == sorted(minimums)

    def test_a_tiny_chat_gets_the_floor_and_a_huge_one_the_ceiling(self):
        assert detector._dynamic_min_count(1) == detector.MIN_ABSOLUTE_COUNT_FLOOR
        assert detector._dynamic_min_count(100_000) == detector.MIN_ABSOLUTE_COUNT_CEILING

    def test_in_between_it_is_a_fraction_of_the_chat(self):
        chatters = 60
        expected = round(chatters * detector.MIN_ABSOLUTE_COUNT_FRACTION)
        assert detector.MIN_ABSOLUTE_COUNT_FLOOR < expected < detector.MIN_ABSOLUTE_COUNT_CEILING
        assert detector._dynamic_min_count(chatters) == expected


class TestMinimumCrowd:
    """`_dynamic_min_reaction_unique`: how many distinct people have to react."""

    @pytest.mark.parametrize("chatters", CHAT_SIZES)
    def test_stays_between_its_floor_and_ceiling(self, chatters):
        minimum = detector._dynamic_min_reaction_unique(chatters)
        assert detector.MIN_ABSOLUTE_UNIQUE <= minimum <= detector.MIN_REACTION_UNIQUE_CEILING

    def test_never_decreases_as_the_chat_grows(self):
        minimums = [detector._dynamic_min_reaction_unique(chatters) for chatters in range(0, 500)]
        assert minimums == sorted(minimums)

    def test_a_small_chat_keeps_the_absolute_floor(self):
        assert detector._dynamic_min_reaction_unique(10) == detector.MIN_ABSOLUTE_UNIQUE

    def test_a_busy_chat_needs_a_bigger_crowd_than_a_small_one(self):
        assert detector._dynamic_min_reaction_unique(100) > detector._dynamic_min_reaction_unique(10)

    def test_a_huge_chat_is_capped(self):
        assert detector._dynamic_min_reaction_unique(100_000) == detector.MIN_REACTION_UNIQUE_CEILING


class TestSpikeRatio:
    WINDOW = detector.SHORT_WINDOW_SECONDS

    def test_below_the_floor_there_is_no_ratio_at_all(self):
        assert detector._spike_ratio(short_count=2, baseline_count=100, baseline_seconds=290, min_absolute=3) is None

    def test_exactly_at_the_floor_counts(self):
        assert (
            detector._spike_ratio(short_count=3, baseline_count=100, baseline_seconds=290, min_absolute=3) is not None
        )

    def test_is_the_short_window_rate_over_the_baseline_rate(self):
        # 20 in the short window against 1 per second before it.
        ratio = detector._spike_ratio(short_count=20, baseline_count=290, baseline_seconds=290, min_absolute=3)
        assert ratio == pytest.approx(20 / self.WINDOW)

    def test_twice_the_activity_is_twice_the_ratio(self):
        once = detector._spike_ratio(10, 58, 290, 3)
        twice = detector._spike_ratio(20, 58, 290, 3)
        assert twice == pytest.approx(2 * once)

    def test_a_signal_absent_from_the_baseline_is_measured_against_the_floor(self):
        # No rate to divide by: "how many times over the floor" instead.
        assert detector._spike_ratio(short_count=12, baseline_count=0, baseline_seconds=290, min_absolute=3) == 4.0

    def test_no_baseline_time_is_treated_like_an_empty_baseline(self):
        assert detector._spike_ratio(short_count=6, baseline_count=5, baseline_seconds=0, min_absolute=3) == 2.0


def entry(time: float, sender: str, *, emote: float = 0.0, laugh: float = 0.0, mention: float = 0.0) -> tuple:
    """One rolling-window entry, in the detector's own tuple layout."""
    return (time, sender, "text", 0, emote, laugh, mention)


LAUGH = 5  # index of laugh_weight in an entry


class TestPerSenderWeight:
    def test_distinct_senders_add_up(self):
        entries = [entry(1.0, "a", laugh=5.0), entry(1.5, "b", laugh=3.0), entry(2.0, "c", laugh=5.0)]
        assert detector._short_reaction_weight(entries, LAUGH) == 13.0

    def test_one_sender_repeating_counts_once(self):
        entries = [entry(1.0 + i * 0.1, "spammer", laugh=5.0) for i in range(20)]
        assert detector._short_reaction_weight(entries, LAUGH) == 5.0

    def test_a_senders_strongest_message_is_the_one_that_counts(self):
        entries = [entry(1.0, "a", laugh=3.0), entry(2.0, "a", laugh=5.0), entry(3.0, "a", laugh=3.0)]
        assert detector._short_reaction_weight(entries, LAUGH) == 5.0

    def test_messages_without_the_signal_weigh_nothing(self):
        entries = [entry(1.0, "a"), entry(2.0, "b", emote=3.5)]
        assert detector._short_reaction_weight(entries, LAUGH) == 0.0

    def test_signals_are_weighed_independently(self):
        entries = [entry(1.0, "a", emote=3.5, laugh=5.0, mention=1.0)]
        assert [detector._short_reaction_weight(entries, index) for index in (4, 5, 6)] == [3.5, 5.0, 1.0]

    def test_in_the_baseline_a_sender_counts_once_per_window_sized_slice(self):
        window = detector.SHORT_WINDOW_SECONDS
        # The same person laughs three times inside one slice and once in the next.
        entries = [
            entry(0.0, "a", laugh=5.0),
            entry(window * 0.3, "a", laugh=5.0),
            entry(window * 0.9, "a", laugh=3.0),
            entry(window * 1.5, "a", laugh=3.0),
        ]
        assert detector._reaction_weight(entries, LAUGH) == 5.0 + 3.0

    def test_a_spammer_inflates_neither_the_window_nor_the_baseline(self):
        # Otherwise a spammer in the baseline would raise the bar for
        # everyone else, or one in the window would lower it.
        window = detector.SHORT_WINDOW_SECONDS
        spam = [entry(i * 0.5, "spammer", laugh=5.0) for i in range(int(window * 2 * 3))]
        slices = {int(e[0] // window) for e in spam}
        assert detector._reaction_weight(spam, LAUGH) == 5.0 * len(slices)
