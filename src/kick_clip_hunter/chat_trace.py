"""Chat set against a clip: the data behind the dashboard's trace.

The review page draws what chat was doing around a moment - messages a
second, and how many of them were laughing - on a strip under the clip, on
the clip's own time axis, and replays the chat lines in step with the
picture. This module works out where everything goes: which second of the
clip a message belongs to, the lines of the trace as SVG paths, the small
version of it in a queue row, and the few characters that say what chat
said most.

"Clip time" is seconds from the first frame of the clip, negative before it.
Chat's clock and the clip's are not the same: where a clip starts is stored
on the stream's program clock, and viewers - so chat - saw each frame some
seconds later. How many is an assumption rather than a measurement (see
CHAT_DELAY_SECONDS), and one the review page lets be corrected per channel.

Nothing here touches the database or the wall clock, so all of it is tested
with plain lists of messages.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from . import chat_identity
from .dashboard_view import IMPORT_REASON, chat_parts, count_words, nick_colour, number_words
from .detector import NATIVE_EMOTE_TOKEN_PATTERN, is_laughing, laugh_emote_names

Row = Mapping[str, Any]

# The trace's height stands for at least this many messages a second, so a
# quiet chat does not get its ripples blown up to the full height.
TRACE_MIN_TOP = 12
# A message this close to a moment's window still counts as part of it. The
# window is rebuilt from wall-clock time after the fact, while the detector
# worked on arrival order - delivery jitter can leave a message that was part
# of the burst a little outside the stored bounds.
MOMENT_PADDING_SECONDS = 2

# How long after a frame was broadcast the people in chat are taken to have
# seen it. A message is put this far back from when it arrived, onto the
# picture its writer was looking at - so that a laugh lands just after what
# caused it. It can't be measured from here: it is the player's buffer,
# usually a few seconds, plus any delay the streamer has set. So this is only
# where a channel starts out, and the review page moves a channel's chat
# either way, between the two limits.
#
# It starts out at 0: chat where it arrived. That is the one placement there
# is something to check against - a streamer's own on-screen chat shows a
# message as it arrives - and against it both 10s and 5s put chat visibly
# early. Even at 0 a message turned up two or three seconds before it did in
# the picture (the stream's clock and the arrival of a chat message are not
# measured at the same point), which is why the delay can go below 0.
#
# Not recorder.PLAYBACK_DELAY_SECONDS on purpose: that one frames the clip,
# where reaching back too far costs nothing, and is on the long side.
CHAT_DELAY_SECONDS = 0
CHAT_DELAY_MIN_SECONDS = -10
CHAT_DELAY_MAX_SECONDS = 60

# The small trace in a queue row: this many steps of this many seconds,
# starting this long before the moment's window does - so the eruption rises
# a quarter of the way in, whatever the moment.
SPARK_STEPS = 16
SPARK_STEP_SECONDS = 4
SPARK_LEAD_SECONDS = 16
# Its drawing height (the SVG's own units; the stylesheet stretches it), and
# how many messages a step the tallest spark on a page at least stands for.
SPARK_HEIGHT = 18
SPARK_MIN_TOP = 8

# "xD", "xDDD" and "XDDDDD" are all the same reaction.
LAUGH_WORD = "xD"
# Between what chat said and how many times: "KEKW", this sign, "31".
TIMES_SIGN = "\N{MULTIPLICATION SIGN}"
_LAUGH_WORD_PATTERN = re.compile(r"^xd+$", re.IGNORECASE)


@dataclass(frozen=True)
class Timeline:
    """The stretch of clip time a strip covers: the clip, and the context
    shown on either side of it.

    `chat_start` is when chat saw the clip's first frame; None for a clip
    that can't be placed against chat at all (one imported from elsewhere).
    """

    chat_start: datetime | None
    clip_seconds: float
    before: float = 0.0
    after: float = 0.0

    @property
    def start(self) -> float:
        return -self.before

    @property
    def end(self) -> float:
        return self.clip_seconds + self.after

    @property
    def seconds(self) -> float:
        return self.end - self.start

    def at(self, moment: datetime) -> float:
        """Clip time of something chat saw at `moment`."""
        return (moment - self.chat_start).total_seconds()


def clip_timeline(
    row: Row,
    *,
    pre_roll: float,
    post_roll: float,
    playback_delay: float,
    chat_delay: float,
    context_before: float,
    context_after: float,
) -> Timeline | None:
    """Where a moment's clip sits against chat, from what is stored of it.

    When the clip's first frame was broadcast is exact if that was stored
    with it. For a clip cut before that was recorded only its length is
    known, so it is assumed to sit centred on the window it was cut for -
    which the recorder reached back `playback_delay` seconds for; with no
    clip at all, the window that would have been cut stands in for it, since
    chat reacted either way. Chat is taken to have seen that frame
    `chat_delay` seconds later (see CHAT_DELAY_SECONDS).
    None for an imported clip of unknown length, which has nothing to draw.
    """
    duration = row["clip_duration"]
    if row["reason"] == IMPORT_REASON:
        return Timeline(None, duration) if duration else None

    if row["clip_start"] and duration:
        broadcast_start = datetime.fromisoformat(row["clip_start"])
    else:
        window_start = datetime.fromisoformat(row["window_start"])
        window_end = datetime.fromisoformat(row["window_end"])
        asked = (window_end - window_start).total_seconds() + pre_roll + post_roll
        duration = duration or asked
        broadcast_start = window_start - timedelta(seconds=playback_delay + pre_roll + (duration - asked) / 2)
    return Timeline(broadcast_start + timedelta(seconds=chat_delay), duration, context_before, context_after)


def _arrived(message: Row) -> datetime:
    return datetime.fromisoformat(message["received_at"])


def _clock(seconds: float) -> str:
    """Time inside a clip, as m:ss."""
    whole = max(0, math.floor(seconds))
    return f"{whole // 60}:{whole % 60:02d}"


# -- drawing ----------------------------------------------------------------
#
# Lines are SVG paths in a box one unit wide per step and `top` units high.
# A value sits at the middle of its step, `value` units up from the bottom.


def _coordinate(value: float) -> str:
    return number_words(value, 2)


def _points(heights: list[float], top: float, first: int = 0) -> str:
    return " L".join(
        f"{_coordinate(first + index + 0.5)},{_coordinate(top - height)}" for index, height in enumerate(heights)
    )


def _line_path(heights: list[float], top: float) -> str:
    """A line through every step."""
    return f"M{_points(heights, top)}" if heights else ""


def _runs(values: list[int]) -> list[tuple[int, int]]:
    """The stretches where there is something to draw, each taken one step
    out on either side so its line leaves the baseline and comes back to it."""
    runs = []
    start = None
    for index, value in enumerate(values):
        if value > 0 and start is None:
            start = index
        if start is not None and (value == 0 or index == len(values) - 1):
            runs.append((max(start - 1, 0), index))
            start = None
    return runs


def _pen_path(values: list[int], heights: list[float], top: float, *, closed: bool = False) -> str:
    """A line that only touches the paper where `values` has something to
    draw; `closed` gives the area under it instead."""
    pieces = []
    for first, last in _runs(values):
        points = _points(heights[first : last + 1], top, first)
        if closed:
            left, right, floor = _coordinate(first + 0.5), _coordinate(last + 0.5), _coordinate(top)
            pieces.append(f"M{left},{floor} L{points} L{right},{floor} Z")
        else:
            pieces.append(f"M{points}")
    return " ".join(pieces)


def _percent(part: float, whole: float) -> str:
    return number_words(min(max(part / whole, 0.0), 1.0) * 100, 3)


# -- the trace under the clip -----------------------------------------------


def message_counts(
    messages: Iterable[Row], timeline: Timeline, laugh_names: Iterable[str] = ()
) -> tuple[list[int], list[int]]:
    """Messages in each second of the timeline: all of them, and the
    laughing ones (see detector.is_laughing)."""
    laugh_names = tuple(laugh_names)
    size = max(1, math.ceil(timeline.seconds))
    everything, laughing = [0] * size, [0] * size
    for message in messages:
        second = math.floor(timeline.at(_arrived(message)) - timeline.start)
        if 0 <= second < size:
            everything[second] += 1
            if is_laughing(message["content"] or "", laugh_names):
                laughing[second] += 1
    return everything, laughing


def _grid_step(peak: float) -> int:
    """How many messages a second apart the chart paper's horizontal lines
    are, so that there are never more than a handful of them."""
    for step in (2, 5, 10, 20, 50):
        if peak <= step * 8:
            return step
    return 100


def _grid_paths(timeline: Timeline, top: int, step: int) -> tuple[str, str]:
    """Chart paper: a fine line every 2 seconds and every `step` messages a
    second, a heavier one every 10 seconds - counted from the start of the
    clip, so that the heavy lines fall on round clip times."""
    fine, heavy = [], []
    second = math.ceil(timeline.start / 2) * 2
    while second < timeline.end:
        if second > timeline.start:
            (heavy if second % 10 == 0 else fine).append(f"M{_coordinate(second - timeline.start)},0V{top}")
        second += 2
    width = _coordinate(timeline.seconds)
    fine.extend(f"M0,{level}H{width}" for level in range(step, top, step))
    return "".join(fine), "".join(heavy)


def _axis(timeline: Timeline) -> list[dict[str, str]]:
    """The times written under the strip: clip time at regular steps, the
    clip's two ends, and what the context on either side is."""
    labels: list[dict[str, str]] = []

    def label(second: float, words: str, align: str) -> None:
        labels.append({"left": _percent(second - timeline.start, timeline.seconds), "words": words, "align": align})

    clip = timeline.clip_seconds
    if timeline.before:
        label(timeline.start, f"{number_words(timeline.before, 0)} s before", "start")
    label(0, _clock(0), "start")
    step = 10 if timeline.seconds <= 150 else 20 if timeline.seconds <= 300 else 30
    second = step
    while second <= clip - step / 2:
        label(second, _clock(second), "middle")
        second += step
    if timeline.after:
        label(clip, _clock(clip), "start")
        label(timeline.end, f"{number_words(timeline.after, 0)} s after", "end")
    else:
        label(clip, _clock(clip), "end")
    return labels


