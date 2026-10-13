import asyncio
import json
import logging
import logging.handlers
import math
import mimetypes
import os
import shutil
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import (
    audio_events,
    chat_identity,
    chat_stats,
    chat_trace,
    chat_video,
    clip_trim,
    dashboard_view,
    detector,
    frame_encoder,
    recorder,
    recording_manager,
    replay,
    sound_events,
    stream_recording,
    transcriber,
    tuning,
    win_console,
)
from .config import load_settings
from .db import (
    MOMENT_TYPES,
    STREAM_TYPES,
    TUNING_FOR_ALL,
    count_channel_chat,
    count_moments,
    count_moments_with_clip,
    count_returning_chatters,
    count_stream_recordings,
    delete_stream_recording,
    finish_stream_recording,
    get_channel_chat_between,
    get_channel_keywords,
    get_chat_around,
    get_chat_between,
    get_chat_channel_ids,
    get_chat_delay,
    get_chat_emotes,
    get_chat_identities,
    get_chat_name,
    get_chat_sessions,
    get_chat_span,
    get_chatter_counts,
    get_connection,
    get_first_message_time,
    get_flag,
    get_moment,
    get_moment_channels,
    get_moment_times,
    get_recent_moments,
    get_stream_recording,
    get_stream_recordings,
    get_streamer_by_slug,
    get_streamer_tracking_enabled,
    get_streamers,
    get_tuning,
    get_tuning_in_force,
    insert_chat_message,
    insert_moment,
    insert_stream_recording,
    iter_channel_chat,
    replace_tuning,
    search_channel_chat,
    set_chat_delay,
    set_chat_identity,
    set_flag,
    set_stream_recording_state,
    set_streamer_always_record,
    set_streamer_own_tuning,
    set_streamer_tracking,
    update_moment_audio_events,
    update_moment_clip_duration,
    update_moment_clip_path,
    update_moment_frame_embedding,
    update_moment_notes,
    update_moment_rating,
    update_moment_sound_embedding,
    update_moment_sound_events,
    update_moment_stream_type,
    update_moment_title,
    update_moment_transcript,
    update_moment_type,
    update_moment_window_end,
)
from .kick_client import (
    get_app_access_token,
    get_channel_by_slug,
    get_event_subscriptions,
    subscribe_chat_messages,
)
from .kick_stream import StreamUrlError
from .recorder import CLIPS_DIR
from .recording_manager import RecorderError
from .stream_recording import STREAMS_DIR
from .timeutil import to_local_datetime
from .watchlist import add_channel_to_watchlist, refresh_emote_pictures
from .webhook_security import get_kick_public_key, verify_signature

LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
LOG_FILE = Path("data/logs/kick_clip_hunter.log")
LOG_FILE_MAX_BYTES = 10 * 1024 * 1024
LOG_FILE_BACKUPS = 10

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
logger = logging.getLogger("kick_clip_hunter")


def _add_log_file() -> None:
    # The console is the only place the log otherwise goes, and its
    # scrollback is gone the moment the window closes or the app restarts -
    # which is exactly when it's needed to work out what went wrong. Capped
    # by rotation (chat logging is chatty) rather than growing forever.
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=LOG_FILE_MAX_BYTES, backupCount=LOG_FILE_BACKUPS, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
    logging.getLogger().addHandler(handler)
    # Under uvicorn its loggers don't propagate to the root logger, so its
    # startup lines and unhandled-exception tracebacks need the handler
    # directly.
    uvicorn_logger = logging.getLogger("uvicorn")
    if not uvicorn_logger.propagate:
        uvicorn_logger.addHandler(handler)


class _DropConnectionResetNoise(logging.Filter):
    """Drops asyncio's "Exception in callback ... _call_connection_lost"
    tracebacks for ConnectionResetError.

    On Windows the proactor event loop logs one whenever the other side
    (here: Kick's webhook sender, via the tunnel) closes a connection before
    the server's own shutdown of that socket runs. Nothing is lost - the
    request was already handled - but it's a 7-line ERROR several times an
    hour that buries real errors in the log.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        error = record.exc_info[1] if record.exc_info else None
        return not (isinstance(error, ConnectionResetError) and "_call_connection_lost" in record.getMessage())


logging.getLogger("asyncio").addFilter(_DropConnectionResetNoise())
_add_log_file()
if win_console.disable_quick_edit():
    logger.info("console QuickEdit mode disabled so a click in the window can't pause the app")

# recorder.py polls each live channel's variant playlist via httpx every few
# seconds (normal HLS reload cadence) - at INFO level httpx logs the full
# request line per call, and these playlist URLs run thousands of characters,
# so left alone this drowns out the app's own log within seconds of startup.
logging.getLogger("httpx").setLevel(logging.WARNING)


def _watchlist_slugs() -> list[str]:
    conn = get_connection()
    try:
        return [row["slug"] for row in get_streamers(conn)]
    finally:
        conn.close()


def _is_channel_tracked(slug: str) -> bool:
    conn = get_connection()
    try:
        row = get_streamer_by_slug(conn, slug)
        return bool(row["tracking_enabled"]) if row else False
    finally:
        conn.close()


def _always_recorded_slugs() -> list[str]:
    """The channels whose every stream is kept whole (see
    _record_stream_by_itself). None while the service is shutting down."""
    if _shutdown_requested:
        return []
    conn = get_connection()
    try:
        return [row["slug"] for row in get_streamers(conn) if row["always_record"]]
    finally:
        conn.close()


# Runtime on/off switch, toggled from the dashboard and persisted in
# app_settings so it survives a restart. Held in memory too so the webhook
# hot path and the recording loop don't hit the DB on every message/tick.
# Chat watching and recording used to be two separate toggles, but neither
# one is useful without the other (a moment needs both a chat spike and
# buffered footage to turn into a clip), so they're one switch now. Maps the
# short dashboard name to its stored key.
SETTING_KEYS = {
    "watching": "watching_enabled",
    "transcript": "transcript_enabled",
    "audio_events": "audio_events_enabled",
    "frames": "frames_enabled",
    "sound_events": "sound_events_enabled",
}
# The per-clip analysis steps (everything but "watching") are pure data
# capture for a future classifier, and each one costs real CPU time on every
# single clip. They're individually switchable so that cost is only paid
# while that data is actually wanted; backfill_taste.py can fill in whatever
# was skipped later. (dashboard name, button label) in display order.
ANALYSIS_SETTINGS = [
    ("transcript", "Transcript"),
    ("audio_events", "Audio events"),
    ("sound_events", "Sound events"),
    ("frames", "Frame embeddings"),
]
# Used until a setting is first toggled from the dashboard.
SETTING_DEFAULTS = {
    "watching_enabled": True,
    "transcript_enabled": False,
    "audio_events_enabled": False,
    "frames_enabled": False,
    "sound_events_enabled": False,
}
_flags: dict[str, bool] = {}

# Graceful shutdown, triggered from the dashboard: stop taking in new chat
# messages immediately (so no new moment starts once this is set), but keep
# the recording loop running as normal - an already-open moment session still
# needs live footage for its post-roll, and the background tasks it eventually
# spawns (clip cut, transcript, audio/frame/sound tagging) still need to run
# to completion. _background_tasks tracks all of that chained work; the
# process only actually exits once it (and any open moment session) is empty.
# A stream that is being kept whole is ended when the shutdown is asked for,
# and the making of its file is one more of those tasks.
_shutdown_requested = False
_background_tasks: set[asyncio.Task] = set()
# The event loop only holds a weak reference to a task, so one that nothing
# else refers to can be garbage-collected before it finishes - which, for the
# task that waits for in-flight work and then exits, would leave a "shutting
# down" service running forever. It deliberately isn't in _background_tasks:
# it is the thing waiting for that set to empty.
_shutdown_task: asyncio.Task | None = None


def _track_task(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


SHUTDOWN_POLL_SECONDS = 5


async def _shutdown_when_idle() -> None:
    while _moment_sessions or _background_tasks:
        await asyncio.sleep(SHUTDOWN_POLL_SECONDS)
    logger.info("all in-flight moments finished - stopping recorders and exiting")
    await recording_manager.stop_all()
    # A plain process exit (rather than raising/returning) so this actually
    # ends the app regardless of what else the event loop is doing - signals
    # aren't a reliable way to ask a Windows process to shut down gracefully,
    # so this relies on having already stopped every recorder ourselves
    # above instead of leaving that to cleanup handlers that may not run.
    os._exit(0)


def _load_flags() -> None:
    conn = get_connection()
    try:
        for key in SETTING_KEYS.values():
            _flags[key] = get_flag(conn, key, default=SETTING_DEFAULTS[key])
    finally:
        conn.close()


# How often the running service re-checks that Kick still has every watched
# channel's chat subscription.
SUBSCRIPTION_CHECK_SECONDS = 600


async def _ensure_chat_subscriptions(report_all_present: bool = True) -> None:
    """Re-subscribe any watchlisted channel that has no chat.message.sent
    subscription on Kick.

    Kick silently drops event subscriptions to zero every so often with no
    error - chat just stops arriving for every channel until they're
    re-subscribed (see CLAUDE.md). Reconciling on startup, and then every
    SUBSCRIPTION_CHECK_SECONDS while running, makes that self-healing instead
    of needing a restart or subscribe.py run by hand. It's check-then-subscribe
    (only the missing ones) so it never duplicates an existing subscription,
    and any API failure is logged but never blocks startup or stops the
    periodic check.
    """
    conn = get_connection()
    try:
        watch = [(row["broadcaster_user_id"], row["slug"]) for row in get_streamers(conn)]
    finally:
        conn.close()
    if not watch:
        return

    try:
        token = await get_app_access_token(settings.kick_client_id, settings.kick_client_secret)
        subscribed = {sub.get("broadcaster_user_id") for sub in await get_event_subscriptions(token)}
    except Exception:
        logger.exception("could not check chat subscriptions")
        return

    missing = [(bid, slug) for bid, slug in watch if bid not in subscribed]
    if not missing:
        if report_all_present:
            logger.info("chat subscriptions present for all %d watched channels", len(watch))
        return

    logger.warning(
        "re-subscribing %d channel(s) with no chat subscription: %s",
        len(missing), ", ".join(slug for _, slug in missing),
    )
    for bid, slug in missing:
        try:
            await subscribe_chat_messages(bid, token)
            logger.info("re-subscribed chat for %s", slug)
        except Exception:
            logger.exception("failed to re-subscribe chat for %s", slug)


async def _keep_chat_subscriptions() -> None:
    """The periodic half of _ensure_chat_subscriptions: quiet while nothing
    is missing, so the log only shows the checks that found something."""
    while True:
        await asyncio.sleep(SUBSCRIPTION_CHECK_SECONDS)
        try:
            await _ensure_chat_subscriptions(report_all_present=False)
        except Exception:
            logger.exception("periodic chat subscription check failed")


async def _refresh_emote_pictures() -> None:
    """Fetch every watched channel's 7TV emote set again, and the global
    emotes every channel has on top of its own, so chat on the dashboard is
    drawn with the pictures in use now.

    Channels change their sets all the time, and one added before pictures
    were stored has none at all. Only the pictures are refreshed here - what
    the detector listens for changes when someone asks for it (subscribe.py,
    refresh_emotes.py), not behind a restart. A set 7TV can't be asked
    about keeps the pictures it had.
    """
    try:
        logger.info("%d global 7TV emote picture(s) stored", await refresh_emote_pictures())
    except Exception:
        logger.exception("could not refresh the global 7TV emote pictures")

    conn = get_connection()
    try:
        watch = [(row["broadcaster_user_id"], row["slug"]) for row in get_streamers(conn)]
    finally:
        conn.close()
    for broadcaster_user_id, slug in watch:
        try:
            count = await refresh_emote_pictures(broadcaster_user_id)
            logger.info("%d 7TV emote picture(s) stored for %s", count, slug)
        except Exception:
            logger.exception("could not refresh the 7TV emote pictures of %s", slug)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_flags()
    _take_up_stream_recordings()
    await _ensure_chat_subscriptions()
    tasks = [
        # In the background: the dashboard works without them, 7TV being
        # slow or down must not hold up the start.
        asyncio.create_task(_refresh_emote_pictures()),
        asyncio.create_task(
            recording_manager.run_forever(
                _watchlist_slugs,
                lambda: _flags.get("watching_enabled", True),
                _is_channel_tracked,
                _stream_recording_over,
                _always_recorded_slugs,
                _record_stream_by_itself,
            )
        ),
        asyncio.create_task(_keep_chat_subscriptions()),
    ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()


app = FastAPI(lifespan=lifespan)
settings = load_settings()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
# The dashboard's stylesheet, script and font. Their content types are set
# here rather than left to the system: on Windows they come from the
# registry, where ".js" is often mapped to something else, and a browser
# ignores a stylesheet that is not served as text/css.
STATIC_DIR = Path(__file__).parent / "static"
for _content_type, _extension in (
    ("text/css", ".css"),
    ("text/javascript", ".js"),
    ("font/ttf", ".ttf"),
    ("image/svg+xml", ".svg"),
):
    mimetypes.add_type(_content_type, _extension)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

Path("data/clips").mkdir(parents=True, exist_ok=True)
app.mount("/clips", StaticFiles(directory="data/clips"), name="clips")
# The streams that were kept whole, for the Recordings page to play.
STREAMS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/streams", StaticFiles(directory=STREAMS_DIR.as_posix()), name="streams")

# What is stored of the Settings page's values (see tuning.py), by owner:
# the ones for every channel and those of each channel that goes by its own.
# The webhook asks for a channel's settings with every message, so they are
# read once and kept; saving a setting drops them.
_tuning_values: dict[int, dict[str, tuning.Value]] | None = None

# Channel keyword weights rarely change (only when re-subscribing/refreshing
# emotes, or when the channel's settings do), so we cache them per-process
# instead of hitting the DB on every single chat message.
_channel_keywords_cache: dict[int, dict[str, float]] = {}

# Stream start times don't need to be looked up on every message - only when
# a moment fires - and barely change within a single stream, so a short
# cache keeps us from hammering the channels endpoint.
_stream_start_cache: dict[str, tuple[datetime | None, float]] = {}
STREAM_INFO_CACHE_SECONDS = 120


# What was last stored of each chatter's identity (name colour and badges),
# by (channel, name). Kick sends it with every message and it hardly ever
# changes, so this is what keeps it from being written every time: only
# someone not seen since the start, or seen changed, costs a write.
_chat_identity_cache: dict[tuple[int, str], chat_identity.Identity] = {}
# Past this many chatters the cache is simply started over - each of them
# then costs one more write the next time they speak.
CHAT_IDENTITY_CACHE_LIMIT = 50_000


def _remember_chat_identity(conn, broadcaster_user_id: int, username: str, sender: dict) -> None:
    identity = chat_identity.from_sender(sender)
    if identity is None or not username:
        return
    key = (broadcaster_user_id, username)
    if _chat_identity_cache.get(key) == identity:
        return
    if len(_chat_identity_cache) >= CHAT_IDENTITY_CACHE_LIMIT:
        _chat_identity_cache.clear()
    set_chat_identity(conn, broadcaster_user_id, username, *identity)
    _chat_identity_cache[key] = identity


def _tuning(conn, broadcaster_user_id: int) -> tuning.Tuning:
    """The settings the service goes by for a channel: what was set for all
    channels, the channel's own on top where it goes by them, and the
    constants for everything nobody set (see tuning.py)."""
    global _tuning_values
    if _tuning_values is None:
        _tuning_values = {owner: tuning.stored(texts) for owner, texts in get_tuning_in_force(conn).items()}
    own = _tuning_values.get(broadcaster_user_id) if broadcaster_user_id != TUNING_FOR_ALL else None
    return tuning.tuned(_tuning_values.get(TUNING_FOR_ALL, {}), own)


def _forget_tuning() -> None:
    """Has the settings read again the next time they are asked for - and
    the keyword weights, which are worked out from them."""
    global _tuning_values
    _tuning_values = None
    _channel_keywords_cache.clear()


def _channel_keywords(conn, broadcaster_user_id: int) -> dict[str, float]:
    """The channel's 7TV emote names the detector listens for, with what
    each weighs under the channel's settings."""
    if broadcaster_user_id not in _channel_keywords_cache:
        _channel_keywords_cache[broadcaster_user_id] = detector.keyword_weights(
            get_channel_keywords(conn, broadcaster_user_id), _tuning(conn, broadcaster_user_id).detector
        )
    return _channel_keywords_cache[broadcaster_user_id]


