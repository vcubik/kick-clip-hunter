"""Whole evenings of chat, start to finish.

The unit tests check one rule at a time; these run a long, scripted stream
through the detector and check the only thing that finally matters: which
moments come out. Each scenario mixes the genuine reactions with everything
that has caused false positives on real streams - polls, raids, music,
spammers, half-hearted titters - on the same channel.

The script is written in terms of the detector's own thresholds (a "big"
reaction is several times the crowd the channel needs, a "small" one well
under it), so ordinary retuning doesn't disturb it. If one of these fails
after a deliberate change, the change altered what counts as a moment.
"""

from __future__ import annotations

import random

import pytest

from kick_clip_hunter import detector
from tests.support.chat import ChatSim, native_emote

WINDOW = detector.SHORT_WINDOW_SECONDS
MINUTE = 60.0


class Stream(ChatSim):
    """A channel with `viewers` regulars chatting at a steady pace, plus the
    events a scenario scripts on top."""

    def __init__(self, viewers: int, seconds_between_messages: float, seed: int = 7, **kwargs) -> None:
        super().__init__(**kwargs)
        self.viewers = [f"regular{number}" for number in range(viewers)]
        self.pace = seconds_between_messages
        self.random = random.Random(seed)
        self._line = 0
        self._guests = 0
        self.script: list[tuple[float, str]] = []  # (clock time, event name)

    # -- background ------------------------------------------------------

    def idle(self, seconds: float) -> None:
        """Ordinary conversation, with the odd lone laugh or emote in it."""
        end = self.now + seconds
        while self.now < end:
            self._line += 1
            sender = self.random.choice(self.viewers)
            roll = self.random.random()
            if roll < 0.02:
                content = "xd"
            elif roll < 0.04:
                content = native_emote("KEKW")
            elif roll < 0.07:
                content = native_emote("asmonSmash")
            else:
                content = f"regular chatter line {self._line}"
            self.say(sender, content)
            self.wait(self.random.uniform(0.5, 1.5) * self.pace)

    # -- events ----------------------------------------------------------

    def crowd(self, name: str, people: int, contents: list[str], over: float = 6.0) -> None:
        """`people` different viewers react within `over` seconds."""
        self.script.append((self.now, name))
        reactors = self._people(people)
        for sender in reactors:
            self.say(sender, self.random.choice(contents))
            self.wait(over / people)

    def _people(self, count: int) -> list[str]:
        # Regulars first; a reaction bigger than the chat draws in lurkers.
        people = self.random.sample(self.viewers, min(count, len(self.viewers)))
        while len(people) < count:
            self._guests += 1
            people.append(f"lurker{self._guests}")
        return people

    def one_account_spams(self, name: str, content: str, times: int, over: float = 8.0) -> None:
        self.script.append((self.now, name))
        for _ in range(times):
            self.say("spammer", content)
            self.wait(over / times)

    def needed(self) -> int:
        return detector._dynamic_min_reaction_unique(len(self.viewers))

    # -- results ---------------------------------------------------------

    def fired(self) -> list[str]:
        """Names of the scripted events a moment fired during (or within
        one short window after the start of)."""
        names = []
        for moment_time, _spike in self.moments:
            started = [(time, name) for time, name in self.script if time <= moment_time <= time + 2 * WINDOW]
            names.append(started[-1][1] if started else f"unscripted moment at {moment_time:.0f}")
        return names


LAUGHS = ["xDDD", "xDDDD", "xd", "to je konec xDDD"]
LAUGH_EMOTES = [native_emote("KEKW"), native_emote("emojiLol"), native_emote("OMEGALUL")]
DANCING = [native_emote("catJAM"), native_emote("beeBobble"), native_emote("headBang")]
UNRELATED_EMOTES = [native_emote("asmonSmash"), native_emote("resttD")]