def _moment_band(timeline: Timeline, everything: list[int], window: tuple[datetime, datetime]) -> dict[str, Any]:
    """Where the moment lies on the strip, and the note that goes with it."""
    first, last = timeline.at(window[0]), timeline.at(window[1])
    seconds = max(1, round(last - first))
    inside = everything[max(0, math.floor(first - timeline.start)) : max(0, math.ceil(last - timeline.start))]
    peak = max(inside, default=0)
    left = _percent(first - timeline.start, timeline.seconds)
    right = _percent(last - timeline.start, timeline.seconds)
    band = {
        "left": left,
        "width": number_words(float(right) - float(left), 3),
        "seconds": seconds,
        "peak": peak,
        "note": f"the moment: {seconds} s" + (f", peaking at {peak}/s" if peak else ""),
    }
    # The note goes beside the band, on whichever side has the room: after
    # it, measured from the strip's left edge, or before it, from the right.
    if float(right) > 62:
        band["note_right"] = number_words(100 - float(left), 3)
    else:
        band["note_left"] = right
    return band


def trace(
    timeline: Timeline,
    everything: list[int],
    laughing: list[int],
    *,
    usual: float,
    window: tuple[datetime, datetime] | None,
) -> dict[str, Any]:
    """Everything the page needs to draw the strip under a clip.

    Lengths along the strip are percentages of its width; the lines are
    paths in a box one unit wide per second and `top` units high, one unit
    per message a second, which the page stretches over the strip. `usual`
    is the channel's pace before the moment, in messages a second; `window`
    is when the moment's reaction ran, on chat's clock.
    """
    peak = max(everything, default=0)
    step = _grid_step(max(peak, TRACE_MIN_TOP))
    top = math.ceil(max(peak, TRACE_MIN_TOP) / step) * step
    fine, heavy = _grid_paths(timeline, top, step)
    context = []
    if timeline.before:
        context.append({"left": "0", "width": _percent(timeline.before, timeline.seconds)})
    if timeline.after:
        context.append(
            {
                "left": _percent(timeline.clip_seconds - timeline.start, timeline.seconds),
                "width": _percent(timeline.after, timeline.seconds),
            }
        )
    strip: dict[str, Any] = {
        "start": number_words(timeline.start, 2),
        "end": number_words(timeline.end, 2),
        "clip_seconds": number_words(timeline.clip_seconds, 2),
        "width": _coordinate(timeline.seconds),
        "top": top,
        "grid": fine,
        "grid_strong": heavy,
        "axis": _axis(timeline),
        # Where the clip begins, which is where the playhead starts out.
        "clip_left": _percent(-timeline.start, timeline.seconds),
        "context": context,
        "has_chat": timeline.chat_start is not None,
        "moment": None,
    }
    if timeline.chat_start is None:
        strip["label"] = "No chat was measured for this clip."
        return strip

    strip.update(
        {
            "all": _line_path(everything, top),
            "laugh": _pen_path(laughing, laughing, top),
            "wash": _pen_path(laughing, laughing, top, closed=True),
            "all_counts": ",".join(map(str, everything)),
            "laugh_counts": ",".join(map(str, laughing)),
            "usual": number_words(usual),
            "usual_height": _percent(usual, top),
        }
    )
    sentences = [f"The usual pace is {number_words(usual)} a second"]
    if window is not None:
        strip["moment"] = band = _moment_band(timeline, everything, window)
        if band["peak"]:
            sentences.append(f"it peaks at {band['peak']} a second during the {band['seconds']} second moment")
    span = "over the clip"
    if timeline.before or timeline.after:
        span = (
            f"from {count_words(round(timeline.before), 'second')} before the clip "
            f"to {count_words(round(timeline.after), 'second')} after it"
        )
    strip["label"] = f"Chat messages a second {span}. {'; '.join(sentences)}."
    return strip


