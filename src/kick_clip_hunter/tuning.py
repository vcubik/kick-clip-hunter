"""What of detection and of clips can be set from the dashboard's Settings
page, for all channels or for one of them.

The detector's thresholds, the moment session's limits and the framing of a
clip started out as constants, and the constants are still there: they are
what is recommended. A value set here is laid over them - the ones set for
all channels first, then, for a channel that goes by its own, that
channel's. Only what was really set is stored, so a channel follows the
settings for all channels in everything it was not given a value of its own
for, and everything nobody set follows the code.

A few settings are a channel's alone (`Setting.per_channel`: the messages
that ask for a clip or the bot for something). Those are not on offer for
all channels, and what a channel has of them holds whether or not it goes
by its own settings in the rest. What the bot tells of in chat, and in what
words, can be set for all channels and for one (`Setting.either_way`): a
channel's own value of those holds either way too - the bot is asked into a
chat channel by channel, whatever detection goes by there.

Three things are in this module:

* `Tuning` - everything the service does for a channel that a setting can
  change: the detector's settings, how long a moment stays open, how its
  clip is cut around it, which messages in chat ask for a clip outright,
  and whether the bot writes in the channel's chat, what it tells of there
  and in what words, and what it is asked there (see chat_bot.py). `recommended()` is what the constants say, read
  where they are kept (detector.py, recorder.py, and the moment session's
  below) at the time it is called, and `buffer_seconds` how much of the
  stream its recorder keeps for its clips;
* `SETTINGS` - the ones on offer: what each is called on the page, what it
  may be, and which fields of a Tuning it sets. One setting can set two:
  the lull that ends a moment is also how soon the next one may fire;
* values on their way in and out: checked as they are posted (`cleaned`,
  `check`), read back from where they are stored (`stored`), laid over
  each other (`values`, `tuned`), and written out for the page (`form`).

All of it is pure. Nothing here applies to what has already happened: the
detector takes a channel's settings with every message, and a moment is
seen through under the ones it fired with.
"""

from __future__ import annotations

import json
import math
import string
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from . import detector, recorder

# -- the moment session ------------------------------------------------------

# A detected moment isn't cut into a clip right away. It stays "open" for a
# short while, and if new people keep piling into the reaction after it fired
# (detector.reaction_active) its end is pushed out, so a reaction that really
# does keep going gets one clip covering all of it. Most moments don't earn
# that and close at their base length (pre-roll + trigger window + post-roll,
# about 35s) once MOMENT_SESSION_QUIET_SECONDS pass; MOMENT_SESSION_MAX_SECONDS
# is the hard cap either way. Only then is the clip cut (see main.py).
MOMENT_SESSION_POLL_SECONDS = 3
# 8s was too tight - a normal lull in chat (reading, catching a breath)
# regularly closed the session early, and a fresh burst soon after opened a
# brand new moment with its own 25s pre-roll reaching back into the first
# clip's tail, producing two overlapping clips instead of one continuous
# one. 20s fixed that but, together with a weak sustain bar, left most clips
# running to the cap at around two minutes. 12s is the middle ground now
# that staying "active" takes a fresh crowd-sized reaction (see
# detector.reaction_active) rather than one straggler.
#
# 30s since the dashboard got the chat trace: a longer clip costs little when
# the trace shows where in it chat erupted and the reviewer goes straight
# there. It equals detector.COOLDOWN_SECONDS on purpose, so there is no gap
# between the two - a fresh reaction within 30s of the last one extends this
# clip, and one after a longer lull opens a moment of its own. On the
# Settings page the two are one value for that reason.
MOMENT_SESSION_QUIET_SECONDS = 30
# Hard cap on how long a moment stays open: with the trigger window, pre-roll
# and post-roll around it, the longest possible clip is about 95s.
MOMENT_SESSION_MAX_SECONDS = 60
# The dynamic window already extends over the reaction itself, so the clip
# needs far less trailing padding than a fixed-window cut would.
DYNAMIC_POST_ROLL_SECONDS = 10

# The commands the bot takes in a channel's chat (see chat_bot.py): start a
# recording of the stream, stop it, say how things stand, say what the
# commands are.
RECORD = "record"
STOP = "stop"
STATUS = "status"
HELP = "help"

# How often the bot says the channel's message of its own while the stream
# is on, in minutes, where nothing else was set.
BOT_REPEAT_MINUTES = 30