def test_an_evening_on_a_busy_channel():
    stream = Stream(viewers=80, seconds_between_messages=0.8)
    big = 3 * stream.needed()
    small = max(1, stream.needed() // 2)

    stream.idle(6 * MINUTE)
    stream.crowd("the streamer falls off a cliff", big, LAUGHS)
    stream.idle(2 * MINUTE)
    stream.crowd("a yes/no poll", 120, ["1", "2"], over=20)
    stream.idle(2 * MINUTE)
    stream.crowd("a raid arrives", 150, ["WELCOME RAIDERS FROM THE OTHER CHANNEL"], over=15)
    stream.idle(2 * MINUTE)
    stream.crowd("a song everybody likes", 40, DANCING, over=10)
    stream.idle(2 * MINUTE)
    stream.one_account_spams("someone leans on their keyboard", "xDDDD", times=60)
    stream.idle(2 * MINUTE)
    stream.crowd("a mildly funny remark", small, ["xd"])
    stream.idle(2 * MINUTE)
    stream.crowd("an absurd in-game bug", big, LAUGH_EMOTES)
    stream.idle(detector.COOLDOWN_SECONDS / 3)
    stream.crowd("the bug happens again right away", big, LAUGHS)
    stream.idle(3 * MINUTE)
    stream.crowd("a perfectly timed joke", big, LAUGHS + LAUGH_EMOTES)
    stream.idle(2 * MINUTE)

    assert stream.fired() == [
        "the streamer falls off a cliff",
        "an absurd in-game bug",
        "a perfectly timed joke",
    ]
    reasons = [spike.reasons for _time, spike in stream.moments]
    assert "laugh" in reasons[0]
    assert "emotes" in reasons[1]
    # A mixed reaction fires on whichever signal crosses its threshold first.
    assert set(reasons[2]) & {"laugh", "emotes"}


def test_an_evening_on_a_small_channel():
    # A dozen regulars, a message every few seconds: here three or four
    # people laughing at once *is* the whole chat reacting.
    stream = Stream(viewers=12, seconds_between_messages=4.0, seed=11)
    assert stream.needed() == detector.MIN_ABSOLUTE_UNIQUE

    stream.idle(8 * MINUTE)
    stream.crowd("two friends share an in-joke", detector.MIN_ABSOLUTE_UNIQUE - 1, LAUGHS, over=3)
    stream.idle(3 * MINUTE)
    stream.crowd("half the chat cracks up", 6, LAUGHS, over=5)
    stream.idle(3 * MINUTE)
    stream.crowd("a few unrelated emotes", 3, UNRELATED_EMOTES, over=4)
    stream.idle(3 * MINUTE)
    stream.one_account_spams("one regular spams", native_emote("KEKW"), times=30)
    stream.idle(3 * MINUTE)
    stream.crowd("everyone cracks up again", 8, LAUGHS + LAUGH_EMOTES, over=5)
    stream.idle(2 * MINUTE)

    assert stream.fired() == ["half the chat cracks up", "everyone cracks up again"]


def test_the_same_reaction_matters_on_a_small_channel_and_not_on_a_busy_one():
    """The detector's core idea: thresholds are relative to the channel."""
    small = Stream(viewers=12, seconds_between_messages=4.0, channel="small")
    busy = Stream(viewers=150, seconds_between_messages=0.5, channel="busy")

    for stream in (small, busy):
        stream.idle(8 * MINUTE)
        stream.crowd("five people laugh", 5, LAUGHS, over=4)
        stream.idle(MINUTE)

    assert small.fired() == ["five people laugh"]
    assert busy.fired() == []


def test_a_restart_in_the_middle_of_a_stream_costs_one_warm_up_period():
    """Detector state is in memory only. After a restart the channel has no
    baseline, so the first few minutes can't fire - documented behaviour, and
    the reason restarts mid-stream are worth avoiding."""
    stream = Stream(viewers=30, seconds_between_messages=1.5)
    big = 3 * stream.needed()

    stream.idle(6 * MINUTE)
    stream.crowd("before the restart", big, LAUGHS)
    stream.idle(2 * MINUTE)

    detector._entries.clear()  # the process restarts
    detector._last_moment_at.clear()

    stream.idle(1 * MINUTE)
    stream.crowd("one minute after the restart", big, LAUGHS)
    stream.idle(detector.BASELINE_WINDOW_SECONDS)
    stream.crowd("once the baseline is back", big, LAUGHS)
    stream.idle(MINUTE)

    assert stream.fired() == ["before the restart", "once the baseline is back"]


REACTIONS = ["xd", "xDDD", native_emote("KEKW"), native_emote("asmonSmash"), native_emote("catJAM"), "KEKW", "1", "2"]
KEYWORDS = detector.classify_emote_names(["KEKW", "Sadge"])


SEEDS = range(10)


def random_stream(seed: int) -> int:
    """Runs one randomised stream - random pace, random crowds reacting in
    random ways - checking every moment it produces. Returns how many fired."""
    rng = random.Random(seed)
    chat = ChatSim(channel=f"random-{seed}", keywords=KEYWORDS)
    viewers = [f"viewer{number}" for number in range(rng.randint(5, 120))]
    pace = rng.uniform(0.3, 3.0)
    line = 0
    fired_at: list[float] = []
    started = chat.now

    for _ in range(1500):
        if rng.random() < 0.01:
            # Somewhere between "two people" and "everyone" reacts at once.
            content = rng.choice(REACTIONS)
            for sender in rng.sample(viewers, rng.randint(1, len(viewers))):
                spike = chat.say(sender, content if rng.random() < 0.8 else rng.choice(REACTIONS))
                if spike is not None:
                    check_moment(chat, spike, fired_at, started)
                chat.wait(rng.uniform(0.0, 0.3))
        else:
            line += 1
            content = rng.choice(REACTIONS) if rng.random() < 0.05 else f"line {line}"
            spike = chat.say(rng.choice(viewers), content)
            if spike is not None:
                check_moment(chat, spike, fired_at, started)
            chat.wait(rng.expovariate(1 / pace))
    return len(fired_at)


def check_moment(chat: ChatSim, spike: detector.Spike, fired_at: list[float], started: float) -> None:
    now = chat.now
    window = [entry for entry in detector._entries[chat.channel] if entry[0] >= now - WINDOW]
    reacting = {
        "emotes": {entry[1] for entry in window if entry[4] > 0},
        "laugh": {entry[1] for entry in window if entry[5] > 0},
        "emote_mention": {entry[1] for entry in window if entry[6] > 0},
    }

    # It names why it fired, and volume alone is never the reason.
    assert spike.reasons and set(spike.reasons) <= {"message_rate", *reacting}
    assert spike.reasons != ["message_rate"]
    # Every reaction it names was carried by several distinct people.
    for reason in spike.reasons:
        if reason != "message_rate":
            assert len(reacting[reason]) >= detector.MIN_ABSOLUTE_UNIQUE, f"{reason} fired on {reacting[reason]}"
    # The numbers it reports are sane.
    assert 0 < spike.score <= detector.MAX_RATIO_SCORE
    assert spike.message_count == len(window)
    # It had a baseline to compare against, and respects the cooldown.
    assert now - started >= detector.BASELINE_WINDOW_SECONDS - WINDOW
    if fired_at:
        assert now - fired_at[-1] >= detector.COOLDOWN_SECONDS
    fired_at.append(now)


@pytest.mark.parametrize("seed", SEEDS)
def test_whatever_chat_does_a_moment_always_rests_on_a_real_crowd(seed):
    """Properties every moment must have, whatever the thresholds are tuned
    to - checked on chat nobody scripted."""
    random_stream(seed)


def test_the_randomised_streams_are_not_vacuous():
    # The property above is only worth something if moments actually fire.
    moments = {seed: random_stream(seed) for seed in SEEDS}

    assert sum(moments.values()) >= 10, moments
    assert sum(1 for count in moments.values() if count) >= len(SEEDS) // 2, moments
