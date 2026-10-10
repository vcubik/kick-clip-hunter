"""What the dashboard says about moments, worked out from their stored rows.

Everything the pages show that is not simply a stored value read back lives
here: how the queue is grouped by stream, how a time, a count or a detector
reason is put into words, how a chat line is split into text and emotes.
All of it is pure - rows and the current date come in as arguments - so the
wording can be tested without rendering a page.

The wording follows a few rules: sentence case, units written out in
sentences ("12 messages in 10 seconds"), stream time as h:mm:ss, and names
the reviewer would use rather than the detector's ("laughing", not "laugh").
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from .timeutil import to_local_datetime

# The three lists the review queue offers.
SHOW_UNRATED = "unrated"
SHOW_ALL = "all"
SHOW_BEST = "best"
SHOWS = (SHOW_UNRATED, SHOW_ALL, SHOW_BEST)
SHOW_LABELS = {SHOW_UNRATED: "Unrated", SHOW_ALL: "All", SHOW_BEST: "Best"}
# A clip rated this or higher is listed under "Best".
BEST_RATING_MIN = 4

# How many colours chat usernames are spread over (nick-1 to nick-6 in the
# stylesheet).
NICK_COLOURS = 6

# Kick puts its native emotes inline in a message as "[emote:12345:name]"
# (the same token detector.NATIVE_EMOTE_TOKEN_PATTERN matches, with the id
# kept), and serves the picture for an id from here.
_EMOTE_TOKEN = re.compile(r"\[emote:(\d+):([^\]]+)\]")
KICK_EMOTE_IMAGE = "https://files.kick.com/emotes/{id}/fullsize"
# 7TV emotes are typed as plain words; which emote a word is depends on the
# channel (see chat_parts). Twice the smallest size, so the picture stays
# sharp on a dense screen.
SEVENTV_EMOTE_IMAGE = "https://cdn.7tv.app/emote/{id}/2x.webp"
# An emote is as tall as a line of chat (the stylesheet's .ch-emote) and as
# wide as its own shape makes it, within reason.
EMOTE_HEIGHT = 20
EMOTE_MAX_WIDTH = 80
_WHITESPACE = re.compile(r"(\s+)")

# Two moments belong to the same stream when the times that stream went
# live, worked out from each of them, agree this closely. They normally agree
# to the second; a stream that was restarted differs by its whole length.
SAME_STREAM_TOLERANCE = timedelta(minutes=2)

# What set a moment off, in the detector's terms and in the reviewer's.
REASON_WORDS = {
    "laugh": "laughing",
    "emotes": "emotes",
    "emote_mention": "emote names",
    "message_rate": "a busy chat",
}
# Never fires a moment by itself (see detector.py), so it is named last and
# left out where there is only room for the main reason.
BOOSTER_REASONS = frozenset({"message_rate"})
# scripts/import_clip.py stores clips from elsewhere under this reason, with
# every detector figure at zero.
IMPORT_REASON = "manual_import"

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

Row = Mapping[str, Any]


# -- words ------------------------------------------------------------------


def count_words(count: int, noun: str) -> str:
    """A count with its noun: "1 message", "12 messages"."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def number_words(value: float, places: int = 2) -> str:
    """A measured value without trailing zeros: 0.5, 1.25, 12."""
    text = f"{value:.{places}f}"
    text = text.rstrip("0").rstrip(".") if "." in text else text
    return "0" if text == "-0" else text


def list_words(items: list[str]) -> str:
    """A list as it is said: "a", "a and b", "a, b and c"."""
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


def sentence_case(text: str) -> str:
    return text[:1].upper() + text[1:]


def clock_words(moment: datetime) -> str:
    return f"{moment:%H:%M}"


def date_words(day: date, today: date) -> str:
    """A date as "8 October", with the year when it is not this one."""
    words = f"{day.day} {_MONTHS[day.month - 1]}"
    return words if day.year == today.year else f"{words} {day.year}"


def day_words(day: date, today: date) -> str:
    """A day as "today", "yesterday", or written out: "Thursday 8 October"."""
    if day == today:
        return "today"
    if day == today - timedelta(days=1):
        return "yesterday"
    return f"{_WEEKDAYS[day.weekday()]} {date_words(day, today)}"