@dataclass(frozen=True)
class Tuning:
    """Everything a setting can change of what the service does for a channel."""

    detector: detector.Settings
    # The moment session: how long without a fresh reaction closes a moment,
    # and how long after it fired it is closed whatever chat is doing.
    quiet_seconds: float
    max_open_seconds: float
    # The clip (see recorder.extract_clip): how far it reaches either side
    # of the reaction, how much earlier than chat reacted all of it is cut,
    # and how much footage is kept in its file around it.
    pre_roll_seconds: float
    post_roll_seconds: float
    playback_delay_seconds: float
    context_before_seconds: float
    context_after_seconds: float
    # Clips on command: the messages (whole, lowercased) that make a moment
    # there and then when the channel's streamer or one of its moderators
    # posts one - or anyone at all, with the switch on. None to begin with.
    clip_commands: frozenset[str] = frozenset()
    clip_command_from_anyone: bool = False
    # The bot in chat: whether it writes in the channel's chat at all, and
    # what it tells of there by itself - the stream starting, a recording of
    # it starting and ending, a moment caught, a clip made - and the
    # messages (whole, lowercased) the channel's streamer or one of its
    # moderators asks it with: to start a recording of the stream, to stop
    # it, to say how things stand, to say what the commands are. Of what it
    # tells of, only recordings to begin with; no command.
    bot_writes: bool = False
    bot_tells_live: bool = False
    bot_tells_recordings: bool = True
    bot_tells_moments: bool = False
    bot_tells_clips: bool = False
    record_commands: frozenset[str] = frozenset()
    stop_commands: frozenset[str] = frozenset()
    status_commands: frozenset[str] = frozenset()
    help_commands: frozenset[str] = frozenset()
    # What the bot says of each, in words the channel chose - with what is
    # in braces filled in (chat_bot.worded) - or "" for its own. And a
    # message of the channel's own it says every so often while the stream
    # is on: none to begin with.
    bot_live_message: str = ""
    bot_started_message: str = ""
    bot_ended_message: str = ""
    bot_moment_message: str = ""
    bot_clip_message: str = ""
    bot_repeat_message: str = ""
    bot_repeat_minutes: int = BOT_REPEAT_MINUTES

    def is_clip_command(self, content: str) -> bool:
        """Whether a chat message is one of the channel's clip commands.
        Compared whole, like a message to ignore: a command named in the
        middle of a sentence is somebody talking about it."""
        return content.strip().lower() in self.clip_commands

    def bot_command(self, content: str) -> str | None:
        """Which of the bot's commands a chat message is (RECORD, STOP,
        STATUS, HELP), if it is one of the channel's - compared whole, like a
        clip command. A message is in one list at the most (see `check`)."""
        said = content.strip().lower()
        for command, messages in (
            (RECORD, self.record_commands),
            (STOP, self.stop_commands),
            (STATUS, self.status_commands),
            (HELP, self.help_commands),
        ):
            if said in messages:
                return command
        return None

    def framing(self) -> dict[str, float]:
        """How a clip is cut for the channel, as recorder.extract_clip takes it."""
        return {
            "pre_roll_seconds": self.pre_roll_seconds,
            "post_roll_seconds": self.post_roll_seconds,
            "playback_delay_seconds": self.playback_delay_seconds,
            "context_before_seconds": self.context_before_seconds,
            "context_after_seconds": self.context_after_seconds,
        }


def recommended() -> Tuning:
    """What a channel gets when nothing was set: the constants, read where
    they are kept, as they stand when this is called."""
    return Tuning(
        detector=detector.recommended(),
        quiet_seconds=MOMENT_SESSION_QUIET_SECONDS,
        max_open_seconds=MOMENT_SESSION_MAX_SECONDS,
        pre_roll_seconds=recorder.PRE_ROLL_SECONDS,
        post_roll_seconds=DYNAMIC_POST_ROLL_SECONDS,
        playback_delay_seconds=recorder.PLAYBACK_DELAY_SECONDS,
        context_before_seconds=recorder.CONTEXT_BEFORE_SECONDS,
        context_after_seconds=recorder.CONTEXT_AFTER_SECONDS,
    )


def buffer_seconds(tuned: Tuning) -> float:
    """How far back a channel's buffer has to reach for its clips to be cut
    under these settings (see recorder.ChannelRecorder.keep): from the first
    frame of the context before a clip to when its file is cut, with room to
    spare."""
    # A moment fires on the short window's reaction and stays open for up to
    # max_open_seconds, seen to every poll; its clip is not cut before the
    # people laughing around it have been counted, and then waits for its
    # post-roll and the context after it to be broadcast (see main.py).
    open_for = max(tuned.max_open_seconds + MOMENT_SESSION_POLL_SECONDS, detector.KEEP_LAUGHING_SECONDS)
    return (
        tuned.context_before_seconds
        + tuned.pre_roll_seconds
        + tuned.playback_delay_seconds
        + detector.SHORT_WINDOW_SECONDS
        + open_for
        + tuned.post_roll_seconds
        + tuned.context_after_seconds
        + recorder.CLIP_SETTLE_SECONDS
        + recorder.BUFFER_MARGIN_SECONDS
    )


# -- the settings on offer ---------------------------------------------------

# What a setting's value is: a whole number, a number, on or off, a message
# the bot writes, or a list - of parts of emote names, of whole messages, or
# of accounts' names. A list is kept lowercased
# and in alphabetical order, so two that hold the same things are equal. It
# is posted as what it holds, item by item - the page adds them one at a
# time - never as text for the service to split: then a message can have a
# comma in it, and no list has a way of its own of being typed.
WHOLE = "whole"
NUMBER = "number"
SWITCH = "switch"
TEXT = "text"
WORDS = "words"
PHRASES = "phrases"
NAMES = "names"
LISTS = (WORDS, PHRASES, NAMES)
# The lists that are taken as sets of what they hold.
SETS = (PHRASES, NAMES)

Value = bool | int | float | str | tuple[str, ...]

# A setting's field of the detector's settings is written with this in front.
DETECTOR = "detector."

