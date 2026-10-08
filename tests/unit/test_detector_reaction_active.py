"""`reaction_active`: is an already-detected moment still going?

The moment session in main.py polls this to decide whether to push a clip's
end out. The rule under test: a clip is only extended by people who react
*after* the moment fired, and there have to be enough of them that they
would have fired a moment of their own.
"""

from __future__ import annotations

from kick_clip_hunter import detector
from tests.support.chat import ChatSim, people

WINDOW = detector.SHORT_WINDOW_SECONDS
LAUGH = "xDDD"


def chat_with_a_moment() -> tuple[ChatSim, float]:
    """A warmed-up channel on which a moment has fired this very instant -
    the laugh that tipped it over is the last message so far. Returns the
    chat and the clock time the moment fired at."""
    chat = ChatSim()
    chat.warm_up()
    assert chat.reactions_needed(LAUGH) is not None
    assert chat.moment_times == [chat.now]
    return chat, chat.now


def sustaining_crowd() -> int:
    """How many fresh people it takes to keep a moment open on that chat."""
    return max(
        detector.MIN_SUSTAIN_UNIQUE,
        round(detector._dynamic_min_reaction_unique(10) * detector.SUSTAIN_FRACTION),
    )


def test_a_channel_that_was_never_seen_is_not_reacting():
    assert detector.reaction_active("nobody-home") is False


def test_ordinary_chatter_is_not_a_reaction():
    chat = ChatSim()
    chat.warm_up()

    assert chat.reaction_active() is False


def test_the_window_still_holds_the_laughs_that_fired_the_moment():
    # Without `since`, the triggering laughs themselves read as "still going".
    chat, _fired_at = chat_with_a_moment()

    assert chat.reaction_active() is True


def test_those_same_laughs_do_not_count_as_the_reaction_continuing():
    # ...which is why the moment session passes the trigger time: before it
    # did, every clip was extended by its own trigger.
    chat, fired_at = chat_with_a_moment()

    assert chat.reaction_active(since=fired_at) is False


def test_new_people_piling_in_after_the_trigger_keep_it_open():
    chat, fired_at = chat_with_a_moment()
    chat.wait(2)

    chat.burst(people(sustaining_crowd(), prefix="latecomer"), LAUGH)

    assert chat.reaction_active(since=fired_at) is True


def test_one_fewer_than_that_is_not_enough():
    chat, fired_at = chat_with_a_moment()
    chat.wait(2)

    chat.burst(people(sustaining_crowd() - 1, prefix="latecomer"), LAUGH)

    assert chat.reaction_active(since=fired_at) is False


def test_a_single_straggler_cannot_hold_a_moment_open():
    # The cause of the routinely two-minute clips: one "xd" every so often
    # used to be enough.
    chat, fired_at = chat_with_a_moment()

    for _ in range(6):
        chat.wait(WINDOW / 2)
        chat.say("straggler", "xd")
        assert chat.reaction_active(since=fired_at) is False


def test_the_people_who_fired_it_laughing_again_do_count():
    # `since` is about time, not identity: the original crowd reacting to a
    # second punchline is a continuing reaction.
    chat, fired_at = chat_with_a_moment()
    chat.wait(3)

    chat.burst(people(8), LAUGH)

    assert chat.reaction_active(since=fired_at) is True


def test_it_ends_once_the_window_has_emptied():
    chat, fired_at = chat_with_a_moment()
    chat.wait(1)
    chat.burst(people(sustaining_crowd(), prefix="latecomer"), LAUGH)
    assert chat.reaction_active(since=fired_at) is True

    chat.wait(WINDOW + 1)

    assert chat.reaction_active(since=fired_at) is False
    assert chat.reaction_active() is False


def test_it_is_read_only():
    chat, fired_at = chat_with_a_moment()
    entries_before = list(detector._entries[chat.channel])
    cooldown_before = dict(detector._last_moment_at)

    for _ in range(5):
        chat.reaction_active()
        chat.reaction_active(since=fired_at)

    assert list(detector._entries[chat.channel]) == entries_before
    assert detector._last_moment_at == cooldown_before


def test_it_defaults_to_the_real_clock(monkeypatch):
    # The service calls it without `now`; entries are stamped with
    # time.monotonic(), so that is the clock it has to read.
    chat, _fired_at = chat_with_a_moment()

    monkeypatch.setattr(detector.time, "monotonic", lambda: chat.now)
    assert detector.reaction_active(chat.channel) is True

    monkeypatch.setattr(detector.time, "monotonic", lambda: chat.now + WINDOW + 1)
    assert detector.reaction_active(chat.channel) is False
