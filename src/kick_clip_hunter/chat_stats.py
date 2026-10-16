"""A channel's stored chat in figures: what the dashboard's Analyze page says.

Every chat message the service receives is kept (see db.py), which makes a
channel's chat something to look into afterwards: who wrote most, which
emotes were used, when chat was busiest, what was said over and over, what
one chatter wrote. This module works that out for a stretch of time - a
period - from plain rows of chat, in one pass over them, and puts it into
the shape the page draws: figures with a caption, the columns of a chart,
the rows of a table.

Nothing here reads the database or the clock: rows, the period and today's
date come in as arguments, so all of it is tested with lists of messages.
Times are told as the rest of the dashboard tells them, in the machine's own
time zone.

Emotes are counted as the dashboard draws them (dashboard_view.chat_parts):
Kick's own are in the message itself, a 7TV emote is a word spelled exactly
like an emote the channel has *now* - one it has since removed is a word
again. Laughing is what the detector takes for it (detector.is_laughing).
"""

from __future__ import annotations

import math
import re
import statistics
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from urllib.parse import urlencode

from . import channel_badges, chat_identity
from .dashboard_view import (
    chat_parts,
    clock_words,
    date_words,
    day_words,
    emote_source,
    nick_colour,
    number_words,
    sentence_case,
    stream_time_words,
)
from .detector import is_laughing

Row = Mapping[str, Any]
Emotes = Mapping[str, tuple[str, int, int]]

# -- periods ------------------------------------------------------------------

# The periods the page offers that end now, by the name they have in its
# address: what the tab says, and how far back they reach.
PRESETS = {
    "day": ("24 hours", timedelta(hours=24)),
    "week": ("7 days", timedelta(days=7)),
    "month": ("30 days", timedelta(days=30)),
}
# Everything that is stored of the channel.
PRESET_ALL = "all"
PRESET_ALL_LABEL = "All"

# Chat with no silence longer than this in it is one session - as a rule one
# stream: stored chat stops when the stream does, give or take a straggler.
SESSION_GAP_SECONDS = 30 * 60
# A session of fewer messages, or a shorter one, is not listed: somebody
# writing into an offline chat is not a stream, and neither is the minute
# of chat that came in while a switch was tried out.
SESSION_MIN_MESSAGES = 20
SESSION_MIN_SECONDS = 10 * 60
SESSIONS_LISTED = 60
# How many of a channel's newest messages are looked at for sessions.
SESSION_MESSAGE_LIMIT = 100_000

# The two things the page shows of a period, and the lists it can sort a
# search into.
VIEW_OVERVIEW = "overview"
VIEW_MESSAGES = "messages"
ORDER_NEWEST = "newest"
ORDER_OLDEST = "oldest"

# How many rows a table has, and how many when more are asked for.
TOP_ROWS = 25
TOP_ROWS_MORE = 100
# The lists that are short by nature.
SIDE_ROWS = 10
# The few whose part of all messages the overview tells.
TOP_FEW = 10
BUSIEST_MINUTES = 5
CHATTER_EMOTES = 8
CHATTER_ROWS = 5

# A page of a search, and how many messages are shown on either side of one
# whose surroundings were asked for - which the page then scrolls to, by the
# name it has in the markup.
MESSAGES_PAGE_SIZE = 100
CONTEXT_MESSAGES = 50
MESSAGE_ANCHOR = "asked"
# Where the messages of a search begin, under what is said of its chatter -
# which every other page of it opens at.
MESSAGES_ANCHOR = "messages"

_ADDRESS = "/dashboard/analyze"
_LOCAL_FORMAT = "%Y-%m-%dT%H:%M"


@dataclass(frozen=True)
class Period:
    """A stretch of time: from `start` up to, but not at, `end`."""

    start: datetime
    end: datetime

    @property
    def seconds(self) -> float:
        return max((self.end - self.start).total_seconds(), 0.0)


def local_text(moment: datetime) -> str:
    """A time as the page's address and its date fields carry it: local,
    to the minute."""
    return moment.astimezone().strftime(_LOCAL_FORMAT)


def parse_local(text: str | None) -> datetime | None:
    """The time a date field or an address gave, or None if it is not one.
    A time without a zone is local time."""
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).astimezone()
    except (ValueError, OverflowError, OSError):
        # OSError: a year the platform's clock does not reach back to.
        return None


