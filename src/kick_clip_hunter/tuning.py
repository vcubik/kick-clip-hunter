"""What of detection and of clips can be set from the dashboard's Settings
page, for all channels or for one of them.

The detector's thresholds, the moment session's limits and the framing of a
clip started out as constants, and the constants are still there: they are
what is recommended. A value set here is laid over them - the ones set for
all channels first, then, for a channel that goes by its own, that
channel's. Only what was really set is stored, so a channel follows the
settings for all channels in everything it was not given a value of its own
for, and everything nobody set follows the code.

Three things are in this module:

* `Tuning` - everything the service does for a channel that a setting can
  change: the detector's settings, how long a moment stays open and how its
  clip is cut around it. `recommended()` is what the constants say, read
  where they are kept (detector.py, recorder.py, and the moment session's
  below) at the time it is called;
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


# -- the settings on offer ---------------------------------------------------

# What a setting's value is: a whole number, a number, on or off, or a list -
# of parts of emote names, or of whole messages. A list is kept lowercased
# and in alphabetical order, so two that hold the same things are equal.
WHOLE = "whole"
NUMBER = "number"
SWITCH = "switch"
WORDS = "words"
PHRASES = "phrases"
LISTS = (WORDS, PHRASES)

Value = bool | int | float | tuple[str, ...]

# A setting's field of the detector's settings is written with this in front.
DETECTOR = "detector."

# What a list may hold. A part of an emote name shorter than this is in most
# names there are - the ones built in are this long at the least.
WORD_MIN_LENGTH = 3
WORD_MAX_LENGTH = 40
PHRASE_MAX_LENGTH = 100
LIST_MAX_ITEMS = 50

# The sections of the page, in the order they come: key, heading, and what is
# said under it.
MOMENTS = "moments"
SIGNALS = "signals"
CLIPS = "clips"
GROUPS = (
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
    front). `advanced` ones are folded away on the page."""

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


_WINDOW = detector.SHORT_WINDOW_SECONDS
# The shortest the lull that ends a moment may be. It is also how soon after
# a moment the next one may fire, and the laughs that came right after a
# moment fired stay in the detector's window for as long as that window is:
# a cooldown no longer than it would let them fire a second moment of their
# own once it ran out.
LULL_MIN_SECONDS = _WINDOW + 5
_BUILT_IN_LAUGHING = ", ".join(detector.LAUGH_EMOTE_NAME_PATTERNS)
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
        "Typed laughs, times the usual",
        "How many times the channel's own usual rate a burst of typed laughs has to be - the laughs, and the "
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
        "Typed laughs",
        '"xd" at any length.',
        (DETECTOR + "laugh_enabled",),
    ),
    Setting(
        "emote_signal", SIGNALS, SWITCH,
        "Kick emotes",
        "Kick's own emotes posted in chat.",
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
        '"xDDD" and longer.',
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
        "Parts of emote names, with spaces between them. An emote whose name has one in it counts as "
        f"laughing, as do the ones built in: {_BUILT_IN_LAUGHING}.",
        (DETECTOR + "added_laugh_emote_patterns",),
    ),
    Setting(
        "ignored_emote_words", SIGNALS, WORDS,
        "Emotes to ignore",
        "Parts of emote names, with spaces between them. An emote whose name has one in it is no reaction at "
        f"all, like the dancing ones built in: {_BUILT_IN_DANCING}.",
        (DETECTOR + "added_ignored_emote_patterns",),
    ),
    Setting(
        "ignored_messages", SIGNALS, PHRASES,
        "Messages to ignore",
        "Whole messages, with commas between them, that the detector leaves out the way it does a bare 1 or 2 "
        "answering a poll: a giveaway's keyword, a bot's command.",
        (DETECTOR + "added_ignored_messages",),
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
)

BY_NAME: dict[str, Setting] = {setting.name: setting for setting in SETTINGS}

