import asyncio
import json
import logging
import logging.handlers
import mimetypes
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import (
    audio_events,
    chat_identity,
    chat_trace,
    dashboard_view,
    detector,
    frame_encoder,
    recorder,
    recording_manager,
    sound_events,
    transcriber,
    win_console,
)
from .config import load_settings
from .db import (
    MOMENT_TYPES,
    STREAM_TYPES,
    count_moments,
    count_moments_with_clip,
    get_channel_keywords,
    get_chat_between,
    get_chat_delay,
    get_chat_emotes,
    get_chat_identities,
    get_connection,
    get_flag,
    get_moment,
    get_moment_channels,
    get_recent_moments,
    get_streamer_by_slug,
    get_streamer_tracking_enabled,
    get_streamers,
    insert_chat_message,
    insert_moment,
    set_chat_delay,
    set_chat_identity,
    set_flag,
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
    await _ensure_chat_subscriptions()
    tasks = [
        # In the background: the dashboard works without them, 7TV being
        # slow or down must not hold up the start.
        asyncio.create_task(_refresh_emote_pictures()),
        asyncio.create_task(
            recording_manager.run_forever(
                _watchlist_slugs, lambda: _flags.get("watching_enabled", True), _is_channel_tracked
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

# Channel keyword weights rarely change (only when re-subscribing/refreshing
# emotes), so we cache them per-process instead of hitting the DB on every
# single chat message.
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


def _channel_keywords(conn, broadcaster_user_id: int) -> dict[str, float]:
    if broadcaster_user_id not in _channel_keywords_cache:
        _channel_keywords_cache[broadcaster_user_id] = get_channel_keywords(conn, broadcaster_user_id)
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
    post_roll_seconds: int = recorder.POST_ROLL_SECONDS,
) -> None:
    # The post-roll footage doesn't exist yet at the instant a moment closes
    # - wait for it to actually be published and downloaded before looking
    # for it, or the clip comes out truncated right at the exciting part.
    await asyncio.sleep(post_roll_seconds + recorder.CLIP_SETTLE_SECONDS)
    try:
        clip = await recording_manager.create_clip_for_moment(
            channel, window_start, window_end, f"moment_{moment_id}.mp4",
            post_roll_seconds=post_roll_seconds,
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
        # what lets the dashboard line chat up with the picture.
        update_moment_clip_path(
            conn,
            moment_id,
            clip_path.relative_to(CLIPS_DIR).as_posix(),
            clip_start=clip.started_at.isoformat(),
            clip_duration=clip.duration,
        )
    finally:
        conn.close()
    logger.info("[%s] clip saved for moment %d: %s", channel, moment_id, clip_path)
    _track_task(
        _save_context_clips_background(moment_id, channel, window_start, window_end, clip_path.name, post_roll_seconds)
    )
    if _flags.get("transcript_enabled"):
        _track_task(_transcribe_clip_background(moment_id, channel, clip_path))
    if _flags.get("audio_events_enabled"):
        _track_task(_detect_audio_events_background(moment_id, channel, clip_path))
    if _flags.get("frames_enabled"):
        _track_task(_encode_frames_background(moment_id, channel, clip_path))
    if _flags.get("sound_events_enabled"):
        _track_task(_tag_sound_events_background(moment_id, channel, clip_path))


async def _save_context_clips_background(
    moment_id: int,
    channel: str,
    window_start: datetime,
    window_end: datetime,
    clip_name: str,
    post_roll_seconds: int,
) -> None:
    # The clip itself is kept short; what led up to it and what followed are
    # saved as separate files next to it. The "after" part hasn't been
    # broadcast yet when the clip is cut, so wait for it.
    await asyncio.sleep(recorder.CONTEXT_AFTER_SECONDS + recorder.CLIP_SETTLE_SECONDS)
    try:
        saved = await recording_manager.create_context_clips_for_moment(
            channel, window_start, window_end, clip_name, post_roll_seconds=post_roll_seconds
        )
    except Exception:
        logger.exception("[%s] context clips failed for moment %d", channel, moment_id)
        return
    logger.info("[%s] context clips saved for moment %d: %s", channel, moment_id, ", ".join(sorted(saved)) or "none")


async def _transcribe_clip_background(moment_id: int, channel: str, clip_path: Path) -> None:
    # CPU-bound (faster-whisper) - runs off the event loop via to_thread, and
    # as its own task so a slow transcription never delays the clip being
    # marked ready on the dashboard.
    try:
        transcript = await asyncio.to_thread(transcriber.transcribe_clip, clip_path)
    except Exception:
        logger.exception("[%s] transcription failed for moment %d", channel, moment_id)
        return

    conn = get_connection()
    try:
        update_moment_transcript(conn, moment_id, transcript)
    finally:
        conn.close()
    logger.info("[%s] transcript saved for moment %d (%d chars)", channel, moment_id, len(transcript))


async def _detect_audio_events_background(moment_id: int, channel: str, clip_path: Path) -> None:
    # CPU-bound (SenseVoice via funasr) - same to_thread/own-task treatment
    # as transcription, and independent of it: one failing never blocks the
    # other or the clip being marked ready on the dashboard.
    try:
        tags = await asyncio.to_thread(audio_events.detect_audio_events, clip_path)
    except Exception:
        logger.exception("[%s] audio event detection failed for moment %d", channel, moment_id)
        return

    conn = get_connection()
    try:
        update_moment_audio_events(conn, moment_id, tags)
    finally:
        conn.close()
    logger.info("[%s] audio events saved for moment %d: %s", channel, moment_id, tags)


async def _encode_frames_background(moment_id: int, channel: str, clip_path: Path) -> None:
    # CPU-bound (ffmpeg frame extraction + SigLIP2) - same to_thread/own-task
    # treatment as transcription.
    try:
        embedding = await asyncio.to_thread(frame_encoder.encode_clip, clip_path)
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


async def _tag_sound_events_background(moment_id: int, channel: str, clip_path: Path) -> None:
    # CPU-bound (ffmpeg audio extraction + PANNs) - same to_thread/own-task
    # treatment as transcription, and independent of audio_events.py's
    # SenseVoice tagging: one failing never blocks the other.
    try:
        tags, embedding = await asyncio.to_thread(sound_events.tag_sound_events, clip_path)
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
# that and close at their base length (pre-roll + trigger window + post-roll,
# about 35s) once MOMENT_SESSION_QUIET_SECONDS pass; MOMENT_SESSION_MAX_SECONDS
# is the hard cap either way. Only then is the clip cut.
MOMENT_SESSION_POLL_SECONDS = 3
# 8s was too tight - a normal lull in chat (reading, catching a breath)
# regularly closed the session early, and a fresh burst soon after opened a
# brand new moment with its own 25s pre-roll reaching back into the first
# clip's tail, producing two overlapping clips instead of one continuous
# one. 20s fixed that but, together with a weak sustain bar, left most clips
# running to the cap at around two minutes. 12s is the middle ground now
# that staying "active" takes a fresh crowd-sized reaction (see
# detector.reaction_active) rather than one straggler.
#
# 30s since the dashboard got the chat trace: a longer clip costs little when
# the trace shows where in it chat erupted and the reviewer goes straight
# there. It equals detector.COOLDOWN_SECONDS on purpose, so there is no gap
# between the two - a fresh reaction within 30s of the last one extends this
# clip, and one after a longer lull opens a moment of its own.
MOMENT_SESSION_QUIET_SECONDS = 30
# Hard cap on how long a moment stays open: with the trigger window, pre-roll
# and post-roll around it, the longest possible clip is about 95s.
MOMENT_SESSION_MAX_SECONDS = 60
# The dynamic window already extends over the reaction itself, so the clip
# needs far less trailing padding than a fixed-window cut would.
DYNAMIC_POST_ROLL_SECONDS = 10


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


# One open moment per channel at a time.
_moment_sessions: dict[str, _MomentSession] = {}


def _session_should_close(session: _MomentSession, reaction_is_active: bool, now: datetime) -> bool:
    """Pure decision for one poll tick: extend the open moment if the reaction
    is still going, and report whether it's time to close it.
    """
    if reaction_is_active:
        session.last_active = now
        session.window_end = now
    if (now - session.trigger_time).total_seconds() >= MOMENT_SESSION_MAX_SECONDS:
        return True
    return (now - session.last_active).total_seconds() >= MOMENT_SESSION_QUIET_SECONDS


async def _run_moment_session(session: _MomentSession) -> None:
    try:
        while True:
            await asyncio.sleep(MOMENT_SESSION_POLL_SECONDS)
            now = datetime.now(timezone.utc)
            still_reacting = detector.reaction_active(session.channel, since=session.triggered_at)
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
            session.moment_id, session.channel, session.window_start, session.window_end,
            post_roll_seconds=DYNAMIC_POST_ROLL_SECONDS,
        )
    )


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
# one on the way. A moment stays open for up to MOMENT_SESSION_MAX_SECONDS,
# then waits for its post-roll to be broadcast, and a cut that has to come
# from the VOD waits for that to be written; past this, no clip is coming.
CLIP_PENDING_SECONDS = 300
# The most chat lines read for one moment: for the trace and the replay
# beside an open clip, and for a queue row's spark. Two minutes of a chat
# posting ten messages a second stays under the first.
CHAT_REPLAY_LIMIT = 2000
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


def _clip_state(row) -> str:
    """Whether a moment's clip is there to play ("ready"), is still being cut
    ("pending") or is not coming ("missing")."""
    if row["clip_path"]:
        return "ready"
    return "pending" if _moment_age_seconds(row) <= CLIP_PENDING_SECONDS else "missing"


def _clip_url(clip_path: str) -> str:
    return f"/clips/{quote(clip_path)}"


def _context_clip_urls(clip_path: str | None) -> dict[str, str]:
    """URLs of whichever before/after context clips exist next to a clip."""
    if not clip_path:
        return {}
    folder = Path(clip_path).parent
    return {
        side: _clip_url((folder / name).as_posix())
        for side, name in recorder.context_clip_names(Path(clip_path).name).items()
        if (CLIPS_DIR / folder / name).exists()
    }


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
    timeline = chat_trace.clip_timeline(
        row,
        pre_roll=recorder.PRE_ROLL_SECONDS,
        post_roll=DYNAMIC_POST_ROLL_SECONDS,
        playback_delay=recorder.PLAYBACK_DELAY_SECONDS,
        chat_delay=chat_delay,
        context_before=recorder.CONTEXT_BEFORE_SECONDS,
        context_after=recorder.CONTEXT_AFTER_SECONDS,
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
    laugh_names = detector.laugh_emote_names(_channel_keywords(conn, row["broadcaster_user_id"]))
    everything, laughing = chat_trace.message_counts(messages, timeline, laugh_names)
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
            messages, window, _channel_keywords(conn, row["broadcaster_user_id"])
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
        "clip_state": _clip_state(row),
        # The footage saved just before and just after the clip, where there is any.
        "footage": _context_clip_urls(row["clip_path"]),
        "strip": strip,
        "summary": dashboard_view.summary_words(row),
        "rating": row["rating"],
        "rating_words": dashboard_view.rating_words(row["rating"]),
        "stream_type": row["stream_type"],
        "moment_type": row["moment_type"],
        "notes": row["notes"] or "",
        "analysis": [
            {"label": label, "text": row[name] or "", "pending": name in pending}
            for name, label in ANALYSIS_SETTINGS
            if name in ANALYSIS_TEXT_RESULTS and (row[name] or name in pending)
        ],
        "chat": chat,
        # Moving chat against the picture is offered where there is a picture
        # for it to be out of step with.
        "chat_delay": (
            {"seconds": chat_delay, "most": chat_trace.CHAT_DELAY_MAX_SECONDS}
            if row["clip_path"] and strip and strip["has_chat"]
            else None
        ),
    }


def _asset_version() -> int:
    """Changes whenever the stylesheet or the script does, so a browser never
    pairs a new page with its cached copy of the old ones."""
    return max(int((STATIC_DIR / name).stat().st_mtime) for name in ("dashboard.css", "dashboard.js"))


def _today() -> date:
    return datetime.now().astimezone().date()


def _page_context(conn, nav: str) -> dict:
    """What every dashboard page needs: which page it is, and the state of
    the service shown in the bar across its top."""
    streamers = get_streamers(conn)
    tracked = sum(1 for row in streamers if row["tracking_enabled"])
    recording = recording_manager.recording_channels()
    watching = _flags.get("watching_enabled", True)
    return {
        "nav": nav,
        "asset_version": _asset_version(),
        "watching_enabled": watching,
        "recording": recording,
        "channel_count_words": dashboard_view.channel_count_words(len(recording), tracked),
        "service_words": dashboard_view.service_words(
            recording=len(recording),
            tracked=tracked,
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
        recording = set(context["recording"])
        context.update(
            {
                "watchlist": [
                    {
                        "slug": row["slug"],
                        "broadcaster_user_id": row["broadcaster_user_id"],
                        "added": dashboard_view.date_words(to_local_datetime(row["added_at"]).date(), today),
                        "recording": row["slug"] in recording,
                        "tracking_enabled": bool(row["tracking_enabled"]),
                    }
                    for row in get_streamers(conn)
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
    pending = len(_moment_sessions) + len(_background_tasks)
    if _shutdown_requested:
        return {"status": "already shutting down", "pending_work_count": pending}
    _shutdown_requested = True
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
    most = chat_trace.CHAT_DELAY_MAX_SECONDS
    if not 0 <= seconds <= most:
        raise HTTPException(status_code=400, detail=f"seconds must be from 0 to {most}")

    conn = get_connection()
    try:
        if slug not in get_moment_channels(conn):
            raise HTTPException(status_code=404, detail=f"no moments from channel {slug!r}")
        set_chat_delay(conn, slug, seconds)
    finally:
        conn.close()
    logger.info("chat delay for %s -> %d s", slug, seconds)
    return {"slug": slug, "chat_delay_seconds": seconds}


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
        if not _flags.get("watching_enabled", True) or _shutdown_requested:
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
        emote_weight = detector.classify_native_emotes(content)

        conn = get_connection()
        try:
            if not get_streamer_tracking_enabled(conn, broadcaster_user_id):
                # Tracking paused for this one channel - same drop as the
                # global pause, scoped to it.
                return {"status": "channel tracking disabled"}

            logger.info("[%s] %s: %s", channel, sender.get("username", "?"), content)
            laugh_weight, mention_weight = detector.classify_message(
                content, _channel_keywords(conn, broadcaster_user_id)
            )

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

            spike = detector.record_message(
                channel,
                sender=sender_username,
                content=content,
                emote_count=emote_count,
                emote_weight=emote_weight,
                laugh_weight=laugh_weight,
                mention_weight=mention_weight,
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
