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

# Kick puts its native emotes inline in a message as "[emote:12345:name]",
# and another platform's own emotes are put in the same form when its chat
# is taken in, with the platform before the id: "[emote:twitch.25:Kappa]"
# (the same token detector.NATIVE_EMOTE_TOKEN_PATTERN matches, with the id
# kept). Each platform serves the picture for an id from its own address.
_EMOTE_TOKEN = re.compile(r"\[emote:([0-9A-Za-z_.]+):([^\]]+)\]")
KICK_EMOTE_IMAGE = "https://files.kick.com/emotes/{id}/fullsize"
TWITCH_EMOTE_IMAGE = "https://static-cdn.jtvnw.net/emoticons/v2/{id}/default/dark/2.0"
# Whose a native emote is and where its picture is, by what stands before
# its id - nothing for Kick's, whose ids stand alone.
NATIVE_EMOTE_IMAGES = {"": ("Kick", KICK_EMOTE_IMAGE), "twitch": ("Twitch", TWITCH_EMOTE_IMAGE)}
# 7TV emotes are typed as plain words; which emote a word is depends on the
# channel (see chat_parts). Twice the smallest size, so the picture stays
# sharp on a dense screen.
SEVENTV_EMOTE_IMAGE = "https://cdn.7tv.app/emote/{id}/2x.webp"
# So are BetterTTV's and FrankerFaceZ's, a Twitch channel's besides 7TV's
# (see twitch_emotes.py), kept with their service before their id. Whose an
# emote typed as a word is and where its picture is, by that.
BTTV_EMOTE_IMAGE = "https://cdn.betterttv.net/emote/{id}/2x"
FFZ_EMOTE_IMAGE = "https://cdn.frankerfacez.com/emote/{id}/2"
TYPED_EMOTE_IMAGES = {
    "": ("7TV", SEVENTV_EMOTE_IMAGE),
    "bttv": ("BTTV", BTTV_EMOTE_IMAGE),
    "ffz": ("FFZ", FFZ_EMOTE_IMAGE),
}
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
# A moment made because somebody posted one of the channel's clip commands
# (see tuning.py). The detector had no part in it, so its figures are zero.
COMMAND_REASON = "command"

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


# Where the stretch of a moment's footage that was cut out for an editor
# stands (see clip_trim.py), and what the Trim dialog says about each.
TRIM_NONE = "none"
TRIM_CUTTING = "cutting"
TRIM_CHAT = "chat"
TRIM_BURNING = "burning"
TRIM_READY = "ready"
TRIM_FAILED = "failed"
TRIM_CHAT_FAILED = "chat_failed"
TRIM_WORDS = {
    TRIM_NONE: "",
    TRIM_CUTTING: "Cutting the video.",
    TRIM_CHAT: "The video is ready. Making its chat.",
    TRIM_BURNING: "Making the video with its chat in it.",
    TRIM_READY: "Ready to download.",
    TRIM_FAILED: "The video could not be made. The server log says why.",
    TRIM_CHAT_FAILED: "The video is ready, but its chat could not be made. The server log says why.",
}
# The states in which more is still to come.
TRIM_BUSY = frozenset({TRIM_CUTTING, TRIM_CHAT, TRIM_BURNING})
# A video that begins less than this before the start that was set is not
# worth a sentence.
TRIM_EARLY_SECONDS = 0.1


def trim_words(
    state: str,
    early: float = 0.0,
    stretch: tuple[float, float] | None = None,
    done: float | None = None,
    burned: bool = False,
) -> str:
    """What the Trim dialog says of a cut. `early` is how long before the
    start that was set the video begins (see recorder.trim_clip). `stretch`
    is which seconds of a recording the cut holds: from hours of footage
    more than one is cut, so one that is there to download is named. `done`
    is how much of its chat has been made, as a share, while that goes on;
    `burned`, that the video has its chat drawn into it."""
    words = TRIM_WORDS[state]
    if stretch is not None and state == TRIM_READY:
        words = f"{stream_time_words(stretch[0])} to {stream_time_words(stretch[1])} is ready to download."
    if burned and state == TRIM_READY:
        words = f"{words.removesuffix('.')}, with its chat in it."
    if done is not None and state in (TRIM_CHAT, TRIM_BURNING):
        words = f"{words.removesuffix('.')}: {int(done * 100)} %."
    if early >= TRIM_EARLY_SECONDS and state in (TRIM_CHAT, TRIM_READY, TRIM_CHAT_FAILED):
        words += f" It begins {number_words(early, 1)} s before the start you set, on a frame a cut can begin on."
    return words