async def _stream_elapsed_seconds(channel_slug: str) -> int | None:
    cached = _stream_start_cache.get(channel_slug)
    now = time.monotonic()
    if cached is None or now - cached[1] > STREAM_INFO_CACHE_SECONDS:
        token = await get_app_access_token(settings.kick_client_id, settings.kick_client_secret)
        channel = await get_channel_by_slug(channel_slug, token)
        stream = channel.get("stream") or {}
        start_time = None
        if stream.get("is_live") and stream.get("start_time"):
            start_time = datetime.fromisoformat(stream["start_time"].replace("Z", "+00:00"))
        _stream_start_cache[channel_slug] = (start_time, now)

    start_time, _ = _stream_start_cache[channel_slug]
    if start_time is None:
        return None
    return int((datetime.now(timezone.utc) - start_time).total_seconds())


async def _create_clip_background(
    moment_id: int,
    channel: str,
    window_start: datetime,
    window_end: datetime,
    tuned: tuning.Tuning,
) -> None:
    # `tuned` is how the channel was set up when the moment fired: how far
    # the clip reaches either side of the reaction and how much footage goes
    # into its file around it.
    #
    # Neither the post-roll footage nor the context that goes into the file
    # after the clip exists yet at the instant a moment closes - wait for it
    # to actually be published and downloaded before looking for it, or the
    # file comes out truncated: without its context, or cut off right at the
    # exciting part.
    await asyncio.sleep(tuned.post_roll_seconds + tuned.context_after_seconds + recorder.CLIP_SETTLE_SECONDS)
    try:
        clip = await recording_manager.create_clip_for_moment(
            channel, window_start, window_end, f"moment_{moment_id}.mp4", **tuned.framing()
        )
    except RecorderError:
        logger.info("[%s] no buffered footage yet for moment %d", channel, moment_id)
        return
    except StreamUrlError:
        logger.info("[%s] moment %d: stream not live, can't fetch fresh URL", channel, moment_id)
        return
    except Exception:
        logger.exception("[%s] clip creation failed for moment %d", channel, moment_id)
        return

    clip_path = clip.path
    conn = get_connection()
    try:
        # Stored with the stretch of the broadcast it really holds, which is
        # what lets the dashboard line chat up with the picture, and with how
        # much footage the file has either side of it.
        update_moment_clip_path(
            conn,
            moment_id,
            clip_path.relative_to(CLIPS_DIR).as_posix(),
            clip_start=clip.started_at.isoformat(),
            clip_duration=clip.duration,
            context_before=clip.context_before,
            context_after=clip.context_after,
        )
    finally:
        conn.close()
    logger.info(
        "[%s] clip saved for moment %d: %s (%.0f s, with %.0f s before it and %.0f s after)",
        channel, moment_id, clip_path, clip.duration, clip.context_before, clip.context_after,
    )
    extent = (clip.context_before, clip.duration, clip.context_after)
    if _flags.get("transcript_enabled"):
        _track_task(_transcribe_clip_background(moment_id, channel, clip_path, extent))
    if _flags.get("audio_events_enabled"):
        _track_task(_detect_audio_events_background(moment_id, channel, clip_path, extent))
    if _flags.get("frames_enabled"):
        _track_task(_encode_frames_background(moment_id, channel, clip_path, extent))
    if _flags.get("sound_events_enabled"):
        _track_task(_tag_sound_events_background(moment_id, channel, clip_path, extent))


# Where a clip lies in its file: seconds of context before it, its own
# length, seconds of context after it.
ClipExtent = tuple[float, float, float]


def _on_clip_alone(analyse, clip_path: Path, extent: ClipExtent):
    """Runs an analysis step on the clip itself. Its file also holds the
    footage either side of it, and what a step finds is stored as a
    description of the moment, not of the minute around it."""
    with recorder.clip_alone(clip_path, *extent) as alone:
        return analyse(alone)


async def _transcribe_clip_background(moment_id: int, channel: str, clip_path: Path, extent: ClipExtent) -> None:
    # CPU-bound (faster-whisper) - runs off the event loop via to_thread, and
    # as its own task so a slow transcription never delays the clip being
    # marked ready on the dashboard.
    try:
        transcript = await asyncio.to_thread(_on_clip_alone, transcriber.transcribe_clip, clip_path, extent)
    except Exception:
        logger.exception("[%s] transcription failed for moment %d", channel, moment_id)
        return

    conn = get_connection()
    try:
        update_moment_transcript(conn, moment_id, transcript)
    finally:
        conn.close()
    logger.info("[%s] transcript saved for moment %d (%d chars)", channel, moment_id, len(transcript))


async def _detect_audio_events_background(
    moment_id: int, channel: str, clip_path: Path, extent: ClipExtent
) -> None:
    # CPU-bound (SenseVoice via funasr) - same to_thread/own-task treatment
    # as transcription, and independent of it: one failing never blocks the
    # other or the clip being marked ready on the dashboard.
    try:
        tags = await asyncio.to_thread(_on_clip_alone, audio_events.detect_audio_events, clip_path, extent)
    except Exception:
        logger.exception("[%s] audio event detection failed for moment %d", channel, moment_id)
        return

    conn = get_connection()
    try:
        update_moment_audio_events(conn, moment_id, tags)
    finally:
        conn.close()
    logger.info("[%s] audio events saved for moment %d: %s", channel, moment_id, tags)


async def _encode_frames_background(moment_id: int, channel: str, clip_path: Path, extent: ClipExtent) -> None:
    # CPU-bound (ffmpeg frame extraction + SigLIP2) - same to_thread/own-task
    # treatment as transcription.
    try:
        embedding = await asyncio.to_thread(_on_clip_alone, frame_encoder.encode_clip, clip_path, extent)
    except Exception:
        logger.exception("[%s] frame encoding failed for moment %d", channel, moment_id)
        return
    if not embedding:
        logger.info("[%s] no frames extracted for moment %d, skipping", channel, moment_id)
        return

    conn = get_connection()
    try:
        update_moment_frame_embedding(conn, moment_id, embedding)
    finally:
        conn.close()
    logger.info("[%s] frame embedding saved for moment %d (%d bytes)", channel, moment_id, len(embedding))


async def _tag_sound_events_background(moment_id: int, channel: str, clip_path: Path, extent: ClipExtent) -> None:
    # CPU-bound (ffmpeg audio extraction + PANNs) - same to_thread/own-task
    # treatment as transcription, and independent of audio_events.py's
    # SenseVoice tagging: one failing never blocks the other.
    try:
        tags, embedding = await asyncio.to_thread(
            _on_clip_alone, sound_events.tag_sound_events, clip_path, extent
        )
    except Exception:
        logger.exception("[%s] sound event tagging failed for moment %d", channel, moment_id)
        return

    conn = get_connection()
    try:
        update_moment_sound_events(conn, moment_id, tags)
        if embedding:
            update_moment_sound_embedding(conn, moment_id, embedding)
    finally:
        conn.close()
    logger.info("[%s] sound events saved for moment %d: %s", channel, moment_id, tags)


# A detected moment isn't cut into a clip right away. It stays "open" for a
# short while, and if new people keep piling into the reaction after it fired
# (detector.reaction_active) its end is pushed out, so a reaction that really
# does keep going gets one clip covering all of it. Most moments don't earn
# that and close at their base length once chat has been quiet for a while;
# there is a hard cap either way. Only then is the clip cut. How long quiet,
# how hard a cap and how long a clip are the channel's settings - see
# tuning.py for what they are recommended to be, and why.


@dataclass
class _MomentSession:
    moment_id: int
    channel: str
    window_start: datetime
    trigger_time: datetime
    window_end: datetime  # pushed out while the reaction continues
    last_active: datetime  # last time the reaction was still above sustain
    # When the moment fired on the detector's (monotonic) clock - only
    # reactions newer than this can extend it.
    triggered_at: float
    # The channel's settings as they stood when the moment fired. It is seen
    # through under these - how long it may stay open, what keeps it open,
    # how its clip is cut - whatever is saved in the meantime.
    tuned: tuning.Tuning


# One open moment per channel at a time.
_moment_sessions: dict[str, _MomentSession] = {}


def _session_should_close(session: _MomentSession, reaction_is_active: bool, now: datetime) -> bool:
    """Pure decision for one poll tick: extend the open moment if the reaction
    is still going, and report whether it's time to close it.
    """
    if reaction_is_active:
        session.last_active = now
        session.window_end = now
    if (now - session.trigger_time).total_seconds() >= session.tuned.max_open_seconds:
        return True
    return (now - session.last_active).total_seconds() >= session.tuned.quiet_seconds


async def _run_moment_session(session: _MomentSession) -> None:
    try:
        while True:
            await asyncio.sleep(tuning.MOMENT_SESSION_POLL_SECONDS)
            now = datetime.now(timezone.utc)
            still_reacting = detector.reaction_active(
                session.channel, since=session.triggered_at, settings=session.tuned.detector
            )
            if _session_should_close(session, still_reacting, now):
                break
    finally:
        # Free the channel as soon as the moment closes (or the task is
        # cancelled at shutdown) so a fresh reaction can open a new moment
        # while this one's clip is still being cut.
        _moment_sessions.pop(session.channel, None)

    conn = get_connection()
    try:
        update_moment_window_end(conn, session.moment_id, session.window_end.isoformat())
    finally:
        conn.close()
    logger.info(
        "[%s] moment %d closed after %.0fs reaction",
        session.channel, session.moment_id,
        (session.window_end - session.window_start).total_seconds(),
    )
    _track_task(
        _create_clip_background(
            session.moment_id, session.channel, session.window_start, session.window_end, session.tuned
        )
    )


# -- streams kept whole --------------------------------------------------------
#
# A stream recording is started and stopped from the Channels page (see
# stream_recording.py for what one is, recording_manager.py for what keeps
# it going). What is seen to here is its row, and the task that makes its
# file once it is over.

# Stream recordings that are being made into their files right now, by id:
# whose stream it is, and how much footage it had when it ended (None for
# one taken up again after a restart).
_stream_saves: dict[int, dict] = {}


def _stream_recordings_not_over() -> dict[int, dict]:
    """What the pages say of each stream recording that runs or is being
    made into its file (see dashboard_view.recording_state), by recording,
    with the channel it is of."""
    on_air = set(recording_manager.recording_channels())
    states = {
        recording_id: {
            **dashboard_view.recording_state(recording_id, running=False, seconds=save["seconds"]),
            "channel": save["channel"],
        }
        for recording_id, save in _stream_saves.items()
    }
    for slug, kept in recording_manager.stream_recordings().items():
        states[kept.recording_id] = {
            **dashboard_view.recording_state(
                kept.recording_id,
                running=True,
                on_air=slug in on_air,
                seconds=kept.writer.seconds,
                size=kept.writer.size,
            ),
            "channel": slug,
        }
    return states


def _by_channel(not_over: dict[int, dict]) -> dict[str, dict]:
    """The same by channel. One that has a recording running and its last
    one still being saved is said to be recording."""
    states: dict[str, dict] = {}
    for state in sorted(not_over.values(), key=lambda state: state["running"]):
        states[state["channel"]] = state
    return states


def _recordings_mark(not_over: dict[int, dict], total: int) -> str:
    """Different whenever the list of recordings would read differently: one
    was added or removed, or went from running to being saved to done. The
    Recordings page compares it with the one it was made with."""
    standing = (f"{recording_id}{'r' if state['running'] else 's'}" for recording_id, state in sorted(not_over.items()))
    return f"{total}:{','.join(standing)}"


def _save_stream_recording_soon(
    recording_id: int, channel: str, folder: Path, seconds: float | None, size: int
) -> None:
    """Has a stream recording that is over made into its file."""
    _stream_saves[recording_id] = {"channel": channel, "seconds": seconds}
    recording_manager.hold_room(recording_id, size)
    _track_task(_save_stream_recording(recording_id, channel, folder))


