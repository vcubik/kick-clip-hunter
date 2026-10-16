"""Background management of one ChannelRecorder per watched channel.

Before doing anything that touches a browser, each tick asks the channel's
platform whether it is live (streams.live_status) - the cheap, no-browser
way to answer "is there any point recording this channel right now." Only a
channel confirmed live gets its recorder ticked, which is what actually
finds its stream - on Kick by launching the headed patchright browser to
(re)fetch the stream URL. Without this check, an offline Kick channel would
pop a browser window open every TICK_INTERVAL_SECONDS forever, since
ChannelRecorder.tick() alone has no way to know a channel is offline until
it's already tried and failed.

A channel's recorder runs for either of two reasons: the channel is tracked
for clips, or its stream is being kept whole (see stream_recording.py). The
second is asked for by hand, and the switches that pause tracking have no
say over it. Which streams are kept that way right now is held here, and the
loop is what notices that such a recording is over - the stream has been
gone for a while, or the disk is running out - and reports it.

A channel can also be set to be recorded always. The loop then looks at
whether it is live like at any channel it records, tracked or not, and
reports when it is and nothing is keeping its stream - which is when a
recording of it is started without anyone asking. Once per stream: a channel
whose recording was ended while its stream was still on is left alone until
that stream is over (`hold_off`).

Everything here is kept by the channel's name on the watchlist. A channel
that renames itself on its platform has it all go by its new name once the
watchlist follows it (`rename`): a recorder that is running carries on, and
so does a recording of its stream, in the folder it was started in.

What the loop saw of each channel on its last round - whether its platform
said it was live, and since when, or why it could not be looked at - is kept
for the Health page, together with when the loop last finished a round: a
loop that has stopped going round fails as quietly as a channel that is not
live.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import stream_recording, streams
from .platforms import StreamUrlError
from .recorder import (
    POST_ROLL_SECONDS,
    PRE_ROLL_SECONDS,
    ChannelRecorder,
    CutClip,
    RecorderError,
    RunState,
    extract_clip,
)
from .stream_recording import StreamWriter

logger = logging.getLogger("kick_clip_hunter")

TICK_INTERVAL_SECONDS = 30
# A stream recording that has footage and has been given none for this long
# ends itself: the stream is over. Long enough to sit out a stream that
# drops and comes back, or an ad break. Before its first footage it waits
# for as long as it takes - it was started ahead of the stream.
STREAM_END_GRACE_SECONDS = 600
# Recordings are ended, and none is started, when the disk would be left
# with less than this once every recording has been saved. Saving one takes
# as much room again as it has kept.
MIN_FREE_BYTES = 5 * 1024**3

_recorders: dict[str, ChannelRecorder] = {}


@dataclass
class StreamRecording:
    """A stream that is being kept whole right now."""

    recording_id: int
    channel_slug: str
    broadcaster_user_id: int
    writer: StreamWriter


# By channel: a channel has one at a time.
_stream_recordings: dict[str, StreamRecording] = {}
# Room on the disk spoken for by recordings that are being saved, by
# recording.
_held_bytes: dict[int, int] = {}
# Channels that are not recorded by themselves until their stream has been
# seen to be over, set to be recorded always or not (see hold_off).
_held_off: set[str] = set()
# Set to have the loop start its next round now. It belongs to the event
# loop the manager runs on, so there is one only while it runs.
_wake: asyncio.Event | None = None


@dataclass(frozen=True)
class Look:
    """What the loop saw of a channel the last time it looked at it."""

    at: datetime
    # Since when its platform has said the channel is live; None while it is
    # not.
    live_since: datetime | None = None
    # Why the look came to nothing, if it did: the platform could not be
    # asked, or the channel's recorder could not be started.
    trouble: str | None = None


# By channel, for the channels the loop looks at. One it has stopped looking
# at - tracking off, watching paused - has none.
_looks: dict[str, Look] = {}
# When the loop last got through all of its channels.
_round_finished_at: datetime | None = None

# What a look says went wrong when a platform calls a channel live and no
# stream address can be found for it. Once is a stream that ended in between.
NO_STREAM_ADDRESS = "no stream address could be captured"


def _saw(channel_slug: str, live: bool) -> None:
    now = datetime.now(timezone.utc)
    before = _looks.get(channel_slug)
    since = ((before.live_since if before else None) or now) if live else None
    _looks[channel_slug] = Look(at=now, live_since=since)


def _could_not_see(channel_slug: str, trouble: str) -> None:
    """Keeps why a look at a channel failed. Since when it has been live is
    left as it was: a look that failed says nothing about that."""
    before = _looks.get(channel_slug)
    _looks[channel_slug] = Look(
        at=datetime.now(timezone.utc), live_since=before.live_since if before else None, trouble=trouble
    )


def looks() -> dict[str, Look]:
    """What the loop last saw of each channel it looks at, by channel."""
    return dict(_looks)


def round_finished_at() -> datetime | None:
    """When the loop last got through all of its channels, or None if it
    has not yet."""
    return _round_finished_at


def _get_recorder(channel_slug: str) -> ChannelRecorder:
    if channel_slug not in _recorders:
        _recorders[channel_slug] = ChannelRecorder(channel_slug)
    return _recorders[channel_slug]


async def _is_live(channel_slug: str) -> bool:
    return (await streams.live_status(channel_slug)).live


def _tick_one(channel_slug: str) -> None:
    recorder = _get_recorder(channel_slug)
    try:
        recorder.tick()
    except StreamUrlError:
        logger.info("[%s] stream ended between the is_live check and recording", channel_slug)
        _could_not_see(channel_slug, NO_STREAM_ADDRESS)


def wake() -> None:
    """Has the loop look at every channel now rather than at its next
    round - for a recording that was only just asked for."""
    if _wake is not None:
        _wake.set()


async def _rest(wake_up: asyncio.Event) -> None:
    try:
        await asyncio.wait_for(wake_up.wait(), timeout=TICK_INTERVAL_SECONDS)
    except TimeoutError:
        pass


# -- streams kept whole ------------------------------------------------------


def start_stream_recording(
    channel_slug: str, broadcaster_user_id: int, recording_id: int, folder: Path | None = None
) -> StreamRecording:
    """Starts keeping a channel's stream whole. Footage arrives once the
    channel is live and its recorder running, which the loop sees to. It is
    kept in `folder` - by default, where a recording of the channel under
    this name is (see stream_recording.parts_folder)."""
    writer = StreamWriter(folder or stream_recording.parts_folder(channel_slug, recording_id))
    kept = StreamRecording(recording_id, channel_slug, broadcaster_user_id, writer)
    _stream_recordings[channel_slug] = kept
    _get_recorder(channel_slug).whole_stream = writer
    wake()
    return kept


def end_stream_recording(channel_slug: str) -> StreamRecording | None:
    """Stops keeping a channel's stream and returns what was being kept,
    its writer closed - or None if nothing was. Making the file of it is the
    caller's to do. The recorder itself carries on for as long as the
    channel is tracked."""
    kept = _stream_recordings.pop(channel_slug, None)
    if kept is None:
        return None
    recorder = _recorders.get(channel_slug)
    if recorder is not None and recorder.whole_stream is kept.writer:
        recorder.whole_stream = None
    kept.writer.close()
    return kept


def stream_recording_of(channel_slug: str) -> StreamRecording | None:
    return _stream_recordings.get(channel_slug)


def stream_recordings() -> dict[str, StreamRecording]:
    """The streams being kept whole right now, by channel."""
    return dict(_stream_recordings)


def keeps_chat_of(broadcaster_user_id: int) -> bool:
    """Whether a channel's stream is being kept whole - in which case its
    chat is kept too, tracked or not."""
    return any(kept.broadcaster_user_id == broadcaster_user_id for kept in _stream_recordings.values())


def hold_off(channel_slug: str) -> None:
    """Keeps a channel from being recorded by itself until its stream has
    been seen to be over - for one whose recording was just ended while the
    stream may still be on, which would otherwise be recorded again the
    moment the loop came round. Starting a recording by hand is not held
    off."""
    _held_off.add(channel_slug)


def stop_holding_off(channel_slug: str) -> None:
    _held_off.discard(channel_slug)


def is_held_off(channel_slug: str) -> bool:
    return channel_slug in _held_off


def rename(old: str, new: str) -> None:
    """Has what is kept here of a channel go by the name it renamed itself
    to. A recorder that is running carries on, finding the stream by its new
    name from now on, with its buffer where it was; a stream that is being
    kept whole goes on being kept, in the folder it was started in. What the
    loop saw of the channel under its old name is forgotten."""
    _looks.pop(old, None)
    if old in _held_off:
        _held_off.discard(old)
        _held_off.add(new)
    recorder = _recorders.pop(old, None)
    waiting = _recorders.get(new)
    if recorder is not None and recorder.is_active and (waiting is None or not waiting.is_active):
        recorder.channel_slug = new
        _recorders[new] = recorder
    kept = _stream_recordings.pop(old, None)
    if kept is not None:
        kept.channel_slug = new
        _stream_recordings[new] = kept
        _get_recorder(new).whole_stream = kept.writer
    wake()


async def forget(channel_slug: str) -> None:
    """Stops a channel's recorder for good, and forgets what was seen of
    it - for a channel taken off the watchlist, which the loop no longer
    looks at, so would never stop. Its buffer stays on disk until the
    recorder of a channel of that name prunes it: a moment of the channel
    whose clip is still to be cut is cut from it."""
    recorder = _recorders.pop(channel_slug, None)
    if recorder is not None and recorder.is_active:
        await asyncio.to_thread(recorder.stop)
    _looks.pop(channel_slug, None)
    _held_off.discard(channel_slug)


def hold_room(recording_id: int, size: int) -> None:
    """Counts a recording that is being saved as taking that much of the
    disk's free room, until `release_room`."""
    _held_bytes[recording_id] = size


