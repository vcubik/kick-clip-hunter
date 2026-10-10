"""A stretch of a moment's footage, chosen on the review page, as a file to
take away - and, if asked for, its chat as a video to lay over it.

A moment's file holds the clip with the footage around it, and what an
editor wants of it is rarely exactly the clip: the review page's Trim
dialog lets them set where their cut starts and ends anywhere in the file.
This module is what that comes to before anything is cut: what the files
are called, what may be asked for, and where in the file it lies. The
cutting itself is `recorder.trim_clip`, the chat video `chat_video.py`.

Times here are clip time, as everywhere on the review page: seconds from
the clip's first frame, negative in the footage before it.
"""

from __future__ import annotations

import math
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


def file_span(row: Mapping[str, Any], start: Any, end: Any) -> tuple[float, float]:
    """Where in a moment's file the stretch from `start` to `end` lies, in
    seconds from the file's first frame. Raises TrimError for a stretch that
    is not in the footage or is too short to cut."""
    start, end = _number(start, "start"), _number(end, "end")
    first, last = footage(row)
    if start < first - SLACK_SECONDS or end > last + SLACK_SECONDS:
        raise TrimError("the stretch asked for is not in the footage")
    start, end = max(start, first), min(end, last)
    if end - start < MIN_SECONDS:
        raise TrimError(f"the stretch has to be at least {MIN_SECONDS:g} s long")
    return start - first, end - first


def chat_shift(value: Any) -> float:
    """How much later than where they stand the chat video shows its lines
    (earlier, if negative), from what was asked for."""
    shift = _number(0 if value is None else value, "chat_shift")
    if abs(shift) > CHAT_SHIFT_LIMIT_SECONDS:
        raise TrimError(f"chat can be moved by {CHAT_SHIFT_LIMIT_SECONDS:g} s at most")
    return shift