def whole_minutes(first: datetime, last: datetime) -> Period:
    """The period that holds everything from `first` to `last`, widened to
    whole minutes - which is as fine as the page's address tells time."""
    start = first.astimezone().replace(second=0, microsecond=0)
    end = last.astimezone().replace(second=0, microsecond=0) + timedelta(minutes=1)
    return Period(start, end)


def chosen_period(
    preset: str | None,
    start: datetime | None,
    end: datetime | None,
    *,
    now: datetime,
    stored: Period,
    newest_session: Period | None,
) -> tuple[Period, str | None]:
    """The period a request is about, and the preset it is if it is one.

    Two ends that were given win, either of them standing in for the other
    where only one was: everything stored up to `end`, or from `start`
    until now. Then a preset by its name. With nothing asked for it is the
    channel's newest session - what one comes to the page for after a
    stream - or everything that is stored (`stored`) where chat has not
    come in sessions worth the name. No period reaches past `now`: nothing
    has been written there yet.
    """
    if start is not None or end is not None:
        period = Period(start or stored.start, min(end or now, now))
        if period.start < period.end:
            return period, None
    if preset in PRESETS:
        return Period(now - PRESETS[preset][1], now), preset
    if preset == PRESET_ALL or newest_session is None:
        return Period(stored.start, min(stored.end, now)), PRESET_ALL
    return Period(newest_session.start, min(newest_session.end, now)), None


def analyze_url(
    channel: str,
    *,
    preset: str | None = None,
    period: Period | None = None,
    view: str = VIEW_OVERVIEW,
    chatter: str | None = None,
    text: str | None = None,
    order: str = ORDER_NEWEST,
    offset: int = 0,
    top: int = TOP_ROWS,
    around: str | None = None,
) -> str:
    """The Analyze page for a channel and a period - a preset by its name,
    or any other by its two ends - and what is shown of it: the overview,
    or its messages, narrowed to a chatter or to a text, or the chat around
    one message (`around`, its id). Whatever is at its default is left
    out."""
    query: dict[str, Any] = {"channel": channel}
    if preset:
        query["period"] = preset
    elif period is not None:
        query["from"] = local_text(period.start)
        query["to"] = local_text(period.end)
    if around:
        query["around"] = around
        return f"{_ADDRESS}?{urlencode(query)}#{MESSAGE_ANCHOR}"
    if chatter:
        query["chatter"] = chatter
    if text:
        query["q"] = text
    if view == VIEW_MESSAGES and not chatter and not text:
        query["view"] = view
    if order != ORDER_NEWEST:
        query["order"] = order
    if offset:
        query["offset"] = offset
    if top != TOP_ROWS:
        query["top"] = top
    return f"{_ADDRESS}?{urlencode(query)}"


# -- words ----------------------------------------------------------------------


def whole_words(count: int) -> str:
    """A count as it is read: 42,834."""
    return f"{count:,}"


def counted_words(count: int, noun: str) -> str:
    """A count with its noun: "1 message", "42,834 messages"."""
    return f"{whole_words(count)} {noun}" if count == 1 else f"{whole_words(count)} {noun}s"


def share_words(part: float, whole: float) -> str:
    """A part of a whole as a percentage: to the point below ten, where a
    whole number would say too little."""
    if whole <= 0 or part <= 0:
        return "0%"
    share = part / whole * 100
    if share < 0.1:
        return "under 0.1%"
    return f"{number_words(share, 1) if share < 10 else round(share)}%"


def one_day(period: Period) -> bool:
    """Whether a period lies within one local day. One that ends on the
    stroke of midnight still does."""
    last = period.end.astimezone() - timedelta(microseconds=1)
    return period.start.astimezone().date() == max(last, period.start.astimezone()).date()


def period_words(period: Period, today: date) -> str:
    """A period in words: "Monday 12 October, 18:43 to 21:16", or with a
    day at either end where it runs over midnight."""
    start, end = period.start.astimezone(), period.end.astimezone()
    if one_day(period):
        # A day that is over ends at what the clock calls 00:00 of the next.
        until = clock_words(end) if end.date() == start.date() else "midnight"
        return f"{sentence_case(day_words(start.date(), today))}, {clock_words(start)} to {until}"
    return (
        f"{date_words(start.date(), today)}, {clock_words(start)} "
        f"to {date_words(end.date(), today)}, {clock_words(end)}"
    )


def second_words(moment: datetime, today: date) -> str:
    """When a message arrived, to the second: "12 October 21:35:02"."""
    local = moment.astimezone()
    return f"{date_words(local.date(), today)} {local:%H:%M:%S}"