# Where the transcript of a moment's footage stands (see subtitles.py), and
# what the page says beside the clip about each.
TRANSCRIPT_NONE = "none"
TRANSCRIPT_MAKING = "making"
TRANSCRIPT_FAILED = "failed"
TRANSCRIPT_READY = "ready"
TRANSCRIPT_WORDS = {
    TRANSCRIPT_NONE: (
        "What is said in the clip and the footage around it, written down line by line with its times: "
        "to correct here, and to take into an editor as subtitles."
    ),
    TRANSCRIPT_MAKING: "Transcribing. The first time takes longer: the speech model has to be loaded.",
    TRANSCRIPT_FAILED: "The transcript could not be made. The server log says why.",
    TRANSCRIPT_READY: "",
}


def transcript_words(state: str, done: float | None = None, said: bool = True) -> str:
    """What the page says of a moment's transcript. `done` is how much of
    the footage has been heard, as a share, while that goes on; `said`,
    whether a transcript that is there has anything in it."""
    if state == TRANSCRIPT_MAKING and done is not None:
        return f"Transcribing: {int(done * 100)} %."
    if state == TRANSCRIPT_READY and not said:
        return "Nothing is said in the footage, or nothing the speech model could make out."
    return TRANSCRIPT_WORDS[state]


# What a time before the clip is written with: a real minus sign, as the
# page's script writes one, not a hyphen.
MINUS = "\u2212"


def clip_time_words(seconds: float, clip_seconds: float | None) -> str:
    """A clip time to the tenth of a second, as the Trim dialog writes one:
    "0:12.5" in the clip, "0:03.0" with a MINUS in front of it before the
    clip, and "+0:10.0" after its end. `clip_seconds` is how long the clip
    is, None where that is not known."""
    after = clip_seconds is not None and seconds > clip_seconds
    sign = MINUS if seconds < 0 else "+" if after else ""
    tenths = round(abs(seconds - clip_seconds if after else seconds) * 10)
    return f"{sign}{tenths // 600}:{tenths // 10 % 60:02d}.{tenths % 10}"


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


NO_CLIP_WORDS = "no clip"


def queue_label(row: Row, reaction: str | None = None, *, named: bool = True) -> str:
    """The few words a queue row has room for: the name the reviewer gave
    the moment or, until it has one, what chat said most in it (`reaction`,
    as chat wrote it) or, failing that, what set it off. `named=False` gives
    what the row would say without its name."""
    if named and row["title"]:
        words = [row["title"]]
    elif reaction:
        words = [reaction]
    elif row["reason"] == IMPORT_REASON:
        words = ["Imported"]
    elif row["reason"] == COMMAND_REASON:
        words = ["On command"]
    else:
        words = [sentence_case(", ".join(reason_words(row["reason"], main_only=True)))]
    if not row["clip_path"]:
        words.append(NO_CLIP_WORDS)
    return ", ".join(words)


def summary_words(row: Row) -> str:
    """The detector's figures for a moment as a sentence or three."""
    if row["reason"] == IMPORT_REASON:
        return "Imported clip. No chat was measured for it."
    if row["reason"] == COMMAND_REASON:
        return "Asked for in chat. The detector had no part in it."

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


def typed_emote_image(emote_id: str) -> str | None:
    """Where the picture of an emote typed as a word is, by its id as it is
    kept (see TYPED_EMOTE_IMAGES) - None for a service there is no address
    for."""
    service, _, bare = emote_id.rpartition(".")
    known = TYPED_EMOTE_IMAGES.get(service)
    return known[1].format(id=bare) if known and bare else None