def release_room(recording_id: int) -> None:
    _held_bytes.pop(recording_id, None)


def room_left() -> int:
    """How much of the disk would be free if every recording that runs or
    is being saved were saved now."""
    spoken_for = sum(kept.writer.size for kept in _stream_recordings.values()) + sum(_held_bytes.values())
    return stream_recording.free_bytes() - spoken_for


def _why_over(kept: StreamRecording) -> str | None:
    """Why a stream recording should be ended now, if it should."""
    if kept.writer.failed:
        return stream_recording.WRITE_FAILED
    idle = kept.writer.idle_seconds()
    if idle is not None and idle >= STREAM_END_GRACE_SECONDS:
        return stream_recording.STREAM_ENDED
    return None


StreamRecordingOver = Callable[[str, str], Awaitable[None]]


async def _report_over(stream_recording_over: StreamRecordingOver) -> None:
    if not _stream_recordings:
        return
    out_of_room = room_left() < MIN_FREE_BYTES
    for slug, kept in list(_stream_recordings.items()):
        why = stream_recording.DISK_FULL if out_of_room else _why_over(kept)
        if why is None:
            continue
        try:
            await stream_recording_over(slug, why)
        except Exception:
            logger.exception("[%s] could not end the stream recording", slug)


StreamIsOn = Callable[[str], Awaitable[None]]
LookFailed = Callable[[str, Exception], Awaitable[None]]