# -- chat lines, replayed with the clip ---------------------------------------


def _during(message: Row, window: tuple[datetime, datetime] | None) -> bool:
    """Whether a message was sent during a moment (see MOMENT_PADDING_SECONDS)."""
    if window is None:
        return False
    padding = timedelta(seconds=MOMENT_PADDING_SECONDS)
    return window[0] - padding <= _arrived(message) <= window[1] + padding


def chat_replay(
    messages: Iterable[Row],
    timeline: Timeline,
    window: tuple[datetime, datetime] | None,
    emotes: Mapping[str, tuple[str, int, int]] | None = None,
    identities: Mapping[str, chat_identity.Identity] | None = None,
) -> list[dict[str, Any]]:
    """The chat lines to show beside a clip, each with the clip time it
    belongs to and whether it was sent during the moment. `emotes` are the
    channel's 7TV emotes, drawn as pictures (see dashboard_view.chat_parts).
    `identities` are what is known of the chatters by name: the colour
    their name has on Kick and their badges (see chat_identity). Someone
    unknown, or without a colour, keeps the colour worked out from the name."""
    identities = identities or {}
    lines = []
    for message in messages:
        nick = message["sender_username"] or ""
        own_colour, badges = identities.get(nick, (None, chat_identity.NO_BADGES))
        lines.append(
            {
                "nick": nick,
                "colour": nick_colour(nick),
                "own_colour": chat_identity.readable(own_colour) if own_colour else None,
                "badges": chat_identity.badge_parts(badges),
                "parts": chat_parts(message["content"] or "", emotes),
                "at": number_words(timeline.at(_arrived(message)), 1),
                "in_moment": _during(message, window),
            }
        )
    return lines