def _text_parts(text: str, emotes: Mapping[str, tuple[str, int, int]]) -> list[dict[str, Any]]:
    """A stretch of typed text with the channel's 7TV emotes picked out of
    it - and on Twitch its BetterTTV and FrankerFaceZ ones. As in the 7TV
    extension, an emote is a whole word spelled exactly like its name:
    "KEKW" is one, "kekw" and "KEKW!" are not."""
    parts: list[dict[str, Any]] = []
    plain = ""
    for piece in _WHITESPACE.split(text):
        image = typed_emote_image(emotes[piece][0]) if piece in emotes else None
        if image is None:
            plain += piece
            continue
        if plain:
            parts.append({"text": plain})
            plain = ""
        _, width, height = emotes[piece]
        parts.append({"emote": piece, "image": image, "width": _emote_width(width, height)})
    if plain:
        parts.append({"text": plain})
    return parts


def chat_parts(content: str, emotes: Mapping[str, tuple[str, int, int]] | None = None) -> list[dict[str, Any]]:
    """A chat message as plain text and emotes, in order. Each emote comes
    with its name, the address of its picture and how wide to draw it.

    A platform's own emotes are in the message itself. 7TV emotes are
    ordinary words that mean a picture only in a channel that has an emote
    of that name, so `emotes` is that channel's own set - (emote id, width,
    height) by name, see db.get_channel_emotes. Without it they stay
    words."""
    emotes = emotes or {}
    parts: list[dict[str, Any]] = []
    position = 0
    for token in _EMOTE_TOKEN.finditer(content):
        parts.extend(_text_parts(content[position : token.start()], emotes))
        image = native_emote_image(token.group(1))
        # A platform there is no picture for: its emote's name, as text.
        parts.append(
            {"emote": token.group(2), "image": image, "width": EMOTE_HEIGHT} if image else {"text": token.group(2)}
        )
        position = token.end()
    parts.extend(_text_parts(content[position:], emotes))
    return parts


def native_emote_image(token_id: str) -> str | None:
    """Where the picture of one of a platform's own emotes is, by the id
    its token carries (see _EMOTE_TOKEN) - None for a platform there is no
    address for."""
    platform, _, emote_id = token_id.rpartition(".")
    known = NATIVE_EMOTE_IMAGES.get(platform)
    return known[1].format(id=emote_id) if known and emote_id else None


def emote_source(picture: str) -> str:
    """Whose emote the picture at this address is: a platform's own - Kick's,
    Twitch's - or a channel's on 7TV, BetterTTV or FrankerFaceZ."""
    for source, template in (*NATIVE_EMOTE_IMAGES.values(), *TYPED_EMOTE_IMAGES.values()):
        if picture.startswith(template.split("{", 1)[0]):
            return source
    return "7TV"


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
                # What the row goes back to saying if its name is taken away.
                "unnamed": queue_label(row, (reactions or {}).get(row["id"]), named=False),
                "has_clip": bool(row["clip_path"]),
                "downloaded": row["downloaded_at"] is not None,
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


def now_words(*, live: bool, recorded: bool = False, looked_at: bool = True) -> str:
    """What a channel is doing right now, in a word or two - and, in the top
    bar, the service as a whole. "Live" is a channel whose stream is coming
    in; "Recording" is kept for one whose stream is also being kept whole
    (`recorded`), so that the word means one thing. A channel nothing is
    coming in from is not live as far as the service can tell - or, if it is
    neither tracked nor recorded (`looked_at`), simply not looked at."""
    if live:
        return "Recording" if recorded else "Live"
    return "Not live" if looked_at else "Not watched"


def channel_count_words(recording: int, tracked: int) -> str:
    """The words beside the lamp in the top bar: how many of the channels
    that are looked at are live (`recording`: their streams are coming in)."""
    if tracked == 0:
        return "No channels watched"
    if recording:
        return f"{recording} of {count_words(tracked, 'channel')}"
    return f"{count_words(tracked, 'channel')} watched"