def minute_words(moment: datetime, today: date) -> str:
    """A day and a time: "Monday 12 October at 21:35"."""
    local = moment.astimezone()
    return f"{sentence_case(day_words(local.date(), today))} at {clock_words(local)}"


# -- the columns of a chart -----------------------------------------------------

DAY_SECONDS = 86400
# How long a column of a chart can stand for, short of a day. The shortest
# that keeps the chart to MAX_COLUMNS is taken; past the last, a column is
# one day or several.
COLUMN_SECONDS = (60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200)
MAX_COLUMNS = 240
# The times written under a chart: at most this many. Under columns shorter
# than a day they are this far apart, counted from midnight; under columns
# of days, this many columns.
MAX_AXIS_LABELS = 8
AXIS_LAST_SHARE = 0.92
AXIS_SECONDS = (300, 600, 1800, 3600, 7200, 21600, 43200, *(days * DAY_SECONDS for days in (1, 2, 7, 14, 28)))
AXIS_COLUMNS = (1, 2, 7, 14, 28)


@dataclass(frozen=True)
class Columns:
    """The columns of a chart over a period, each standing for as long as
    the next.

    Columns shorter than a day are counted from the local midnight before
    the period starts (`anchor`), so that they begin on round times of the
    clock; `first` is the one the period starts in. Columns of `days` whole
    days hold local days, however long the clock made them.
    """

    anchor: datetime
    seconds: int
    first: int
    count: int
    days: int = 0

    def of(self, moment: datetime) -> int:
        """Which column a moment falls in. One outside the period gets a
        number that is no column's."""
        if self.days:
            return (moment.astimezone().date() - self.anchor.date()).days // self.days
        return math.floor((moment - self.anchor).total_seconds() / self.seconds) - self.first

    def start(self, column: int) -> datetime:
        """When a column begins. The one after the last begins where the
        last one ends."""
        if self.days:
            day = self.anchor.date() + timedelta(days=column * self.days)
            return datetime.combine(day, time()).astimezone()
        return (self.anchor + timedelta(seconds=(self.first + column) * self.seconds)).astimezone()


def columns_for(period: Period) -> Columns:
    """The columns a chart over `period` has: the finest that keeps it to
    MAX_COLUMNS of them."""
    start = period.start.astimezone()
    anchor = start.replace(hour=0, minute=0, second=0, microsecond=0)
    for seconds in COLUMN_SECONDS:
        if period.seconds / seconds <= MAX_COLUMNS:
            first = math.floor((start - anchor).total_seconds() / seconds)
            last = math.ceil((period.end - anchor).total_seconds() / seconds)
            return Columns(anchor, seconds, first, max(last - first, 1))
    last_day = (period.end.astimezone() - timedelta(microseconds=1)).date()
    span = (last_day - anchor.date()).days + 1
    days = next((days for days in (1, 7) if span / days <= MAX_COLUMNS), math.ceil(span / MAX_COLUMNS))
    return Columns(anchor, days * DAY_SECONDS, 0, math.ceil(span / days), days)


# -- one pass over the chat -----------------------------------------------------

# A name after an "@" - where the sign begins a word, which leaves out the
# one in the middle of an address.
_MENTION = re.compile(r"(?<![\w.])@([A-Za-z0-9_]{2,40})")
_LAUGH_WORD = re.compile(r"^xd+$", re.IGNORECASE)
# The same character more than three times in a row says no more than three.
_LONG_RUN = re.compile(r"(.)\1{3,}")
LAUGH_WORD = "xD"


def said_key(content: str) -> tuple[str, str]:
    """What a message says, for telling how often the same thing was said:
    the form it is counted under, and the form it is shown in.

    Chat repeats itself with variations that mean nothing: capitals, a
    longer row of question marks, the same word three times over, every
    length of "xDDD". So the case is folded, a run of one character is cut
    to three, a word said several times in a row is said once, and every
    typed laugh is the same laugh.
    """
    words: list[str] = []
    folded: list[str] = []
    for word in _LONG_RUN.sub(r"\1\1\1", content).split():
        if word[0] in "xX" and _LAUGH_WORD.match(word):
            word = LAUGH_WORD
        key = word.casefold()
        if not folded or folded[-1] != key:
            words.append(word)
            folded.append(key)
    return " ".join(folded), " ".join(words)