# -- the queue: a spark and a word per row ----------------------------------


def spark_span(window_start: datetime) -> tuple[datetime, datetime]:
    """The stretch of chat a queue row's spark covers."""
    start = window_start - timedelta(seconds=SPARK_LEAD_SECONDS)
    return start, start + timedelta(seconds=SPARK_STEPS * SPARK_STEP_SECONDS)


def spark_counts(
    messages: Iterable[Row], window_start: datetime, laugh_names: Iterable[str] = ()
) -> tuple[list[int], list[int]]:
    """Messages in each step of a spark: all of them, and the laughing ones."""
    laugh_names = tuple(laugh_names)
    start, _end = spark_span(window_start)
    everything, laughing = [0] * SPARK_STEPS, [0] * SPARK_STEPS
    for message in messages:
        step = math.floor((_arrived(message) - start).total_seconds() / SPARK_STEP_SECONDS)
        if 0 <= step < SPARK_STEPS:
            everything[step] += 1
            if is_laughing(message["content"] or "", laugh_names):
                laughing[step] += 1
    return everything, laughing


def spark_top(sparks: Iterable[list[int]]) -> int:
    """What the full height of a spark stands for. One value for the whole
    page, so that a bigger eruption looks bigger than its neighbours."""
    return max([SPARK_MIN_TOP, *(max(spark, default=0) for spark in sparks)])


