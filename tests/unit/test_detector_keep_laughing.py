"""Whether a closed moment is kept: how many people laughed around it
(`laughing_people`), and how many it takes (`laughing_needed`).

A moment fires on a ten-second burst; most of the worst-rated ones were a
burst of three people that nobody else joined. The bar scales with the chat
like the firing floor does, and a small chat is never held to more than
that floor.
"""

from __future__ import annotations

from dataclasses import replace

from kick_clip_hunter import detector
from tests.support.chat import ChatSim, native_emote, people

FRACTION = detector.KEEP_LAUGHING_FRACTION
MOST = detector.KEEP_LAUGHING_MOST
LEAST = detector.MIN_ABSOLUTE_UNIQUE


def settings(**changed) -> detector.Settings:
    return replace(detector.recommended(), **changed)


def chat_size_asking_for(people_needed: int) -> int:
    """A chat whose share comes to exactly `people_needed`."""
    return round(people_needed / FRACTION)


class TestHowManyHaveToLaugh:
    def test_the_bar_is_a_share_of_the_chat(self):
        needed = LEAST + 1
        assert needed < MOST

        assert detector.laughing_needed(chat_size_asking_for(needed)) == needed

    def test_a_small_chat_is_never_held_to_more_than_the_firing_floor(self):
        tiny = chat_size_asking_for(1)

        assert detector.laughing_needed(tiny) == LEAST
        assert detector.laughing_needed(0) == LEAST

    def test_nor_to_more_than_its_own_floor_where_it_has_one(self):
        own = settings(min_absolute_unique=2)

        assert detector.laughing_needed(chat_size_asking_for(1), own) == 2

    def test_a_big_chat_is_never_asked_for_more_than_the_most(self):
        assert detector.laughing_needed(chat_size_asking_for(MOST * 10)) == MOST

    def test_a_channel_can_ask_for_a_bigger_share(self):
        chat = chat_size_asking_for(LEAST)

        assert detector.laughing_needed(chat, settings(keep_laughing_fraction=FRACTION * 2)) > LEAST

    def test_at_zero_every_moment_is_kept(self):
        assert detector.laughing_needed(500, settings(keep_laughing_fraction=0)) == 0


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