async def _save_stream_recording(recording_id: int, channel: str, folder: Path) -> None:
    # Copying hours of footage takes minutes: off the event loop, and as a
    # tracked task so a shutdown waits for it. A save that is cut short all
    # the same leaves the row saying "saving", and the next start takes it
    # up again.
    try:
        parts: list[stream_recording.Part] = []
        started = stream_recording.kept_from(folder)
        if started is not None:
            name = stream_recording.file_name(channel, recording_id, started.astimezone())
            output = stream_recording.channel_folder(channel) / name
            parts = await asyncio.to_thread(stream_recording.save, folder, output)
        conn = get_connection()
        try:
            if parts:
                finish_stream_recording(
                    conn,
                    recording_id,
                    state=stream_recording.READY,
                    file_path=output.relative_to(STREAMS_DIR).as_posix(),
                    started_at=parts[0].started_at.isoformat(),
                    ended_at=parts[-1].ended_at.isoformat(),
                    duration=stream_recording.length(parts),
                    size_bytes=output.stat().st_size,
                    parts=stream_recording.encoded(parts),
                )
            else:
                delete_stream_recording(conn, recording_id)
        finally:
            conn.close()
    except Exception:
        logger.exception(
            "[%s] stream recording %d could not be saved - what it kept is in %s", channel, recording_id, folder
        )
        conn = get_connection()
        try:
            set_stream_recording_state(conn, recording_id, stream_recording.FAILED)
        finally:
            conn.close()
        return
    finally:
        # From here on its row says how it stands.
        _stream_saves.pop(recording_id, None)
        recording_manager.release_room(recording_id)

    # Only now: until its parts were stored, the folder was what the
    # recording could be made from again.
    await asyncio.to_thread(shutil.rmtree, folder, True)
    if parts:
        logger.info(
            "[%s] stream recording %d saved: %s (%.0f s in %d part(s))",
            channel, recording_id, output, stream_recording.length(parts), len(parts),
        )
    else:
        logger.info("[%s] stream recording %d kept no footage - nothing to save", channel, recording_id)


def _end_stream_recording(slug: str, ended_by: str) -> bool:
    """Ends the stream recording of a channel and has what it kept made into
    its file. Returns whether there is anything to make one of; a recording
    that kept nothing - stopped before its stream began - is forgotten."""
    kept = recording_manager.end_stream_recording(slug)
    if kept is None:
        return False
    if ended_by != stream_recording.STREAM_ENDED:
        # Its stream may well be on still. A channel that is recorded always
        # is left alone until it is over - or stopping a recording would
        # only start the next one.
        recording_manager.hold_off(slug)
    writer = kept.writer
    conn = get_connection()
    try:
        # By what it holds, not by how long that runs: footage that could
        # not be measured is still footage.
        if writer.size:
            set_stream_recording_state(conn, kept.recording_id, stream_recording.SAVING, ended_by)
        else:
            delete_stream_recording(conn, kept.recording_id)
    finally:
        conn.close()
    if not writer.size:
        shutil.rmtree(writer.folder, ignore_errors=True)
        logger.info("[%s] stream recording %d ended (%s) before any footage", slug, kept.recording_id, ended_by)
        return False
    unasked = ended_by in (stream_recording.DISK_FULL, stream_recording.WRITE_FAILED)
    (logger.warning if unasked else logger.info)(
        "[%s] stream recording %d ended (%s) with %.0f s of footage - saving it",
        slug, kept.recording_id, ended_by, writer.seconds,
    )
    _save_stream_recording_soon(kept.recording_id, slug, writer.folder, writer.seconds, writer.size)
    return True


class _NotStarted(Exception):
    """Why a stream recording was not started, in words for whoever asked
    for it, with the HTTP status that goes with them."""

    def __init__(self, status_code: int, words: str) -> None:
        super().__init__(words)
        self.status_code = status_code
        self.words = words


def _start_stream_recording(slug: str, broadcaster_user_id: int) -> int:
    """Starts keeping a channel's stream whole and returns the recording's
    id - or says why not (_NotStarted): the channel is being recorded
    already, or the disk is too full."""
    if recording_manager.stream_recording_of(slug) is not None:
        raise _NotStarted(409, f"{slug} is being recorded already.")
    room = recording_manager.room_left()
    if room < recording_manager.MIN_FREE_BYTES:
        raise _NotStarted(
            409,
            "The disk is too full to start a recording: "
            f"{dashboard_view.size_words(max(room, 0))} would be left once what is being recorded "
            f"is saved, and it takes {dashboard_view.size_words(recording_manager.MIN_FREE_BYTES)}.",
        )
    conn = get_connection()
    try:
        recording_id = insert_stream_recording(
            conn,
            broadcaster_user_id=broadcaster_user_id,
            channel_slug=slug,
            requested_at=datetime.now(timezone.utc).isoformat(),
            state=stream_recording.RECORDING,
        )
    finally:
        conn.close()
    try:
        recording_manager.start_stream_recording(slug, broadcaster_user_id, recording_id)
    except OSError:
        # Nowhere to keep it: there is no recording, so there is no row.
        logger.exception("[%s] stream recording %d could not be started", slug, recording_id)
        conn = get_connection()
        try:
            delete_stream_recording(conn, recording_id)
        finally:
            conn.close()
        raise _NotStarted(500, "Could not make a place to keep the recording. The server log says why.") from None
    return recording_id


async def _record_stream_by_itself(slug: str) -> None:
    """What the recording loop calls when a channel that is recorded always
    is live and nothing is keeping its stream (see
    recording_manager.run_forever): a recording is started, as the button on
    the Channels page starts one. If it can't be, the channel is left alone
    until its next stream instead of being tried again every round."""
    if _shutdown_requested:
        return
    conn = get_connection()
    try:
        row = get_streamer_by_slug(conn, slug)
    finally:
        conn.close()
    if row is None or not row["always_record"]:
        return  # switched off since the loop last looked
    try:
        recording_id = _start_stream_recording(slug, row["broadcaster_user_id"])
    except _NotStarted as refused:
        recording_manager.hold_off(slug)
        logger.warning(
            "[%s] is live and set to be recorded always, but this stream is not being recorded: %s",
            slug, refused.words,
        )
        return
    logger.info("[%s] stream recording %d started by itself - the channel is recorded always", slug, recording_id)


async def _stream_recording_over(slug: str, why: str) -> None:
    """What the recording loop calls when a stream recording should end
    without anyone stopping it: its stream is over, or the disk is running
    out (see recording_manager.run_forever)."""
    _end_stream_recording(slug, why)


def _take_up_stream_recordings() -> None:
    """Sees to the stream recordings the service was stopped under.

    One that was running goes on: its channel is recorded again from now,
    into the same recording - the stretch in between is a break in it like
    any other. That holds for one that was still waiting for its stream to
    begin, and for one whose footage is fresh. If what it kept is older than
    a stream may be gone for before its recording ends, the stream it was
    started for is over: the recording ends where it was cut short. It, and
    any that was being saved, is made into its file from what its folder
    holds.
    """
    now = datetime.now(timezone.utc)
    stale = timedelta(seconds=recording_manager.STREAM_END_GRACE_SECONDS)
    cut_short = []
    conn = get_connection()
    try:
        # The oldest first: of two that were running on one channel - which
        # nothing lets happen - it is the first that goes on.
        for row in reversed(get_stream_recordings(conn, (stream_recording.RECORDING, stream_recording.SAVING))):
            slug, recording_id = row["channel_slug"], row["id"]
            was_running = row["state"] == stream_recording.RECORDING
            written = stream_recording.last_written(stream_recording.parts_folder(slug, recording_id))
            fresh = written is None or now - written < stale
            if was_running and fresh and recording_manager.stream_recording_of(slug) is None:
                kept = recording_manager.start_stream_recording(slug, row["broadcaster_user_id"], recording_id)
                logger.warning(
                    "[%s] stream recording %d goes on after the restart, with %.0f s of footage so far",
                    slug, recording_id, kept.writer.seconds,
                )
                continue
            if was_running:
                set_stream_recording_state(conn, recording_id, stream_recording.SAVING, stream_recording.INTERRUPTED)
            cut_short.append((recording_id, slug))
    finally:
        conn.close()
    for recording_id, slug in cut_short:
        folder = stream_recording.parts_folder(slug, recording_id)
        logger.warning(
            "[%s] stream recording %d was not finished when the service stopped - saving what it kept",
            slug, recording_id,
        )
        _save_stream_recording_soon(recording_id, slug, folder, None, stream_recording.kept_bytes(folder))


def _stream_file(row) -> Path | None:
    """The file of a stream recording, or None if it has none - or has one
    that is not where recordings are kept."""
    if not row["file_path"]:
        return None
    file = STREAMS_DIR / row["file_path"]
    return file if file.resolve().is_relative_to(STREAMS_DIR.resolve()) else None


@app.get("/health")
async def health():
    return {"status": "ok"}


MOMENTS_PAGE_SIZE = 50

# Which moments each of the review queue's lists holds, as filters for the
# moment queries.
SHOW_FILTERS: dict[str, dict] = {
    dashboard_view.SHOW_UNRATED: {"unrated": True},
    dashboard_view.SHOW_ALL: {},
    dashboard_view.SHOW_BEST: {"min_rating": dashboard_view.BEST_RATING_MIN},
}

# How long after detection a missing analysis result is still shown as "in
# progress". Cutting the clip and running a step on it takes a minute or two;
# anything older without a result was either never analysed or failed.
ANALYSIS_PENDING_SECONDS = 600
# The analysis steps whose result is text the dashboard shows (frame
# embeddings are stored but there is nothing of them to read).
ANALYSIS_TEXT_RESULTS = ("transcript", "audio_events", "sound_events")
# How long after detection a moment without a clip is still shown as having
# one on the way: for as long as the channel's settings let a moment stay
# open and have it wait for its post-roll and the context after it to be
# broadcast, and this much on top - for the cut itself, and for the VOD to
# be written where the cut has to come from that. Past it, no clip is coming.
CLIP_PENDING_MARGIN_SECONDS = 230
# The most chat lines read for one moment: for the trace and the replay
# beside an open clip, and for a queue row's spark. Two minutes of a chat
# posting ten messages a second stays under the first.
CHAT_REPLAY_LIMIT = 2000
# A moment's name has to fit a queue row; longer ones are cut to this.
MOMENT_TITLE_MAX_LENGTH = 60
QUEUE_CHAT_LIMIT = 1000


def _moment_age_seconds(row) -> float:
    detected = datetime.fromisoformat(row["detected_at"]).astimezone(timezone.utc)
    return (datetime.now(timezone.utc) - detected).total_seconds()


def _pending_analysis(row) -> set[str]:
    """Which analysis results the dashboard should show as still on their way.

    Only a step that is switched on, for a clip recent enough that the step
    can actually be running, with nothing stored yet (an empty result means
    "done, found nothing"). The page used to show "transcribing..." on every
    clip without a transcript - which, with analysis off by default, was
    every clip, forever.
    """
    if not row["clip_path"] or _moment_age_seconds(row) > ANALYSIS_PENDING_SECONDS:
        return set()
    return {
        name for name in ANALYSIS_TEXT_RESULTS if row[name] is None and _flags.get(SETTING_KEYS[name], False)
    }


def _clip_pending_seconds(tuned: tuning.Tuning) -> float:
    return (
        tuned.max_open_seconds
        + tuned.post_roll_seconds
        + tuned.context_after_seconds
        + CLIP_PENDING_MARGIN_SECONDS
    )


def _clip_state(row, tuned: tuning.Tuning) -> str:
    """Whether a moment's clip is there to play ("ready"), is still being cut
    ("pending") or is not coming ("missing")."""
    if row["clip_path"]:
        return "ready"
    return "pending" if _moment_age_seconds(row) <= _clip_pending_seconds(tuned) else "missing"


def _clip_url(clip_path: str) -> str:
    return f"/clips/{quote(clip_path)}"


# Stretches of footage being cut out for an editor right now, the ones that
# failed, and how long before the start that was set the last one of a
# moment begins, by moment id. Any other moment's is told from whether its
# files are there, so a restart forgets nothing that matters.
_trims: dict[int, dict] = {}


def _trim_paths(clip_path: str) -> tuple[Path, Path]:
    """Where a moment's trimmed video and the chat video that goes with it
    are kept, like the clip's own path: under CLIPS_DIR."""
    clip = Path(clip_path)
    video = clip.with_name(clip_trim.video_name(clip.name))
    return video, video.with_name(chat_video.video_name(video.name))


def _download_url(relative: Path) -> str | None:
    """The address of a file under CLIPS_DIR, or None if it is not there."""
    file = CLIPS_DIR / relative
    if not file.exists():
        return None
    # Made again, it is a different file under the same name.
    return f"{_clip_url(relative.as_posix())}?v={int(file.stat().st_mtime)}"


def _trim(row) -> dict:
    """Where the stretch cut out of a moment's footage stands: being cut,
    its chat being rendered, there to download, failed, or never asked
    for."""
    known = _trims.get(row["id"], {})
    state = known.get("state")
    video, chat = (_download_url(path) for path in _trim_paths(row["clip_path"]))
    if state in (dashboard_view.TRIM_CUTTING, dashboard_view.TRIM_FAILED):
        video = chat = None
    elif state in (dashboard_view.TRIM_CHAT, dashboard_view.TRIM_CHAT_FAILED):
        chat = None
    else:
        state = dashboard_view.TRIM_READY if video else dashboard_view.TRIM_NONE
    return {
        "state": state,
        "words": dashboard_view.trim_words(state, known.get("early", 0.0)),
        "video_url": video,
        "chat_url": chat,
    }