def when_words(moment: datetime, today: date) -> str:
    """A day and time: "Thursday 8 October at 21:47"."""
    return f"{sentence_case(day_words(moment.date(), today))} at {clock_words(moment)}"


def since_words(moment: datetime, today: date) -> str:
    """The time alone for today, otherwise the day with it."""
    if moment.date() == today:
        return clock_words(moment)
    return f"{day_words(moment.date(), today)}, {clock_words(moment)}"


def stream_time_words(seconds: int | None) -> str | None:
    """How far into its stream a moment was, as h:mm:ss; None when that was
    not recorded."""
    if seconds is None:
        return None
    hours, rest = divmod(max(0, int(seconds)), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


# Where a moment's chat video stands (see chat_video.py), and what the
# page says about each.
CHAT_VIDEO_NONE = "none"
CHAT_VIDEO_RENDERING = "rendering"
CHAT_VIDEO_READY = "ready"
CHAT_VIDEO_FAILED = "failed"
CHAT_VIDEO_WORDS = {
    CHAT_VIDEO_NONE: "Chat as a video to lay over the clip",
    CHAT_VIDEO_RENDERING: "Rendering the chat video. That takes a minute or two.",
    CHAT_VIDEO_READY: "Chat video is ready",
    CHAT_VIDEO_FAILED: "The chat video could not be rendered. The server log says why.",
}


def chat_video_words(state: str) -> str:
    return CHAT_VIDEO_WORDS[state]


def rating_words(rating: int | None) -> str:
    return f"rated {rating} of 5" if rating else "not rated yet"


def reason_words(reason: str, *, main_only: bool = False) -> list[str]:
    """The detector's comma-separated reasons as words, the main ones first.
    A reason this page has no word for is shown as it was stored."""
    tokens = [token.strip() for token in reason.split(",") if token.strip()]
    main = [token for token in tokens if token not in BOOSTER_REASONS]
    boosters = [token for token in tokens if token in BOOSTER_REASONS]
    chosen = main if main_only and main else main + boosters
    return [REASON_WORDS.get(token, token.replace("_", " ")) for token in chosen]


def queue_label(row: Row, reaction: str | None = None) -> str:
    """The few words a queue row has room for: what chat said most in the
    moment (`reaction`, as chat wrote it) or, failing that, what set the
    moment off."""
    if reaction:
        words = [reaction]
    elif row["reason"] == IMPORT_REASON:
        words = ["Imported"]
    else:
        words = [sentence_case(", ".join(reason_words(row["reason"], main_only=True)))]
    if not row["clip_path"]:
        words.append("no clip")
    return ", ".join(words)


def summary_words(row: Row) -> str:
    """The detector's figures for a moment as a sentence or three."""
    if row["reason"] == IMPORT_REASON:
        return "Imported clip. No chat was measured for it."

    reasons = reason_words(row["reason"])
    score = f"Score {number_words(row['score'], 1)}"
    sentences = [f"{score}, set off by {list_words(reasons)}." if reasons else f"{score}."]

    messages = count_words(row["message_count"], "message")
    if row["current_message_rate"] > 0:
        # The window the count was taken over is not stored, but the count
        # and the rate over that same window are.
        seconds = round(row["message_count"] / row["current_message_rate"])
        messages += f" in {count_words(seconds, 'second')}"
    sentences.append(f"{messages}; the usual pace is {number_words(row['baseline_message_rate'])} a second.")

    among = []
    if row["keyword_hits"]:
        plural = "" if row["keyword_hits"] == 1 else "s"
        among.append(f"{row['keyword_hits']} laugh{plural} or emote name{plural}")
    if row["emote_count"]:
        among.append(count_words(row["emote_count"], "emote"))
    if among:
        sentences.append(f"{list_words(among)} among them.")
    return " ".join(sentences)


# -- chat -------------------------------------------------------------------


def nick_colour(name: str) -> int:
    """Which of the username colours a name gets. Worked out from the name
    itself, so a person keeps their colour from one moment to the next."""
    value = 0
    for character in name:
        value = (value * 31 + ord(character)) % 997
    return value % NICK_COLOURS + 1


def _emote_width(width: int, height: int) -> int:
    """How wide an emote of that shape is drawn. One of unknown shape is
    taken to be square."""
    if width <= 0 or height <= 0:
        return EMOTE_HEIGHT
    return max(1, min(EMOTE_MAX_WIDTH, round(EMOTE_HEIGHT * width / height)))


def _text_parts(text: str, emotes: Mapping[str, tuple[str, int, int]]) -> list[dict[str, Any]]:
    """A stretch of typed text with the channel's 7TV emotes picked out of
    it. As in the 7TV extension, an emote is a whole word spelled exactly
    like its name: "KEKW" is one, "kekw" and "KEKW!" are not."""
    parts: list[dict[str, Any]] = []
    plain = ""
    for piece in _WHITESPACE.split(text):
        if piece not in emotes:
            plain += piece
            continue
        if plain:
            parts.append({"text": plain})
            plain = ""
        emote_id, width, height = emotes[piece]
        parts.append(
            {"emote": piece, "image": SEVENTV_EMOTE_IMAGE.format(id=emote_id), "width": _emote_width(width, height)}
        )
    if plain:
        parts.append({"text": plain})
    return parts


def chat_parts(content: str, emotes: Mapping[str, tuple[str, int, int]] | None = None) -> list[dict[str, Any]]:
    """A chat message as plain text and emotes, in order. Each emote comes
    with its name, the address of its picture and how wide to draw it.

    Native Kick emotes are in the message itself. 7TV emotes are ordinary
    words that mean a picture only in a channel that has an emote of that
    name, so `emotes` is that channel's own set - (emote id, width, height)
    by name, see db.get_channel_emotes. Without it they stay words."""
    emotes = emotes or {}
    parts: list[dict[str, Any]] = []
    position = 0
    for token in _EMOTE_TOKEN.finditer(content):
        parts.extend(_text_parts(content[position : token.start()], emotes))
        parts.append(
            {"emote": token.group(2), "image": KICK_EMOTE_IMAGE.format(id=token.group(1)), "width": EMOTE_HEIGHT}
        )
        position = token.end()
    parts.extend(_text_parts(content[position:], emotes))
    return parts


# -- the queue --------------------------------------------------------------


def review_url(
    show: str = SHOW_UNRATED, channel: str | None = None, *, offset: int = 0, moment: int | None = None
) -> str:
    """The review page for one list, channel, page and open moment. Whatever
    is at its default is left out, so the plain address is the unrated list."""
    query: dict[str, Any] = {}
    if show != SHOW_UNRATED:
        query["show"] = show
    if channel:
        query["channel"] = channel
    if offset:
        query["offset"] = offset
    if moment is not None:
        query["moment"] = moment
    return "/dashboard" + (f"?{urlencode(query)}" if query else "")


def _same_stream(group: dict[str, Any], started: datetime | None, day: date) -> bool:
    if group["started"] is None or started is None:
        return group["started"] is None and started is None and group["day"] == day
    return abs(group["started"] - started) <= SAME_STREAM_TOLERANCE


def queue_groups(
    rows: Iterable[Row], today: date, reactions: Mapping[int, str | None] | None = None
) -> list[dict[str, Any]]:
    """The queue's rows, given newest first, grouped by the stream they came
    from. `reactions` maps a moment's id to what chat said most in it, where
    that is known (see chat_trace.reaction_words).

    A stream is a channel and the time it went live, which is known for any
    moment that recorded how far into the stream it was. Moments without it
    (imported clips, or Kick's API not answering at the time) are grouped by
    channel and day instead. Groups are in the order of their newest moment,
    so the list as a whole still reads newest first.
    """
    groups: list[dict[str, Any]] = []
    for row in rows:
        detected = to_local_datetime(row["detected_at"])
        elapsed = row["stream_elapsed_seconds"]
        started = detected - timedelta(seconds=elapsed) if elapsed is not None else None
        day = (started or detected).date()

        group = next(
            (g for g in groups if g["channel"] == row["channel_slug"] and _same_stream(g, started, day)),
            None,
        )
        if group is None:
            when = day_words(day, today)
            if started is not None:
                when += f", from {clock_words(started)}"
            group = {"channel": row["channel_slug"], "started": started, "day": day, "when": when, "rows": []}
            groups.append(group)

        group["rows"].append(
            {
                "id": row["id"],
                # Stream time where it is known; the time of day otherwise.
                "time": stream_time_words(elapsed) or clock_words(detected),
                "time_title": when_words(detected, today),
                "what": queue_label(row, (reactions or {}).get(row["id"])),
                "rating": row["rating"],
                "rating_words": rating_words(row["rating"]),
            }
        )
    return groups


def empty_queue_words(show: str, channel: str | None, newest: datetime | None, today: date) -> dict[str, str]:
    """What to say when the chosen list has nothing in it: a few words for
    the queue, a heading for the rest of the page and, where the state of the
    service is not what explains it, a sentence to go under that heading.
    `newest` is when the last moment of any rating was detected (on that
    channel, if one is chosen), or None if there has never been one."""
    of_channel = f" from {channel}" if channel else ""
    if newest is not None and show == SHOW_BEST:
        words = f"No clips rated {BEST_RATING_MIN} or higher"
        return {
            "queue": words,
            "heading": f"{words}{of_channel} yet",
            "lead": f"A clip you rate {BEST_RATING_MIN} or higher is listed here.",
        }
    if newest is not None and show == SHOW_UNRATED:
        return {
            "queue": "No unrated moments",
            "heading": f"Nothing new{of_channel} since {since_words(newest, today)}",
        }
    return {"queue": "No moments yet", "heading": f"No moments{of_channel} yet"}


# -- the service ------------------------------------------------------------


def channel_count_words(recording: int, tracked: int) -> str:
    """The words beside the recording lamp in the top bar."""
    if tracked == 0:
        return "No channels watched"
    if recording:
        return f"{recording} of {count_words(tracked, 'channel')}"
    return f"{count_words(tracked, 'channel')} watched"


def service_words(*, recording: int, tracked: int, watchlist: int, watching: bool, shutting_down: bool) -> str:
    """What the service is doing right now, for a page with no moment to
    show - so that "nothing new" can be told apart from "not running"."""
    if shutting_down:
        return "The service is shutting down, so nothing new is being watched."
    if watchlist == 0:
        return "No channels are on the watchlist yet. Add one under Channels to start."
    if not watching:
        return "Watching is off. Nothing new arrives until you turn it on."
    if tracked == 0:
        return "Tracking is off for every channel on the watchlist."

    lands = "the moment lands in the queue"
    if tracked == 1:
        if recording:
            return f"The watched channel is live and being recorded. When its chat erupts, {lands}."
        return f"The watched channel is not being recorded right now. When it goes live and its chat erupts, {lands}."
    if recording == 0:
        return (
            f"None of the {tracked} watched channels is being recorded right now. "
            f"When one goes live and its chat erupts, {lands}."
        )
    are = "is" if recording == 1 else "are"
    return f"{recording} of the {tracked} watched channels {are} live and being recorded. When chat erupts, {lands}."


def shutdown_words(pending: int) -> str:
    """What a shutdown that has been asked for is still waiting on. `pending`
    counts open moments and the work that follows one: cutting its clip, its
    context clips, each analysis step."""
    if pending == 0:
        return "Nothing new is being watched."
    return (
        f"Waiting for {count_words(pending, 'job')} to finish (clips being cut or analysed). "
        "Nothing new is being watched."
    )


# What each per-clip analysis step does, said beside the switch that turns
# it on. Keyed like main.ANALYSIS_SETTINGS.
ANALYSIS_CAPTIONS = {
    "transcript": "Speech in each new clip is written down.",
    "audio_events": "The language, the mood and sounds such as laughter are tagged.",
    "sound_events": "What is heard in each new clip is tagged: speech, music, laughter, game sound.",
    "frames": "Three frames of each new clip are stored as embeddings for a future model.",
}