# What a list may hold. A part of an emote name shorter than this is in most
# names there are - the ones built in are this long at the least. No emote's
# name has a space in it, so neither may a part of one.
WORD_MIN_LENGTH = 3
WORD_MAX_LENGTH = 40
PHRASE_MAX_LENGTH = 100
# A YouTube channel's name can be this long; Kick's and Twitch's are shorter.
NAME_MAX_LENGTH = 100
LIST_MAX_ITEMS = 50
# A message the bot writes: as long as a chat takes, on Kick and on Twitch
# (chat_bot.MAX_LENGTH) - what fills in its braces can take it past that,
# and then it is cut short.
TEXT_MAX_LENGTH = 500
# What the field an item is added from says while it is empty.
LIST_PROMPTS = {WORDS: "Part of an emote's name", PHRASES: "A whole message", NAMES: "An account's name"}

# The sections of the page, in the order they come: key, heading, and what is
# said under it. The first is on a channel's page only (see
# Setting.per_channel).
MOMENTS = "moments"
SIGNALS = "signals"
CLIPS = "clips"
COMMANDS = "commands"
BOT = "bot"
MESSAGES = "messages"
GROUPS = (
    (
        COMMANDS,
        "A clip on command",
        "A message in chat that makes a moment there and then, however quiet chat is - for a channel too small "
        "for its chat to set one off. The clip is cut like any other, reaching back from the command. Each "
        "channel has its own, whether or not it goes by its own settings in the rest.",
    ),
    (
        BOT,
        "The bot in chat",
        "The bot can write in a channel's chat - what it tells of by itself, and its answers to the commands the "
        "channel's streamer and moderators post there, nobody else's. What a channel is given of its own here "
        "holds whether or not it goes by its own settings in the rest; the commands are set channel by channel.",
    ),
    (
        MESSAGES,
        "What the bot says",
        "Each message in words of your own. Left empty, it is the bot's own, shown in its field. A word in braces "
        "is filled in as the message goes out; a brace by itself is written twice. What a channel is given of its "
        "own here holds whether or not it goes by its own settings in the rest.",
    ),
    (
        MOMENTS,
        "How big a reaction has to be",
        "Everything is measured against the channel's own chat of the last "
        f"{detector.BASELINE_WINDOW_SECONDS // 60} minutes. Moments are meant to be rare.",
    ),
    (
        SIGNALS,
        "What counts as a reaction",
        "Three signs are listened for. A sender counts once for each, with the strongest thing they said.",
    ),
    (
        CLIPS,
        "The clip",
        "How long a moment may run and how much footage goes into its file. Clips cut already stay as they are.",
    ),
)


@dataclass(frozen=True)
class Setting:
    """One thing that can be set: what it is called and may be, and which
    fields of a Tuning take its value (the detector's with "detector." in
    front). `advanced` ones are folded away on the page. A `per_channel`
    one is set for each channel by itself: it is not on offer for all
    channels, and a channel's value holds whether or not the channel goes
    by its own settings in the rest - as does an `either_way` one's, which
    can be set for all channels as well. A message the bot writes (TEXT)
    may have in braces the words of `fills`, and is `built_in` - an example
    of the bot's own words - while it is left empty."""

    name: str
    group: str
    kind: str
    label: str
    caption: str
    sets: tuple[str, ...]
    least: float | None = None
    most: float | None = None
    unit: str = ""
    advanced: bool = False
    per_channel: bool = False
    either_way: bool = False
    fills: tuple[str, ...] = ()
    built_in: str = ""


_WINDOW = detector.SHORT_WINDOW_SECONDS
# The shortest the lull that ends a moment may be. It is also how soon after
# a moment the next one may fire, and the laughs that came right after a
# moment fired stay in the detector's window for as long as that window is:
# a cooldown no longer than it would let them fire a second moment of their
# own once it ran out.
LULL_MIN_SECONDS = _WINDOW + 5
_BUILT_IN_LAUGHING = ", ".join(detector.LAUGH_EMOTE_NAME_PATTERNS) + "".join(
    f", and an emote called just {name}" for name in detector.LAUGH_EMOTE_WHOLE_NAMES
)
_BUILT_IN_DANCING = ", ".join(detector.DANCE_EMOTE_NAME_PATTERNS)

