"""Background management of one ChannelRecorder per watched channel.

Before doing anything that touches a browser, each tick checks is_live via
the official Kick public API - the cheap, no-browser way
to answer "is there any point recording this channel right now." Only a
channel confirmed live gets its recorder ticked, which is what actually
launches the headed patchright browser to (re)fetch the stream URL. Without
this check, an offline watchlist entry would pop a browser window open
every TICK_INTERVAL_SECONDS forever, since ChannelRecorder.tick() alone has
no way to know a channel is offline until it's already tried and failed.

A channel's recorder runs for either of two reasons: the channel is tracked
for clips, or its stream is being kept whole (see stream_recording.py). The
second is asked for by hand, and the switches that pause tracking have no
say over it. Which streams are kept that way right now is held here, and the
loop is what notices that such a recording is over - the stream has been
gone for a while, or the disk is running out - and reports it.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from . import stream_recording
from .config import load_settings
from .kick_client import get_app_access_token, get_channel_by_slug
from .kick_stream import StreamUrlError
from .recorder import (
    POST_ROLL_SECONDS,
    PRE_ROLL_SECONDS,
    ChannelRecorder,
    CutClip,
    RecorderError,
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
# Set to have the loop start its next round now. It belongs to the event
# loop the manager runs on, so there is one only while it runs.
_wake: asyncio.Event | None = None


def _get_recorder(channel_slug: str) -> ChannelRecorder:
    if channel_slug not in _recorders:
        _recorders[channel_slug] = ChannelRecorder(channel_slug)
    return _recorders[channel_slug]


async def _is_live(channel_slug: str) -> bool:
    settings = load_settings()
    token = await get_app_access_token(settings.kick_client_id, settings.kick_client_secret)
    channel = await get_channel_by_slug(channel_slug, token)
    return bool((channel.get("stream") or {}).get("is_live"))


def _tick_one(channel_slug: str) -> None:
    recorder = _get_recorder(channel_slug)
    try:
        recorder.tick()
    except StreamUrlError:
        logger.info("[%s] stream ended between the is_live check and recording", channel_slug)


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


def start_stream_recording(channel_slug: str, broadcaster_user_id: int, recording_id: int) -> StreamRecording:
    """Starts keeping a channel's stream whole. Footage arrives once the
    channel is live and its recorder running, which the loop sees to."""
    writer = StreamWriter(stream_recording.parts_folder(channel_slug, recording_id))
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


async def run_forever(
    get_channel_slugs,
    recording_enabled=lambda: True,
    is_channel_tracked=lambda slug: True,
    stream_recording_over: StreamRecordingOver | None = None,
) -> None:
    """Drives the recorders until cancelled. `stream_recording_over` is
    told, with the channel and why, when a stream recording should be ended;
    ending it (`end_stream_recording`) and saving what it kept is up to
    whoever that is."""
    global _wake
    _wake = wake_up = asyncio.Event()
    try:
        while True:
            wake_up.clear()
            if recording_enabled():
                slugs = get_channel_slugs()
                slugs = [*slugs, *(slug for slug in _stream_recordings if slug not in slugs)]
            else:
                # Recording paused from the dashboard: tear down any running
                # recorders and don't touch a browser until it's back on. A
                # stream that is being kept whole is none of that switch's
                # business, and is seen to as on any other round.
                for slug, recorder in list(_recorders.items()):
                    if recorder.is_active and slug not in _stream_recordings:
                        await asyncio.to_thread(recorder.stop)
                slugs = list(_stream_recordings)
            for slug in slugs:
                try:
                    if slug not in _stream_recordings and not is_channel_tracked(slug):
                        # Tracking paused for this one channel - same
                        # teardown as the global pause, scoped to it.
                        if _get_recorder(slug).is_active:
                            await asyncio.to_thread(_get_recorder(slug).stop)
                        continue
                    if await _is_live(slug):
                        await asyncio.to_thread(_tick_one, slug)
                    elif _get_recorder(slug).is_active:
                        await asyncio.to_thread(_get_recorder(slug).stop)
                except Exception:
                    logger.exception("[%s] recording tick failed", slug)
            if stream_recording_over is not None:
                try:
                    await _report_over(stream_recording_over)
                except Exception:
                    logger.exception("could not check on the stream recordings")
            await _rest(wake_up)
    finally:
        if _wake is wake_up:
            _wake = None


def recording_channels() -> list[str]:
    """The channels being recorded right now, for the dashboard."""
    return [slug for slug, recorder in _recorders.items() if recorder.is_active]


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