def laugh_test(laugh_names: Iterable[str], laugh_patterns: Iterable[str]) -> Callable[[str], bool]:
    """detector.is_laughing for one channel, made to be asked of every
    message of a long period: the channel's laugh emote names, which it
    looks for one after the other, are looked for all at once. The answer
    for a message is the same."""
    laugh_patterns = tuple(laugh_patterns)
    names = [re.escape(name) for name in laugh_names if name]
    named = re.compile("|".join(names)) if names else None

    def laughing(content: str) -> bool:
        return is_laughing(content, (), laugh_patterns) or bool(named and named.search(content.lower()))

    return laughing


@dataclass
class Collected:
    """What one pass over a stretch of chat counted (see collect)."""

    columns: Columns
    messages: int = 0
    laughing: int = 0
    with_emote: int = 0
    first: datetime | None = None
    last: datetime | None = None
    by_chatter: Counter[str] = field(default_factory=Counter)
    by_column: list[int] = field(default_factory=list)
    laughing_by_column: list[int] = field(default_factory=list)
    by_hour: list[int] = field(default_factory=lambda: [0] * 24)
    # Messages in each minute there were any, by the minute's number since 1970.
    by_minute: Counter[int] = field(default_factory=Counter)
    days: set[date] = field(default_factory=set)
    # Emotes by the address of their picture, which tells Kick's "KEKW"
    # from the channel's 7TV one: how often each was used and by whom, what
    # it is called and how wide it is drawn, and each chatter's own count.
    emote_uses: Counter[str] = field(default_factory=Counter)
    emote_people: dict[str, set[str]] = field(default_factory=dict)
    emote_shape: dict[str, tuple[str, int]] = field(default_factory=dict)
    chatter_emotes: dict[str, Counter[str]] = field(default_factory=dict)
    # What was said, by said_key: how often, the first one met - who said it
    # and how it was written - and, for whatever was said more than once,
    # everyone who did. Most of what chat says is said once; a set for each
    # of those would be most of what a pass keeps in memory.
    said: Counter[str] = field(default_factory=Counter)
    said_first: dict[str, tuple[str, str]] = field(default_factory=dict)
    said_people: dict[str, set[str]] = field(default_factory=dict)
    # Who was addressed with an "@", by the name in small letters: in how
    # many messages, by whom, and how the name was written.
    mentions: Counter[str] = field(default_factory=Counter)
    mention_people: dict[str, set[str]] = field(default_factory=dict)
    mention_spelling: dict[str, Counter[str]] = field(default_factory=dict)