def _always_recorded(always_recorded: Callable[[], Iterable[str]]) -> set[str]:
    try:
        return set(always_recorded())
    except Exception:
        logger.exception("could not tell which channels are always recorded")
        return set()


async def _see_to(slug: str, *, tracked: bool, always: bool, stream_is_on: StreamIsOn | None) -> None:
    """One channel's turn in a round: its recorder is ticked if the channel
    is live and its stream is wanted - for clips (`tracked`) or whole - and
    stopped if it is running for nothing. A channel that is recorded
    `always` and turns out to be live with nothing keeping its stream is
    reported first, so that what is started for it gets this round's tick."""
    recorder = _get_recorder(slug)
    if not (tracked or always or slug in _stream_recordings):
        # Tracking paused for this one channel - same teardown as the
        # global pause, scoped to it. Not even the liveness check is spent.
        _looks.pop(slug, None)
        if recorder.is_active:
            await asyncio.to_thread(recorder.stop)
        return
    live = await _is_live(slug)
    _saw(slug, live)
    if not live:
        # Whatever stream a recording was ended in is over.
        _held_off.discard(slug)
    elif always and stream_is_on is not None and slug not in _stream_recordings and slug not in _held_off:
        try:
            await stream_is_on(slug)
        except Exception:
            logger.exception("[%s] is live and always recorded, but its recording could not be started", slug)
    if live and (tracked or slug in _stream_recordings):
        await asyncio.to_thread(_tick_one, slug)
    elif recorder.is_active:
        await asyncio.to_thread(recorder.stop)