def service_words(*, recording: int, tracked: int, watchlist: int, watching: bool, shutting_down: bool) -> str:
    """What the service is doing right now, for a page with no moment to
    show - so that "nothing new" can be told apart from "not running".
    `recording` is how many of the `tracked` channels are live, their
    streams coming in."""
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
            return f"The watched channel is live. When its chat erupts, {lands}."
        return f"The watched channel is not live right now. When it goes live and its chat erupts, {lands}."
    if recording == 0:
        return (
            f"None of the {tracked} watched channels is live right now. "
            f"When one goes live and its chat erupts, {lands}."
        )
    are = "is" if recording == 1 else "are"
    return f"{recording} of the {tracked} watched channels {are} live. When chat erupts, {lands}."


# What a shutdown can be waiting on; the script says the same when the
# button is pressed.
SHUTDOWN_JOBS_WORDS = "clips being cut or analysed, recordings being saved"


def shutdown_words(pending: int) -> str:
    """What a shutdown that has been asked for is still waiting on. `pending`
    counts open moments and the work that follows one - cutting its clip,
    each analysis step - and stream recordings being made into their files."""
    if pending == 0:
        return "Nothing new is being watched."
    return (
        f"Waiting for {count_words(pending, 'job')} to finish ({SHUTDOWN_JOBS_WORDS}). Nothing new is being watched."
    )


# -- stream recordings --------------------------------------------------------


def size_words(size: int) -> str:
    """How much room something takes, as it is said: "512 MB", "4.2 GB"."""
    megabytes = size / 1024**2
    if megabytes < 1000:
        return f"{round(megabytes)} MB"
    return f"{number_words(megabytes / 1024, 1)} GB"


def duration_words(seconds: float) -> str:
    """A length of time written out: "38 seconds", "1 minute 32 seconds",
    "2 hours 5 minutes" - to the second up to an hour, to the minute past it."""
    whole = round(seconds)
    if whole < 1:
        return "less than a second"
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    words = []
    if hours:
        words.append(count_words(hours, "hour"))
    if minutes:
        words.append(count_words(minutes, "minute"))
    if secs and not hours:
        words.append(count_words(secs, "second"))
    return " ".join(words)


def recording_state(
    recording_id: int, *, running: bool, on_air: bool = False, seconds: float | None = None, size: int = 0
) -> dict[str, Any]:
    """What the pages say of a stream recording that is not over yet: one
    that runs (`running`; `on_air` while its channel's stream is coming in)
    or one that is being made into its file. `seconds` is how much footage
    it has, where that is known, and `size` how much room that takes."""
    kept = f"{stream_time_words(seconds)} recorded" if seconds else None
    if not running:
        words = f"Saving {stream_time_words(seconds)}" if seconds else "Saving"
    elif kept and on_air:
        words = f"{kept}, {size_words(size)}"
    elif kept:
        words = f"{kept}, the stream is off"
    elif on_air:
        words = "Starting"
    else:
        words = "Waiting for the stream"
    return {"id": recording_id, "running": running, "words": words}


def recording_row_words(state: str, not_over: Mapping[str, Any] | None) -> str | None:
    """What a recording's row in the list says beyond when it was and how
    long it runs: what it is doing while it is not over (`not_over`, see
    recording_state), or that no file came of it. Nothing for one that can
    simply be played."""
    if not_over is not None:
        return not_over["words"]
    return "Not saved" if state == "failed" else None


# What stopping a recording answers with: whether it had kept anything.
RECORDING_STOPPED_WORDS = {
    True: "Saving the recording. It will be on the Recordings page.",
    False: "Nothing had been recorded yet.",
}


def recording_stopped_words(kept: bool, always: bool) -> str:
    """What stopping a recording answers with. For a channel that is
    recorded always it also says what stopping does not do: switch that
    off."""
    words = RECORDING_STOPPED_WORDS[kept]
    return f"{words} The channel's next stream is recorded again." if always else words


# What switching a channel to being recorded always, or back, answers with.
ALWAYS_RECORD_WORDS = {
    True: "Every stream of {channel} is recorded from when it goes live. Stopping a recording ends it for that stream.",
    False: "Streams of {channel} are recorded only when you start a recording.",
}


