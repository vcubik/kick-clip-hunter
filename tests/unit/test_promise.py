"""How promising a moment is (promise.py): written against what the weights
mean, not their values, so refitting them breaks nothing that still holds."""

from __future__ import annotations

import math

import pytest

from kick_clip_hunter import detector, promise

WINDOW = detector.SHORT_WINDOW_SECONDS


class TestPromise:
    def test_it_is_a_chance(self):
        for laughing, seconds, chatters in [(0, WINDOW, 0), (3, WINDOW, 500), (400, 600, 1), (10_000, 3600, 2)]:
            assert 0 < promise.promise(laughing, seconds, chatters) < 1

    def test_more_people_laughing_is_more_promising(self):
        assert promise.promise(12, WINDOW, 100) > promise.promise(6, WINDOW, 100)

    def test_the_same_laughing_in_a_smaller_chat_is_more_promising(self):
        assert promise.promise(8, WINDOW, 25) > promise.promise(8, WINDOW, 400)

    def test_a_reaction_that_held_the_moment_open_longer_is_more_promising(self):
        assert promise.promise(8, 60, 100) > promise.promise(8, WINDOW, 100)

    def test_its_window_alone_is_no_reaction_held(self):
        assert promise.promise(8, WINDOW - 5, 100) == promise.promise(8, WINDOW, 100)

    def test_no_chat_before_it_counts_as_one_person(self):
        assert promise.promise(5, WINDOW, 0) == promise.promise(5, WINDOW, 1)

    def test_it_follows_the_weights(self):
        score = (
            promise.INTERCEPT
            + promise.LAUGHING_WEIGHT * math.log1p(9)
            + promise.SHARE_WEIGHT * math.log1p(9 / math.sqrt(36))
            + promise.HELD_WEIGHT * math.log1p(30)
        )
        assert promise.promise(9, WINDOW + 30, 36) == pytest.approx(1 / (1 + math.exp(-score)))


class TestChattersBefore:
    def test_they_are_the_people_in_the_baseline_window_before_it_fired(self):
        fired = 10_000.0
        heard = [
            ("too_early", fired - detector.BASELINE_WINDOW_SECONDS - 1),
            ("first", fired - detector.BASELINE_WINDOW_SECONDS),
            ("Second", fired - 100),
            ("@second", fired - 50),  # the same person, as the detector tells them
            ("in_the_reaction", fired - WINDOW),
            ("after", fired + 5),
        ]

        assert promise.chatters_before(heard, fired) == 2

    def test_nobody_chatting_is_none(self):
        assert promise.chatters_before([], 1000.0) == 0
