"""Whether a closed moment is kept: how many people laughed around it
(`laughing_people`), and how many it takes (`laughing_needed`).

A moment fires on a ten-second burst; most of the worst-rated ones were a
burst of three people that nobody else joined. The bar grows with the square
root of the chat, so a small chat is asked for a bigger part of itself than a
big one, but never for more than half of it, nor for fewer people than fire a
moment.
"""

from __future__ import annotations

import math
from dataclasses import replace

from kick_clip_hunter import detector
from tests.support.chat import ChatSim, native_emote, people

PER_ROOT = detector.KEEP_LAUGHING_PER_ROOT
MOST_SHARE = detector.KEEP_LAUGHING_MOST_SHARE
LEAST = detector.MIN_ABSOLUTE_UNIQUE


def settings(**changed) -> detector.Settings:
    return replace(detector.recommended(), **changed)


def by_the_root(chatters: int, per_root: float = PER_ROOT) -> int:
    return round(per_root * math.sqrt(chatters))


class TestHowManyHaveToLaugh:
    # Chats where the root is the bar: above the firing floor, below half.
    ROOTED = tuple(chatters for chatters in range(1, 2000) if LEAST < by_the_root(chatters) < chatters * MOST_SHARE)

    def test_the_bar_grows_with_the_root_of_the_chat(self):
        assert self.ROOTED
        for chatters in self.ROOTED:
            assert detector.laughing_needed(chatters) == by_the_root(chatters)

    def test_a_big_chat_is_asked_for_a_smaller_part_of_itself(self):
        small, big = self.ROOTED[0], self.ROOTED[-1]

        assert detector.laughing_needed(big) > detector.laughing_needed(small)
        assert detector.laughing_needed(big) / big < detector.laughing_needed(small) / small

    def test_a_small_chat_is_never_asked_for_more_than_half_of_itself(self):
        for chatters in range(1, 200):
            needed = detector.laughing_needed(chatters)
            assert needed <= max(LEAST, math.ceil(chatters * MOST_SHARE))

    def test_nor_for_fewer_people_than_fire_a_moment(self):
        for chatters in (0, 1, 2, 5, 8):
            assert detector.laughing_needed(chatters) >= LEAST

    def test_nor_than_its_own_firing_floor_where_it_has_one(self):
        own = settings(min_absolute_unique=2)

        assert detector.laughing_needed(4, own) == 2

    def test_a_channel_can_ask_for_more(self):
        chatters = self.ROOTED[-1]

        assert detector.laughing_needed(chatters, settings(keep_laughing_per_root=PER_ROOT * 2)) > (
            detector.laughing_needed(chatters)
        )

    def test_at_zero_every_moment_is_kept(self):
        assert detector.laughing_needed(500, settings(keep_laughing_per_root=0)) == 0

    def test_as_recommended_it_asks_more_than_it_takes_to_fire_in_a_usual_chat(self):
        # Firing is a ten-second burst; what was measured to tell a usable
        # clip from a useless one is how many joined in over the next minute.
        for chatters in (20, 40, 100):
            assert detector.laughing_needed(chatters) > detector._dynamic_min_reaction_unique(chatters)


class TestWhoLaughed:
    def chat(self, **fields) -> ChatSim:
        chat = ChatSim(**fields)
        chat.warm_up()
        return chat

    def count(self, chat: ChatSim, since: float, until: float | None = None, laugh_names=()) -> int:
        return detector.laughing_people(
            chat.channel, since, chat.now if until is None else until, laugh_names, chat.settings
        )

    def test_each_person_laughing_counts_once(self):
        chat = self.chat()
        start = chat.now

        chat.burst(people(4), "xDDD")
        chat.burst(people(2), "xd again")

        assert self.count(chat, start) == 4

    def test_typed_laughs_laughing_emotes_and_laughing_7tv_names_all_count(self):
        chat = self.chat(keywords={"omegalul": detector.EMOTE_MENTION_LAUGH_WEIGHT})
        start = chat.now

        chat.say("typed", "xd")
        chat.say("native", native_emote("KEKW"))
        chat.say("seventv", "OMEGALUL")

        assert self.count(chat, start, laugh_names=["omegalul"]) == 3

    def test_other_emotes_and_ordinary_words_do_not(self):
        chat = self.chat()
        start = chat.now

        chat.say("sad", native_emote("Sadge"))
        chat.say("waving", native_emote("peepoWave"))
        chat.say("talking", "what happened?")

        assert self.count(chat, start) == 0

    def test_a_channels_own_laughing_emotes_count_too(self):
        chat = self.chat(settings=settings(added_laugh_emote_patterns=("restt",)))
        start = chat.now

        chat.say("fan", native_emote("resttR"))

        assert self.count(chat, start) == 1

    def test_only_who_laughed_within_the_stretch_is_counted(self):
        chat = self.chat()
        chat.say("too_early", "xDDD")
        chat.wait(1)
        start = chat.now
        chat.say("on_time", "xDDD")
        chat.wait(1)
        end = chat.now
        chat.wait(1)
        chat.say("too_late", "xDDD")

        assert self.count(chat, start, end) == 1

    def test_an_account_the_detector_ignores_is_not_counted(self):
        chat = self.chat()
        start = chat.now
        bot = next(iter(detector.IGNORED_SENDERS))

        chat.say(bot, "xDDD")

        assert self.count(chat, start) == 0

    def test_a_channel_never_seen_has_nobody_laughing(self):
        assert detector.laughing_people("nobody-home", 0, float("inf")) == 0

    def test_counting_changes_nothing(self):
        chat = self.chat()
        chat.burst(people(4), "xDDD")
        before = list(detector._entries[chat.channel])

        self.count(chat, 0)

        assert list(detector._entries[chat.channel]) == before


def test_a_fired_moment_carries_how_big_its_chat_was():
    chat = ChatSim()
    chat.warm_up(chatters=10)

    assert chat.reactions_needed("xDDD") is not None

    ((_time, spike),) = chat.moments
    assert spike.baseline_unique_senders == 10