def collect(
    messages: Iterable[Row],
    columns: Columns,
    *,
    emotes: Emotes | None = None,
    laugh_names: Iterable[str] = (),
    laugh_patterns: Iterable[str] = (),
) -> Collected:
    """Counts a stretch of chat: `messages` are rows with when each
    arrived, who sent it and what it says. Their order makes no difference
    to any count - only to which of several spellings of the same thing
    said is the one that is shown, which is the first that is met.

    `columns` are the chart's, which is what a message's time is counted
    into. `emotes` are the channel's 7TV emotes (db.get_chat_emotes);
    `laugh_names` and `laugh_patterns` are what a laughing message is told
    by there (detector.is_laughing).
    """
    is_laugh = laugh_test(laugh_names, laugh_patterns)
    kept = Collected(columns, by_column=[0] * columns.count, laughing_by_column=[0] * columns.count)
    for message in messages:
        arrived = datetime.fromisoformat(message["received_at"])
        local = arrived.astimezone()
        sender = message["sender_username"] or ""
        content = message["content"] or ""

        kept.messages += 1
        kept.by_chatter[sender] += 1
        kept.first = arrived if kept.first is None or arrived < kept.first else kept.first
        kept.last = arrived if kept.last is None or arrived > kept.last else kept.last
        kept.by_hour[local.hour] += 1
        kept.by_minute[int(arrived.timestamp() // 60)] += 1
        kept.days.add(local.date())
        laughing = is_laugh(content)
        kept.laughing += laughing
        column = columns.of(arrived)
        if 0 <= column < columns.count:
            kept.by_column[column] += 1
            kept.laughing_by_column[column] += laughing

        parts = chat_parts(content, emotes)
        used = [part for part in parts if "emote" in part]
        for part in used:
            picture = part["image"]
            kept.emote_uses[picture] += 1
            kept.emote_people.setdefault(picture, set()).add(sender)
            kept.emote_shape.setdefault(picture, (part["emote"], part["width"]))
            kept.chatter_emotes.setdefault(sender, Counter())[picture] += 1
        kept.with_emote += bool(used)

        # Emotes have a table of their own; what was said is words.
        if any("text" in part and part["text"].strip() for part in parts):
            key, shown = said_key(content)
            kept.said[key] += 1
            if kept.said[key] == 1:
                kept.said_first[key] = (sender, shown)
            else:
                kept.said_people.setdefault(key, {kept.said_first[key][0]}).add(sender)

        addressed: dict[str, str] = {}
        for name in _MENTION.findall(content):
            addressed.setdefault(name.lower(), name)
        for key, name in addressed.items():
            kept.mentions[key] += 1
            kept.mention_people.setdefault(key, set()).add(sender)
            kept.mention_spelling.setdefault(key, Counter())[name] += 1
    return kept


def mentions_of(messages: Iterable[Row], name: str) -> int:
    """How many of `messages` address a chatter with an "@" - not counting
    their own, and not counting someone whose name merely starts the same."""
    wanted = name.lower()
    return sum(
        1
        for message in messages
        if (message["sender_username"] or "").lower() != wanted
        and wanted in {found.lower() for found in _MENTION.findall(message["content"] or "")}
    )


# -- who is who -----------------------------------------------------------------


def chatter_name(
    name: str,
    identities: Mapping[str, chat_identity.Identity] | None = None,
    pictures: Iterable[channel_badges.Picture] = (),
) -> dict[str, Any]:
    """A chatter's name as the page draws it, like a line of chat does: in
    the colour it has on Kick where that is known, behind their badges - a
    subscriber's as the channel's own picture of it (`pictures`)."""
    own_colour, badges = (identities or {}).get(name, (None, chat_identity.NO_BADGES))
    return {
        "nick": name,
        "colour": nick_colour(name),
        "own_colour": chat_identity.readable(own_colour) if own_colour else None,
        "badges": chat_identity.badge_parts(badges, pictures),
    }


def find_chatter(counts: list[tuple[str, int]], wanted: str) -> tuple[str | None, list[tuple[str, int]]]:
    """Who was meant by a name typed into the search: the chatter of that
    name however it is capitalised, or failing that the only one whose name
    has it in it. With several of those there is nobody to show, and they
    are returned for the page to offer instead, the most active first.
    `counts` is everyone who wrote in the period with how many messages."""
    wanted = wanted.strip().lstrip("@").casefold()
    if not wanted:
        return None, []
    for name, _count in counts:
        if name.casefold() == wanted:
            return name, []
    having = [(name, count) for name, count in counts if wanted in name.casefold()]
    if len(having) == 1:
        return having[0][0], []
    return None, having[:SIDE_ROWS]


def rank_of(counts: list[tuple[str, int]], name: str) -> int | None:
    """Where a chatter stands by how much they wrote: one more than the
    number of chatters who wrote more. None for someone who wrote nothing."""
    mine = dict(counts).get(name)
    if mine is None:
        return None
    return 1 + sum(1 for _name, count in counts if count > mine)


# -- figures --------------------------------------------------------------------


def _figure(label: str, value: str, caption: str = "", href: str | None = None) -> dict[str, Any]:
    return {"label": label, "value": value, "caption": caption, "href": href}


def busiest_minutes(kept: Collected, limit: int = BUSIEST_MINUTES) -> list[tuple[datetime, int]]:
    """The minutes with the most messages, the busiest first: when each
    began and how many it had."""
    return [
        (datetime.fromtimestamp(minute * 60).astimezone(), count)
        for minute, count in sorted(kept.by_minute.items(), key=lambda item: (-item[1], item[0]))[:limit]
    ]


def _part_figure(label: str, part: int, whole: int) -> dict[str, Any]:
    """A figure that is a part of the messages: the share, and how many."""
    return _figure(label, share_words(part, whole), counted_words(part, "message"))


def overview_figures(
    kept: Collected,
    today: date,
    *,
    returning: int | None = None,
    moments: int = 0,
    moments_href: str | None = None,
) -> list[dict[str, Any]]:
    """The figures at the top of the overview of a period that has chat.
    `returning` is how many of its chatters had written in the channel
    before (None where that cannot be told), `moments` how many were
    detected in it."""
    chatters = len(kept.by_chatter)
    minutes = len(kept.by_minute)
    pace = f"{number_words(kept.messages / minutes, 1)} a minute in the {counted_words(minutes, 'minute')} with chat"
    half = math.floor(statistics.median(kept.by_chatter.values()))
    once = sum(1 for count in kept.by_chatter.values() if count == 1)
    (busiest, most_in_one), *_ = busiest_minutes(kept, 1)

    figures = [
        _figure("Messages", whole_words(kept.messages), pace),
        _figure(
            "Chatters",
            whole_words(chatters),
            f"half of them wrote {counted_words(half, 'message')}" + ("" if half == 1 else " or fewer"),
        ),
    ]
    if returning is not None:
        new = chatters - returning
        figures.append(
            _figure("New chatters", whole_words(new), f"{share_words(new, chatters)} had not written here before")
        )
    figures.append(
        _figure("Wrote once", share_words(once, chatters), f"{counted_words(once, 'chatter')} with one message")
    )
    if chatters > TOP_FEW:
        theirs = sum(count for _name, count in most(kept.by_chatter, TOP_FEW))
        figures.append(
            _figure(f"The {TOP_FEW} most active", share_words(theirs, kept.messages), "of all messages are theirs")
        )
    figures += [
        _part_figure("Laughing", kept.laughing, kept.messages),
        _part_figure("With an emote", kept.with_emote, kept.messages),
        _figure("Busiest minute", whole_words(most_in_one), f"messages, {minute_words(busiest, today)}"),
        _figure(
            "Moments",
            whole_words(moments),
            "detected in this period" if moments else "none was detected in this period",
            moments_href if moments else None,
        ),
    ]
    return figures


def chatter_figures(
    kept: Collected,
    today: date,
    *,
    everyone: int,
    rank: int | None,
    chatters: int,
    first_seen: datetime | None,
    mentioned: int,
) -> list[dict[str, Any]]:
    """The figures of one chatter in a period they wrote in. `everyone` is
    how many messages the period has in all, `rank` where the chatter
    stands among its `chatters`, `first_seen` when their oldest stored
    message arrived and `mentioned` how many messages of others address
    them."""
    share = f"{share_words(kept.messages, everyone)} of the chat"
    if rank:
        share += f", number {whole_words(rank)} of {counted_words(chatters, 'chatter')}"
    figures = [_figure("Messages", whole_words(kept.messages), share)]
    if first_seen is not None:
        seen = first_seen.astimezone()
        oldest = f"their oldest stored message, at {clock_words(seen)}"
        figures.append(_figure("First seen", date_words(seen.date(), today), oldest))
    last = kept.last.astimezone()
    last_day = sentence_case(day_words(last.date(), today))
    figures += [
        _figure("Last message", last_day, f"in this period, at {clock_words(last)}"),
        _figure("Days", whole_words(len(kept.days)), "with a message from them in this period"),
        _part_figure("Laughing", kept.laughing, kept.messages),
        _part_figure("With an emote", kept.with_emote, kept.messages),
        _figure("Mentioned", whole_words(mentioned), "messages of others address them"),
    ]
    return figures


# -- charts ---------------------------------------------------------------------


def _chart_top(peak: int) -> int:
    """What the full height of a chart stands for: the first of 4, 8, 20,
    40, 80, 200... that the tallest column fits under, so that the lines
    across the chart are at round numbers."""
    scale = 1
    while True:
        for step in (4, 8, 20):
            if peak <= step * scale:
                return step * scale
        scale *= 10


def _height(value: int, top: int) -> str:
    return number_words(value / top * 100, 2)


def _levels(top: int) -> list[dict[str, str]]:
    """The lines across a chart: at each quarter of its height, counted
    down from its top edge, with how many messages a column that tall is."""
    return [{"top": str(100 - quarter * 25), "words": whole_words(top * quarter // 4)} for quarter in (4, 3, 2, 1)]


def _column_words(columns: Columns, column: int, today: date, one_day: bool) -> str:
    """What stretch of time a column stands for."""
    start = columns.start(column)
    if columns.days == 1:
        return sentence_case(day_words(start.date(), today))
    if columns.days:
        last = columns.start(column + 1) - timedelta(days=1)
        return f"{date_words(start.date(), today)} to {date_words(last.date(), today)}"
    span = f"{clock_words(start)} to {clock_words(columns.start(column + 1))}"
    return span if one_day else f"{date_words(start.date(), today)}, {span}"


def _axis(columns: Columns, today: date, one_day: bool) -> list[dict[str, str]]:
    """The times written under a chart: where a round time of the clock
    begins a column, as few of them as leaves room for the words."""
    fewest = math.ceil(columns.count / MAX_AXIS_LABELS)
    if columns.days:
        every = next((step for step in AXIS_COLUMNS if step >= fewest), fewest)
    else:
        # Only a step that is a whole number of columns lands on their edges.
        every = next(
            (
                seconds // columns.seconds
                for seconds in AXIS_SECONDS
                if seconds % columns.seconds == 0 and seconds >= fewest * columns.seconds
            ),
            fewest,
        )
    # A time is written from its column's left edge on, so the last few
    # columns have no room for one.
    last = math.floor(columns.count * AXIS_LAST_SHARE)
    marked = [column for column in range(last + 1) if (columns.first + column) % every == 0]

    labels = []
    for column in marked:
        start = columns.start(column)
        at_midnight = (start.hour, start.minute) == (0, 0)
        words = date_words(start.date(), today) if columns.days or (at_midnight and not one_day) else clock_words(start)
        labels.append({"left": number_words(column / columns.count * 100, 3), "words": words})
    return labels


def activity_chart(
    kept: Collected,
    today: date,
    *,
    one_day: bool,
    moments: Iterable[datetime] = (),
    link: Callable[[Columns, int], str | None] | None = None,
) -> dict[str, Any]:
    """Messages over a period as the columns of a chart: how tall each
    stands and how much of it was laughing, both as percentages of the
    chart's height, what it says when pointed at, and where it leads
    (`link`, given the columns and the column's number). A column a moment
    was detected in is marked. `one_day` says the period lies within one
    day, so that times need no date."""
    columns = kept.columns
    in_column = Counter(columns.of(moment) for moment in moments)
    top = _chart_top(max(kept.by_column, default=0))
    drawn = []
    for column, (everything, laughing) in enumerate(zip(kept.by_column, kept.laughing_by_column, strict=True)):
        words = f"{_column_words(columns, column, today, one_day)}: {counted_words(everything, 'message')}"
        if laughing:
            words += f", {whole_words(laughing)} laughing"
        if in_column[column]:
            words += f", {counted_words(in_column[column], 'moment')}"
        drawn.append(
            {
                "all": _height(everything, top),
                "laugh": _height(laughing, top),
                "moment": bool(in_column[column]),
                "words": words,
                "href": link(columns, column) if link and everything else None,
            }
        )
    each = (
        counted_words(columns.days, "day")
        if columns.days
        else counted_words(columns.seconds // 3600, "hour")
        if columns.seconds >= 3600
        else counted_words(columns.seconds // 60, "minute")
    )
    return {
        "columns": drawn,
        "levels": _levels(top),
        "axis": _axis(columns, today, one_day),
        "each": each,
        "label": f"Chat messages in each stretch of {each}, and how many of them were laughing.",
    }


def hours_chart(kept: Collected) -> dict[str, Any]:
    """Messages by the hour of the day they arrived in, whatever the day."""
    top = _chart_top(max(kept.by_hour))
    return {
        "columns": [
            {
                "all": _height(count, top),
                "laugh": "0",
                "moment": False,
                "words": f"{hour:02d}:00 to {(hour + 1) % 24:02d}:00: {counted_words(count, 'message')}",
                "href": None,
            }
            for hour, count in enumerate(kept.by_hour)
        ],
        "levels": _levels(top),
        "axis": [{"left": number_words(hour / 24 * 100, 3), "words": f"{hour:02d}:00"} for hour in range(0, 24, 3)],
        "label": "Chat messages by the hour of the day they arrived in.",
    }


# -- tables ---------------------------------------------------------------------


def _emote(kept: Collected, picture: str) -> dict[str, Any]:
    name, width = kept.emote_shape[picture]
    return {"emote": name, "image": picture, "width": width}


def top_chatters(kept: Collected, limit: int, link: Callable[[str], str] | None = None) -> list[dict[str, Any]]:
    """Who wrote most, the most active first: how many messages, what part
    of all of them, and the emote each used most."""
    rows = []
    for place, (name, count) in enumerate(most(kept.by_chatter, limit), start=1):
        favourite = _most_emotes(kept, kept.chatter_emotes.get(name, Counter()), 1)
        rows.append(
            {
                "place": place,
                "name": name,
                "href": link(name) if link and name else None,
                "messages": whole_words(count),
                "share": share_words(count, kept.messages),
                "emote": _emote(kept, favourite[0][0]) if favourite else None,
            }
        )
    return rows


def most(counter: Counter[str], limit: int) -> list[tuple[str, int]]:
    """The `limit` highest counts, ties in the order of their names - so
    that a table reads the same each time it is made."""
    return sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:limit]


def _most_emotes(kept: Collected, uses: Counter[str], limit: int) -> list[tuple[str, int]]:
    """The same for emotes, which are counted by their pictures and named
    by something else."""
    return sorted(uses.items(), key=lambda item: (-item[1], kept.emote_shape[item[0]][0].lower(), item[0]))[:limit]


def top_emotes(kept: Collected, limit: int) -> list[dict[str, Any]]:
    """The emotes used most: how often, by how many people, and whose it
    is - the platform's own or the channel's on 7TV."""
    return [
        {
            **_emote(kept, picture),
            "source": emote_source(picture),
            "uses": whole_words(uses),
            "people": whole_words(len(kept.emote_people[picture])),
        }
        for picture, uses in _most_emotes(kept, kept.emote_uses, limit)
    ]


def said_most(kept: Collected, limit: int, emotes: Emotes | None = None) -> list[dict[str, Any]]:
    """What was said more than once, the most repeated first (see
    said_key): as text and emotes, how often, and by how many people - one
    account pasting a line twenty times is not twenty people saying it."""
    return [
        {
            "parts": chat_parts(kept.said_first[key][1], emotes),
            "times": whole_words(times),
            "people": whole_words(len(kept.said_people[key])),
        }
        for key, times in most(kept.said, limit)
        if times > 1
    ]


def mentioned_most(kept: Collected, limit: int, link: Callable[[str], str] | None = None) -> list[dict[str, Any]]:
    """Who was addressed with an "@" most: in how many messages, and by how
    many people. A name is written as its owner writes it, where they wrote
    in the period themselves - and only then is there anything of theirs to
    lead to."""
    writers = {name.lower(): name for name in kept.by_chatter}
    rows = []
    for key, count in most(kept.mentions, limit):
        rows.append(
            {
                "name": writers.get(key) or most(kept.mention_spelling[key], 1)[0][0],
                "href": link(writers[key]) if link and key in writers else None,
                "messages": whole_words(count),
                "people": whole_words(len(kept.mention_people[key])),
            }
        )
    return rows


def chatter_emotes(kept: Collected, limit: int = CHATTER_EMOTES) -> list[dict[str, Any]]:
    """The emotes one chatter used most, with how often."""
    return [
        {**_emote(kept, picture), "uses": whole_words(uses)}
        for picture, uses in _most_emotes(kept, kept.emote_uses, limit)
    ]


# -- sessions and messages --------------------------------------------------------


def session_period(session: Row) -> Period:
    """The period that holds a session (a row of db.get_chat_sessions)."""
    return whole_minutes(datetime.fromisoformat(session["first"]), datetime.fromisoformat(session["last"]))


def session_rows(
    sessions: Iterable[Row], today: date, current: Period, link: Callable[[Period], str]
) -> list[dict[str, Any]]:
    """The sessions a channel's chat came in (db.get_chat_sessions) as the
    page lists them: which day, from when to when, how long, how many
    messages - and which of them is the period that is open. One that is
    still going is: its period ends now, short of the minute its newest
    message is in."""
    rows = []
    for session in sessions:
        first = datetime.fromisoformat(session["first"]).astimezone()
        last = datetime.fromisoformat(session["last"]).astimezone()
        period = session_period(session)
        rows.append(
            {
                "day": sentence_case(day_words(first.date(), today)),
                "length": stream_time_words((last - first).total_seconds()),
                "when": f"{clock_words(first)} to {clock_words(last)}, {counted_words(session['messages'], 'message')}",
                "href": link(period),
                "current": period.start == current.start and last < current.end <= period.end,
            }
        )
    return rows


def found_words(total: int, chatter: str | None, text: str) -> str:
    """What a search came to: "312 messages from alice", "1 message with
    "lol" in it"."""
    words = counted_words(total, "message") if total else "No messages"
    if chatter:
        words += f" from {chatter}"
    if text:
        words += f' with "{text}" in {"it" if total == 1 else "them"}'
    return words


def preset_labels() -> list[tuple[str, str]]:
    """The periods the page has a tab for, by name, with what each tab says."""
    return [*((name, label) for name, (label, _reach) in PRESETS.items()), (PRESET_ALL, PRESET_ALL_LABEL)]


def stored_words(stored: Period, today: date) -> str:
    """How far back a channel's stored chat goes, and how far forward."""
    first = stored.start.astimezone().date()
    last = (stored.end.astimezone() - timedelta(microseconds=1)).date()
    if first == last:
        return f"Chat is stored from {day_words(first, today)}."
    return f"Chat is stored from {date_words(first, today)} to {day_words(last, today)}."