def spark_paths(everything: list[int], laughing: list[int], top: int) -> dict[str, str]:
    """A spark's two lines, in a box one unit wide per step and SPARK_HEIGHT
    high, drawn a unit clear of its top and bottom edges."""

    def heights(values: list[int]) -> list[float]:
        return [1 + value / top * (SPARK_HEIGHT - 2) for value in values]

    return {
        "all": _line_path(heights(everything), SPARK_HEIGHT),
        "laugh": _pen_path(laughing, heights(laughing), SPARK_HEIGHT),
    }


def queue_activity(
    messages: Iterable[Row], window: tuple[datetime, datetime], emote_names: Iterable[str] = ()
) -> dict[str, Any]:
    """What a queue row shows of chat around its moment: the two series of
    its spark, and what was said most while the moment lasted. `emote_names`
    are the channel's own 7TV emote names, lowercased."""
    messages = list(messages)
    emote_names = tuple(emote_names)
    everything, laughing = spark_counts(messages, window[0], laugh_emote_names(emote_names))
    said = reaction_words((message for message in messages if _during(message, window)), emote_names)
    return {"all": everything, "laugh": laughing, "said": said}


def _reaction_tokens(content: str) -> list[str]:
    """What a message is made of, for telling what chat said most: emote
    names and words, with every form of the typed laugh counted as one."""
    tokens = NATIVE_EMOTE_TOKEN_PATTERN.findall(content)
    for word in NATIVE_EMOTE_TOKEN_PATTERN.sub(" ", content).split():
        if _LAUGH_WORD_PATTERN.match(word.strip(".,!?:;")):
            tokens.append(LAUGH_WORD)
        else:
            tokens.append(word.strip(".,:;") or word)
    return tokens


def reaction_words(messages: Iterable[Row], emote_names: Iterable[str] = ()) -> str | None:
    """What chat said most in a moment and in how many messages, as the
    word and the count with a times sign between them - or None if nothing
    was said more than once.

    Chat reacts in single tokens: an emote, a laugh, a "W", a "???", often
    repeated. So a message counts for a token when it consists of nothing
    else, and for any emote or laugh it contains even inside a sentence
    (`emote_names` are the channel's own, lowercased). Ordinary words inside
    sentences are not counted, which keeps "the" from ever winning.
    """
    emote_names = frozenset(emote_names)
    counts: Counter[str] = Counter()
    spellings: dict[str, Counter[str]] = {}
    for message in messages:
        content = message["content"] or ""
        tokens = _reaction_tokens(content)
        native = set(NATIVE_EMOTE_TOKEN_PATTERN.findall(content))
        only_one_kind = len({token.lower() for token in tokens}) == 1
        counted = {
            token.lower()
            for token in tokens
            if only_one_kind or token in native or token == LAUGH_WORD or token.lower() in emote_names
        }
        counts.update(counted)
        for token in tokens:
            if token.lower() in counted:
                spellings.setdefault(token.lower(), Counter())[token] += 1
    if not counts:
        return None
    key, count = counts.most_common(1)[0]
    if count < 2:
        return None
    return f"{spellings[key].most_common(1)[0][0]} {TIMES_SIGN}{count}"