async def _trim_background(
    moment_id: int,
    channel: str,
    clip: Path,
    span: tuple[float, float],
    lead: float,
    lines: list[dict] | None,
    shift: float,
    background: str | None,
    video: Path,
    chat: Path,
) -> None:
    # The cut takes a moment; the chat video drives a browser and an encoder
    # for about twice the cut's length. Both off the event loop, and as a
    # tracked task so a shutdown waits for them.
    try:
        # What was cut before goes first: a chat video left over from it
        # would not belong to the new video.
        chat.unlink(missing_ok=True)
        video.unlink(missing_ok=True)
        begins, duration = await asyncio.to_thread(recorder.trim_clip, clip, *span, video)
    except Exception:
        logger.exception("[%s] trimming failed for moment %d", channel, moment_id)
        _trims[moment_id] = {"state": dashboard_view.TRIM_FAILED}
        return
    early = max(span[0] - begins, 0.0)
    logger.info("[%s] moment %d trimmed: %s (%.1f s)", channel, moment_id, video.name, duration)
    if lines is None:
        _trims[moment_id] = {"early": early}
        return

    _trims[moment_id] = {"state": dashboard_view.TRIM_CHAT, "early": early}
    try:
        # The chat video starts on the trimmed video's first frame, wherever
        # in the file that turned out to be.
        shown = chat_video.lines_from(lines, begins - lead, shift)
        frames = await asyncio.to_thread(chat_video.render, shown, duration, chat, background)
    except Exception:
        logger.exception("[%s] rendering the chat video failed for moment %d", channel, moment_id)
        _trims[moment_id] = {"state": dashboard_view.TRIM_CHAT_FAILED, "early": early}
        return
    _trims[moment_id] = {"early": early}
    logger.info("[%s] chat video saved for moment %d: %s (%d frames)", channel, moment_id, chat.name, frames)


def _moment_window(row) -> tuple[datetime, datetime]:
    """When a moment's reaction ran, on chat's clock."""
    return datetime.fromisoformat(row["window_start"]), datetime.fromisoformat(row["window_end"])


async def _with_clip_length(conn, row):
    """The moment with the length of its clip filled in. Clips cut before
    lengths were stored are measured the first time one is opened, and that
    is kept; a file that can't be measured is left as it is."""
    if not row["clip_path"] or row["clip_duration"] is not None:
        return row
    path = CLIPS_DIR / row["clip_path"]
    if not path.exists():
        return row
    duration = await asyncio.to_thread(recorder.probe_duration, path)
    if duration is None:
        return row
    update_moment_clip_duration(conn, row["id"], duration)
    return get_moment(conn, row["id"])


def _chat_delay(conn, channel_slug: str) -> int:
    """How far behind its broadcast a channel's chat is taken to run: what
    was set for the channel on the review page, or what every channel starts
    out with."""
    stored = get_chat_delay(conn, channel_slug)
    return chat_trace.CHAT_DELAY_SECONDS if stored is None else stored


def _chat_against_clip(conn, row, chat_delay: int) -> tuple[dict | None, list[dict]]:
    """The strip under a moment's clip and the chat lines beside it: what
    chat was doing from a little before the clip to a little after it, on
    the clip's own time axis. No strip when the clip can't be placed at all
    (see chat_trace.clip_timeline)."""
    tuned = _tuning(conn, row["broadcaster_user_id"])
    timeline = chat_trace.clip_timeline(
        row,
        pre_roll=tuned.pre_roll_seconds,
        post_roll=tuned.post_roll_seconds,
        playback_delay=tuned.playback_delay_seconds,
        chat_delay=chat_delay,
        context_before=tuned.context_before_seconds,
        context_after=tuned.context_after_seconds,
    )
    if timeline is None:
        return None, []
    if timeline.chat_start is None:
        return chat_trace.trace(timeline, [], [], usual=0.0, window=None), []

    messages = get_chat_between(
        conn,
        row["channel_slug"],
        timeline.chat_start + timedelta(seconds=timeline.start),
        timeline.chat_start + timedelta(seconds=timeline.end),
        CHAT_REPLAY_LIMIT,
    )
    window = _moment_window(row)
    laughing_too = tuned.detector.added_laugh_emote_patterns
    laugh_names = detector.laugh_emote_names(_channel_keywords(conn, row["broadcaster_user_id"]), laughing_too)
    everything, laughing = chat_trace.message_counts(messages, timeline, laugh_names, laughing_too)
    strip = chat_trace.trace(timeline, everything, laughing, usual=row["baseline_message_rate"], window=window)
    emotes = get_chat_emotes(conn, row["broadcaster_user_id"])
    chatters = {message["sender_username"] for message in messages if message["sender_username"]}
    identities = get_chat_identities(conn, row["broadcaster_user_id"], chatters)
    return strip, chat_trace.chat_replay(messages, timeline, window, emotes, identities)


def _queue_activity(conn, rows) -> dict[int, dict]:
    """What chat did around each moment of the queue (see
    chat_trace.queue_activity), by moment id. Imported clips have no chat
    and are left out."""
    activity = {}
    for row in rows:
        if row["reason"] == dashboard_view.IMPORT_REASON:
            continue
        window = _moment_window(row)
        start, end = chat_trace.spark_span(window[0])
        end = max(end, window[1] + timedelta(seconds=chat_trace.MOMENT_PADDING_SECONDS))
        messages = get_chat_between(conn, row["channel_slug"], start, end, QUEUE_CHAT_LIMIT)
        activity[row["id"]] = chat_trace.queue_activity(
            messages,
            window,
            _channel_keywords(conn, row["broadcaster_user_id"]),
            _tuning(conn, row["broadcaster_user_id"]).detector.added_laugh_emote_patterns,
        )
    return activity


def _open_moment(conn, row, today: date) -> dict:
    """Everything the review page shows of the one moment that is open."""
    pending = _pending_analysis(row)
    chat_delay = _chat_delay(conn, row["channel_slug"])
    strip, chat = _chat_against_clip(conn, row, chat_delay)
    return {
        "id": row["id"],
        "channel": row["channel_slug"],
        "when": dashboard_view.when_words(to_local_datetime(row["detected_at"]), today),
        "stream_time": dashboard_view.stream_time_words(row["stream_elapsed_seconds"]),
        "clip_url": _clip_url(row["clip_path"]) if row["clip_path"] else None,
        "clip_state": _clip_state(row, _tuning(conn, row["broadcaster_user_id"])),
        # How far into its file the clip begins: the footage that led up to
        # it comes first. The page starts the player there.
        "clip_lead": dashboard_view.number_words(row["context_before"] or 0.0, 3),
        "strip": strip,
        "summary": dashboard_view.summary_words(row),
        "rating": row["rating"],
        "rating_words": dashboard_view.rating_words(row["rating"]),
        "stream_type": row["stream_type"],
        "moment_type": row["moment_type"],
        "notes": row["notes"] or "",
        "title": row["title"] or "",
        "title_max": MOMENT_TITLE_MAX_LENGTH,
        "analysis": [
            {"label": label, "text": row[name] or "", "pending": name in pending}
            for name, label in ANALYSIS_SETTINGS
            if name in ANALYSIS_TEXT_RESULTS and (row[name] or name in pending)
        ],
        "chat": chat,
        # Moving chat against the picture is offered where there is a picture
        # for it to be out of step with.
        "chat_delay": (
            {
                "seconds": chat_delay,
                "least": chat_trace.CHAT_DELAY_MIN_SECONDS,
                "most": chat_trace.CHAT_DELAY_MAX_SECONDS,
                # How far along the slider "as it arrived" is.
                "rest": dashboard_view.number_words(
                    chat_trace.CHAT_DELAY_MAX_SECONDS
                    / (chat_trace.CHAT_DELAY_MAX_SECONDS - chat_trace.CHAT_DELAY_MIN_SECONDS),
                    4,
                ),
            }
            if row["clip_path"] and strip and strip["has_chat"]
            else None
        ),
        # Cutting a stretch of the footage out for an editor: where there is
        # a clip to cut from and a timeline to choose the stretch on.
        "trim": (
            {
                "last": dashboard_view.number_words(clip_trim.footage(row)[1], 2),
                "min_seconds": dashboard_view.number_words(clip_trim.MIN_SECONDS),
                "chat": bool(chat),
                "chat_shift_limit": dashboard_view.number_words(clip_trim.CHAT_SHIFT_LIMIT_SECONDS),
                "chat_background": clip_trim.DEFAULT_CHAT_BACKGROUND,
            }
            if row["clip_path"] and row["clip_duration"] and strip
            else None
        ),
    }


def _asset_version() -> int:
    """Changes whenever the stylesheet or the script does, so a browser never
    pairs a new page with its cached copy of the old ones."""
    return max(int((STATIC_DIR / name).stat().st_mtime) for name in ("dashboard.css", "dashboard.js"))


def _today() -> date:
    return datetime.now().astimezone().date()


def _right_now(streamers) -> dict:
    """What the watched channels are doing right now, as the bar across the
    top says it of the service and the Channels page of each channel: the
    words (see dashboard_view.now_words), whether the lamp beside them is
    lit, and for the bar how many channels that is of. A page is made with
    this, and kept saying it while a stream is being recorded (see
    recordings_status)."""
    tracked = {row["slug"] for row in streamers if row["tracking_enabled"]}
    # A channel is looked at for either reason: it is tracked for clips, or
    # its stream is being kept whole - or will be once it is on. One whose
    # recording was ended is not looked at for that until its next stream.
    whole_streams = set(recording_manager.stream_recordings())
    awaited = {
        row["slug"] for row in streamers if row["always_record"] and not recording_manager.is_held_off(row["slug"])
    }
    live = set(recording_manager.recording_channels())
    watching = _flags.get("watching_enabled", True)
    return {
        "live": bool(live),
        "words": dashboard_view.now_words(live=bool(live), recorded=bool(live & whole_streams)),
        "count": dashboard_view.channel_count_words(len(live), len(tracked | whole_streams | awaited)),
        "channels": {
            slug: {
                "live": slug in live,
                "words": dashboard_view.now_words(
                    live=slug in live,
                    recorded=slug in whole_streams,
                    looked_at=slug in whole_streams or slug in awaited or (watching and slug in tracked),
                ),
            }
            for slug in (row["slug"] for row in streamers)
        },
    }


def _page_context(conn, nav: str) -> dict:
    """What every dashboard page needs: which page it is, and the state of
    the service shown in the bar across its top."""
    streamers = get_streamers(conn)
    tracked = {row["slug"] for row in streamers if row["tracking_enabled"]}
    recording = recording_manager.recording_channels()
    watching = _flags.get("watching_enabled", True)
    return {
        "nav": nav,
        "asset_version": _asset_version(),
        "watching_enabled": watching,
        "streamers": streamers,
        "now": _right_now(streamers),
        "service_words": dashboard_view.service_words(
            # About moments, so only the channels they can come from count.
            recording=sum(1 for slug in recording if slug in tracked),
            tracked=len(tracked),
            watchlist=len(streamers),
            watching=watching,
            shutting_down=_shutdown_requested,
        ),
        "shutdown_requested": _shutdown_requested,
        "shutdown_words": dashboard_view.shutdown_words(len(_moment_sessions) + len(_background_tasks)),
    }


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    show: str = dashboard_view.SHOW_UNRATED,
    channel: str | None = None,
    offset: int = 0,
    moment: int | None = None,
):
    """The review page: a queue of moments and the one that is open.

    `show` picks the list (unrated, all, best), `channel` narrows it to one
    channel and `offset` pages through it. `moment` is the one to open; left
    out, it is the first of the list, and with an empty list the page says
    what the service is doing instead.
    """
    show = show if show in dashboard_view.SHOWS else dashboard_view.SHOW_UNRATED
    channel = channel or None
    today = _today()

    conn = get_connection()
    try:
        counts = {
            name: count_moments(conn, channel_slug=channel, **filters) for name, filters in SHOW_FILTERS.items()
        }
        # A list gets shorter as its moments are rated, so a page that was
        # there a minute ago may be past its end now: show the last one then.
        last_page = max(counts[show] - 1, 0) // MOMENTS_PAGE_SIZE * MOMENTS_PAGE_SIZE
        offset = min(max(0, offset), last_page)
        rows = get_recent_moments(
            conn, limit=MOMENTS_PAGE_SIZE, offset=offset, channel_slug=channel, **SHOW_FILTERS[show]
        )
        activity = _queue_activity(conn, rows)
        spark_top = chat_trace.spark_top(chat["all"] for chat in activity.values())
        groups = dashboard_view.queue_groups(rows, today, {key: chat["said"] for key, chat in activity.items()})
        for group in groups:
            for item in group["rows"]:
                item["href"] = dashboard_view.review_url(show, channel, offset=offset, moment=item["id"])
                chat = activity.get(item["id"])
                item["spark"] = chat_trace.spark_paths(chat["all"], chat["laugh"], spark_top) if chat else None

        opened = get_moment(conn, moment) if moment is not None else None
        if opened is None and rows:
            opened = rows[0]
        if opened is not None:
            opened = await _with_clip_length(conn, opened)

        empty = None
        if not rows:
            newest = get_recent_moments(conn, limit=1, channel_slug=channel)
            empty = dashboard_view.empty_queue_words(
                show, channel, to_local_datetime(newest[0]["detected_at"]) if newest else None, today
            )

        context = _page_context(conn, "review")
        context.update(
            {
                "show": show,
                "tabs": [
                    {
                        "show": name,
                        "label": dashboard_view.SHOW_LABELS[name],
                        "count": counts[name],
                        "href": dashboard_view.review_url(name, channel),
                        "current": name == show,
                    }
                    for name in dashboard_view.SHOWS
                ],
                "channels": get_moment_channels(conn),
                "selected_channel": channel,
                "groups": groups,
                "empty": empty,
                "paging": _paging(show, channel, offset, len(rows), counts[show]),
                "moment": _open_moment(conn, opened, today) if opened is not None else None,
                # Offered from an empty list when there is something to go through elsewhere.
                "rated_href": (
                    dashboard_view.review_url(dashboard_view.SHOW_ALL, channel)
                    if show != dashboard_view.SHOW_ALL and counts[dashboard_view.SHOW_ALL]
                    else None
                ),
                "moment_status": _moment_status(conn),
                "best_rating_min": dashboard_view.BEST_RATING_MIN,
                "spark_height": chat_trace.SPARK_HEIGHT,
                "spark_steps": chat_trace.SPARK_STEPS,
                "stream_types": STREAM_TYPES,
                "moment_types": MOMENT_TYPES,
            }
        )
    finally:
        conn.close()

    return templates.TemplateResponse(request, "review.html", context)