SETTINGS: tuple[Setting, ...] = (
    Setting(
        "reacting_share", MOMENTS, NUMBER,
        "Share of chat that has to react",
        f"Of the people who chatted lately, the part that has to react within {_WINDOW} seconds: 0.1 is one in ten. "
        "This is what decides how many moments there are - higher means fewer.",
        (DETECTOR + "min_reaction_unique_fraction",),
        least=0.01, most=1,
    ),
    Setting(
        "reacting_least", MOMENTS, WHOLE,
        "At least this many people",
        "However small the chat, fewer people reacting than this are never a moment.",
        (DETECTOR + "min_absolute_unique",),
        least=1, most=50,
    ),
    Setting(
        "reacting_most", MOMENTS, WHOLE,
        "At most this many people",
        "However big the chat, more than this are never asked for. Raise it for a very big chat, "
        "where this many people are only a small part of it.",
        (DETECTOR + "min_reaction_unique_ceiling",),
        least=1, most=1000,
    ),
    Setting(
        "laughing_share", MOMENTS, NUMBER,
        "Share of chat that has to laugh to keep a moment",
        f"Once a moment has closed, it is kept only if this part of the people who chatted lately laughed "
        f"from {_WINDOW} seconds before it fired to {detector.KEEP_LAUGHING_SECONDS} seconds after - never "
        f"more than {detector.KEEP_LAUGHING_MOST} people, never fewer than the least above. At 0 every moment "
        "is kept.",
        (DETECTOR + "keep_laughing_fraction",),
        least=0, most=1,
    ),
    Setting(
        "lull_seconds", MOMENTS, WHOLE,
        "Lull that ends a moment",
        "A moment closes once chat has gone this long without a fresh reaction, and nothing new fires sooner "
        "than this after one. A reaction that comes back within it extends the clip; a later one gets a clip "
        "of its own.",
        (DETECTOR + "cooldown_seconds", "quiet_seconds"),
        least=LULL_MIN_SECONDS, most=90, unit="seconds",
    ),
    Setting(
        "emote_weight_share", MOMENTS, NUMBER,
        "Emote weight to reach, as a share of chat",
        f"The emotes posted within {_WINDOW} seconds, weighed as set below, have to add up to this share of the "
        "people who chatted lately.",
        (DETECTOR + "min_absolute_count_fraction",),
        least=0.01, most=5, advanced=True,
    ),
    Setting(
        "emote_weight_least", MOMENTS, WHOLE,
        "Emote weight to reach, at least",
        "However small the chat. With the weights as recommended, 6 is six people posting an ordinary emote, "
        "or two posting a laughing one.",
        (DETECTOR + "min_absolute_count_floor",),
        least=1, most=200, advanced=True,
    ),
    Setting(
        "emote_weight_most", MOMENTS, WHOLE,
        "Emote weight to reach, at most",
        "However big the chat.",
        (DETECTOR + "min_absolute_count_ceiling",),
        least=1, most=2000, advanced=True,
    ),
    Setting(
        "laugh_multiplier", MOMENTS, NUMBER,
        "Laughs, times the usual",
        "How many times the channel's own usual rate a burst of laughs has to be - the laughs, and the "
        "people laughing. In a lively chat nearly any cluster clears it; the share above is what holds.",
        (DETECTOR + "laugh_multiplier", DETECTOR + "laugh_unique_sender_multiplier"),
        least=1, most=20, advanced=True,
    ),
    Setting(
        "emote_multiplier", MOMENTS, NUMBER,
        "Kick emotes, times the usual",
        "The same for Kick's own emotes.",
        (DETECTOR + "emote_spike_multiplier",),
        least=1, most=20, advanced=True,
    ),
    Setting(
        "mention_multiplier", MOMENTS, NUMBER,
        "7TV emote names, times the usual",
        "The same for 7TV emotes typed by name.",
        (DETECTOR + "emote_mention_multiplier", DETECTOR + "emote_mention_unique_sender_multiplier"),
        least=1, most=20, advanced=True,
    ),
    Setting(
        "sustain_fraction", MOMENTS, NUMBER,
        "Reaction needed to extend a clip",
        "As a part of what fires a moment. At 1, the people who react after a moment has fired have to be "
        "enough to have fired one themselves; lower, and clips are extended more easily.",
        (DETECTOR + "sustain_fraction",),
        least=0.1, most=3, advanced=True,
    ),
    Setting(
        "laugh_signal", SIGNALS, SWITCH,
        "Laughs",
        "\"xd\" at any length, and Kick's own laughing emotes.",
        (DETECTOR + "laugh_enabled",),
    ),
    Setting(
        "emote_signal", SIGNALS, SWITCH,
        "Kick emotes",
        "Kick's own emotes posted in chat, other than the laughing ones - those are laughs.",
        (DETECTOR + "emotes_enabled",),
    ),
    Setting(
        "mention_signal", SIGNALS, SWITCH,
        "7TV emote names",
        "The channel's 7TV emotes, which arrive as their names typed out. Worth switching off where those "
        "names are ordinary words.",
        (DETECTOR + "emote_mention_enabled",),
    ),
    Setting(
        "laugh_strong_weight", SIGNALS, NUMBER,
        "Weight of an exaggerated laugh",
        '"xDD" and longer.',
        (DETECTOR + "laugh_strong_weight",),
        least=0, most=20,
    ),
    Setting(
        "laugh_weak_weight", SIGNALS, NUMBER,
        "Weight of a bare laugh",
        '"xd" and no more.',
        (DETECTOR + "laugh_weak_weight",),
        least=0, most=20,
    ),
    Setting(
        "laugh_emote_weight", SIGNALS, NUMBER,
        "Weight of a laughing emote",
        "An emote whose name says laughing, whether Kick's or 7TV's.",
        (DETECTOR + "emote_laugh_weight", DETECTOR + "emote_mention_laugh_weight"),
        least=0, most=20,
    ),
    Setting(
        "other_emote_weight", SIGNALS, NUMBER,
        "Weight of any other emote",
        "At 0 only laughing emotes count.",
        (DETECTOR + "emote_other_weight", DETECTOR + "emote_mention_other_weight"),
        least=0, most=20,
    ),
    Setting(
        "laugh_emote_words", SIGNALS, WORDS,
        "More laughing emotes",
        "Parts of emote names. An emote whose name has one of them in it counts as laughing, as do the ones "
        f"built in: {_BUILT_IN_LAUGHING}.",
        (DETECTOR + "added_laugh_emote_patterns",),
    ),
    Setting(
        "ignored_emote_words", SIGNALS, WORDS,
        "Emotes to ignore",
        "Parts of emote names. An emote whose name has one of them in it is no reaction at all, like the "
        f"dancing ones built in: {_BUILT_IN_DANCING}.",
        (DETECTOR + "added_ignored_emote_patterns",),
    ),
    Setting(
        "ignored_messages", SIGNALS, PHRASES,
        "Messages to ignore",
        "Whole messages that the detector leaves out the way it does a bare 1 or 2 answering a poll: a "
        "giveaway's keyword, a bot's command.",
        (DETECTOR + "added_ignored_messages",),
    ),
    Setting(
        "ignored_senders", SIGNALS, NAMES,
        "Accounts to ignore",
        "Chat bots, whose messages the detector leaves out altogether: they post on a timer and answer "
        "commands, which is no reaction. The well-known ones are in to begin with - take one out if a person "
        "goes by that name, and add a channel's own bot.",
        (DETECTOR + "ignored_senders",),
    ),
    Setting(
        "longest_seconds", CLIPS, WHOLE,
        "Longest a moment stays open",
        "A reaction that keeps drawing in new people extends its clip, for up to this long after it fired.",
        ("max_open_seconds",),
        least=20, most=120, unit="seconds",
    ),
    Setting(
        "pre_roll_seconds", CLIPS, WHOLE,
        "Clip starts this long before the reaction",
        "Chat reacts to what has already happened, so the clip reaches back from the first sign of it.",
        ("pre_roll_seconds",),
        least=0, most=60, unit="seconds",
    ),
    Setting(
        "post_roll_seconds", CLIPS, WHOLE,
        "Clip runs on this long after the reaction",
        "Counted from the last time chat was still reacting.",
        ("post_roll_seconds",),
        least=0, most=60, unit="seconds",
    ),
    Setting(
        "context_before_seconds", CLIPS, WHOLE,
        "Footage kept before the clip",
        "In the clip's file, for the lead-up.",
        ("context_before_seconds",),
        least=0, most=120, unit="seconds", advanced=True,
    ),
    Setting(
        "context_after_seconds", CLIPS, WHOLE,
        "Footage kept after the clip",
        "In the clip's file, for what followed. A clip is cut once this has been broadcast, so it is ready "
        "that much later.",
        ("context_after_seconds",),
        least=0, most=120, unit="seconds", advanced=True,
    ),
    Setting(
        "playback_delay_seconds", CLIPS, WHOLE,
        "Clip is cut this much earlier than chat reacted",
        "Viewers see the stream some seconds late and react later still; the whole clip is moved back by "
        "this to make up for it.",
        ("playback_delay_seconds",),
        least=0, most=30, unit="seconds", advanced=True,
    ),
    Setting(
        "clip_commands", COMMANDS, PHRASES,
        "Messages that ask for a clip",
        "Whole messages: !clip, for one. Posted by the channel's streamer or one of its moderators, any of them "
        "makes a moment. The channel has to be tracked, and the detector leaves these messages out.",
        ("clip_commands",),
        per_channel=True,
    ),
    Setting(
        "clip_command_from_anyone", COMMANDS, SWITCH,
        "Anyone in chat may ask",
        "Off, only the streamer and the moderators are listened to. On, every viewer can have a clip made, as "
        "often as they like.",
        ("clip_command_from_anyone",),
        per_channel=True,
    ),
    Setting(
        "bot_writes", BOT, SWITCH,
        "Write in chat",
        "The bot says in chat what is switched on below, and answers the commands. Off, it writes nothing there, "
        "and the commands are carried out without an answer.",
        ("bot_writes",),
        either_way=True,
    ),
    Setting(
        "bot_tells_live", BOT, SWITCH,
        "Say when the stream starts",
        "Once the channel is seen live - not for a stream that was on already when the service started.",
        ("bot_tells_live",),
        either_way=True,
    ),
    Setting(
        "bot_tells_recordings", BOT, SWITCH,
        "Say when a recording starts and ends",
        "A recording of the whole stream: when it starts, whoever started it, and when it ends, why and how much "
        "of the stream it holds.",
        ("bot_tells_recordings",),
        either_way=True,
    ),
    Setting(
        "bot_tells_moments", BOT, SWITCH,
        "Say when a moment is caught",
        "As soon as chat sets one off or a clip is asked for, before its clip is cut. One that chat set off can "
        "still be thrown away once it closes, if too few people laughed around it.",
        ("bot_tells_moments",),
        either_way=True,
    ),
    Setting(
        "bot_tells_clips", BOT, SWITCH,
        "Say when a clip is made",
        "Once a clip's file is saved, the bot says so in chat, and how far into the stream the clip is.",
        ("bot_tells_clips",),
        either_way=True,
    ),
    Setting(
        "record_commands", BOT, PHRASES,
        "Messages that start a recording",
        "Whole messages: !record start, for one. Any of them starts a recording of the whole stream, as the button "
        "on the Channels page does - whether or not the channel is tracked.",
        ("record_commands",),
        per_channel=True,
    ),
    Setting(
        "stop_commands", BOT, PHRASES,
        "Messages that stop a recording",
        "The same, !record stop for one. The recording ends and is saved; a channel that is recorded always is "
        "recorded again from its next stream.",
        ("stop_commands",),
        per_channel=True,
    ),
    Setting(
        "status_commands", BOT, PHRASES,
        "Messages that ask how things stand",
        "The same, !status for one. The bot answers whether it looks for clips and how many it made of this "
        "stream, and whether the stream is being recorded and for how long.",
        ("status_commands",),
        per_channel=True,
    ),
    Setting(
        "help_commands", BOT, PHRASES,
        "Messages that ask for the commands",
        "The same, !commands for one. The bot answers with the channel's commands, and what each does.",
        ("help_commands",),
        per_channel=True,
    ),
    Setting(
        "bot_live_message", MESSAGES, TEXT,
        "When the stream starts",
        "Said where the bot says when the stream starts.",
        ("bot_live_message",),
        either_way=True, fills=("channel",), built_in="Watching chat for clips this stream.",
    ),
    Setting(
        "bot_started_message", MESSAGES, TEXT,
        "When a recording starts",
        "Also when the stream is not on yet, and is recorded from when it starts.",
        ("bot_started_message",),
        either_way=True, fills=("channel",), built_in="Recording the stream from now on.",
    ),
    Setting(
        "bot_ended_message", MESSAGES, TEXT,
        "When a recording ends",
        "{reason} is why it ended, as a sentence - \"The stream is over.\" for one - and {length} how much of the "
        "stream it holds.",
        ("bot_ended_message",),
        either_way=True, fills=("channel", "reason", "length"),
        built_in="Recording stopped. 1 hour 5 minutes of the stream recorded.",
    ),
    Setting(
        "bot_moment_message", MESSAGES, TEXT,
        "When a moment is caught",
        "{stream_time} is how far into the stream, as 1:23:45 - empty where that is not known - and {by} who "
        "asked for a clip, empty where chat set the moment off.",
        ("bot_moment_message",),
        either_way=True, fills=("channel", "stream_time", "by"), built_in="Moment caught at 1:23:45 into the stream.",
    ),
    Setting(
        "bot_clip_message", MESSAGES, TEXT,
        "When a clip is made",
        "{length} is how long the clip is, {stream_time} how far into the stream chat reacted - empty where that "
        "is not known.",
        ("bot_clip_message",),
        either_way=True, fills=("channel", "length", "stream_time"),
        built_in="Clip saved: 45 seconds, at 1:23:45 into the stream.",
    ),
    Setting(
        "bot_repeat_message", MESSAGES, TEXT,
        "Every so often",
        "A message of your own, said again and again while the stream is on - a link, a rule of the chat. Left "
        "empty, nothing is.",
        ("bot_repeat_message",),
        either_way=True, fills=("channel",), built_in="Nothing",
    ),
    Setting(
        "bot_repeat_minutes", MESSAGES, WHOLE,
        "How often",
        "The message above, every this many minutes - the first time this long after the stream started.",
        ("bot_repeat_minutes",),
        least=5, most=240, unit="minutes", either_way=True,
    ),
)

