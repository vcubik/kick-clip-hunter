"""The running figures record_message reads instead of going through its
windows again for every message.

They have to come out exactly as going through the windows would - the
same floats, not just close ones - however messages came into a window and
left it, or a moment would fire on one replay of a chat and not another.
"""

from __future__ import annotations

import math
import random

from kick_clip_hunter import detector

CHANNEL = "some_channel"
WINDOW = detector.SHORT_WINDOW_SECONDS
# Weights whose plain running total drifts once some of them have been
# taken away again.
WEIGHTS = (0.0, 0.1, 0.2, 0.3, 1.0 / 3, 0.7, 1e-17, 2.5)


def recounted(entries: list, short_cutoff: float) -> dict:
    """The windows' figures, gone through from scratch."""
    short = [e for e in entries if e[0] >= short_cutoff]
    baseline = [e for e in entries if e[0] < short_cutoff]
    figures = {}
    for name, window, weigh in (
        ("short", short, detector._short_reaction_weight),
        ("baseline", baseline, detector._reaction_weight),
    ):
        figures[name] = {
            "count": len(window),
            "senders": len({e[1] for e in window}),
            "contents": len({e[2] for e in window if e[2]}),
            "emote_count": sum(e[3] for e in window),
            **{
                f"signal{index}": (
                    weigh(window, index),
                    len({e[1] for e in window if e[index] > 0}),
                    sum(1 for e in window if e[index] > 0),
                )
                for index in (4, 5, 6)
            },
        }
    return figures


def running(windows) -> dict:
    figures = {}
    for name, window in (("short", windows.short), ("baseline", windows.baseline)):
        figures[name] = {
            "count": window.count,
            "senders": len(window.senders),
            "contents": len(window.contents),
            "emote_count": window.emote_count,
            **{
                f"signal{index}": (signal.weight, len(signal.senders), signal.count)
                for index, signal in window.signals.items()
            },
        }
    return figures


class TestRunningFigures:
    def test_they_match_going_through_the_windows_after_every_message(self):
        rng = random.Random(7)
        now = 1000.0
        for _ in range(3000):
            # Bursts and lulls, so messages pile up in the windows and then
            # leave them - a few times past the whole baseline window.
            now += rng.choice((0.05, 0.3, 1.0, 4.0, detector.BASELINE_WINDOW_SECONDS / 3))
            detector.record_message(
                CHANNEL,
                sender=f"viewer{rng.randrange(25)}",
                content=rng.choice(("", "hi", "xd", "lol", "what")),
                emote_count=rng.randrange(3),
                emote_weight=rng.choice(WEIGHTS),
                laugh_weight=rng.choice(WEIGHTS),
                mention_weight=rng.choice(WEIGHTS),
                now=now,
            )
            assert running(detector._windows[CHANNEL]) == recounted(list(detector._entries[CHANNEL]), now - WINDOW)

    def test_forgetting_a_channel_drops_its_figures(self):
        detector.record_message(CHANNEL, sender="alice", content="xd", laugh_weight=1.0, now=1.0)

        detector.forget(CHANNEL)

        assert CHANNEL not in detector._windows
        detector.record_message(CHANNEL, sender="bob", now=2.0)
        assert running(detector._windows[CHANNEL])["short"]["senders"] == 1


class TestExactSum:
    def test_it_is_the_correctly_rounded_sum_whatever_came_and_went(self):
        rng = random.Random(3)
        partials: list[float] = []
        held: list[float] = []
        for _ in range(2000):
            if held and rng.random() < 0.45:
                value = held.pop(rng.randrange(len(held)))
                detector._add_exactly(partials, -value)
            else:
                value = rng.choice(WEIGHTS) * rng.choice((1, 1e10, 1e-10))
                held.append(value)
                detector._add_exactly(partials, value)
            assert math.fsum(partials) == math.fsum(held)

    def test_taking_everything_away_leaves_exactly_nothing(self):
        partials: list[float] = []
        for value in (0.1, 0.2, 0.3, 1e16, 1.0):
            detector._add_exactly(partials, value)
        for value in (1.0, 0.1, 1e16, 0.3, 0.2):
            detector._add_exactly(partials, -value)

        assert math.fsum(partials) == 0.0