def chat_delay_words(seconds: int) -> str:
    """Where chat is put against the picture, as the review page words it:
    how long before it arrived (see chat_trace.CHAT_DELAY_SECONDS)."""
    if seconds > 0:
        return f"Chat {seconds} s earlier"
    return f"Chat {-seconds} s later" if seconds < 0 else "Chat as it arrived"


def recording_span_words(started: datetime, ended: datetime | None, today: date) -> str:
    """When a recording's footage was on air: "Yesterday, 21:04 to 23:31"."""
    words = f"{sentence_case(day_words(started.date(), today))}, {clock_words(started)}"
    return f"{words} to {clock_words(ended)}" if ended else words


# Why a recording ended, where that is worth a sentence (the keys are
# stream_recording.py's). One that was stopped by hand needs none. `{grace}`
# is how long a stream has to have been gone for its recording to end itself.
RECORDING_ENDED_WORDS = {
    "stopped": "",
    "stream_ended": "It ended by itself once the stream had been gone for {grace}.",
    "disk_full": "It was ended because the disk was running out of room.",
    "write_failed": "It was ended because its footage could no longer be written to the disk.",
    "shutdown": "It was ended by the service being shut down.",
    "interrupted": "The service stopped while it was running, which is where it ends.",
}


def recording_summary_words(
    size: int | None, parts: int, missing: float, ended_by: str | None, grace_seconds: float
) -> str:
    """What is worth knowing of a recording that can be played, in a
    sentence or three: how big its file is, whether the stream broke off
    while it was recorded (`parts` is how many unbroken stretches the file
    is made of, `missing` how many seconds of the stream fell between them)
    and why it ended."""
    sentences = []
    if size:
        sentences.append(f"{size_words(size)}.")
    if parts > 1:
        sentences.append(
            f"The recording has {count_words(parts - 1, 'break')}, "
            f"where the file is missing {duration_words(missing)} of the stream."
        )
    ended = RECORDING_ENDED_WORDS.get(ended_by or "", "").format(grace=duration_words(grace_seconds))
    if ended:
        sentences.append(ended)
    return " ".join(sentences)


def free_room_words(free: int) -> str:
    """How much room the disk recordings are kept on has left."""
    return f"{size_words(free)} free on the disk recordings are kept on."


# Where looking for moments in a recording's chat stands (see
# recording_moments.py), and what the Recordings page says of each state
# that needs no figures to say it.
FIND_NONE = "none"
FIND_LOOKING = "looking"
FIND_FOUND = "found"
FIND_CUTTING = "cutting"
FIND_FAILED = "failed"
FIND_WORDS = {
    FIND_NONE: (
        "Replays the chat stored with this recording through the detector, under the channel's settings as "
        "they are saved, and lists what would have been a moment."
    ),
    FIND_FAILED: "The recording's chat could not be replayed. The server log says why.",
}
# The states in which more is still to come.
FIND_BUSY = frozenset({FIND_LOOKING, FIND_CUTTING})


# Where the chat of a whole recording, as a video of its own, stands (see
# main._recording_chat_file): never asked for, being made, there to
# download, or failed.
CHAT_FILE_NONE = "none"
CHAT_FILE_MAKING = "making"
CHAT_FILE_READY = "ready"
CHAT_FILE_FAILED = "failed"


def chat_file_words(state: str, done: float | None = None) -> str:
    """What the Recordings page says, beside Download chat, of the chat
    video of a whole recording. `done` is how much of it has been made, as
    a share, while that goes on."""
    if state == CHAT_FILE_MAKING:
        return f"Making the chat video: {int((done or 0.0) * 100)} %."
    if state == CHAT_FILE_FAILED:
        return "The chat video could not be made. The server log says why."
    return ""