BY_NAME: dict[str, Setting] = {setting.name: setting for setting in SETTINGS}
# The settings that are a channel's alone.
PER_CHANNEL = frozenset(setting.name for setting in SETTINGS if setting.per_channel)
# The settings of which a channel's own value holds whether or not it goes by
# its own settings in the rest: those that are a channel's alone, and the
# bot's that can be set for all channels too.
EITHER_WAY = frozenset(setting.name for setting in SETTINGS if setting.per_channel or setting.either_way)

# Pairs of settings that only make sense one way round: the first may not be
# more than the second - or, where the third says so, has to be less.
RULES = (
    ("reacting_least", "reacting_most", False),
    ("emote_weight_least", "emote_weight_most", False),
    # Otherwise every moment would run to its cap, and the lull would mean
    # nothing.
    ("lull_seconds", "longest_seconds", True),
)
# The lists of messages that are commands. A message can be one command at
# the most: in two lists, it would be carried out as whichever came first.
COMMAND_LISTS = ("clip_commands", "record_commands", "stop_commands", "status_commands", "help_commands")


# -- values ------------------------------------------------------------------


def _field(tuning: Tuning, path: str) -> Any:
    if path.startswith(DETECTOR):
        return getattr(tuning.detector, path.removeprefix(DETECTOR))
    return getattr(tuning, path)