def _paging(show: str, channel: str | None, offset: int, shown: int, total: int) -> dict | None:
    """Links to the neighbouring pages of the queue, or None when the whole
    list fits on one."""
    newer = offset > 0
    older = offset + shown < total
    if not newer and not older:
        return None
    return {
        "words": f"{offset + 1} to {offset + shown} of {total}",
        "newer_href": (
            dashboard_view.review_url(show, channel, offset=max(offset - MOMENTS_PAGE_SIZE, 0)) if newer else None
        ),
        "older_href": dashboard_view.review_url(show, channel, offset=offset + MOMENTS_PAGE_SIZE) if older else None,
    }


@app.get("/dashboard/moments/{moment_id}", response_class=HTMLResponse)
async def dashboard_moment(request: Request, moment_id: int):
    """One moment as the review page shows it, on its own - what the page
    fetches to open another moment from the queue without loading everything
    around it again."""
    conn = get_connection()
    try:
        row = get_moment(conn, moment_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown moment {moment_id}")
        context = {
            "moment": _open_moment(conn, await _with_clip_length(conn, row), _today()),
            "stream_types": STREAM_TYPES,
            "moment_types": MOMENT_TYPES,
        }
    finally:
        conn.close()
    return templates.TemplateResponse(request, "_moment.html", context)


@app.get("/dashboard/channels", response_class=HTMLResponse)
async def dashboard_channels(request: Request):
    """The watchlist and the controls of the service itself."""
    today = _today()
    conn = get_connection()
    try:
        context = _page_context(conn, "channels")
        whole_streams = _by_channel(_stream_recordings_not_over())
        context.update(
            {
                "watchlist": [
                    {
                        "slug": row["slug"],
                        "broadcaster_user_id": row["broadcaster_user_id"],
                        "added": dashboard_view.date_words(to_local_datetime(row["added_at"]).date(), today),
                        "now": context["now"]["channels"][row["slug"]],
                        "tracking_enabled": bool(row["tracking_enabled"]),
                        "always_record": bool(row["always_record"]),
                        # Its stream being kept whole, or the last such
                        # recording still being saved - None if neither.
                        "whole_stream": whole_streams.get(row["slug"]),
                    }
                    for row in context["streamers"]
                ],
                "analysis_settings": [
                    {
                        "name": name,
                        "label": label,
                        "caption": dashboard_view.ANALYSIS_CAPTIONS[name],
                        "enabled": _flags.get(SETTING_KEYS[name], False),
                    }
                    for name, label in ANALYSIS_SETTINGS
                ],
            }
        )
    finally:
        conn.close()

    return templates.TemplateResponse(request, "channels.html", context)


@app.get("/dashboard/settings", response_class=HTMLResponse)
async def dashboard_settings(request: Request, channel: str | None = None):
    """The Settings page: what of detection and of clips can be set (see
    tuning.py) - for all channels, or with `channel` for one of the
    watchlist's, which then shows what it has of its own."""
    channel = channel or None
    conn = get_connection()
    try:
        streamers = get_streamers(conn)
        row = next((streamer for streamer in streamers if streamer["slug"] == channel), None)
        if channel and row is None:
            raise HTTPException(status_code=404, detail=f"unknown channel {channel!r}")

        for_all = tuning.stored(get_tuning(conn))
        own = bool(row and row["own_tuning"])
        if row is None:
            every, rest, rest_label = tuning.values(for_all), tuning.recommended_values(), "Recommended"
            address = "/tuning"
        else:
            # A channel that does not go by its own settings is shown the
            # ones it does go by.
            mine = tuning.stored(get_tuning(conn, row["broadcaster_user_id"])) if own else {}
            every, rest, rest_label = tuning.values(for_all, mine), tuning.values(for_all), "All channels"
            address = f"/channels/{quote(channel)}/tuning"

        context = _page_context(conn, "settings")
        context.update(
            {
                "channel": channel,
                "channels": [
                    {"slug": streamer["slug"], "own": bool(streamer["own_tuning"])} for streamer in streamers
                ],
                "own": own,
                "editable": row is None or own,
                "sections": tuning.form(every, rest, rest_label),
                "save_url": address,
                "trial_url": f"{address}/trial",
                "own_url": f"/channels/{quote(channel)}/own_tuning" if channel else None,
                "own_words": (
                    dashboard_view.own_settings_words(
                        [streamer["slug"] for streamer in streamers if streamer["own_tuning"]]
                    )
                    if row is None
                    else None
                ),
                "trial_messages": dashboard_view.count_words(TRIAL_MESSAGE_LIMIT, "message"),
            }
        )
    finally:
        conn.close()

    return templates.TemplateResponse(request, "settings.html", context)


# How much of a recording's chat the page is given at a time: from this long
# before the second it asks about to this long after it, and at most this
# many lines - past which the stretch ends where the lines do, and the page
# asks again from there.
RECORDING_CHAT_BEFORE_SECONDS = 60
RECORDING_CHAT_AFTER_SECONDS = 300
RECORDING_CHAT_LIMIT = 3000


def _recording_chat_delay(conn, row) -> int:
    """How far behind its broadcast a recorded channel's chat is taken to
    run: what the review page has for the channel. That is kept under the
    name the channel's chat and moments have - Kick's spelling of it, where
    a recording has the watchlist's."""
    return _chat_delay(conn, get_chat_name(conn, row["broadcaster_user_id"]) or row["channel_slug"])


def _recording_chat(conn, row, parts: list[stream_recording.Part], at: float) -> dict:
    """The chat that goes with a stretch of a recording's file, around its
    second `at`: the lines, each with the second of the file it belongs to,
    and the stretch they cover. A message is put against the picture its
    writer was watching, as on the review page (see chat_trace.py). The
    channel's chat is told by the channel's id: its name on the watchlist,
    which is the one a recording has, is not the one chat is stored under."""
    whole = stream_recording.length(parts)
    at = min(max(at, 0.0), whole)
    first = max(at - RECORDING_CHAT_BEFORE_SECONDS, 0.0)
    last = min(at + RECORDING_CHAT_AFTER_SECONDS, whole)
    delay = timedelta(seconds=_recording_chat_delay(conn, row))
    messages = get_channel_chat_between(
        conn,
        row["broadcaster_user_id"],
        stream_recording.broadcast_time(parts, first) + delay,
        stream_recording.broadcast_time(parts, last) + delay,
        RECORDING_CHAT_LIMIT,
    )
    chatters = {message["sender_username"] for message in messages if message["sender_username"]}
    lines = chat_trace.chat_lines(
        messages,
        lambda arrived: stream_recording.file_second(parts, arrived - delay),
        get_chat_emotes(conn, row["broadcaster_user_id"]),
        get_chat_identities(conn, row["broadcaster_user_id"], chatters),
    )
    if len(messages) == RECORDING_CHAT_LIMIT:
        last = max(float(lines[-1]["at"]), at)
    return {
        "url": f"/dashboard/recordings/{row['id']}/chat",
        "first": dashboard_view.number_words(first, 1),
        "last": dashboard_view.number_words(last, 1),
        "whole": last >= whole,
        "lines": lines,
    }


def _open_recording(conn, row, today: date, not_over: dict | None) -> dict:
    """Everything the Recordings page shows of the one that is open."""
    parts = stream_recording.decoded(row["parts"])
    file = _stream_file(row)
    if not_over is not None:
        standing = "running" if not_over["running"] else "saving"
    elif row["state"] == stream_recording.READY and parts and file is not None:
        standing = "playable" if file.exists() else "gone"
    else:
        standing = "failed"
    started = to_local_datetime(row["started_at"] or row["requested_at"])
    delay = _recording_chat_delay(conn, row)
    return {
        "id": row["id"],
        "channel": row["channel_slug"],
        "standing": standing,
        "when": dashboard_view.recording_span_words(
            started, to_local_datetime(row["ended_at"]) if row["ended_at"] else None, today
        ),
        "length": dashboard_view.stream_time_words(row["duration"]),
        "words": not_over["words"] if not_over else None,
        "video_url": f"/streams/{quote(row['file_path'])}" if standing == "playable" else None,
        # Where on this machine: what whoever wants the file itself needs.
        "file": str(file.resolve()) if file is not None else None,
        "folder": str(stream_recording.parts_folder(row["channel_slug"], row["id"]).resolve()),
        "summary": dashboard_view.recording_summary_words(
            row["size_bytes"],
            len(parts),
            stream_recording.missing_seconds(parts),
            row["ended_by"],
            recording_manager.STREAM_END_GRACE_SECONDS,
        ),
        "chat": _recording_chat(conn, row, parts, 0.0) if standing == "playable" else None,
        "chat_delay_words": dashboard_view.chat_delay_words(delay),
    }


@app.get("/dashboard/recordings", response_class=HTMLResponse)
async def dashboard_recordings(request: Request, recording: int | None = None):
    """The Recordings page: the streams that were kept whole, the newest
    first, and the one that is open - its video, with the chat that went
    with it replayed beside it. `recording` is the one to open; left out, it
    is the newest."""
    today = _today()
    conn = get_connection()
    try:
        rows = get_stream_recordings(conn)
        not_over = _stream_recordings_not_over()
        opened = next((row for row in rows if row["id"] == recording), rows[0] if rows else None)
        context = _page_context(conn, "recordings")
        context.update(
            {
                "recordings": [
                    {
                        "id": row["id"],
                        "channel": row["channel_slug"],
                        "when": dashboard_view.since_words(
                            to_local_datetime(row["started_at"] or row["requested_at"]), today
                        ),
                        "length": dashboard_view.stream_time_words(row["duration"]),
                        "words": dashboard_view.recording_row_words(row["state"], not_over.get(row["id"])),
                        "running": row["id"] in not_over,
                        "href": f"/dashboard/recordings?recording={row['id']}",
                        "current": opened is not None and row["id"] == opened["id"],
                    }
                    for row in rows
                ],
                "recording": (
                    _open_recording(conn, opened, today, not_over.get(opened["id"])) if opened is not None else None
                ),
                "mark": _recordings_mark(not_over, len(rows)),
                # Whether a recording may begin without anyone on a page
                # having started it - which the page then has to ask about.
                "awaited": any(row["always_record"] for row in context["streamers"]),
                "free_words": dashboard_view.free_room_words(stream_recording.free_bytes()),
            }
        )
    finally:
        conn.close()

    return templates.TemplateResponse(request, "recordings.html", context)


@app.get("/dashboard/recordings/{recording_id}/chat", response_class=HTMLResponse)
async def dashboard_recording_chat(request: Request, recording_id: int, at: float = 0.0):
    """The chat that goes with a stretch of a recording, around second `at`
    of its file - what the Recordings page fetches as its video plays on or
    is moved in."""
    conn = get_connection()
    try:
        row = get_stream_recording(conn, recording_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown recording {recording_id}")
        parts = stream_recording.decoded(row["parts"])
        if not parts:
            raise HTTPException(status_code=409, detail="the recording has no footage to put chat against yet")
        context = {
            "chat": _recording_chat(conn, row, parts, at if math.isfinite(at) else 0.0),
            "asset_version": _asset_version(),
        }
    finally:
        conn.close()
    return templates.TemplateResponse(request, "_recording_chat.html", context)


# -- the Analyze page: a channel's stored chat in figures (see chat_stats.py) --

# How many of a period's newest messages its figures are worked out from. A
# pass over them takes about a second for every forty thousand, next to a
# service that has chat to keep up with - which is also why it runs on a
# thread of its own.
ANALYZE_MESSAGE_LIMIT = 100_000


def _now() -> datetime:
    return datetime.now().astimezone()


@dataclass(frozen=True)
class _ChatWords:
    """What a channel's chat is read by: its own 7TV emote names, in small
    letters, and what a laughing message is told by there. Looked up once
    for a request, where the service keeps them - the thread that counts
    the chat is given them and stays away from the service's own state."""

    emote_names: tuple[str, ...]
    laugh_names: tuple[str, ...]
    laugh_patterns: tuple[str, ...]


@dataclass(frozen=True)
class _Asked:
    """What a request for the Analyze page asked for, beyond the channel."""

    preset: str | None
    start: datetime | None
    end: datetime | None
    view: str
    chatter: str
    text: str
    order: str
    offset: int
    top: int
    around: str


def _chat_channels(conn, streamers) -> list[dict]:
    """The channels that have chat stored, in the order of their names:
    each one's id, the name the page knows it by - the watchlist's, or for a
    channel that is no longer on it the one its chat came under - and when
    its oldest and newest messages arrived."""
    watched = {row["broadcaster_user_id"]: row["slug"] for row in streamers}
    channels = []
    for owner in get_chat_channel_ids(conn):
        first, last = get_chat_span(conn, owner)
        channels.append(
            {"id": owner, "name": watched.get(owner) or get_chat_name(conn, owner), "first": first, "last": last}
        )
    return sorted(channels, key=lambda channel: channel["name"].lower())


def _named(conn, owner: int, *tables: list[dict]) -> None:
    """Gives every row of the tables that names a chatter the name as the
    page draws it: in their colour, behind their badges."""
    names = {row["name"] for rows in tables for row in rows}
    identities = get_chat_identities(conn, owner, names)
    for rows in tables:
        for row in rows:
            row["who"] = chat_stats.chatter_name(row["name"], identities)


def _analyze_lines(conn, owner: int, rows, emotes, today: date, url, asked: str = "") -> list[dict]:
    """Stored messages as the page lists them: a line of chat each, with
    when it arrived, where its writer's other messages are and where the
    chat around it is. The one whose surroundings were asked for (`asked`,
    its id) is marked."""
    chatters = {row["sender_username"] for row in rows if row["sender_username"]}
    lines = chat_trace.chat_lines(
        rows, lambda arrived: 0.0, emotes, get_chat_identities(conn, owner, chatters)
    )
    for line, row in zip(lines, rows, strict=True):
        line["when"] = chat_stats.second_words(datetime.fromisoformat(row["received_at"]), today)
        line["around_href"] = url(around=row["message_id"])
        line["chatter_href"] = url(chatter=row["sender_username"]) if row["sender_username"] else None
        line["asked"] = row["message_id"] == asked
    return lines


def _analyze_overview(conn, owner: int, period: chat_stats.Period, words: _ChatWords, today: date, url, top: int):
    """The overview of a period of a channel's chat: its figures, the chart
    of its messages, and the tables. None for a period without chat."""
    total = count_channel_chat(conn, owner, period.start, period.end)
    if not total:
        return None
    emotes = get_chat_emotes(conn, owner)
    kept = chat_stats.collect(
        iter_channel_chat(conn, owner, period.start, period.end, limit=ANALYZE_MESSAGE_LIMIT),
        chat_stats.columns_for(period),
        emotes=emotes,
        laugh_names=words.laugh_names,
        laugh_patterns=words.laugh_patterns,
    )
    whole = kept.messages == total
    moments = [
        datetime.fromisoformat(detected)
        for detected in get_moment_times(conn, owner, period.start, period.end, not_for=dashboard_view.IMPORT_REASON)
    ]

    def column(columns: chat_stats.Columns, number: int) -> str:
        """A column leads to the stretch it stands for: looked at more
        closely, or read once it is a single minute."""
        stretch = chat_stats.Period(columns.start(number), columns.start(number + 1))
        if columns.seconds == chat_stats.COLUMN_SECONDS[0]:
            return url(stretch, view=chat_stats.VIEW_MESSAGES, order=chat_stats.ORDER_OLDEST)
        return url(stretch)

    busiest = []
    for minute, count in chat_stats.busiest_minutes(kept):
        stretch = chat_stats.Period(minute, minute + timedelta(minutes=1))
        said = search_channel_chat(conn, owner, stretch.start, stretch.end, limit=QUEUE_CHAT_LIMIT)
        busiest.append(
            {
                "when": chat_stats.minute_words(minute, today),
                "href": url(stretch, view=chat_stats.VIEW_MESSAGES, order=chat_stats.ORDER_OLDEST),
                "messages": chat_stats.whole_words(count),
                "said": chat_trace.reaction_words(said, words.emote_names) or "",
            }
        )

    chatters = chat_stats.top_chatters(kept, top, lambda name: url(chatter=name))
    mentioned = chat_stats.mentioned_most(kept, chat_stats.SIDE_ROWS, lambda name: url(chatter=name))
    _named(conn, owner, chatters, mentioned)
    longer = max(len(kept.by_chatter), len(kept.emote_uses)) > chat_stats.TOP_ROWS
    return {
        "figures": chat_stats.overview_figures(
            kept,
            today,
            # Told from everything the period holds, which the figures
            # beside it are not when only its newest messages were counted.
            returning=count_returning_chatters(conn, owner, period.start, period.end) if whole else None,
            moments=len(moments),
            moments_href=dashboard_view.review_url(dashboard_view.SHOW_ALL, get_chat_name(conn, owner)),
        ),
        "activity": chat_stats.activity_chart(
            kept, today, one_day=chat_stats.one_day(period), moments=moments, link=column
        ),
        "hours": chat_stats.hours_chart(kept) if len(kept.days) > 1 else None,
        "busiest": busiest,
        "chatters": chatters,
        "emotes": chat_stats.top_emotes(kept, top),
        "said": chat_stats.said_most(kept, chat_stats.SIDE_ROWS, emotes),
        "mentioned": mentioned,
        "rows": top,
        "more_href": url(top=chat_stats.TOP_ROWS_MORE) if longer and top == chat_stats.TOP_ROWS else None,
        "fewer_href": url(top=chat_stats.TOP_ROWS) if top != chat_stats.TOP_ROWS else None,
        "counted_words": (
            None
            if whole
            else f"Only the newest {chat_stats.whole_words(kept.messages)} "
            f"of this period's {chat_stats.counted_words(total, 'message')} were counted."
        ),
    }


def _analyze_chatter(
    conn,
    owner: int,
    period: chat_stats.Period,
    words: _ChatWords,
    today: date,
    url,
    name: str,
    counts: list[tuple[str, int]],
    emotes,
) -> dict:
    """What the page says of one chatter in a period they wrote in."""
    in_period = {"limit": ANALYZE_MESSAGE_LIMIT}
    kept = chat_stats.collect(
        iter_channel_chat(conn, owner, period.start, period.end, sender=name, **in_period),
        chat_stats.columns_for(period),
        emotes=emotes,
        laugh_names=words.laugh_names,
        laugh_patterns=words.laugh_patterns,
    )
    addressing = iter_channel_chat(conn, owner, period.start, period.end, text=f"@{name}", **in_period)
    first_seen = get_first_message_time(conn, owner, name)

    def column(columns: chat_stats.Columns, number: int) -> str:
        return url(chat_stats.Period(columns.start(number), columns.start(number + 1)), chatter=name)

    talks_to = chat_stats.mentioned_most(kept, chat_stats.CHATTER_ROWS, lambda other: url(chatter=other))
    card = [{"name": name}]
    _named(conn, owner, card, talks_to)
    return {
        "who": card[0]["who"],
        "figures": chat_stats.chatter_figures(
            kept,
            today,
            everyone=sum(count for _name, count in counts),
            rank=chat_stats.rank_of(counts, name),
            chatters=len(counts),
            first_seen=datetime.fromisoformat(first_seen) if first_seen else None,
            mentioned=chat_stats.mentions_of(addressing, name),
        ),
        "activity": chat_stats.activity_chart(kept, today, one_day=chat_stats.one_day(period), link=column),
        "emotes": chat_stats.chatter_emotes(kept),
        "said": chat_stats.said_most(kept, chat_stats.CHATTER_ROWS, emotes),
        "talks_to": talks_to,
    }


def _analyze_messages(conn, owner: int, period: chat_stats.Period, words: _ChatWords, today: date, url, asked: _Asked):
    """A period's messages, a page of them: all, or what a search for a
    chatter, for a text or for both finds - with what there is to say of
    the chatter. A name nobody in the period has gets the names that have
    it in them instead."""
    emotes = get_chat_emotes(conn, owner)
    counts = get_chatter_counts(conn, owner, period.start, period.end)
    text = asked.text.strip()
    name, alike = chat_stats.find_chatter(counts, asked.chatter) if asked.chatter.strip() else (None, [])
    if asked.chatter.strip() and name is None:
        offered = [
            {
                "name": other,
                "href": url(chatter=other, text=text),
                "messages": chat_stats.counted_words(count, "message"),
            }
            for other, count in alike
        ]
        _named(conn, owner, offered)
        return {"nobody": asked.chatter.strip(), "alike": offered}

    found = {"sender": name, "text": text or None}
    total = count_channel_chat(conn, owner, period.start, period.end, **found)
    size = chat_stats.MESSAGES_PAGE_SIZE
    # As in the review queue, a page past the end is the last one.
    offset = min(max(asked.offset, 0), max(total - 1, 0) // size * size)
    oldest_first = asked.order == chat_stats.ORDER_OLDEST
    rows = search_channel_chat(
        conn, owner, period.start, period.end, newest_first=not oldest_first, limit=size, offset=offset, **found
    )

    def page(**changes) -> str:
        shown = {"view": chat_stats.VIEW_MESSAGES, "chatter": name, "text": text, "order": asked.order}
        return url(**{**shown, **changes})

    return {
        "nobody": None,
        "chatter": _analyze_chatter(conn, owner, period, words, today, url, name, counts, emotes) if name else None,
        "found_words": chat_stats.found_words(total, name, text),
        "lines": _analyze_lines(conn, owner, rows, emotes, today, url),
        "orders": [
            {"label": "Newest first", "href": page(order=chat_stats.ORDER_NEWEST), "current": not oldest_first},
            {"label": "Oldest first", "href": page(order=chat_stats.ORDER_OLDEST), "current": oldest_first},
        ],
        "paging": (
            {
                "words": chat_stats.paging_words(offset, len(rows), total),
                "back_href": page(offset=max(offset - size, 0)) if offset > 0 else None,
                "on_href": page(offset=offset + size) if offset + len(rows) < total else None,
            }
            if total > len(rows)
            else None
        ),
    }


def _analyze_around(conn, owner: int, today: date, url, message_id: str) -> dict:
    """The chat around one message, in the order it was written - what its
    writer was answering, and what was answered. A message the channel does
    not have leaves nothing to show."""
    reach = chat_stats.CONTEXT_MESSAGES
    rows = get_chat_around(conn, owner, message_id, before=reach, after=reach)
    if rows is None:
        return {"lines": [], "when": None, "earlier_href": None, "later_href": None}
    place = next(index for index, row in enumerate(rows) if row["message_id"] == message_id)
    return {
        "lines": _analyze_lines(conn, owner, rows, get_chat_emotes(conn, owner), today, url, asked=message_id),
        "when": chat_stats.second_words(datetime.fromisoformat(rows[place]["received_at"]), today),
        # Offered where there may be more that way: as many lines as were
        # asked for came back.
        "earlier_href": url(around=rows[0]["message_id"]) if place == reach else None,
        "later_href": url(around=rows[-1]["message_id"]) if len(rows) - place - 1 == reach else None,
    }


def _analysis(channel: dict, words: _ChatWords, asked: _Asked, now: datetime, today: date) -> dict:
    """Everything the Analyze page shows of a channel's chat: the periods
    on offer, the one that is open, and of that either its overview or its
    messages. Runs in a thread of its own - a pass over a long period takes
    seconds - so with a connection of its own."""
    owner, name = channel["id"], channel["name"]
    stored = chat_stats.whole_minutes(
        datetime.fromisoformat(channel["first"]), datetime.fromisoformat(channel["last"])
    )
    conn = get_connection()
    try:
        sessions = get_chat_sessions(
            conn,
            owner,
            gap_seconds=chat_stats.SESSION_GAP_SECONDS,
            min_messages=chat_stats.SESSION_MIN_MESSAGES,
            min_seconds=chat_stats.SESSION_MIN_SECONDS,
            limit=chat_stats.SESSIONS_LISTED,
            newest=chat_stats.SESSION_MESSAGE_LIMIT,
        )
        period, preset = chat_stats.chosen_period(
            asked.preset,
            asked.start,
            asked.end,
            now=now,
            stored=stored,
            newest_session=chat_stats.session_period(sessions[0]) if sessions else None,
        )

        def url(other: chat_stats.Period | None = None, **shown) -> str:
            """The page for this channel and what it is to show: of the
            period that is open, or of another."""
            if other is not None:
                return chat_stats.analyze_url(name, period=other, **shown)
            shown.setdefault("top", asked.top)
            return chat_stats.analyze_url(name, preset=preset, period=period, **shown)

        # Messages are what is shown as soon as anything about them is asked.
        searching = asked.chatter.strip() or asked.text.strip()
        reading = bool(asked.around or searching or asked.view == chat_stats.VIEW_MESSAGES)
        page = {
            "channel": name,
            "period_words": chat_stats.period_words(period, today),
            "start": chat_stats.local_text(period.start),
            "end": chat_stats.local_text(period.end),
            # What a form on the page sends along to stay in the period.
            "kept": (
                [("period", preset)]
                if preset
                else [("from", chat_stats.local_text(period.start)), ("to", chat_stats.local_text(period.end))]
            ),
            "presets": [
                {
                    "label": label,
                    "href": chat_stats.analyze_url(name, preset=key),
                    "current": key == preset,
                }
                for key, label in chat_stats.preset_labels()
            ],
            "sessions": chat_stats.session_rows(sessions, today, period, url),
            "stored_words": chat_stats.stored_words(stored, today),
            "views": [
                {"label": "Overview", "href": url(), "current": not reading},
                {"label": "Messages", "href": url(view=chat_stats.VIEW_MESSAGES), "current": reading},
            ],
            "chatter": asked.chatter.strip(),
            "text": asked.text.strip(),
            "all_href": chat_stats.analyze_url(name, preset=chat_stats.PRESET_ALL),
            "overview": None,
            "messages": None,
            "around": None,
        }
        if asked.around:
            page["around"] = _analyze_around(conn, owner, today, url, asked.around)
        elif reading:
            page["messages"] = _analyze_messages(conn, owner, period, words, today, url, asked)
        else:
            page["overview"] = _analyze_overview(conn, owner, period, words, today, url, asked.top)
        return page
    finally:
        conn.close()


@app.get("/dashboard/analyze", response_class=HTMLResponse)
async def dashboard_analyze(
    request: Request,
    channel: str | None = None,
    period: str | None = None,
    start: str | None = Query(None, alias="from"),
    end: str | None = Query(None, alias="to"),
    view: str = chat_stats.VIEW_OVERVIEW,
    chatter: str = "",
    q: str = "",
    order: str = chat_stats.ORDER_NEWEST,
    offset: int = 0,
    top: int = chat_stats.TOP_ROWS,
    around: str = "",
):
    """The Analyze page: a channel's stored chat in figures.

    `channel` is whose - left out, the one whose chat is the newest. The
    period is a preset by its name (`period`: day, week, month, all) or any
    other by its two ends (`from` and `to`, local time); left out, it is
    the channel's newest session. Of that period the page shows the
    overview, or - with `view=messages`, a `chatter` or a text to look for
    (`q`) - its messages, a page at a time from `offset` on, in `order`.
    `around` is a message to show the chat around instead, and `top` how
    long the overview's tables are.
    """
    conn = get_connection()
    try:
        context = _page_context(conn, "analyze")
        channels = _chat_channels(conn, context["streamers"])
        chosen = next((item for item in channels if item["name"].lower() == (channel or "").lower()), None)
        if channel and chosen is None:
            raise HTTPException(status_code=404, detail=f"no chat is stored for a channel called {channel!r}")
        if chosen is None and channels:
            chosen = max(channels, key=lambda item: item["last"])
        words = None
        if chosen is not None:
            keywords = _channel_keywords(conn, chosen["id"])
            patterns = tuple(_tuning(conn, chosen["id"]).detector.added_laugh_emote_patterns)
            words = _ChatWords(tuple(keywords), tuple(detector.laugh_emote_names(keywords, patterns)), patterns)
    finally:
        conn.close()

    context["channels"] = [item["name"] for item in channels]
    context["analysis"] = None
    if chosen is not None:
        asked = _Asked(
            preset=period,
            start=chat_stats.parse_local(start),
            end=chat_stats.parse_local(end),
            view=view,
            chatter=chatter,
            text=q,
            order=chat_stats.ORDER_OLDEST if order == chat_stats.ORDER_OLDEST else chat_stats.ORDER_NEWEST,
            offset=offset,
            top=top if top in (chat_stats.TOP_ROWS, chat_stats.TOP_ROWS_MORE) else chat_stats.TOP_ROWS,
            around=around,
        )
        context["analysis"] = await asyncio.to_thread(_analysis, chosen, words, asked, _now(), _today())
    return templates.TemplateResponse(request, "analyze.html", context)


def _moment_status(conn) -> dict[str, int]:
    return {"moments": count_moments(conn), "clips": count_moments_with_clip(conn)}


@app.get("/moments/status")
async def moments_status():
    """Totals the dashboard polls to tell whether anything new has arrived
    since it was loaded, without reloading the page."""
    conn = get_connection()
    try:
        return _moment_status(conn)
    finally:
        conn.close()


@app.post("/moments/{moment_id}/rating")
async def set_moment_rating(moment_id: int, value: int = 0):
    if not 0 <= value <= 5:
        raise HTTPException(status_code=400, detail="value must be 1-5, or 0 to clear")

    conn = get_connection()
    try:
        update_moment_rating(conn, moment_id, value or None)
    finally:
        conn.close()
    return {"moment_id": moment_id, "rating": value or None}


@app.post("/moments/{moment_id}/trim")
async def trim_moment(moment_id: int, request: Request):
    """Starts cutting a stretch of a moment's footage out as a video of its
    own (see clip_trim.py), and with `chat` its chat as a video to lay over
    it (chat_video.py). `start` and `end` are clip time; `chat_shift` moves
    the chat video's lines that many seconds later than the channel's chat
    delay has them, and `chat_background` ("#rrggbb") puts a colour behind
    them instead of nothing. What was cut for the moment before is replaced."""
    if _shutdown_requested:
        raise HTTPException(status_code=409, detail="the service is shutting down")
    try:
        asked = await request.json()
    except ValueError:
        asked = None
    if not isinstance(asked, dict):
        raise HTTPException(status_code=400, detail="expected start, end, chat and chat_shift as JSON")

    conn = get_connection()
    try:
        row = get_moment(conn, moment_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown moment {moment_id}")
        row = await _with_clip_length(conn, row)
        if not row["clip_path"] or not row["clip_duration"] or not (CLIPS_DIR / row["clip_path"]).exists():
            raise HTTPException(status_code=409, detail="the moment has no clip to trim")
        try:
            span = clip_trim.file_span(row, asked.get("start"), asked.get("end"))
            shift = clip_trim.chat_shift(asked.get("chat_shift"))
            background = clip_trim.chat_background(asked.get("chat_background"))
        except clip_trim.TrimError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        lines = None
        if asked.get("chat"):
            _strip, lines = _chat_against_clip(conn, row, _chat_delay(conn, row["channel_slug"]))
            if not lines:
                raise HTTPException(status_code=409, detail="no chat was stored for this moment")
    finally:
        conn.close()

    if _trims.get(moment_id, {}).get("state") not in dashboard_view.TRIM_BUSY:
        _trims[moment_id] = {"state": dashboard_view.TRIM_CUTTING}
        video, chat = (CLIPS_DIR / path for path in _trim_paths(row["clip_path"]))
        _track_task(
            _trim_background(
                moment_id,
                row["channel_slug"],
                CLIPS_DIR / row["clip_path"],
                span,
                row["context_before"] or 0.0,
                lines,
                shift,
                background,
                video,
                chat,
            )
        )
    return {"moment_id": moment_id, **_trim(row)}


@app.get("/moments/{moment_id}/trim")
async def trim_state(moment_id: int):
    """Where the stretch cut out of a moment's footage stands - what the
    Trim dialog asks when it is opened and while one is being made."""
    conn = get_connection()
    try:
        row = get_moment(conn, moment_id)
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown moment {moment_id}")
    if not row["clip_path"]:
        state = dashboard_view.TRIM_NONE
        return {
            "moment_id": moment_id,
            "state": state,
            "words": dashboard_view.trim_words(state),
            "video_url": None,
            "chat_url": None,
        }
    return {"moment_id": moment_id, **_trim(row)}


@app.post("/moments/{moment_id}/stream_type")
async def set_moment_stream_type(moment_id: int, value: str = ""):
    valid_values = {v for v, _ in STREAM_TYPES}
    if value and value not in valid_values:
        raise HTTPException(status_code=400, detail=f"value must be one of {sorted(valid_values)}, or empty to clear")

    conn = get_connection()
    try:
        update_moment_stream_type(conn, moment_id, value or None)
    finally:
        conn.close()
    return {"moment_id": moment_id, "stream_type": value or None}


@app.post("/moments/{moment_id}/moment_type")
async def set_moment_moment_type(moment_id: int, value: str = ""):
    valid_values = {v for v, _ in MOMENT_TYPES}
    if value and value not in valid_values:
        raise HTTPException(status_code=400, detail=f"value must be one of {sorted(valid_values)}, or empty to clear")

    conn = get_connection()
    try:
        update_moment_type(conn, moment_id, value or None)
    finally:
        conn.close()
    return {"moment_id": moment_id, "moment_type": value or None}


@app.post("/moments/{moment_id}/title")
async def set_moment_title(moment_id: int, request: Request):
    """Names a moment; an empty name takes the name away again."""
    data = await request.json()
    title = " ".join((data.get("title") or "").split())[:MOMENT_TITLE_MAX_LENGTH]

    conn = get_connection()
    try:
        update_moment_title(conn, moment_id, title or None)
    finally:
        conn.close()
    return {"moment_id": moment_id, "title": title or None}


@app.post("/moments/{moment_id}/notes")
async def set_moment_notes(moment_id: int, request: Request):
    data = await request.json()
    notes = (data.get("notes") or "").strip()

    conn = get_connection()
    try:
        update_moment_notes(conn, moment_id, notes or None)
    finally:
        conn.close()
    return {"moment_id": moment_id, "notes": notes or None}


@app.post("/settings/{name}")
async def set_setting(name: str, enabled: int = 1):
    if name not in SETTING_KEYS:
        raise HTTPException(status_code=404, detail=f"unknown setting {name!r}")
    key = SETTING_KEYS[name]
    value = bool(enabled)

    conn = get_connection()
    try:
        set_flag(conn, key, value)
    finally:
        conn.close()
    _flags[key] = value
    logger.info("setting %s -> %s", key, "on" if value else "off")
    return {"setting": name, "enabled": value}


@app.post("/shutdown")
async def shutdown():
    global _shutdown_requested, _shutdown_task
    if _shutdown_requested:
        pending = len(_moment_sessions) + len(_background_tasks)
        return {"status": "already shutting down", "pending_work_count": pending}
    _shutdown_requested = True
    # Chat stops being taken in right now, so a stream that is being kept
    # whole ends here too. Making its file is one of the things waited for.
    for slug in recording_manager.stream_recordings():
        _end_stream_recording(slug, stream_recording.SHUTDOWN)
    pending = len(_moment_sessions) + len(_background_tasks)
    logger.info(
        "shutdown requested from dashboard - watching stopped, waiting on %d pending item(s) before exiting",
        pending,
    )
    _shutdown_task = asyncio.create_task(_shutdown_when_idle())
    return {"status": "shutting down", "pending_work_count": pending}


@app.post("/channels")
async def add_channel(slug: str):
    slug = slug.strip()
    if not slug:
        raise HTTPException(status_code=400, detail="slug must not be empty")
    try:
        result = await add_channel_to_watchlist(slug)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 400:
            # Kick's API returns 400, not an empty result, for an unknown slug.
            raise HTTPException(status_code=404, detail=f"no such Kick channel: {slug!r}") from e
        logger.exception("failed to add %s to the watchlist", slug)
        raise HTTPException(status_code=502, detail=f"could not add {slug!r} - see server log") from e
    except Exception as e:
        logger.exception("failed to add %s to the watchlist", slug)
        raise HTTPException(status_code=502, detail=f"could not add {slug!r} - see server log") from e
    logger.info(
        "added %s to the watchlist (broadcaster_user_id=%s, %d emote keyword(s))",
        slug, result["broadcaster_user_id"], result["emote_count"],
    )
    return result


@app.post("/channels/{slug}/tracking")
async def set_channel_tracking(slug: str, enabled: int = 1):
    value = bool(enabled)
    conn = get_connection()
    try:
        row = get_streamer_by_slug(conn, slug)
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown channel {slug!r}")
        set_streamer_tracking(conn, row["broadcaster_user_id"], value)
    finally:
        conn.close()
    logger.info("tracking for %s -> %s", slug, "on" if value else "off")
    return {"slug": slug, "tracking_enabled": value}


@app.post("/channels/{slug}/chat_delay")
async def set_channel_chat_delay(slug: str, seconds: int):
    """Sets how far behind its broadcast a channel's chat is taken to run
    when it is shown against a clip (see chat_trace.CHAT_DELAY_SECONDS). Any
    channel there are moments from, on the watchlist or not."""
    least, most = chat_trace.CHAT_DELAY_MIN_SECONDS, chat_trace.CHAT_DELAY_MAX_SECONDS
    if not least <= seconds <= most:
        raise HTTPException(status_code=400, detail=f"seconds must be from {least} to {most}")

    conn = get_connection()
    try:
        if slug not in get_moment_channels(conn):
            raise HTTPException(status_code=404, detail=f"no moments from channel {slug!r}")
        set_chat_delay(conn, slug, seconds)
    finally:
        conn.close()
    logger.info("chat delay for %s -> %d s", slug, seconds)
    return {"slug": slug, "chat_delay_seconds": seconds}


@app.post("/channels/{slug}/recording/start")
async def start_channel_recording(slug: str):
    """Starts keeping a channel's stream whole (see stream_recording.py).
    Whether the channel is tracked for clips makes no difference, and its
    chat is kept with it either way. A channel that is not live yet is
    recorded from when it goes live."""
    if _shutdown_requested:
        raise HTTPException(status_code=409, detail="The service is shutting down.")
    conn = get_connection()
    try:
        row = get_streamer_by_slug(conn, slug)
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown channel {slug!r}")
    try:
        recording_id = _start_stream_recording(slug, row["broadcaster_user_id"])
    except _NotStarted as refused:
        raise HTTPException(status_code=refused.status_code, detail=refused.words) from None
    logger.info("[%s] stream recording %d started", slug, recording_id)
    return {"slug": slug, "recording": _by_channel(_stream_recordings_not_over()).get(slug)}


@app.post("/channels/{slug}/recording/stop")
async def stop_channel_recording(slug: str):
    """Ends the stream recording of a channel. What it kept is made into
    its file in the background; one that kept nothing is forgotten."""
    conn = get_connection()
    try:
        row = get_streamer_by_slug(conn, slug)
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown channel {slug!r}")
    if recording_manager.stream_recording_of(slug) is None:
        raise HTTPException(status_code=409, detail=f"{slug} is not being recorded.")
    kept = _end_stream_recording(slug, stream_recording.STOPPED)
    return {
        "slug": slug,
        "recording": _by_channel(_stream_recordings_not_over()).get(slug),
        "said": dashboard_view.recording_stopped_words(kept, bool(row["always_record"])),
    }


@app.post("/channels/{slug}/always_record")
async def set_channel_always_record(slug: str, enabled: int = 1):
    """Sets whether every stream of a channel is kept whole: with it on, a
    recording is started whenever the channel is live and none is running -
    as soon as the recording loop has looked, if it is live now. A recording
    that is running is left as it is either way, and the buttons that start
    and stop one work as they do without it. Stopping one ends it for that
    stream, not for the next."""
    value = bool(enabled)
    conn = get_connection()
    try:
        row = get_streamer_by_slug(conn, slug)
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown channel {slug!r}")
        set_streamer_always_record(conn, row["broadcaster_user_id"], value)
    finally:
        conn.close()
    # Set afresh, it holds for the stream that is on as well - whatever was
    # stopped in it before.
    recording_manager.stop_holding_off(slug)
    recording_manager.wake()
    logger.info("always recording %s -> %s", slug, "on" if value else "off")
    return {
        "slug": slug,
        "always_record": value,
        "said": dashboard_view.ALWAYS_RECORD_WORDS[value].format(channel=slug),
    }


@app.get("/recordings/status")
async def recordings_status():
    """What the stream recordings that are not over yet are doing - by
    channel for the Channels page and by recording for the Recordings one,
    which both ask every couple of seconds while there is one - with the
    mark of the list as it stands (see _recordings_mark) and what the
    channels are doing right now, for the bar and the watchlist (see
    _right_now)."""
    conn = get_connection()
    try:
        total = count_stream_recordings(conn)
        now = _right_now(get_streamers(conn))
    finally:
        conn.close()
    not_over = _stream_recordings_not_over()
    return {
        "channels": _by_channel(not_over),
        "recordings": {str(recording_id): state for recording_id, state in not_over.items()},
        "mark": _recordings_mark(not_over, total),
        "now": now,
    }


def _stream_recording_at_rest(conn, recording_id: int):
    """A stream recording nothing is being done with right now - which is
    when it can be removed or saved again. Otherwise a 404 or a 409."""
    row = get_stream_recording(conn, recording_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown recording {recording_id}")
    if recording_id in _stream_recordings_not_over():
        raise HTTPException(status_code=409, detail="This recording is not over yet.")
    return row


@app.post("/recordings/{recording_id}/delete")
async def delete_recording(recording_id: int):
    """Removes a stream recording for good: its file, whatever is left of
    its parts, and its row. Not one that is still running or being saved."""
    conn = get_connection()
    try:
        row = _stream_recording_at_rest(conn, recording_id)
        file = _stream_file(row)
        try:
            if file is not None:
                file.unlink(missing_ok=True)
            folder = stream_recording.parts_folder(row["channel_slug"], recording_id)
            if folder.exists():
                await asyncio.to_thread(shutil.rmtree, folder)
        except OSError:
            logger.warning("stream recording %d could not be removed", recording_id, exc_info=True)
            raise HTTPException(
                status_code=409, detail="The recording could not be removed. Something may still have its file open."
            ) from None
        delete_stream_recording(conn, recording_id)
    finally:
        conn.close()
    logger.info("[%s] stream recording %d deleted", row["channel_slug"], recording_id)
    return {"deleted": recording_id}


@app.post("/recordings/{recording_id}/save")
async def save_recording_again(recording_id: int):
    """Tries once more to make a stream recording that could not be saved
    into its file, from the parts still in its folder."""
    if _shutdown_requested:
        raise HTTPException(status_code=409, detail="The service is shutting down.")
    conn = get_connection()
    try:
        row = _stream_recording_at_rest(conn, recording_id)
        if row["state"] != stream_recording.FAILED:
            raise HTTPException(status_code=409, detail="Only a recording that could not be saved can be saved again.")
        set_stream_recording_state(conn, recording_id, stream_recording.SAVING)
    finally:
        conn.close()
    folder = stream_recording.parts_folder(row["channel_slug"], recording_id)
    _save_stream_recording_soon(recording_id, row["channel_slug"], folder, None, stream_recording.kept_bytes(folder))
    return {"recording": recording_id, "state": stream_recording.SAVING}


async def _posted_values(request: Request) -> dict[str, tuning.Value]:
    """The settings' values a request brings (`values`, by name), each as
    the value its setting takes. One that can't be taken is a 400 that says
    why in words for the page."""
    try:
        asked = await request.json()
    except ValueError:
        asked = None
    posted = asked.get("values") if isinstance(asked, dict) else None
    if not isinstance(posted, dict):
        raise HTTPException(status_code=400, detail="expected the settings' values as JSON")
    try:
        return tuning.cleaned(posted)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


def _checked(every: dict[str, tuning.Value], whose: str | None = None) -> None:
    """Refuses, with a 400, values that contradict each other - a channel's
    (`whose`) when it is not the ones being saved that do."""
    try:
        tuning.check(every)
    except ValueError as e:
        about = f" That is what {whose}'s own settings would then come to." if whose else ""
        raise HTTPException(status_code=400, detail=f"{e}{about}") from None


def _saved_words(kept: dict[str, tuning.Value]) -> str:
    """What was stored, for the log."""
    return ", ".join(f"{name}={value!r}" for name, value in sorted(kept.items())) or "nothing set"


@app.post("/tuning")
async def set_tuning(request: Request):
    """Saves settings for all channels (see tuning.py): `values`, by name.
    One given what is recommended goes back to following the code, and one
    that is not mentioned stays as it is. Chat is judged by them from the
    next message on; a moment that is open keeps the ones it fired with."""
    given = await _posted_values(request)
    conn = get_connection()
    try:
        for_all = {**tuning.stored(get_tuning(conn)), **given}
        _checked(tuning.values(for_all))
        # A channel with a value of its own for one half of a pair must not
        # be left with the pair the wrong way round.
        for row in get_streamers(conn):
            if row["own_tuning"]:
                mine = tuning.stored(get_tuning(conn, row["broadcaster_user_id"]))
                _checked(tuning.values(for_all, mine), whose=row["slug"])
        kept = tuning.differing(for_all, tuning.recommended_values())
        replace_tuning(conn, TUNING_FOR_ALL, tuning.encoded(kept))
    finally:
        conn.close()
    _forget_tuning()
    logger.info("settings for all channels -> %s", _saved_words(kept))
    return {"channel": None, "set": sorted(kept)}


@app.post("/channels/{slug}/tuning")
async def set_channel_tuning(slug: str, request: Request):
    """Saves a channel's own settings: `values`, by name. One given what
    all channels have goes back to following them. They are in force while
    the channel goes by its own settings (see set_channel_own_tuning), and
    kept either way."""
    given = await _posted_values(request)
    conn = get_connection()
    try:
        row = get_streamer_by_slug(conn, slug)
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown channel {slug!r}")
        for_all = tuning.stored(get_tuning(conn))
        mine = {**tuning.stored(get_tuning(conn, row["broadcaster_user_id"])), **given}
        _checked(tuning.values(for_all, mine))
        kept = tuning.differing(mine, tuning.values(for_all))
        replace_tuning(conn, row["broadcaster_user_id"], tuning.encoded(kept))
    finally:
        conn.close()
    _forget_tuning()
    logger.info("settings of %s -> %s", slug, _saved_words(kept))
    return {"channel": slug, "set": sorted(kept)}


@app.post("/channels/{slug}/own_tuning")
async def set_channel_own_tuning(slug: str, enabled: int = 1):
    """Whether a channel goes by settings of its own where it has them, or
    by the ones for all channels in everything. Switching it off leaves the
    channel's values where they are, for when it is switched on again."""
    value = bool(enabled)
    conn = get_connection()
    try:
        row = get_streamer_by_slug(conn, slug)
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown channel {slug!r}")
        set_streamer_own_tuning(conn, row["broadcaster_user_id"], value)
    finally:
        conn.close()
    _forget_tuning()
    logger.info("own settings for %s -> %s", slug, "on" if value else "off")
    return {"slug": slug, "own_tuning": value}


# How much of a channel's newest stored chat settings are tried on. A replay
# takes about half a second per thousand messages, and a trial is two.
TRIAL_MESSAGE_LIMIT = 10_000
# One at a time: a trial keeps a processor busy for several seconds, next to
# a service that has chat to keep up with.
_trial_running = False


def _replay_both(slug: str, saved: tuning.Tuning, proposed: tuning.Tuning) -> dict | None:
    """A channel's newest stored chat replayed under two sets of settings,
    as the page shows it - or None if there is no chat to replay. Runs in a
    thread of its own, so with a connection of its own."""
    conn = get_connection()
    try:
        messages, keywords = replay.load_messages(conn, slug, limit=TRIAL_MESSAGE_LIMIT)
    finally:
        conn.close()
    if not messages:
        return None
    hours = replay.chat_hours(messages)
    rows = [
        (label, replay.summarise(replay.replay(messages, keywords, tuned), hours, tuned))
        for label, tuned in (("As saved", saved), ("With these values", proposed))
    ]
    first, last = (datetime.fromtimestamp(message.time).astimezone() for message in (messages[0], messages[-1]))
    return dashboard_view.trial_words(len(messages), hours, first, last, _today(), rows)


async def _trial(slug: str, saved: tuning.Tuning, proposed: tuning.Tuning) -> dict:
    global _trial_running
    if _trial_running:
        raise HTTPException(status_code=409, detail="Another trial is still running. Try again in a moment.")
    _trial_running = True
    try:
        result = await asyncio.to_thread(_replay_both, slug, saved, proposed)
    finally:
        _trial_running = False
    if result is None:
        raise HTTPException(status_code=409, detail=f"No chat is stored for {slug} yet.")
    return result


@app.post("/tuning/trial")
async def try_tuning(request: Request, chat: str):
    """What `values` would do if they were saved for all channels, shown on
    the newest stored chat of the channel `chat`: how many moments it would
    have made with the settings as they are saved, and with these. Nothing
    is saved."""
    given = await _posted_values(request)
    conn = get_connection()
    try:
        row = get_streamer_by_slug(conn, chat)
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown channel {chat!r}")
        owner = row["broadcaster_user_id"]
        for_all = {**tuning.stored(get_tuning(conn)), **given}
        mine = tuning.stored(get_tuning(conn, owner)) if row["own_tuning"] else {}
        _checked(tuning.values(for_all))
        _checked(tuning.values(for_all, mine), whose=chat)
        saved = _tuning(conn, owner)
    finally:
        conn.close()
    return await _trial(chat, saved, tuning.tuned(for_all, mine))


@app.post("/channels/{slug}/tuning/trial")
async def try_channel_tuning(slug: str, request: Request):
    """The same for a channel's own settings, on its own stored chat."""
    given = await _posted_values(request)
    conn = get_connection()
    try:
        row = get_streamer_by_slug(conn, slug)
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown channel {slug!r}")
        owner = row["broadcaster_user_id"]
        for_all = tuning.stored(get_tuning(conn))
        mine = {**tuning.stored(get_tuning(conn, owner)), **given}
        _checked(tuning.values(for_all, mine))
        saved = _tuning(conn, owner)
    finally:
        conn.close()
    return await _trial(slug, saved, tuning.tuned(for_all, mine))


@app.post("/webhooks/kick")
async def kick_webhook(
    request: Request,
    kick_event_message_id: str = Header(...),
    kick_event_message_timestamp: str = Header(...),
    kick_event_signature: str = Header(...),
    kick_event_type: str = Header(...),
):
    body = await request.body()

    public_key = await get_kick_public_key()
    if not verify_signature(
        public_key, kick_event_message_id, kick_event_message_timestamp, body, kick_event_signature
    ):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    payload = await request.json()

    if kick_event_type == "chat.message.sent":
        watching = _flags.get("watching_enabled", True)
        # The switches are about looking for clips. A channel whose stream
        # is being kept whole has its chat kept with it whatever they say.
        kept_whole = recording_manager.keeps_chat_of((payload.get("broadcaster") or {}).get("user_id"))
        if _shutdown_requested or not (watching or kept_whole):
            # Watching paused (or a shutdown is in progress) - drop the
            # message without storing or running detection, so no new
            # moments pile up.
            return {"status": "watching disabled"}

        broadcaster = payload.get("broadcaster", {})
        sender = payload.get("sender", {})
        channel = broadcaster.get("channel_slug", "?")
        content = payload.get("content", "")
        broadcaster_user_id = broadcaster["user_id"]
        sender_username = sender.get("username", "")
        emotes = payload.get("emotes", [])
        emote_count = sum(len(e.get("positions", [])) for e in emotes)

        conn = get_connection()
        try:
            tracked = watching and get_streamer_tracking_enabled(conn, broadcaster_user_id)
            if not tracked and not kept_whole:
                # Tracking paused for this one channel - same drop as the
                # global pause, scoped to it.
                return {"status": "channel tracking disabled"}

            logger.info("[%s] %s: %s", channel, sender.get("username", "?"), content)
            is_new = insert_chat_message(
                conn,
                message_id=payload["message_id"],
                broadcaster_user_id=broadcaster_user_id,
                channel_slug=channel,
                sender_username=sender_username,
                content=content,
                emotes_json=json.dumps(emotes),
                created_at=payload.get("created_at", ""),
                received_at=datetime.now(timezone.utc).isoformat(),
            )
            if not is_new:
                # Kick delivers a webhook again when it doesn't see it
                # acknowledged in time - i.e. exactly when this service is
                # struggling. The message is already stored and counted;
                # feeding it to the detector a second time would inflate the
                # very burst it is trying to measure.
                return {"status": "duplicate"}

            _remember_chat_identity(conn, broadcaster_user_id, sender_username, sender)
            if not tracked:
                # Kept to go with the recording of the stream. Nobody is
                # looking for moments in it.
                return {"status": "stored"}

            # Everything from here on is judged by the channel's settings as
            # they stand for this message.
            tuned = _tuning(conn, broadcaster_user_id)
            emote_weight = detector.classify_native_emotes(content, tuned.detector)
            laugh_weight, mention_weight = detector.classify_message(
                content, _channel_keywords(conn, broadcaster_user_id), tuned.detector
            )
            spike = detector.record_message(
                channel,
                sender=sender_username,
                content=content,
                emote_count=emote_count,
                emote_weight=emote_weight,
                laugh_weight=laugh_weight,
                mention_weight=mention_weight,
                settings=tuned.detector,
            )
            if spike is not None and channel in _moment_sessions:
                # A moment is already open for this channel (a re-fire past the
                # cooldown) - it's the same reaction continuing, so just keep
                # it alive rather than opening a duplicate.
                _moment_sessions[channel].last_active = datetime.now(timezone.utc)
            elif spike is not None:
                window_end = datetime.now(timezone.utc)
                window_start = window_end - timedelta(seconds=detector.SHORT_WINDOW_SECONDS)
                reason = ",".join(spike.reasons)
                try:
                    stream_elapsed = await _stream_elapsed_seconds(channel)
                except Exception:
                    # Stream time is a label on the dashboard. The detector
                    # has already started its cooldown, so a moment dropped
                    # here because Kick's API hiccuped would simply be gone.
                    logger.warning("[%s] could not look up the stream's start time", channel, exc_info=True)
                    stream_elapsed = None
                moment_id = insert_moment(
                    conn,
                    broadcaster_user_id=broadcaster_user_id,
                    channel_slug=channel,
                    window_start=window_start.isoformat(),
                    window_end=window_end.isoformat(),
                    reason=reason,
                    score=spike.score,
                    message_count=spike.message_count,
                    baseline_message_rate=spike.baseline_message_rate,
                    current_message_rate=spike.current_message_rate,
                    emote_count=spike.emote_count,
                    keyword_hits=spike.keyword_hits,
                    stream_elapsed_seconds=stream_elapsed,
                )
                # Open a moment session: hold it open and extend the clip while
                # the reaction lasts, then cut one clip covering all of it.
                session = _MomentSession(
                    moment_id=moment_id,
                    channel=channel,
                    window_start=window_start,
                    trigger_time=window_end,
                    window_end=window_end,
                    last_active=window_end,
                    triggered_at=time.monotonic(),
                    tuned=tuned,
                )
                _moment_sessions[channel] = session
                # Tracked (not just held via _moment_sessions) so there's no
                # gap between the session closing and its clip-cut task
                # existing - both would otherwise look "idle" to
                # _shutdown_when_idle for the moment in between.
                _track_task(_run_moment_session(session))
                stream_time = str(timedelta(seconds=stream_elapsed)) if stream_elapsed is not None else "unknown"
                logger.info(
                    "MOMENT detected in [%s] (%s) at stream time %s: "
                    "%d msgs, %d emotes, %d keyword hits in %ds, score=%.2f",
                    channel,
                    reason,
                    stream_time,
                    spike.message_count,
                    spike.emote_count,
                    spike.keyword_hits,
                    detector.SHORT_WINDOW_SECONDS,
                    spike.score,
                )
        finally:
            conn.close()
    else:
        logger.info("Received %s event: %s", kick_event_type, payload)

    return {"status": "received"}
