"""Moments looked for in a stream that was kept whole, once it is over.

A recording has its chat stored with it, whether or not the channel was
tracked for clips while it ran. So its stream can be gone through again
afterwards: the chat is replayed through the detector (replay.py) under the
channel's settings as they stand now, and what would have been a moment is
found in the recording's file - to be watched there, and made into a moment
like any other, its clip cut out of the file instead of out of the
recorder's buffer. That is also the way to see what a change of settings
does on the picture, not only in how many moments it makes.

This module is what lies between the replay and the file: which chat to
replay, where in the file a replayed moment's clip and the footage around
it lie, what the file cut for it holds, and which of the moments found are
moments already. It is pure. The replaying is replay.py's, the cutting
recorder.trim_clip's, and main.py does both from the Recordings page.

A clip is framed as the recorder frames one (recorder._moment_segments):
around when chat reacted, widened by the pre- and post-roll, moved back
onto the stream's clock, with context on either side. What differs is what
it is cut from. Where the recorder keeps to one group of segments, this
keeps to one part of the recording, so nothing is cut across a break in the
stream; and a file is not cut on segments but on the frames a cut can begin
on, so what comes out begins a little earlier than was asked (`extent` is
what it then holds).

Not found this way: clips made on command (see replay.py), and anything in
a stretch of the stream the recording has no footage of.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from . import detector
from .replay import ReplayedMoment
from .stream_recording import Part
from .tuning import Tuning

# A clip the recording has less than this share of is not cut: the recording
# began, ended or broke off in the middle of what chat was reacting to.
MIN_CLIP_SHARE = 0.5


@dataclass(frozen=True)
class Found:
    """A moment the replay of a recording's chat came to, placed in the
    recording's file. Stretches of the file are in seconds from its first
    frame."""

    # Tells it from the others found in one recording: when it fired, in
    # milliseconds since the epoch. The same chat replayed under the same
    # settings gives the same one.
    key: int
    # When its reaction ran, on chat's clock - what a moment is stored with.
    window_start: datetime
    window_end: datetime
    spike: detector.Spike
    # The clip, and the footage its file is cut as: the clip with the
    # context either side of it.
    clip: tuple[float, float]
    footage: tuple[float, float]
    # When the clip's first frame was broadcast.
    clip_started_at: datetime
    # How many people laughed around it, as a moment is stored with that
    # too (see replay.ReplayedMoment). None where the replay did not count.
    laughing: int | None = None

    @property
    def fired_at(self) -> datetime:
        """When the detector fired: the time the moment is detected at."""
        return self.window_start + timedelta(seconds=detector.SHORT_WINDOW_SECONDS)

    @property
    def clip_seconds(self) -> float:
        return self.clip[1] - self.clip[0]


def chat_span(parts: Sequence[Part]) -> tuple[datetime, datetime]:
    """The chat to replay for a recording: what arrived from a little before
    its first frame was broadcast to a little after its last. The detector
    measures a reaction against the chat of the minutes before it, so the
    recording's first minutes need the chat from before them - and a moment
    that fired just before the footage ends is held open by what followed."""
    around = timedelta(seconds=detector.BASELINE_WINDOW_SECONDS)
    return parts[0].started_at - around, parts[-1].ended_at + around


def _clip_asked(window_start: datetime, window_end: datetime, tuning: Tuning) -> tuple[datetime, datetime]:
    """The stretch of the broadcast a moment's clip is to hold, as the
    recorder works it out for a live one."""
    start = window_start - timedelta(seconds=tuning.pre_roll_seconds + tuning.playback_delay_seconds)
    end = window_end + timedelta(seconds=tuning.post_roll_seconds - tuning.playback_delay_seconds)
    return start, end


def _placed(
    parts: Sequence[Part], start: datetime, end: datetime, tuning: Tuning
) -> tuple[tuple[float, float], tuple[float, float], datetime] | None:
    """Where in a recording's file the clip of the broadcast from `start` to
    `end` lies, the footage that goes into its file around it, and when its
    first frame was broadcast - or None if the file has too little of it.

    All of it comes from one part: the one that holds the most of the clip.
    The clip is as much of what was asked as that part has, and the context
    reaches no further than the part does.
    """
    asked = (end - start).total_seconds()
    best: tuple[float, Part, float] | None = None
    begins = 0.0
    for part in parts:
        held = (min(part.ended_at, end) - max(part.started_at, start)).total_seconds()
        if held > 0 and (best is None or held > best[0]):
            best = (held, part, begins)
        begins += part.seconds
    if best is None or asked <= 0 or best[0] < asked * MIN_CLIP_SHARE:
        return None

    held, part, begins = best
    started_at = max(start, part.started_at)
    first = begins + (started_at - part.started_at).total_seconds()
    last = first + held
    footage = (
        max(first - tuning.context_before_seconds, begins),
        min(last + tuning.context_after_seconds, begins + part.seconds),
    )
    return (first, last), footage, started_at


def find(replayed: Iterable[ReplayedMoment], parts: Sequence[Part], tuning: Tuning) -> tuple[list[Found], int]:
    """The replayed moments of a recording's chat as they lie in its file,
    in the order they fired - and how many more the recording has too little
    footage of: ones whose clip would reach into it, but lies in a break in
    the stream or runs off either end. One that lies outside the recording
    altogether is not counted: the chat replayed reaches past both ends (see
    chat_span).

    `tuning` is what the chat was replayed under: it frames the clips too.
    """
    found: list[Found] = []
    missing = 0
    if not parts:
        return found, missing
    recorded = (parts[0].started_at, parts[-1].ended_at)
    for moment in replayed:
        if moment.spike is None:
            continue
        fired = datetime.fromtimestamp(moment.time, timezone.utc)
        window_start = fired - timedelta(seconds=detector.SHORT_WINDOW_SECONDS)
        window_end = fired + timedelta(seconds=moment.extension_seconds)
        start, end = _clip_asked(window_start, window_end, tuning)
        placed = _placed(parts, start, end, tuning)
        if placed is None:
            if start < recorded[1] and end > recorded[0]:
                missing += 1
            continue
        clip, footage, started_at = placed
        found.append(
            Found(
                key=round(moment.time * 1000),
                window_start=window_start,
                window_end=window_end,
                spike=moment.spike,
                clip=clip,
                footage=footage,
                clip_started_at=started_at,
                laughing=moment.laughing,
            )
        )
    return found, missing


def extent(found: Found, begins: float, duration: float) -> tuple[float, float, float]:
    """What the file cut for a found moment holds, as it is stored with the
    moment: seconds of footage before the clip's first frame, the clip's own
    length, and seconds of footage after its last.

    `begins` is the second of the recording the cut really begins at, and
    `duration` how long it runs (see recorder.trim_clip): a cut begins on a
    frame that is whole in itself, so a little before the footage that was
    asked for. That much more context comes before the clip; the clip is
    where it was meant to be.
    """
    first, last = found.clip
    ends = begins + duration
    before = max(first - begins, 0.0)
    clip = max(min(last, ends) - max(first, begins), 0.0)
    return before, clip, max(ends - last, 0.0)


def moment_of(found: Found, windows: Iterable[Mapping[str, Any]]) -> int | None:
    """The stored moment a found one is, by its id: the first whose reaction
    ran while the found one's did - the one the service made of it while the
    stream was on, or the one it was made into from here. None if it is no
    moment yet. `windows` are the channel's moments (`id`, `window_start`,
    `window_end`, as db.get_channel_moment_windows gives them)."""
    for window in windows:
        began = datetime.fromisoformat(window["window_start"])
        ended = datetime.fromisoformat(window["window_end"])
        if began <= found.window_end and ended >= found.window_start:
            return window["id"]
    return None