def recommended_values() -> dict[str, Value]:
    """Every setting's value when nothing was set."""
    tuning = recommended()
    found: dict[str, Value] = {}
    for setting in SETTINGS:
        value = _field(tuning, setting.sets[0])
        found[setting.name] = tuple(sorted(value)) if setting.kind in LISTS else value
    return found


def shared(given: Mapping[str, Value]) -> dict[str, Value]:
    """Those of the given values that can be set for all channels."""
    return {name: value for name, value in given.items() if name not in PER_CHANNEL}


def per_channel(given: Mapping[str, Value]) -> dict[str, Value]:
    """Those of the given values that are a channel's alone."""
    return {name: value for name, value in given.items() if name in PER_CHANNEL}


def either_way(given: Mapping[str, Value]) -> dict[str, Value]:
    """Those of the given values that hold of a channel's own either way:
    what is in force of them while it goes by the settings for all channels."""
    return {name: value for name, value in given.items() if name in EITHER_WAY}


def values(for_all: Mapping[str, Value], own: Mapping[str, Value] | None = None) -> dict[str, Value]:
    """Every setting's value for a channel: what is recommended, with what
    was set for all channels laid over it, then the channel's `own`. A
    setting that is a channel's alone takes nothing from all channels."""
    return {**recommended_values(), **shared(for_all), **(own or {})}