def find_words(
    state: str,
    *,
    found: int = 0,
    already: int = 0,
    missing: int = 0,
    messages: int = 0,
    looked: datetime | None = None,
    today: date | None = None,
    done: int = 0,
    of: int = 0,
    made: int = 0,
    failed: int = 0,
) -> str:
    """What the Recordings page says of the moments looked for in a
    recording. While its chat is replayed: how many messages of how many
    have been (`done`, `of`). Once it has been: how many moments that came
    to (`found`) in how many `messages`, under the settings as they were
    when it was `looked` for, how many of them are moments `already`, and
    how many more the recording is `missing` the footage of. While clips are
    cut, which one of how many (`done`, `of` again); after that, how many
    were `made` and how many `failed`."""
    if state == FIND_LOOKING:
        if not of:
            return "Reading the recording's chat."
        return (
            f"Replaying the recording's chat: {done} of {count_words(of, 'message')}. "
            "A long stream with a busy chat takes minutes."
        )
    if state == FIND_CUTTING:
        return f"Cutting clip {min(done + 1, of)} of {of}."
    if state != FIND_FOUND:
        return FIND_WORDS[state]

    sentences = []
    if made:
        sentences.append(f"{count_words(made, 'clip')} made, and in the review queue.")
    if failed:
        sentences.append(f"{count_words(failed, 'clip')} could not be cut. The server log says why.")
    if not messages:
        sentences.append("No chat was stored for this recording, so there is nothing to look for moments in.")
        return " ".join(sentences)

    when = since_words(looked, today) if looked is not None and today is not None else "then"
    chat = f"the recording's chat ({count_words(messages, 'message')})"
    under = f"under the settings as they were saved at {when}"
    if not found:
        sentences.append(f"Nothing in {chat} would have been a moment {under}.")
    else:
        sentences.append(f"{count_words(found, 'moment')} in {chat}, {under}.")
        if already == found:
            sentences.append("It is a moment already." if found == 1 else "All of them are moments already.")
        elif already:
            sentences.append(f"{already} of them {'is a moment' if already == 1 else 'are moments'} already.")
    if missing:
        them = "it" if missing == 1 else "them"
        sentences.append(f"{missing} more fell where the recording has too little footage of {them}.")
    return " ".join(sentences)


def found_words(reason: str, score: float, clip_seconds: float) -> str:
    """What the list of moments found in a recording says of one: what set
    it off, how strongly, and how long its clip is."""
    reasons = sentence_case(", ".join(reason_words(reason, main_only=True)))
    return f"{reasons}, score {number_words(score, 1)}, a clip of {duration_words(clip_seconds)}"


def own_settings_words(channels: list[str]) -> str | None:
    """Which channels the settings for all channels do not hold for in
    everything, said on the page those are set on - or None if they hold
    for all of them."""
    if not channels:
        return None
    have = "has settings of its own" if len(channels) == 1 else "have settings of their own"
    return f"{list_words(channels)} {have} on top of these."


def trial_words(
    messages: int, hours: float, first: datetime, last: datetime, today: date, rows: list[tuple[str, Any]]
) -> dict[str, Any]:
    """What trying settings on stored chat came to: what was replayed, and
    for each set of settings (a label and a replay.Summary) how many moments
    it would have made and how long their clips would have been."""

    def seconds(value: float | None) -> str:
        return "-" if value is None else f"{value:.0f} s"

    long = number_words(hours, 1)
    return {
        "summary": (
            f"{count_words(messages, 'message')} over {long} {'hour' if long == '1' else 'hours'} of chat, "
            f"from {since_words(first, today)} to {since_words(last, today)}."
        ),
        "rows": [
            {
                "label": label,
                "moments": str(summary.moments),
                "per_hour": number_words(summary.per_hour, 1),
                "not_extended": str(summary.not_extended),
                "median": seconds(summary.median_clip_seconds),
                "longest": seconds(summary.longest_clip_seconds),
            }
            for label, summary in rows
        ],
    }


# What each per-clip analysis step does, said beside the switch that turns
# it on. Keyed like main.ANALYSIS_SETTINGS.
ANALYSIS_CAPTIONS = {
    "transcript": "Speech in each new clip is written down.",
    "audio_events": "The language, the mood and sounds such as laughter are tagged.",
    "sound_events": "What is heard in each new clip is tagged: speech, music, laughter, game sound.",
    "frames": "Three frames of each new clip are stored as embeddings for a future model.",
}