async def run_forever(
    get_channel_slugs,
    recording_enabled=lambda: True,
    is_channel_tracked=lambda slug: True,
    stream_recording_over: StreamRecordingOver | None = None,
    always_recorded: Callable[[], Iterable[str]] = lambda: (),
    stream_is_on: StreamIsOn | None = None,
    look_failed: LookFailed | None = None,
) -> None:
    """Drives the recorders until cancelled. `stream_recording_over` is
    told, with the channel and why, when a stream recording should be ended;
    ending it (`end_stream_recording`) and saving what it kept is up to
    whoever that is. `always_recorded` is the channels whose every stream is
    to be kept whole, and `stream_is_on` is told when one of them is live
    with no recording of it running; starting one (`start_stream_recording`)
    is up to whoever that is, too. `look_failed` is told, with the channel
    and what was raised, when a look at a channel fails - for one whose
    platform no longer knows it by that name, which it may have changed."""
    global _wake, _round_finished_at
    _wake = wake_up = asyncio.Event()
    try:
        while True:
            wake_up.clear()
            watching = recording_enabled()
            always = _always_recorded(always_recorded)
            # A stream that is being kept whole, or is to be once it is on,
            # is none of the switches' business.
            whole = [*_stream_recordings, *sorted(always.difference(_stream_recordings))]
            if watching:
                slugs = get_channel_slugs()
                slugs = [*slugs, *(slug for slug in whole if slug not in slugs)]
            else:
                # Recording paused from the dashboard: tear down any running
                # recorders and don't touch a browser until it's back on -
                # but for the streams wanted whole, which are seen to as on
                # any other round.
                for slug, recorder in list(_recorders.items()):
                    if recorder.is_active and slug not in whole:
                        await asyncio.to_thread(recorder.stop)
                slugs = whole
            # What was seen of a channel that is no longer looked at is not
            # what it is doing now.
            for slug in _looks.keys() - set(slugs):
                del _looks[slug]
            for slug in slugs:
                try:
                    await _see_to(
                        slug,
                        tracked=watching and is_channel_tracked(slug),
                        always=slug in always,
                        stream_is_on=stream_is_on,
                    )
                except Exception as error:
                    logger.exception("[%s] recording tick failed", slug)
                    _could_not_see(slug, type(error).__name__)
                    if look_failed is not None:
                        try:
                            await look_failed(slug, error)
                        except Exception:
                            logger.exception("[%s] could not see to a failed look", slug)
            if stream_recording_over is not None:
                try:
                    await _report_over(stream_recording_over)
                except Exception:
                    logger.exception("could not check on the stream recordings")
            _round_finished_at = datetime.now(timezone.utc)
            await _rest(wake_up)
    finally:
        if _wake is wake_up:
            _wake = None


def recording_channels() -> list[str]:
    """The channels being recorded right now, for the dashboard."""
    return [slug for slug, recorder in _recorders.items() if recorder.is_active]


def recorder_runs() -> dict[str, RunState]:
    """Where each recorder that is running stands, by channel (see
    recorder.RunState)."""
    runs = {slug: recorder.state() for slug, recorder in list(_recorders.items())}
    return {slug: run for slug, run in runs.items() if run is not None}


async def stop_all() -> None:
    """Stop every active recorder - used on app shutdown so no download is
    cut off mid-segment when the app itself exits.
    """
    for recorder in _recorders.values():
        if recorder.is_active:
            await asyncio.to_thread(recorder.stop)


async def create_clip_for_moment(
    channel_slug: str,
    window_start: datetime,
    window_end: datetime,
    output_name: str,
    pre_roll_seconds: float = PRE_ROLL_SECONDS,
    post_roll_seconds: float = POST_ROLL_SECONDS,
    **framing: float,
) -> CutClip:
    """Cuts a moment's file from the channel's buffer. `framing` is whatever
    else of how the clip is framed was set for the channel, as
    recorder.extract_clip takes it."""
    recorder = _get_recorder(channel_slug)
    return await asyncio.to_thread(
        extract_clip, recorder, window_start, window_end, output_name,
        pre_roll_seconds, post_roll_seconds, **framing,
    )


__all__ = ["RecorderError", "create_clip_for_moment", "run_forever"]