def tuned(for_all: Mapping[str, Value], own: Mapping[str, Value] | None = None) -> Tuning:
    """What the service works from for a channel with these values set.

    Only a setting that was given a value changes anything: the rest of a
    Tuning stays what its own constant says, also where one setting stands
    for two of them."""
    base = recommended()
    fields: dict[str, Any] = {}
    detector_fields: dict[str, Any] = {}
    for name, value in {**shared(for_all), **(own or {})}.items():
        setting = BY_NAME[name]
        if setting.kind in SETS:
            value = frozenset(value)
        for path in setting.sets:
            if path.startswith(DETECTOR):
                detector_fields[path.removeprefix(DETECTOR)] = value
            else:
                fields[path] = value
    return replace(base, detector=replace(base.detector, **detector_fields), **fields)


def _number_words(value: float) -> str:
    return f"{value:g}"


def _range_words(setting: Setting) -> str:
    """What a number may be: "a whole number from 15 to 90"."""
    kind = "a whole number" if setting.kind == WHOLE else "a number"
    return f"{kind} from {_number_words(setting.least)} to {_number_words(setting.most)}"


def _items(setting: Setting, raw: object) -> list[str]:
    """What a list holds, as it was posted or stored: each of its items,
    lowercased and without the spaces around it, an empty one left out - and
    an account's name without the "@" a YouTube handle is written with."""
    if not isinstance(raw, (list, tuple)) or not all(isinstance(item, str) for item in raw):
        raise ValueError(f'"{setting.label}" has to be a list of words.')
    if setting.kind == NAMES:
        raw = [detector.sender_key(item) for item in raw]
    return [item.strip().lower() for item in raw if item.strip()]


def fills_words(setting: Setting) -> str:
    """What a message may have in braces, as the page says it."""
    return ", ".join(f"{{{name}}}" for name in setting.fills)


def _check_fills(setting: Setting, text: str) -> None:
    """Raises a ValueError, in words for the page, if what a message has in
    braces is not all words it can fill in - nothing more in them than the
    word, and every brace of its own written twice."""
    label = f'"{setting.label}"'
    try:
        parts = list(string.Formatter().parse(text))
    except ValueError:
        raise ValueError(f"{label}: a brace that is not around a word is written twice, {{{{ or }}}}.") from None
    for _, name, spec, conversion in parts:
        if name is None:
            continue
        if name not in setting.fills or spec or conversion:
            raise ValueError(f"{label}: {{{name}}} is not a word it fills in. It fills in {fills_words(setting)}.")


def clean(name: str, raw: object) -> Value:
    """A value as it was posted or stored, as the value the setting takes -
    or a ValueError that says, in words for the page, why it can't be."""
    setting = BY_NAME.get(name)
    if setting is None:
        raise ValueError(f"There is no setting called {name!r}.")
    label = f'"{setting.label}"'

    if setting.kind == SWITCH:
        if not isinstance(raw, bool):
            raise ValueError(f"{label} has to be on or off.")
        return raw

    if setting.kind == TEXT:
        if not isinstance(raw, str):
            raise ValueError(f"{label} has to be a message.")
        # One line, as a chat message is.
        text = " ".join(raw.split())
        if len(text) > TEXT_MAX_LENGTH:
            raise ValueError(f"{label} can be {TEXT_MAX_LENGTH} characters long at the most.")
        _check_fills(setting, text)
        return text

    if setting.kind in LISTS:
        items = sorted(set(_items(setting, raw)))
        if len(items) > LIST_MAX_ITEMS:
            raise ValueError(f"{label} can hold {LIST_MAX_ITEMS} at the most.")
        if setting.kind == WORDS:
            spaced = [item for item in items if any(character.isspace() for character in item)]
            if spaced:
                raise ValueError(f"{label}: {spaced[0]!r} has to be one word - no emote's name has a space in it.")
            wrong = [item for item in items if not WORD_MIN_LENGTH <= len(item) <= WORD_MAX_LENGTH]
            if wrong:
                raise ValueError(
                    f"{label}: {wrong[0]!r} has to be {WORD_MIN_LENGTH} to {WORD_MAX_LENGTH} characters long."
                )
        elif setting.kind == NAMES:
            if any(len(item) > NAME_MAX_LENGTH for item in items):
                raise ValueError(f"{label}: a name can be {NAME_MAX_LENGTH} characters long at the most.")
        elif any(len(item) > PHRASE_MAX_LENGTH for item in items):
            raise ValueError(f"{label}: a message can be {PHRASE_MAX_LENGTH} characters long at the most.")
        return tuple(items)

    whole = setting.kind == WHOLE
    if (
        isinstance(raw, bool)
        or not isinstance(raw, (int, float))
        or not math.isfinite(raw)
        or (whole and float(raw) != int(raw))
        or not setting.least <= raw <= setting.most
    ):
        raise ValueError(f"{label} has to be {_range_words(setting)}.")
    return int(raw) if whole else float(raw)


def cleaned(raw: Mapping[str, object]) -> dict[str, Value]:
    """Several values at once (see `clean`); the first that can't be taken
    is what the ValueError is about."""
    return {name: clean(name, value) for name, value in raw.items()}