# Pairs of settings that only make sense one way round: the first may not be
# more than the second - or, where the third says so, has to be less.
RULES = (
    ("reacting_least", "reacting_most", False),
    ("emote_weight_least", "emote_weight_most", False),
    # Otherwise every moment would run to its cap, and the lull would mean
    # nothing.
    ("lull_seconds", "longest_seconds", True),
)


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


def values(for_all: Mapping[str, Value], own: Mapping[str, Value] | None = None) -> dict[str, Value]:
    """Every setting's value for a channel: what is recommended, with what
    was set for all channels laid over it, then the channel's `own`."""
    return {**recommended_values(), **for_all, **(own or {})}


def tuned(for_all: Mapping[str, Value], own: Mapping[str, Value] | None = None) -> Tuning:
    """What the service works from for a channel with these values set.

    Only a setting that was given a value changes anything: the rest of a
    Tuning stays what its own constant says, also where one setting stands
    for two of them."""
    base = recommended()
    fields: dict[str, Any] = {}
    detector_fields: dict[str, Any] = {}
    for name, value in {**for_all, **(own or {})}.items():
        setting = BY_NAME[name]
        if setting.kind == PHRASES:
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
    """A list as it was typed, or as it was stored, in pieces."""
    if isinstance(raw, str):
        pieces = raw.replace("\n", ",").split(",") if setting.kind == PHRASES else raw.replace(",", " ").split()
    elif isinstance(raw, (list, tuple)) and all(isinstance(item, str) for item in raw):
        pieces = list(raw)
    else:
        raise ValueError(f'"{setting.label}" has to be a list of words.')
    return [piece.strip().lower() for piece in pieces if piece.strip()]


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

    if setting.kind in LISTS:
        items = sorted(set(_items(setting, raw)))
        if len(items) > LIST_MAX_ITEMS:
            raise ValueError(f"{label} can hold {LIST_MAX_ITEMS} at the most.")
        if setting.kind == WORDS:
            wrong = [item for item in items if not WORD_MIN_LENGTH <= len(item) <= WORD_MAX_LENGTH]
            if wrong:
                raise ValueError(
                    f"{label}: {wrong[0]!r} has to be {WORD_MIN_LENGTH} to {WORD_MAX_LENGTH} characters long."
                )
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
    field would be if it were left alone."""
    if setting.kind == SWITCH:
        return "On" if value else "Off"
    if setting.kind == WORDS:
        return " ".join(value)
    if setting.kind == PHRASES:
        return ", ".join(value)
    return _number_words(value)


def _form_field(setting: Setting, value: Value, rest: Value, rest_label: str) -> dict[str, Any]:
    numeric = setting.kind in (WHOLE, NUMBER)
    return {
        "name": setting.name,
        "kind": setting.kind,
        "label": setting.label,
        "caption": setting.caption,
        "value": words(setting, value),
        "on": value is True,
        "rest": words(setting, rest),
        "rest_words": f"{rest_label}: {words(setting, rest) or 'none'}",
        "at_rest": value == rest,
        # What may be typed into the field, for its tooltip.
        "range_words": _range_words(setting).capitalize() if numeric else None,
        "unit": setting.unit,
        "advanced": setting.advanced,
    }


def form(every: Mapping[str, Value], rest: Mapping[str, Value], rest_label: str) -> list[dict[str, Any]]:
    """The Settings page's form: its sections in order, and in each the
    settings as the page draws them - what each is set to, and what it would
    be if it were left alone (`rest`: what is recommended, or for a channel
    what all channels have), which a button beside it puts back. The
    advanced ones are listed apart; the page folds them away unless one of
    them is set to something."""
    sections = []
    for key, title, lead in GROUPS:
        fields = [
            _form_field(setting, every[setting.name], rest[setting.name], rest_label)
            for setting in SETTINGS
            if setting.group == key
        ]
        advanced = [field for field in fields if field["advanced"]]
        sections.append(
            {
                "title": title,
                "lead": lead,
                "fields": [field for field in fields if not field["advanced"]],
                "advanced": advanced,
                "advanced_set": any(not field["at_rest"] for field in advanced),
            }
        )
    return sections

