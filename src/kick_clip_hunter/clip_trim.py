"""A stretch of footage, chosen in the Trim dialog, as a file to take away -
and, if asked for, its chat as a video to lay over it.

A moment's file holds the clip with the footage around it, and what an
editor wants of it is rarely exactly the clip: the review page's Trim
dialog lets them set where their cut starts and ends anywhere in the file.
The Recordings page has the same dialog over a stream that was kept whole,
where the stretch can be any part of hours of footage. This module is what
either comes to before anything is cut: what the files are called, what may
be asked for, and where in the file it lies. The cutting itself is
`recorder.trim_clip`, the chat video `chat_video.py`.

Times of a moment are clip time, as everywhere on the review page: seconds
from the clip's first frame, negative in the footage before it. Times of a
recording are seconds of its file.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# The shortest stretch that can be cut.
MIN_SECONDS = 1.0
# How far the chat video's lines can be moved against the picture, either
# way, for one cut. Where they stand to begin with is the channel's chat
# delay (see chat_trace.py), which is a guess; this is the editor putting it
# right for the one video they are making.
CHAT_SHIFT_LIMIT_SECONDS = 10.0
# The footage's ends are known to the page from the player, to the server
# from what was stored when the file was cut. A time this far past an end is
# taken to mean the end.
SLACK_SECONDS = 1.0
# The longest stretch chat can go with as a video. Rendering one takes about
# twice as long as it runs and three or four megabytes for every second of
# it (see chat_video.py), which nobody means to ask for by ticking a box
# over half an hour of a recording. A moment's file is never this long.
CHAT_VIDEO_MAX_SECONDS = 600.0
# How much of the chat from before a stretch of a recording is looked at
# for the lines its chat video starts out with (chat_video.PREFILL_LINES).
CHAT_VIDEO_LEAD_SECONDS = 300.0


# What the chat video has behind its text when it is not to be laid over
# the picture but put next to it. It goes into a style attribute, so it is
# a colour written one way and nothing else.
DEFAULT_CHAT_BACKGROUND = "#18181b"
_COLOUR = re.compile(r"#[0-9a-fA-F]{6}")


class TrimError(ValueError):
    """What was asked for cannot be cut; says why."""


def video_name(clip_name: str) -> str:
    """The trimmed video's file name, next to the clip it was cut from."""
    return f"{Path(clip_name).stem}_trim.mp4"


def _number(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise TrimError(f"{what} must be a number of seconds")
    return float(value)


def footage(row: Mapping[str, Any]) -> tuple[float, float]:
    """Where the footage in a moment's file starts and ends, in clip time."""
    return -(row["context_before"] or 0.0), row["clip_duration"] + (row["context_after"] or 0.0)


def stretch(first: float, last: float, start: Any, end: Any) -> tuple[float, float]:
    """The stretch from `start` to `end` of footage that runs from `first`
    to `last`, as it can be cut. Raises TrimError for one that is not in
    the footage or is too short to cut."""
    start, end = _number(start, "start"), _number(end, "end")
    if start < first - SLACK_SECONDS or end > last + SLACK_SECONDS:
        raise TrimError("the stretch asked for is not in the footage")
    start, end = max(start, first), min(end, last)
    if end - start < MIN_SECONDS:
        raise TrimError(f"the stretch has to be at least {MIN_SECONDS:g} s long")
    return start, end


def file_span(row: Mapping[str, Any], start: Any, end: Any) -> tuple[float, float]:
    """Where in a moment's file the stretch from `start` to `end` lies, in
    seconds from the file's first frame. Raises TrimError for a stretch that
    is not in the footage or is too short to cut."""
    first, last = footage(row)
    start, end = stretch(first, last, start, end)
    return start - first, end - first


def with_chat(seconds: float) -> None:
    """Raises TrimError for a stretch too long for its chat to go with it
    as a video (see CHAT_VIDEO_MAX_SECONDS)."""
    if seconds > CHAT_VIDEO_MAX_SECONDS:
        raise TrimError(
            f"chat can go as a video with a stretch of up to {CHAT_VIDEO_MAX_SECONDS / 60:g} minutes, "
            f"and this one is {seconds / 60:.1f} minutes long"
        )


# -- a stretch of a recording ------------------------------------------------
#
# A recording has one cut at a time, like a moment, but its file is named
# for the stretch it holds: from a stream of hours several are cut one after
# another, and whoever downloads them has to be able to tell them apart.


def _clock(seconds: float) -> str:
    whole = max(0, round(seconds))
    return f"{whole // 3600}h{whole // 60 % 60:02d}m{whole % 60:02d}s"


def recording_video_name(file_name: str, start: float, end: float) -> str:
    """The file name of the stretch from `start` to `end` seconds of a
    recording's file, next to that file."""
    return f"{Path(file_name).stem}_trim_{_clock(start)}-{_clock(end)}.mp4"


def recording_stretch(file_name: str, video_name: str) -> tuple[int, int] | None:
    """The stretch of a recording's file that a file next to it holds, going
    by its name: the seconds it was cut from and to, or None for a file
    that is not a cut of that recording."""
    times = r"(\d+)h(\d\d)m(\d\d)s"
    match = re.fullmatch(rf"{re.escape(Path(file_name).stem)}_trim_{times}-{times}\.mp4", video_name)
    if match is None:
        return None
    hours, minutes, seconds, to_hours, to_minutes, to_seconds = map(int, match.groups())
    return hours * 3600 + minutes * 60 + seconds, to_hours * 3600 + to_minutes * 60 + to_seconds


def chat_shift(value: Any) -> float:
    """How much later than where they stand the chat video shows its lines
    (earlier, if negative), from what was asked for."""
    shift = _number(0 if value is None else value, "chat_shift")
    if abs(shift) > CHAT_SHIFT_LIMIT_SECONDS:
        raise TrimError(f"chat can be moved by {CHAT_SHIFT_LIMIT_SECONDS:g} s at most")
    return shift


def chat_background(value: Any) -> str | None:
    """The colour behind the chat video's text, from what was asked for:
    "#rrggbb", or None for nothing behind it."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not _COLOUR.fullmatch(value):
        raise TrimError("chat_background must be a colour written as #rrggbb")
    return value.lower()