def check(every: Mapping[str, Value]) -> None:
    """Raises a ValueError if a channel's values (all of them, see `values`)
    contradict each other."""
    for lower, upper, strictly in RULES:
        low, high = BY_NAME[lower].label, BY_NAME[upper].label
        if strictly and every[lower] >= every[upper]:
            raise ValueError(f'"{high}" has to be more than "{low}".')
        if every[lower] > every[upper]:
            raise ValueError(f'"{low}" cannot be more than "{high}".')
    for index, first in enumerate(COMMAND_LISTS):
        for second in COMMAND_LISTS[index + 1 :]:
            both = sorted(set(every[first]) & set(every[second]))
            if both:
                raise ValueError(
                    f'{both[0]!r} cannot be in both "{BY_NAME[first].label}" and "{BY_NAME[second].label}".'
                )


def differing(given: Mapping[str, Value], rest: Mapping[str, Value]) -> dict[str, Value]:
    """Those of the given values that are not what the setting would be
    anyway (`rest`) - which is what there is to store."""
    return {name: value for name, value in given.items() if value != rest[name]}


def encoded(given: Mapping[str, Value]) -> dict[str, str]:
    """Values as they are stored: JSON, by name."""
    return {name: json.dumps(list(value) if isinstance(value, tuple) else value) for name, value in given.items()}


def stored(texts: Mapping[str, str]) -> dict[str, Value]:
    """Values read back from where they are stored. One that can no longer
    be taken - a setting that is gone, a value outside what is allowed now -
    is left out, so the setting falls back on what it would be without it."""
    read: dict[str, Value] = {}
    for name, text in texts.items():
        try:
            read[name] = clean(name, json.loads(text))
        except ValueError:
            continue
    return read


def words(setting: Setting, value: Value) -> str:
    """A value as the page writes it: in a field, and where it names what a
    field would be if it were left alone - a list as what it holds, one
    item after another."""
    if setting.kind == SWITCH:
        return "On" if value else "Off"
    if setting.kind in LISTS:
        return ", ".join(value)
    if setting.kind == TEXT:
        return value
    return _number_words(value)


def _form_field(setting: Setting, value: Value, rest: Value, rest_label: str) -> dict[str, Any]:
    numeric = setting.kind in (WHOLE, NUMBER)
    text = setting.kind == TEXT
    caption = setting.caption
    if text:
        caption = f"{caption} It fills in {fills_words(setting)}."
    field = {
        "name": setting.name,
        "kind": setting.kind,
        "label": setting.label,
        "caption": caption,
        "value": words(setting, value),
        "on": value is True,
        "rest": words(setting, rest),
        "rest_words": f"{rest_label}: {words(setting, rest) or ('its own words' if text else 'none')}",
        "at_rest": value == rest,
        # What may be typed into the field, for its tooltip.
        "range_words": _range_words(setting).capitalize() if numeric else None,
        "unit": setting.unit,
        "advanced": setting.advanced,
    }
    if setting.kind in LISTS:
        # A list is drawn as what it holds, an item at a time, and added to
        # from a field of its own - an item at a time, which the page checks
        # the way `clean` would before it takes it.
        of_words = setting.kind == WORDS
        field.update(
            entries=list(value),
            rest_entries=list(rest),
            shortest=WORD_MIN_LENGTH if of_words else 1,
            longest={WORDS: WORD_MAX_LENGTH, PHRASES: PHRASE_MAX_LENGTH, NAMES: NAME_MAX_LENGTH}[setting.kind],
            most_items=LIST_MAX_ITEMS,
            prompt=LIST_PROMPTS[setting.kind],
        )
    if text:
        # Typed whole into a field of its own, which shows the bot's own
        # words while it is empty.
        field.update(longest=TEXT_MAX_LENGTH, prompt=setting.built_in)
    return field


def form(
    every: Mapping[str, Value], rest: Mapping[str, Value], rest_label: str, for_channel: bool = False
) -> list[dict[str, Any]]:
    """The Settings page's form: its sections in order, and in each the
    settings as the page draws them - what each is set to, and what it would
    be if it were left alone (`rest`: what is recommended, or for a channel
    what all channels have), which a button beside it puts back. The
    advanced ones are listed apart; the page folds them away unless one of
    them is set to something. The page shows one section at a time, so each
    counts how many of its settings are set to something else (`set_count`),
    for its tab to say.

    The settings that are a channel's alone are in a channel's form only
    (`for_channel`), where what they would be if left alone is what is
    recommended. A section of nothing but those and the ones that hold of a
    channel's own either way says so (`either_way`), since it can be set
    whether or not the channel goes by its own settings. On a channel's
    page those come first, being what can be set there whatever it goes by;
    for all channels, after detection's, which is what the page is for."""
    sections = []
    for key, title, lead in GROUPS:
        offered = [setting for setting in SETTINGS if setting.group == key and (for_channel or not setting.per_channel)]
        if not offered:
            continue
        fields = [
            _form_field(
                setting,
                every[setting.name],
                rest[setting.name],
                "Recommended" if setting.per_channel else rest_label,
            )
            for setting in offered
        ]
        advanced = [field for field in fields if field["advanced"]]
        sections.append(
            {
                "key": key,
                "title": title,
                "lead": lead,
                "fields": [field for field in fields if not field["advanced"]],
                "advanced": advanced,
                "advanced_set": any(not field["at_rest"] for field in advanced),
                "set_count": sum(not field["at_rest"] for field in fields),
                "either_way": all(setting.name in EITHER_WAY for setting in offered),
            }
        )
    if not for_channel:
        sections.sort(key=lambda section: section["either_way"])
    return sections

