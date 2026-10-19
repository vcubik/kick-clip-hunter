import asyncio
import logging
import logging.handlers
import math
import mimetypes
import os
import shutil
import sqlite3
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import (
    audio_events,
    chat_bot,
    chat_identity,
    chat_stats,
    chat_trace,
    chat_video,
    clip_trim,
    dashboard_view,
    db,
    detector,
    frame_encoder,
    health,
    kick_auth,
    kick_chat,
    kick_client,
    notify,
    platforms,
    recorder,
    recording_manager,
    recording_moments,
    replay,
    sound_events,
    stream_recording,
    streams,
    subtitles,
    tiktok_chat,
    transcriber,
    tuning,
    tunnel,
    twitch_auth,
    twitch_chat,
    twitch_client,
    win_console,
    youtube_chat,
)
from .chat_message import ChatMessage
from .config import load_settings
from .db import (
    MOMENT_TYPES,
    STREAM_TYPES,
    TUNING_FOR_ALL,
    ChatRow,
    add_channel_rename,
    add_subscription_drop,
    count_channel_chat,
    count_moments,
    count_moments_by_channel,
    count_moments_with_clip,
    count_returning_chatters,
    count_stream_recordings,
    delete_moment,
    delete_stream_recording,
    finish_stream_recording,
    forget_chat_delay,
    get_channel_badges,
    get_channel_chat_between,
    get_channel_keywords,
    get_channel_moment_windows,
    get_channel_moments,
    get_channel_renames,
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
    get_moment_names,
    get_moment_times,
    get_notify_setup,
    get_recent_moments,
    get_stream_recording,
    get_stream_recording_offset,
    get_stream_recordings,
    get_streamer_by_id,
    get_streamer_by_slug,
    get_streamers,
    get_subscription_drops,
    get_tuning,
    get_tuning_in_force,
    insert_chat_messages,
    insert_moment,
    insert_stream_recording,
    iter_channel_chat,
    iter_channel_chat_between,
    remove_channel_chat,
    remove_streamer,
    rename_streamer,
    replace_notify_setup,
    replace_tuning,
    search_channel_chat,
    set_chat_delay,
    set_chat_identities,
    set_flag,
    set_stream_recording_state,
    set_streamer_always_record,
    set_streamer_own_tuning,
    set_streamer_tracking,
    update_moment_audio_events,
    update_moment_clip_duration,
    update_moment_clip_path,
    update_moment_downloaded,
    update_moment_frame_embedding,
    update_moment_notes,
    update_moment_rating,
    update_moment_sound_embedding,
    update_moment_sound_events,
    update_moment_stream_type,
    update_moment_subtitles,
    update_moment_title,
    update_moment_transcript,
    update_moment_type,
    update_moment_window_end,
    was_removed,
)
from .kick_client import (
    NoSuchChannel,
    get_app_access_token,
    get_channel_by_id,
    get_event_subscriptions,
    subscribe_chat_messages,
)
from .platforms import StreamUrlError
from .recorder import CLIPS_DIR
from .recording_manager import RecorderError
from .stream_recording import STREAMS_DIR
from .timeutil import to_local_datetime
from .watchlist import EMOTE_SOURCES, add_to_watchlist, refresh_badge_pictures, refresh_emote_pictures
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
    if platforms.platform_of(slug) in platforms.RECORDED_ONLY:
        return False
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
    "notify_clips": "notify_clips_enabled",
    "notify_clip_videos": "notify_clip_videos_enabled",
    "notify_recordings": "notify_recordings_enabled",
    "notify_health": "notify_health_enabled",
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
# What notifications tell of (see notify.py), each switched on its own on
# the Settings page - all of it on to begin with, the clips themselves too:
# the clip alone, half a minute or so, goes where it fits. (dashboard name,
# button label) in display order.
NOTIFY_SETTINGS = [
    ("notify_clips", "Clips"),
    ("notify_clip_videos", "Clip videos"),
    ("notify_recordings", "Recordings"),
    ("notify_health", "Problems"),
]
# Used until a setting is first toggled from the dashboard.
SETTING_DEFAULTS = {
    "watching_enabled": True,
    "transcript_enabled": False,
    "audio_events_enabled": False,
    "frames_enabled": False,
    "sound_events_enabled": False,
    "notify_clips_enabled": True,
    "notify_clip_videos_enabled": True,
    "notify_recordings_enabled": True,
    "notify_health_enabled": True,
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


# Whether stored chat is being replayed through the detector right now: for a
# trial of settings, or to find the moments of a recording. One at a time: a
# replay keeps a processor busy for seconds on end, next to a service that
# has chat to keep up with.
_replay_running = False


def _one_replay_at_a_time() -> None:
    """Refuses, with a 409, to start a replay while another is running."""
    if _replay_running:
        raise HTTPException(
            status_code=409, detail="Another replay of stored chat is still running. Try again in a moment."
        )


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

# What the Health page is made from (see health.py): what the service has
# noticed since it started of the things it depends on. None of it is
# needed to run, all of it is forgotten by a restart - except the checks
# that found subscriptions missing, which are also kept in the database.
_started_at = datetime.now(timezone.utc)


@dataclass
class _Heard:
    """What has arrived at the webhook receiver: when the last delivery
    did, when the last chat message of each channel did (by broadcaster id,
    kept or not), and the deliveries turned away for their signature."""

    last_at: datetime | None = None
    chat_at: dict[int, datetime] = field(default_factory=dict)
    refused: int = 0
    last_refused_at: datetime | None = None


_heard = _Heard()
# What the last check of Kick's chat subscriptions found, and whether one is
# under way.
_subscriptions: health.SubscriptionCheck | None = None
_subscription_check_running = False


def _subscriptions_checked(watched: int, **found) -> health.SubscriptionCheck:
    """Keeps what a check of the subscriptions found; one that found some
    missing is also remembered past a restart."""
    global _subscriptions
    _subscriptions = check = health.SubscriptionCheck(at=datetime.now(timezone.utc), watched=watched, **found)
    drop = health.drop_of(check)
    if drop is not None:
        try:
            conn = get_connection()
            try:
                add_subscription_drop(conn, drop.stored())
            finally:
                conn.close()
        except Exception:
            # A note for the Health page; the check itself has done its work.
            logger.exception("could not keep a record of the dropped chat subscriptions")
    return check


async def _ensure_chat_subscriptions(report_all_present: bool = True) -> None:
    """Re-subscribe any watchlisted channel that has no chat.message.sent
    subscription on Kick.

    Kick silently drops event subscriptions to zero every so often with no
    error - chat just stops arriving for every channel until they're
    re-subscribed (see docs/operations.md). Reconciling on startup, and then every
    SUBSCRIPTION_CHECK_SECONDS while running, makes that self-healing instead
    of needing a restart or subscribe.py run by hand. It's check-then-subscribe
    (only the missing ones) so it never duplicates an existing subscription,
    and any API failure is logged but never blocks startup or stops the
    periodic check.

    One at a time: a check asked for from the Health page while the periodic
    one is under way would find the same channels missing and subscribe them
    twice. What a check found is kept for that page (_subscriptions_checked).
    """
    global _subscription_check_running
    if _subscription_check_running:
        return
    _subscription_check_running = True
    try:
        await _reconcile_chat_subscriptions(report_all_present)
    finally:
        _subscription_check_running = False


async def _reconcile_chat_subscriptions(report_all_present: bool) -> None:
    conn = get_connection()
    try:
        watch = [(row["broadcaster_user_id"], row["slug"]) for row in get_streamers(conn, platforms.KICK)]
    finally:
        conn.close()
    if not watch:
        _subscriptions_checked(0)
        return

    try:
        token = await get_app_access_token(settings.kick_client_id, settings.kick_client_secret)
        subscribed = {sub.get("broadcaster_user_id") for sub in await get_event_subscriptions(token)}
    except Exception as error:
        logger.exception("could not check chat subscriptions")
        _subscriptions_checked(len(watch), error=type(error).__name__)
        return

    missing = [(bid, slug) for bid, slug in watch if bid not in subscribed]
    if not missing:
        if report_all_present:
            logger.info("chat subscriptions present for all %d watched channels", len(watch))
        _subscriptions_checked(len(watch))
        return

    logger.warning(
        "re-subscribing %d channel(s) with no chat subscription: %s",
        len(missing), ", ".join(slug for _, slug in missing),
    )
    failed = []
    for bid, slug in missing:
        try:
            await subscribe_chat_messages(bid, token)
            logger.info("re-subscribed chat for %s", slug)
        except Exception:
            logger.exception("failed to re-subscribe chat for %s", slug)
            failed.append(slug)
    _subscriptions_checked(len(watch), missing=tuple(slug for _, slug in missing), failed=tuple(failed))


async def _keep_chat_subscriptions() -> None:
    """The periodic half of _ensure_chat_subscriptions: quiet while nothing
    is missing, so the log only shows the checks that found something."""
    while True:
        await asyncio.sleep(SUBSCRIPTION_CHECK_SECONDS)
        try:
            await _ensure_chat_subscriptions(report_all_present=False)
        except Exception:
            logger.exception("periodic chat subscription check failed")


# How often the tunnel Kick reaches the service through is looked at (see
# tunnel.py), and how old a look may be for the Health page to show it
# without looking again.
TUNNEL_CHECK_SECONDS = 300
TUNNEL_FRESH_SECONDS = 30

# The tunnel as it was last looked at, and when. The port is the one the
# service was last seen to answer on, which only a request can tell.
_funnel: tuple[datetime, tunnel.Funnel] | None = None
_served_port: int | None = None
# A look the Health page asked for that is still under way; held so that
# the event loop, which only holds tasks weakly, does not drop it.
_tunnel_look: asyncio.Task | None = None


async def _look_at_tunnel() -> tunnel.Funnel:
    global _funnel
    before = _funnel[1].state if _funnel else None
    funnel = await asyncio.to_thread(tunnel.look, _served_port)
    _funnel = (datetime.now(timezone.utc), funnel)
    if funnel.state != before:
        # Said when it changes, so that the log can be read for when it did.
        up = funnel.state in (tunnel.UP, tunnel.NOT_INSTALLED)
        logger.log(
            logging.INFO if up else logging.WARNING,
            "tunnel: %s%s", funnel.state, f" ({funnel.said})" if funnel.said else "",
        )
    return funnel


async def _keep_looking_at_tunnel() -> None:
    while True:
        try:
            await _look_at_tunnel()
        except Exception:
            logger.exception("could not look at the tunnel")
        await asyncio.sleep(TUNNEL_CHECK_SECONDS)


async def _refresh_emote_pictures() -> None:
    """Fetch every watched channel's 7TV emote set again, and the global
    emotes every channel has on top of its own, so chat on the dashboard is
    drawn with the pictures in use now - and its subscriber badges (see
    channel_badges.py), for the same reason.

    Channels change their sets all the time, and one added before pictures
    were stored has none at all. Only the pictures are refreshed here - what
    the detector listens for changes when someone asks for it (subscribe.py,
    refresh_emotes.py), not behind a restart. A set 7TV can't be asked
    about keeps the pictures it had, and so do badges that can't be.
    """
    try:
        logger.info("%d global 7TV emote picture(s) stored", await refresh_emote_pictures())
    except Exception:
        logger.exception("could not refresh the global 7TV emote pictures")

    conn = get_connection()
    try:
        # A channel of a platform no emote service knows has no set to fetch.
        watch = [
            (row["broadcaster_user_id"], row["slug"], row["platform"], row["platform_user_id"])
            for row in get_streamers(conn)
            if row["platform"] in EMOTE_SOURCES
        ]
    finally:
        conn.close()
    for broadcaster_user_id, slug, platform, platform_user_id in watch:
        try:
            count = await refresh_emote_pictures(broadcaster_user_id, platform, platform_user_id)
            logger.info("%d 7TV emote picture(s) stored for %s", count, slug)
        except Exception:
            logger.exception("could not refresh the 7TV emote pictures of %s", slug)
        try:
            count = await refresh_badge_pictures(broadcaster_user_id, slug, platform_user_id)
            logger.info("%d subscriber badge(s) stored for %s", count, slug)
        except Exception as error:
            logger.warning("could not refresh the subscriber badges of %s: %r", slug, error)


# What reads the chat of the Twitch channels on the watchlist (see
# twitch_chat.py), while the service is set up for Twitch; None otherwise.
_twitch_reader: twitch_chat.ChatReader | None = None


def _twitch_channels() -> list[twitch_chat.Channel]:
    """The Twitch channels whose chat is read: every one on the watchlist.
    What is done with it - kept, looked for moments in - is decided message
    by message, as for Kick's (see _take_chats)."""
    conn = get_connection()
    try:
        return [
            twitch_chat.Channel(row["broadcaster_user_id"], row["slug"], row["platform_user_id"])
            for row in get_streamers(conn, platforms.TWITCH)
        ]
    finally:
        conn.close()


def _start_reading_twitch_chat() -> twitch_chat.ChatReader | None:
    """The reader of Twitch's chat, if the service is set up for Twitch.
    It reads as the account the bot writes in Twitch's chat as, and the two
    share its token (see _twitch_token_keeper)."""
    global _twitch_reader, _twitch_tokens
    if not settings.twitch:
        _twitch_reader = None
        return None
    _twitch_tokens = twitch_auth.TokenKeeper(settings.twitch_client_id, settings.twitch_client_secret)
    _twitch_reader = twitch_chat.ChatReader(_twitch_channels, _take_chats, _twitch_tokens, settings.twitch_client_id)
    return _twitch_reader


# What reads the chat of the YouTube channels on the watchlist while they
# are live (see youtube_chat.py); None until the service has started it.
_youtube_reader: youtube_chat.ChatReader | None = None


def _youtube_channels() -> list[youtube_chat.Channel]:
    """The YouTube channels whose chat is read: every one on the watchlist,
    while it is live. What is done with it is decided message by message,
    as for Kick's (see _take_chats)."""
    conn = get_connection()
    try:
        return [
            youtube_chat.Channel(row["broadcaster_user_id"], row["slug"])
            for row in get_streamers(conn, platforms.YOUTUBE)
        ]
    finally:
        conn.close()


def _start_reading_youtube_chat() -> youtube_chat.ChatReader:
    """The reader of YouTube's chat. YouTube needs nothing set up."""
    global _youtube_reader
    _youtube_reader = youtube_chat.ChatReader(_youtube_channels, _take_chats)
    return _youtube_reader


# What reads the chat of the TikTok channels whose streams are being
# recorded (see tiktok_chat.py); None until the service has started it.
_tiktok_reader: tiktok_chat.ChatReader | None = None


def _tiktok_channels() -> list[tiktok_chat.Channel]:
    """The TikTok channels whose chat is read: those whose stream is being
    kept whole right now - a TikTok channel is watched for nothing else
    (see platforms.RECORDED_ONLY)."""
    return [
        tiktok_chat.Channel(kept.broadcaster_user_id, slug)
        for slug, kept in recording_manager.stream_recordings().items()
        if platforms.platform_of(slug) == platforms.TIKTOK
    ]


def _start_reading_tiktok_chat() -> tiktok_chat.ChatReader:
    """The reader of TikTok's chat. It needs nothing set up: its requests
    are signed without a key of Euler Stream's, up to a few thousand a day."""
    global _tiktok_reader
    tiktok_chat.use_sign_key(settings.euler_stream_api_key)
    _tiktok_reader = tiktok_chat.ChatReader(_tiktok_channels, _take_chats)
    return _tiktok_reader


# -- the bot in chat ----------------------------------------------------------
#
# What the bot writes in a channel's chat, and the commands it takes there
# (see chat_bot.py). Whether it writes in a channel at all, and which
# messages are its commands, is the channel's own to set (tuning.py).

# The token of the account the bot writes in Kick's chat as (kick_auth.py),
# and of the one Twitch's chat is read - and written - as: the chat reader
# and the bot share that, since a refresh token used twice signs the
# account out.
_kick_tokens: kick_auth.TokenKeeper | None = None
_twitch_tokens: twitch_auth.TokenKeeper | None = None
# What writes, and what has become of what it wrote.
_chat_bot: chat_bot.ChatBot | None = None
# The command messages carried out lately: each is carried out once.
_bot_commands = chat_bot.Recent()
# Sign-ins of the bot's Kick account that were started and have not come
# back yet, by what they will come back with (their state).
_kick_sign_ins: dict[str, kick_auth.SignIn] = {}


def _kick_bot_tokens() -> kick_auth.TokenKeeper:
    global _kick_tokens
    if _kick_tokens is None:
        _kick_tokens = kick_auth.TokenKeeper(settings.kick_client_id, settings.kick_client_secret)
    return _kick_tokens


def _twitch_token_keeper() -> twitch_auth.TokenKeeper:
    """The Twitch account's token: the chat reader's, once it has started."""
    global _twitch_tokens
    if _twitch_tokens is None:
        _twitch_tokens = twitch_auth.TokenKeeper(settings.twitch_client_id, settings.twitch_client_secret)
    return _twitch_tokens


def _bot_writer(platform: str) -> chat_bot.Writer | None:
    """How the bot writes on a platform - not at all on YouTube or TikTok,
    nor on Twitch while the service is not set up for it."""
    if platform == platforms.KICK:
        return chat_bot.KickWriter(_kick_bot_tokens())
    if platform == platforms.TWITCH and settings.twitch:
        return chat_bot.TwitchWriter(_twitch_token_keeper(), settings.twitch_client_id)
    return None


def _bot() -> chat_bot.ChatBot:
    global _chat_bot
    if _chat_bot is None:
        _chat_bot = chat_bot.ChatBot(_bot_writer)
    return _chat_bot


def _bot_channel(row) -> chat_bot.Channel:
    return chat_bot.Channel(
        broadcaster_user_id=row["broadcaster_user_id"],
        slug=row["slug"],
        platform=row["platform"],
        platform_user_id=row["platform_user_id"] or str(row["broadcaster_user_id"]),
    )


def _tell(broadcaster_user_id: int, text: str, *, of_a_clip: bool = False) -> None:
    """Has the bot write `text` in a watched channel's chat, if the channel
    has it writing there - and, `of_a_clip`, saying when a clip is made as
    well. In the background: nothing waits for it. What holds is the
    channel's settings as they are now."""
    conn = get_connection()
    try:
        row = get_streamer_by_id(conn, broadcaster_user_id)
        tuned = _tuning(conn, broadcaster_user_id)
    finally:
        conn.close()
    if row is None or not tuned.bot_writes or (of_a_clip and not tuned.bot_tells_clips):
        return
    _track_task(_bot().say(_bot_channel(row), text))


def _bot_command(conn, message: ChatMessage) -> str | None:
    """Which of the bot's commands a chat message is (tuning.RECORD, STOP,
    STATUS): one of its channel's, posted by its streamer or a moderator."""
    if not message.runs_channel:
        return None
    return _tuning(conn, message.broadcaster_user_id).bot_command(message.content)


def _record_on_command(row, asked_by: str) -> str | None:
    """Starts recording a channel's stream, as asked in its chat (by
    `asked_by`). What is answered: why not, where it isn't - otherwise
    nothing, as starting it says so itself (_start_stream_recording)."""
    slug = row["slug"]
    kept = recording_manager.stream_recording_of(slug)
    if kept is not None:
        return chat_bot.recording_already_words(kept.writer.seconds, waiting=not kept.writer.size)
    try:
        recording_id = _start_stream_recording(slug, row["broadcaster_user_id"], notify.CHAT, asked_by)
    except _NotStarted as refused:
        logger.warning("[%s] a recording asked for in chat was not started: %s", slug, refused.words)
        return chat_bot.NOT_STARTED_WORDS[refused.status_code == 409]
    logger.info("[%s] stream recording %d started - asked for in chat", slug, recording_id)
    return None


def _stop_on_command(row) -> str | None:
    """Ends the recording of a channel's stream, as asked in its chat - as
    the button on the Channels page does. What is answered where there was
    none to end; ending one says so itself (_end_stream_recording)."""
    slug = row["slug"]
    if recording_manager.stream_recording_of(slug) is None:
        return chat_bot.NOT_RECORDING_WORDS
    _end_stream_recording(slug, stream_recording.STOPPED)
    return None


def _status_answer(row) -> str:
    """How things stand for a channel, as the bot answers when asked."""
    slug, owner = row["slug"], row["broadcaster_user_id"]
    look = recording_manager.looks().get(slug)
    clips = None
    if look is not None and look.live_since is not None:
        conn = get_connection()
        try:
            clips = len(
                get_moment_times(
                    conn, owner, look.live_since, datetime.now(timezone.utc), not_for=dashboard_view.IMPORT_REASON
                )
            )
        finally:
            conn.close()
    kept = recording_manager.stream_recording_of(slug)
    return chat_bot.status_words(
        chat_bot.Status(
            watching=_flags.get("watching_enabled", True),
            tracked=bool(row["tracking_enabled"]),
            clips=clips,
            recording=kept is not None,
            seconds=kept.writer.seconds if kept else 0.0,
            waiting=kept is not None and not kept.writer.size,
            saving=any(save["channel"] == slug for save in _stream_saves.values()),
        )
    )


def _carry_out(message: ChatMessage, command: str) -> None:
    """Does what one of the bot's commands asks: starts or ends the
    recording of the channel's stream, or says how things stand - answering
    in chat where the channel has the bot writing there. A message that
    comes again is not carried out again."""
    if not _bot_commands.first_time(message.message_id):
        return
    conn = get_connection()
    try:
        row = get_streamer_by_id(conn, message.broadcaster_user_id)
    finally:
        conn.close()
    if row is None:
        return
    logger.info("[%s] %s asked the bot to %s", row["slug"], message.sender or "?", command)
    if command == tuning.RECORD:
        answer = _record_on_command(row, message.sender)
    elif command == tuning.STOP:
        answer = _stop_on_command(row)
    else:
        answer = _status_answer(row)
    if answer:
        _tell(row["broadcaster_user_id"], answer)


# -- notifications -------------------------------------------------------------
#
# What the service tells whoever runs it, on Telegram and Discord (see
# notify.py): where to, and which kinds, is set on the Settings page - and
# where to, for whatever the page never set, in .env.

# How often the Health page's judgement is looked at for what to tell of,
# and how long a part has to have been trouble on end before it is.
HEALTH_NOTE_SECONDS = 60
HEALTH_NOTE_AFTER_SECONDS = 120

# What sends the notes, and what has become of them - and the setup it was
# made by. Both are made again once the Settings page saves another.
_notifier: notify.Notifier | None = None
_note_setup = notify.Setup()
# The parts of the Health page that were told of as trouble.
_health_watch = notify.HealthWatch(HEALTH_NOTE_AFTER_SECONDS)


def _env_setup() -> notify.Setup:
    """Where .env says notes go."""
    return notify.Setup(
        telegram_bot_token=settings.telegram_bot_token,
        telegram_chat_id=settings.telegram_chat_id,
        telegram_api_url=settings.telegram_api_url,
        discord_webhook_url=settings.discord_webhook_url,
        dashboard_url=settings.dashboard_url,
    )


def _saved_setup() -> dict[str, str]:
    """What of where notes go was saved on the Settings page."""
    conn = get_connection()
    try:
        return get_notify_setup(conn)
    finally:
        conn.close()


def _notes() -> notify.Notifier:
    global _notifier, _note_setup
    if _notifier is None:
        _note_setup = notify.in_force(_env_setup(), _saved_setup())
        found = notify.destinations(
            _note_setup.telegram_bot_token,
            _note_setup.telegram_chat_id,
            _note_setup.discord_webhook_url,
            telegram_api_url=_note_setup.telegram_api_url,
            sleep=asyncio.sleep,
        )
        _notifier = notify.Notifier(found, sleep=asyncio.sleep)
    return _notifier


def _notifying(name: str) -> bool:
    """Whether notes of a kind (its switch's name) go out: there is
    somewhere to send them, and the switch is on."""
    key = SETTING_KEYS[name]
    return bool(_notes().destinations) and _flags.get(key, SETTING_DEFAULTS[key])


def _notify(name: str, note: notify.Note) -> None:
    """Sends a note of a kind where notes go, in the background - where
    notes of that kind go out at all."""
    if _notifying(name):
        _track_task(_notes().notify(note))


def _link(path: str) -> str | None:
    """Where a page of the dashboard is, for a note to lead to."""
    _notes()
    return notify.dashboard_link(_note_setup.dashboard_url, path)


def _clip_to_send(clip_path: Path, extent: "ClipExtent", folder: Path) -> Path:
    """The clip itself, without the footage around it, as a file in
    `folder` - or the clip's own file, where that is all it holds."""
    with recorder.clip_alone(clip_path, *extent) as alone:
        if alone == clip_path:
            return clip_path
        return Path(shutil.move(alone, folder / clip_path.name))


async def _notify_of_clip(moment, channel: str, clip_path: Path, extent: "ClipExtent") -> None:
    """Tells of a clip that was saved - with the clip itself, where that is
    switched on and a destination takes it."""
    conn = get_connection()
    try:
        said = _queue_activity(conn, [moment]).get(moment["id"], {}).get("said")
    finally:
        conn.close()
    note = notify.clip_note(
        channel,
        extent[1],
        moment["stream_elapsed_seconds"],
        said=said,
        reasons=dashboard_view.reason_words(moment["reason"], main_only=True),
        commanded=moment["reason"] == dashboard_view.COMMAND_REASON,
        link=_link(dashboard_view.review_url(moment=moment["id"])),
    )
    if not _notifying("notify_clip_videos"):
        await _notes().notify(note)
        return
    with tempfile.TemporaryDirectory(prefix="notify_") as folder:
        try:
            video = await asyncio.to_thread(_clip_to_send, clip_path, extent, Path(folder))
        except Exception:
            logger.warning(
                "[%s] the clip of moment %d could not be cut out to be sent", channel, moment["id"], exc_info=True
            )
            note, video = note.plus("The clip could not be cut out to be sent."), None
        await _notes().notify(note, video)


async def _keep_telling_of_health() -> None:
    """Looks at the Health page's judgement every HEALTH_NOTE_SECONDS, and
    tells of what changed in it."""
    while True:
        await asyncio.sleep(HEALTH_NOTE_SECONDS)
        try:
            _tell_of_health()
        except Exception:
            logger.exception("the Health page could not be looked at for notifications")


def _tell_of_health(now: datetime | None = None) -> None:
    """Tells of the parts of the Health page that have needed a look for
    HEALTH_NOTE_AFTER_SECONDS, and of the ones told of that look fine again
    (see notify.HealthWatch). With the switch off nothing is kept either:
    what is trouble once it is on again is told of as it is found. Not
    while the service is shutting down, which ends what it depends on."""
    if not _notifying("notify_health"):
        _health_watch.forget()
        return
    if _shutdown_requested:
        return
    conn = get_connection()
    try:
        streamers = get_streamers(conn)
    finally:
        conn.close()
    report = health.report(_health_facts(streamers, _right_now(streamers)["channels"]), _today())
    link = _link("/dashboard/health")
    for change in _health_watch.look(report, health.PARTS, now or datetime.now(timezone.utc)):
        _track_task(
            _notes().notify(notify.health_note(change.words, change.plural, change.said, change.trouble, link=link))
        )


def _say_where_notes_go() -> None:
    """Logs where notifications go, and what in .env keeps them from going
    where it was meant to. What the Settings page saved was checked as it
    was, so whatever is wrong is .env's."""
    names = [destination.name for destination in _notes().destinations]
    setup = _note_setup
    for trouble in notify.setup_trouble(
        setup.telegram_bot_token, setup.telegram_chat_id, setup.discord_webhook_url, setup.telegram_api_url
    ):
        logger.warning("notifications: %s", trouble)
    if names:
        logger.info("notifications go to %s", dashboard_view.list_words(names))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _started_at
    _started_at = datetime.now(timezone.utc)
    _load_flags()
    _say_where_notes_go()
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
                _look_failed,
            )
        ),
        asyncio.create_task(_keep_chat_subscriptions()),
        asyncio.create_task(_keep_looking_at_tunnel()),
        asyncio.create_task(_keep_telling_of_health()),
    ]
    reader = _start_reading_twitch_chat()
    if reader is not None:
        tasks.append(asyncio.create_task(reader.run_forever()))
    tasks.append(asyncio.create_task(_start_reading_youtube_chat().run_forever()))
    tasks.append(asyncio.create_task(_start_reading_tiktok_chat().run_forever()))
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        # Let each finish what it does when it is stopped - the chat reader
        # closes its connection.
        await asyncio.gather(*tasks, return_exceptions=True)


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
# the ones for every channel and those of each channel that goes by its own
# (of any other channel, only what is a channel's alone).
# The webhook asks for a channel's settings with every message, so they are
# read once and kept; saving a setting drops them.
_tuning_values: dict[int, dict[str, tuning.Value]] | None = None

# Channel keyword weights rarely change (only when re-subscribing/refreshing
# emotes, or when the channel's settings do), so we cache them per-process
# instead of hitting the DB on every single chat message.
_channel_keywords_cache: dict[int, dict[str, float]] = {}

# Stream start times don't need to be looked up on every message - only when
# a moment fires - and barely change within a single stream, so a short
# cache keeps us from hammering the channels endpoint. Kept by the channel's
# id, as a Kick channel is asked for by it: the name its chat comes under
# need not be one Kick's API finds it by just after it renamed itself.
_stream_start_cache: dict[int, tuple[datetime | None, float]] = {}
STREAM_INFO_CACHE_SECONDS = 120


# What was last stored of each chatter's identity (name colour and badges),
# by (channel, name). A platform sends it with every message and it hardly
# ever changes, so this is what keeps it from being written every time: only
# someone not seen since the start, or seen changed, costs a write.
_chat_identity_cache: dict[tuple[int, str], chat_identity.Identity] = {}
# Past this many chatters the cache is simply started over - each of them
# then costs one more write the next time they speak.
CHAT_IDENTITY_CACHE_LIMIT = 50_000


def _remember_chat_identities(conn, messages: Iterable[ChatMessage]) -> None:
    """Stores the identities of the senders of messages that are new or
    changed since they were last stored - all of them in one go."""
    latest = {
        (message.broadcaster_user_id, message.sender): message.identity
        for message in messages
        if message.identity is not None and message.sender
    }
    changed = {key: identity for key, identity in latest.items() if _chat_identity_cache.get(key) != identity}
    if not changed:
        return
    set_chat_identities(conn, [(owner, name, *identity) for (owner, name), identity in changed.items()])
    for key, identity in changed.items():
        if len(_chat_identity_cache) >= CHAT_IDENTITY_CACHE_LIMIT:
            _chat_identity_cache.clear()
        _chat_identity_cache[key] = identity


def _tuning(conn, broadcaster_user_id: int) -> tuning.Tuning:
    """The settings the service goes by for a channel: what was set for all
    channels, the channel's own on top where it goes by them (what is a
    channel's alone, like its clip commands, either way), and the constants
    for everything nobody set (see tuning.py)."""
    global _tuning_values
    if _tuning_values is None:
        in_force = get_tuning_in_force(conn, tuning.PER_CHANNEL)
        _tuning_values = {owner: tuning.stored(texts) for owner, texts in in_force.items()}
    own = _tuning_values.get(broadcaster_user_id) if broadcaster_user_id != TUNING_FOR_ALL else None
    return tuning.tuned(_tuning_values.get(TUNING_FOR_ALL, {}), own)


def _tuning_now(broadcaster_user_id: int) -> tuning.Tuning:
    """The same, for a caller with no connection of its own: one is opened
    only where the settings have to be read."""
    if _tuning_values is not None:
        return _tuning(None, broadcaster_user_id)
    conn = get_connection()
    try:
        return _tuning(conn, broadcaster_user_id)
    finally:
        conn.close()


def _own_values(conn, streamer) -> dict[str, tuning.Value]:
    """What a watched channel has of its own that is in force: all of it
    where it goes by its own settings, otherwise only what is a channel's
    alone."""
    mine = tuning.stored(get_tuning(conn, streamer["broadcaster_user_id"]))
    return mine if streamer["own_tuning"] else tuning.per_channel(mine)


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


async def _stream_elapsed_seconds(channel_slug: str, broadcaster_user_id: int) -> int | None:
    """How far into its stream a channel is now; None while it is not live."""
    cached = _stream_start_cache.get(broadcaster_user_id)
    now = time.monotonic()
    if cached is None or now - cached[1] > STREAM_INFO_CACHE_SECONDS:
        status = await streams.live_status(channel_slug, broadcaster_user_id)
        _stream_start_cache[broadcaster_user_id] = (status.started_at, now)

    start_time, _ = _stream_start_cache[broadcaster_user_id]
    if start_time is None:
        return None
    return int((datetime.now(timezone.utc) - start_time).total_seconds())


# A Kick channel can rename itself. It is kept by its id, which stays, and
# its chat comes under the new name from then on - but the watchlist has the
# old one, by which Kick no longer finds its stream. The rename is noticed
# two ways: chat arriving under a name that is not the watchlist's, or Kick
# knowing no channel by the watchlist's name when the recording loop asks.
# Either way Kick is asked what the channel is called now, by its id, and
# the watchlist follows it (_follow_rename).
#
# The name each Kick channel's chat last came under, by channel id: chat
# under another one is what has the watchlist's name checked.
_chat_names_seen: dict[int, str] = {}
# When Kick was last asked for a channel's name because the loop could not
# find it, by channel id. A channel that is really gone would otherwise be
# asked about every round.
_renames_asked_at: dict[int, float] = {}
RENAME_ASK_SECONDS = 600


def _notice_chat_name(conn, message: ChatMessage) -> None:
    """Has the watchlist's name of a Kick channel checked against Kick's
    when its chat comes under another one."""
    owner, name = message.broadcaster_user_id, message.channel
    if _chat_names_seen.get(owner) == name or platforms.platform_of(name) != platforms.KICK:
        return
    _chat_names_seen[owner] = name
    row = get_streamer_by_id(conn, owner)
    # Kick sends its names in small letters; the watchlist keeps the one a
    # channel was added under, which may have capitals.
    if row is not None and row["platform"] == platforms.KICK and row["slug"].lower() != name.lower():
        _track_task(_follow_rename(owner))


async def _look_failed(slug: str, error: Exception) -> None:
    """What the recording loop calls when a look at a channel fails (see
    recording_manager.run_forever): a Kick channel that Kick knows no
    channel by the name of may have renamed itself."""
    if not isinstance(error, NoSuchChannel):
        return
    conn = get_connection()
    try:
        row = get_streamer_by_slug(conn, slug)
    finally:
        conn.close()
    if row is None or row["platform"] != platforms.KICK:
        return
    owner = row["broadcaster_user_id"]
    asked = _renames_asked_at.get(owner)
    if asked is not None and time.monotonic() - asked < RENAME_ASK_SECONDS:
        return
    _renames_asked_at[owner] = time.monotonic()
    await _follow_rename(owner)


async def _follow_rename(broadcaster_user_id: int) -> str | None:
    """Asks Kick what a watched channel is called now, and has the watchlist
    follow it if it renamed itself. Returns its new name, or None if it is
    still called what the watchlist says (or Kick knows it no more).

    What is kept of a channel by its id goes with it as it is. What is kept
    by name stays under the old one: its past moments and chat, and its
    stream recordings - a recording that runs goes on, in its folder. The
    chat delay set for it is carried over to the new name, which is what its
    chat and moments come under from now on."""
    try:
        token = await get_app_access_token(settings.kick_client_id, settings.kick_client_secret)
        channel = await get_channel_by_id(broadcaster_user_id, token)
    except NoSuchChannel:
        logger.warning("Kick knows no channel of broadcaster %d any more - left on the watchlist", broadcaster_user_id)
        return None
    except httpx.HTTPError as error:
        logger.warning("could not ask Kick what channel %d is called now: %r", broadcaster_user_id, error)
        return None
    new = str(channel.get("slug") or "")
    conn = get_connection()
    try:
        row = get_streamer_by_id(conn, broadcaster_user_id)
        if row is None or row["platform"] != platforms.KICK or not new or new.lower() == row["slug"].lower():
            return None
        old = row["slug"]
        try:
            rename_streamer(conn, broadcaster_user_id, new)
        except sqlite3.IntegrityError:
            logger.error("[%s] renamed itself on Kick to %s, which another watched channel is called", old, new)
            return None
        _carry_chat_delay(conn, broadcaster_user_id, new)
        add_channel_rename(conn, health.Rename(datetime.now(timezone.utc), old, new).stored())
    finally:
        conn.close()
    recording_manager.rename(old, new)
    logger.warning("[%s] renamed itself on Kick to %s - the watchlist follows it", old, new)
    return new


def _carry_chat_delay(conn, broadcaster_user_id: int, new: str) -> None:
    """Gives a channel's new name the chat delay that was set for it under
    the name of its newest moments, unless one is set for the new name."""
    if get_chat_delay(conn, new) is not None:
        return
    for name in get_moment_names(conn, broadcaster_user_id):
        seconds = get_chat_delay(conn, name) if name != new else None
        if seconds is not None:
            set_chat_delay(conn, new, seconds)
            return


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
        moment = get_moment(conn, moment_id)
    finally:
        conn.close()
    if moment is None:
        # Removed while its clip was being cut.
        clip_path.unlink(missing_ok=True)
        logger.info("[%s] clip of moment %d thrown away: the moment was removed", channel, moment_id)
        return
    _tell(
        moment["broadcaster_user_id"],
        chat_bot.clip_words(clip.duration, moment["stream_elapsed_seconds"]),
        of_a_clip=True,
    )
    logger.info(
        "[%s] clip saved for moment %d: %s (%.0f s, with %.0f s before it and %.0f s after)",
        channel, moment_id, clip_path, clip.duration, clip.context_before, clip.context_after,
    )
    extent = (clip.context_before, clip.duration, clip.context_after)
    if _notifying("notify_clips"):
        _track_task(_notify_of_clip(moment, channel, clip_path, extent))
    for analyse in _analysis_steps():
        _track_task(analyse(moment_id, channel, clip_path, extent))


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


def _analysis_steps() -> list:
    """The per-clip analysis steps that are switched on right now. Each is
    called with a moment's id, its channel, its file and where the clip
    lies in that file."""
    steps = (
        ("transcript_enabled", _transcribe_clip_background),
        ("audio_events_enabled", _detect_audio_events_background),
        ("frames_enabled", _encode_frames_background),
        ("sound_events_enabled", _tag_sound_events_background),
    )
    return [analyse for flag, analyse in steps if _flags.get(flag)]


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

# What a moment made on command is stored with in place of what the detector
# measured: it measured nothing.
_COMMANDED = detector.Spike(
    reasons=[dashboard_view.COMMAND_REASON],
    score=0.0,
    message_count=0,
    baseline_message_rate=0.0,
    current_message_rate=0.0,
    emote_count=0,
    keyword_hits=0,
)


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
            asked = get_stream_recording(conn, recording_id)
            ended_by = asked["ended_by"] if asked is not None else None
            if parts:
                size = output.stat().st_size
                finish_stream_recording(
                    conn,
                    recording_id,
                    state=stream_recording.READY,
                    file_path=output.relative_to(STREAMS_DIR).as_posix(),
                    started_at=parts[0].started_at.isoformat(),
                    ended_at=parts[-1].ended_at.isoformat(),
                    duration=stream_recording.length(parts),
                    size_bytes=size,
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
        _notify(
            "notify_recordings",
            notify.recording_failed_note(channel, link=_link(dashboard_view.recordings_url(recording_id))),
        )
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
        _notify(
            "notify_recordings",
            notify.recording_saved_note(
                channel,
                ended_by,
                stream_recording.length(parts),
                size,
                len(parts),
                link=_link(dashboard_view.recordings_url(recording_id)),
            ),
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
    _tell(kept.broadcaster_user_id, chat_bot.ended_words(ended_by, bool(writer.size), writer.seconds))
    if not writer.size:
        shutil.rmtree(writer.folder, ignore_errors=True)
        logger.info("[%s] stream recording %d ended (%s) before any footage", slug, kept.recording_id, ended_by)
        _notify("notify_recordings", notify.recording_empty_note(slug, ended_by))
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


def _start_stream_recording(
    slug: str, broadcaster_user_id: int, started_by: str = notify.DASHBOARD, asked_by: str | None = None
) -> int:
    """Starts keeping a channel's stream whole and returns the recording's
    id - or says why not (_NotStarted): the channel is being recorded
    already, or the disk is too full. `started_by` is where it was asked
    for (notify.DASHBOARD, CHAT - by `asked_by` - or ALWAYS), which a
    notification says."""
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
    look = recording_manager.looks().get(slug)
    live = None if look is None else look.live_since is not None
    _tell(broadcaster_user_id, chat_bot.started_words(live))
    _notify(
        "notify_recordings",
        notify.recording_started_note(
            slug, started_by, live=live, asked_by=asked_by, link=_link(dashboard_view.recordings_url(recording_id))
        ),
    )
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
        recording_id = _start_stream_recording(slug, row["broadcaster_user_id"], notify.ALWAYS)
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
            folder = stream_recording.parts_folder(slug, recording_id)
            written = stream_recording.last_written(folder)
            fresh = written is None or now - written < stale
            # Under the name the channel goes by now, which is not the one
            # the recording was started under if the channel renamed itself
            # since. Its footage stays in the folder it was started in.
            watched = get_streamer_by_id(conn, row["broadcaster_user_id"])
            name = watched["slug"] if watched is not None else slug
            if was_running and fresh and recording_manager.stream_recording_of(name) is None:
                kept = recording_manager.start_stream_recording(name, row["broadcaster_user_id"], recording_id, folder)
                logger.warning(
                    "[%s] stream recording %d goes on after the restart, with %.0f s of footage so far",
                    name, recording_id, kept.writer.seconds,
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


def _recording_with_file(conn, recording_id: int, to: str) -> tuple:
    """A stream recording that is over and has its file - what something
    can be cut out of, or looked for in: its row, its parts and the file.
    Otherwise a 404, or a 409 that says there is no file `to` do that
    with."""
    row = get_stream_recording(conn, recording_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown recording {recording_id}")
    parts = stream_recording.decoded(row["parts"])
    file = _stream_file(row)
    over = row["state"] == stream_recording.READY and recording_id not in _stream_recordings_not_over()
    if not over or not parts or file is None or not file.exists():
        raise HTTPException(status_code=409, detail=f"The recording has no file to {to}.")
    return row, parts, file


@app.get("/health")
async def health_check():
    """That the service answers at all - what a script asks. What it
    depends on is the Health page's (`GET /dashboard/health`)."""
    return {"status": "ok"}


MOMENTS_PAGE_SIZE = 50
RECORDINGS_PAGE_SIZE = 50

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


def _cut_record_path(video: Path) -> Path:
    """Where what is kept of a cut video is (see clip_trim.cut_record)."""
    return video.with_name(clip_trim.record_name(video.name))


def _cut_record(video: Path) -> clip_trim.Cut | None:
    """Where a cut video begins in the file it was cut from, how long it
    runs and where it was set to end - or None where that was not kept: a
    cut from before it was, or one whose video is not there."""
    record = _cut_record_path(video)
    if not video.exists() or not record.exists():
        return None
    try:
        return clip_trim.read_cut_record(record.read_text(encoding="utf-8"))
    except OSError:
        return None


def _trim_standing(
    known: dict,
    video: str | None,
    chat: str | None,
    stretch: tuple[float, float] | None = None,
    held: tuple[float, float] | None = None,
    burned: bool = False,
    subtitles_url: str | None = None,
) -> dict:
    """Where a stretch cut out of some footage stands: being cut, its chat
    being made, there to download, failed, or never asked for. `known` is
    what is remembered of it, `video` and `chat` the addresses of its files
    where those are there, `stretch` which seconds of a recording it holds
    going by its name, `held` exactly where its video begins and ends in the
    dialog's times where that was kept (see clip_trim.cut_record) - which
    its chat can be made later by, too - `burned` whether the video has its
    chat drawn into it already, and `subtitles_url` where subtitles timed to
    the video are downloaded from, where there are any (a moment's
    transcript, see _cut_subtitles_url)."""
    state = known.get("state")
    if state in (dashboard_view.TRIM_CUTTING, dashboard_view.TRIM_BURNING, dashboard_view.TRIM_FAILED):
        video = chat = None
    elif state in (dashboard_view.TRIM_CHAT, dashboard_view.TRIM_CHAT_FAILED):
        chat = None
    else:
        state = dashboard_view.TRIM_READY if video else dashboard_view.TRIM_NONE
    burned = burned and bool(video)
    return {
        "state": state,
        "words": dashboard_view.trim_words(state, known.get("early", 0.0), stretch, known.get("done"), burned),
        "video_url": video,
        "chat_url": chat,
        "subtitles_url": subtitles_url if video else None,
        # What the video that is there holds, for the dialog to say in its
        # own way of writing times.
        "cut": {"from": round(held[0], 2), "to": round(held[1], 2)} if held and video else None,
        # A video that is there without its chat can have it made now.
        "chat_later": bool(
            held
            and video
            and not chat
            and not burned
            and state in (dashboard_view.TRIM_READY, dashboard_view.TRIM_CHAT_FAILED)
        ),
    }


def _held(record: clip_trim.Cut | None, lead: float) -> tuple[float, float] | None:
    """Where a cut video begins and was set to end in the dialog's times -
    in the file's seconds less `lead` - from what was kept of it."""
    if record is None:
        return None
    return record.begins - lead, record.end - lead


def _trim_download_url(moment_id: int, what: str, relative: Path) -> str | None:
    """The address a moment's cut video or its chat video is downloaded
    from, or None if it is not there. Not the file's own address under
    /clips: a download through this one is what marks the moment as taken
    (see download_trim)."""
    file = CLIPS_DIR / relative
    if not file.exists():
        return None
    # Made again, it is a different file under the same name.
    return f"/moments/{moment_id}/trim/{what}?v={int(file.stat().st_mtime)}"


def _trim(row) -> dict:
    """Where the stretch cut out of a moment's footage stands."""
    video, chat = _trim_paths(row["clip_path"])
    record = _cut_record(CLIPS_DIR / video)
    return _trim_standing(
        _trims.get(row["id"], {}),
        _trim_download_url(row["id"], "video", video),
        _trim_download_url(row["id"], "chat", chat),
        # A moment's times are clip time.
        held=_held(record, row["context_before"] or 0.0),
        burned=bool(record and record.burned),
        subtitles_url=_cut_subtitles_url(row, record),
    )


def _trim_refused(error: clip_trim.TrimError) -> HTTPException:
    """What was asked for can't be cut, said as the dialog shows it."""
    return HTTPException(status_code=400, detail=f"{dashboard_view.sentence_case(str(error))}.")


def _write_cut_record(video: Path, cut: clip_trim.Cut, channel: str, what: str) -> None:
    """Keeps what is known of a cut video beside it (see
    clip_trim.cut_record), so that its chat can be asked for later as well
    and the dialog can say which stretch it holds."""
    try:
        _cut_record_path(video).write_text(clip_trim.cut_record(*cut), encoding="utf-8")
    except OSError:
        logger.warning("[%s] where the cut of %s begins could not be kept", channel, what, exc_info=True)


def _making(standing: dict[int, dict], key: int, state: str, early: float = 0.0) -> Callable[[float], None]:
    """What chat that is being made for a cut - or a moment's transcript -
    tells how far it has got: it is kept in `standing` under `key`, for the
    page to say. Once the service is shutting down it gives up (_GivenUp)
    rather than have the shutdown wait minutes for it."""

    def progress(done: float) -> None:
        if _shutdown_requested:
            raise _GivenUp
        standing[key] = {"state": state, "early": early, "done": done}

    return progress


async def _trim_background(
    standing: dict[int, dict],
    key: int,
    what: str,
    channel: str,
    clip: Path,
    span: tuple[float, float],
    lead: float,
    lines: list[dict] | None,
    look: clip_trim.ChatLook,
    video: Path,
    chat: Path,
    replaced: tuple[Path, ...] = (),
) -> None:
    # The cut takes a moment, and its chat as a video of its own a while
    # after it; chat drawn into its picture is made together with it, and
    # takes minutes for a long stretch. All of it off the event loop, and as
    # a tracked task so a shutdown waits for it (or has it give up, see
    # _making). Where it stands is kept in `standing` under `key`: a
    # moment's in _trims, a recording's in _recording_trims; `what` is whose
    # cut it is, for the log.
    #
    # What was cut before goes first: a chat video left over from it would
    # not belong to the new video.
    old = (chat, video, _cut_record_path(video), *replaced)
    if lines is not None and look.where != clip_trim.CHAT_APART:
        # Encoded anew, it begins on the very frame that was set.
        cut = clip_trim.Cut(span[0], span[1] - span[0], span[1], burned=True)
        await _burn_background(
            standing, key, what, channel, source=clip, first=span[0], cut=cut, start=span[0] - lead,
            lines=lines, look=look, video=video, old=old,
        )
        return
    try:
        for path in old:
            path.unlink(missing_ok=True)
        begins, duration = await asyncio.to_thread(recorder.trim_clip, clip, *span, video)
    except Exception:
        logger.exception("[%s] trimming failed for %s", channel, what)
        standing[key] = {"state": dashboard_view.TRIM_FAILED}
        return
    early = max(span[0] - begins, 0.0)
    logger.info("[%s] %s trimmed: %s (%.1f s)", channel, what, video.name, duration)
    _write_cut_record(video, clip_trim.Cut(begins, duration, span[1]), channel, what)
    if lines is None:
        standing[key] = {"early": early}
        return
    # The chat video starts on the trimmed video's first frame, wherever in
    # the file that turned out to be.
    await _trim_chat_background(standing, key, what, channel, lines, begins - lead, duration, look, chat, early)


async def _trim_chat_background(
    standing: dict[int, dict],
    key: int,
    what: str,
    channel: str,
    lines: list[dict],
    start: float,
    duration: float,
    look: clip_trim.ChatLook,
    chat: Path,
    early: float = 0.0,
) -> None:
    # The chat video of a cut, made with it or later for a cut that is
    # there: `start` is where the cut's first frame is on the time its
    # chat lines are placed on, `duration` how long the cut runs.
    standing[key] = {"state": dashboard_view.TRIM_CHAT, "early": early}
    try:
        chat.unlink(missing_ok=True)
        shown = chat_video.lines_from(lines, start, look.shift)
        progress = _making(standing, key, dashboard_view.TRIM_CHAT, early)
        frames = await asyncio.to_thread(chat_video.render, shown, duration, chat, look.background, progress)
    except _GivenUp:
        standing[key] = {"early": early}
        logger.info("[%s] making the chat video of %s given up: shutting down", channel, what)
        return
    except Exception:
        logger.exception("[%s] making the chat video failed for %s", channel, what)
        standing[key] = {"state": dashboard_view.TRIM_CHAT_FAILED, "early": early}
        return
    standing[key] = {"early": early}
    logger.info("[%s] chat video saved for %s: %s (%d frames)", channel, what, chat.name, frames)


async def _burn_background(
    standing: dict[int, dict],
    key: int,
    what: str,
    channel: str,
    source: Path,
    first: float,
    cut: clip_trim.Cut,
    start: float,
    lines: list[dict],
    look: clip_trim.ChatLook,
    video: Path,
    old: tuple[Path, ...] = (),
) -> None:
    # A cut with its chat drawn into its picture, which is encoded anew for
    # it: from the second `first` of `source`, as long as `cut` runs - `cut`
    # being what is kept of it, in seconds of the footage it is a stretch
    # of - with its first frame at `start` on the time its chat lines are
    # placed on. A video that is there already can be the source: then it
    # is replaced only once the new one is whole, and is what is left if
    # that fails.
    standing[key] = {"state": dashboard_view.TRIM_BURNING, "done": 0.0}
    kept = source == video
    try:
        for path in old:
            path.unlink(missing_ok=True)
        shown = chat_video.lines_from(lines, start, look.shift)
        progress = _making(standing, key, dashboard_view.TRIM_BURNING)
        encoder = await asyncio.to_thread(
            chat_video.burn,
            shown,
            source,
            first,
            cut.duration,
            video,
            look.box,
            look.background,
            progress,
        )
    except _GivenUp:
        standing.pop(key, None)
        logger.info("[%s] drawing chat into the cut of %s given up: shutting down", channel, what)
        return
    except Exception:
        logger.exception("[%s] drawing chat into the cut of %s failed", channel, what)
        standing[key] = {"state": dashboard_view.TRIM_CHAT_FAILED if kept else dashboard_view.TRIM_FAILED}
        return
    duration = await asyncio.to_thread(recorder.probe_duration, video) or cut.duration
    _write_cut_record(video, cut._replace(duration=duration, burned=True), channel, what)
    standing[key] = {}
    logger.info(
        "[%s] %s cut with its chat in it: %s (%.1f s, encoded with %s)", channel, what, video.name, duration, encoder
    )


# -- a moment's transcript, as subtitles (see subtitles.py) -------------------

# Moments whose footage is being transcribed right now, and those whose
# transcript could not be made, by moment id. Any other moment's transcript
# is told from its row, so a restart forgets nothing that matters.
_transcripts: dict[int, dict] = {}


async def _transcribe_background(moment_id: int, channel: str, clip: Path, lead: float) -> None:
    # The speech model takes a while over a file of a couple of minutes,
    # and longer the first time, when it is loaded: off the event loop, and
    # as a tracked task, which gives up when the service shuts down rather
    # than hold that up (see _making) - it is made again when asked for.
    progress = _making(_transcripts, moment_id, dashboard_view.TRANSCRIPT_MAKING)
    try:
        heard = await asyncio.to_thread(transcriber.transcribe_words, clip, progress)
    except _GivenUp:
        _transcripts.pop(moment_id, None)
        logger.info("[%s] transcribing moment %d given up: shutting down", channel, moment_id)
        return
    except Exception:
        logger.exception("[%s] transcribing the footage of moment %d failed", channel, moment_id)
        _transcripts[moment_id] = {"state": dashboard_view.TRANSCRIPT_FAILED}
        return
    # Heard on the file's time, whose first frame is `lead` before the clip's.
    lines = subtitles.lines(heard, begins=-lead)
    conn = get_connection()
    try:
        update_moment_subtitles(conn, moment_id, subtitles.stored(lines))
    finally:
        conn.close()
    _transcripts.pop(moment_id, None)
    logger.info("[%s] transcript of moment %d saved (%d lines)", channel, moment_id, len(lines))


def _cut_subtitles_url(row, record: clip_trim.Cut | None) -> str | None:
    """Where the subtitles timed to the video cut out of a moment's footage
    are downloaded from - or None where there are none: no cut kept where it
    begins (see clip_trim.cut_record), no transcript, or nothing in it said
    while the cut runs."""
    transcript = subtitles.read(row["subtitles"])
    if record is None or not transcript:
        return None
    if not subtitles.srt(transcript, record.begins - (row["context_before"] or 0.0), record.duration):
        return None
    return f"/moments/{row['id']}/trim/subtitles"


def _transcript(row) -> dict:
    """What the page shows of a moment's transcript beside its clip: where
    it stands, its lines - each with its times in clip time, and its time
    written as the page writes one - and where its subtitles are downloaded
    from, for the whole file and for the cut made with Trim."""
    moment_id = row["id"]
    transcript = subtitles.read(row["subtitles"])
    known = _transcripts.get(moment_id, {})
    if transcript is not None:
        state = dashboard_view.TRANSCRIPT_READY
    else:
        state = known.get("state", dashboard_view.TRANSCRIPT_NONE)
    lines = transcript or []
    video, _chat = _trim_paths(row["clip_path"])
    return {
        "url": f"/moments/{moment_id}/subtitles",
        "page": f"/dashboard/moments/{moment_id}/subtitles",
        "busy": state == dashboard_view.TRANSCRIPT_MAKING,
        "words": dashboard_view.transcript_words(state, known.get("done"), said=bool(lines)),
        "can_make": state in (dashboard_view.TRANSCRIPT_NONE, dashboard_view.TRANSCRIPT_FAILED),
        # What the browser checks the spelling of a line in.
        "language": transcriber.LANGUAGE,
        "lines": [
            {
                "start": dashboard_view.number_words(line.start, 3),
                "end": dashboard_view.number_words(line.end, 3),
                "time": dashboard_view.clip_time_words(line.start, row["clip_duration"]),
                "text": line.text,
            }
            for line in lines
        ],
        "file_url": f"/moments/{moment_id}/subtitles" if subtitles.said(lines) else None,
        "file_name": Path(row["clip_path"]).name,
        "cut_url": _cut_subtitles_url(row, _cut_record(CLIPS_DIR / video)),
        "cut_name": video.name,
    }


def _subtitles_file(transcript: list[subtitles.Line], start: float, length: float | None, video: str) -> Response:
    """A video's subtitles to save beside it, named for it: the transcript's
    lines from clip time `start`, where the video's first frame is, for as
    long as it runs (see subtitles.srt)."""
    name = subtitles.name(video)
    quoted = quote(name)
    return Response(
        subtitles.srt(transcript, start, length).encode(subtitles.ENCODING),
        media_type="application/x-subrip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{name}"' if quoted == name else f"attachment; filename*=utf-8''{quoted}"
            ),
            # Corrected, they are other subtitles under the same address.
            "Cache-Control": "no-store",
        },
    )


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
    was set for the channel on the review page, or what a channel of its
    platform starts out with."""
    stored = get_chat_delay(conn, channel_slug)
    return chat_trace.default_chat_delay(channel_slug) if stored is None else stored


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
    badges = get_channel_badges(conn, row["broadcaster_user_id"])
    return strip, chat_trace.chat_replay(messages, timeline, window, emotes, identities, badges)


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
                "id": str(row["id"]),
                "url": f"/moments/{row['id']}/trim",
                "video_url": _clip_url(row["clip_path"]),
                "lead": dashboard_view.number_words(row["context_before"] or 0.0, 3),
                "last": dashboard_view.number_words(clip_trim.footage(row)[1], 2),
                "chat": bool(chat),
                "chat_rest_words": "Chat where the review page has it",
                "time_hint": (
                    "A time in the clip, written like 0:12.5. With a minus in front it counts back "
                    "from the clip's start, with a plus on from its end."
                ),
                # A moment's strip shows all of its footage at once.
                "window": None,
                **_trim_dialog(),
            }
            if row["clip_path"] and row["clip_duration"] and strip
            else None
        ),
        # What is said in the footage, written down when it is asked for:
        # beside the clip, where there is a clip to write it down from.
        "transcript": _transcript(row) if row["clip_path"] else None,
    }


def _trim_dialog() -> dict:
    """What every Trim dialog is told, whatever it cuts from."""
    return {
        "min_seconds": dashboard_view.number_words(clip_trim.MIN_SECONDS),
        "chat_shift_limit": dashboard_view.number_words(clip_trim.CHAT_SHIFT_LIMIT_SECONDS),
        "chat_background": clip_trim.DEFAULT_CHAT_BACKGROUND,
        "chat_places": {"video": clip_trim.CHAT_VIDEO, "apart": clip_trim.CHAT_APART},
        # What the dialog's box of chat over the picture keeps to, as the
        # chat drawn into the cut does (see chat_video.placement), and where
        # it starts out.
        "chat_column": {
            "width": chat_video.WIDTH,
            "height": chat_video.HEIGHT,
            "margin": chat_video.MARGIN_PIXELS,
            "scale_least": chat_video.SCALE_LEAST,
            "scale_most": chat_video.SCALE_MOST,
            "least": chat_video.COLUMN_LEAST,
            "bottom": chat_video.BOTTOM_PIXELS,
        },
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


def _health_facts(streamers, right_now: dict, *, stored_chat: dict | None = None, **looked_up) -> health.Facts:
    """What the Health page, and the count beside its name in the bar of
    every page, are made from (see health.py): what the service has noticed
    of the things it depends on. `right_now` is what the channels are doing
    (see _right_now). The page itself adds what it takes the database or a
    walk over the disk to know: `stored_chat`, when each channel's newest
    stored message arrived by broadcaster id, and health.Facts' `drops` and
    `use` - none of which changes what is trouble."""
    looks = recording_manager.looks()
    runs = recording_manager.recorder_runs()
    channels = []
    for row in streamers:
        slug, owner = row["slug"], row["broadcaster_user_id"]
        look, run = looks.get(slug), runs.get(slug)
        chat = [at for at in (_heard.chat_at.get(owner), (stored_chat or {}).get(owner)) if at is not None]
        channels.append(
            health.Channel(
                slug=slug,
                platform=row["platform"],
                now_words=right_now[slug]["words"],
                coming_in=right_now[slug]["live"],
                live_since=look.live_since if look else None,
                trouble=look.trouble if look else None,
                last_chat_at=max(chat, default=None),
                recording_since=run.since if run else None,
                footage_at=run.footage_at if run else None,
                has_vod=bool(run and run.has_vod),
            )
        )
    try:
        free, room = stream_recording.free_bytes(), recording_manager.room_left()
    except OSError:
        # Said on the Health page. Every page asks, and none may fail for it.
        free = room = None
    looked_at, funnel = _funnel or (None, None)
    return health.Facts(
        now=datetime.now(timezone.utc),
        started_at=_started_at,
        channels=tuple(channels),
        watching=_flags.get("watching_enabled", True),
        last_webhook_at=_heard.last_at,
        refused=_heard.refused,
        last_refused_at=_heard.last_refused_at,
        subscriptions=_subscriptions,
        check_seconds=SUBSCRIPTION_CHECK_SECONDS,
        round_finished_at=recording_manager.round_finished_at(),
        round_seconds=recording_manager.TICK_INTERVAL_SECONDS,
        free=free,
        room=room,
        min_free=recording_manager.MIN_FREE_BYTES,
        funnel=funnel,
        funnel_looked_at=looked_at,
        port=_served_port,
        twitch=_twitch_facts(sum(1 for row in streamers if row["platform"] == platforms.TWITCH)),
        youtube=_youtube_facts(sum(1 for row in streamers if row["platform"] == platforms.YOUTUBE)),
        tiktok=_tiktok_facts(sum(1 for row in streamers if row["platform"] == platforms.TIKTOK)),
        bot=_bot_facts(streamers),
        notifications=_notification_facts(),
        **looked_up,
    )


def _notification_facts() -> health.Notifications | None:
    """Where notifications go and what they tell of, for the Health page:
    None while they go nowhere."""
    notes = _notes()
    if not notes.destinations:
        return None
    return health.Notifications(
        destinations=tuple(
            health.NoteDestination(
                destination.name,
                destination.where(),
                notes.sent[destination.name].sent_at,
                notes.sent[destination.name].trouble,
            )
            for destination in notes.destinations
        ),
        clips=_notifying("notify_clips"),
        clip_videos=_notifying("notify_clip_videos"),
        recordings=_notifying("notify_recordings"),
        problems=_notifying("notify_health"),
        dashboard_url=_note_setup.dashboard_url,
    )


def _tiktok_facts(channels: int) -> health.TikTokChat | None:
    """Where reading TikTok's chat stands, for the Health page: None while
    no TikTok channel is watched."""
    if not channels:
        return None
    reader = _tiktok_reader
    if reader is None:
        return health.TikTokChat(channels=channels)
    reading, failing = dict(reader.reading), dict(reader.failing)
    return health.TikTokChat(
        channels=channels,
        recorded=reader.wanted,
        reading=tuple(sorted(reading.items())),
        waiting=tuple(sorted(reader.waiting)),
        failing=tuple(sorted((slug, why, since) for slug, (why, since) in failing.items())),
        last_heard_at=reader.last_heard_at,
    )


def _bot_account(platform: str) -> health.BotAccount:
    """As whom the bot writes on Kick or Twitch, as far as the service knows
    without asking: the account that signed in, and what it let the bot
    do."""
    if platform == platforms.TWITCH:
        if not settings.twitch:
            return health.BotAccount(platform, set_up=False)
        tokens, scope = _twitch_token_keeper(), twitch_auth.WRITE_SCOPE
    else:
        tokens, scope = _kick_bot_tokens(), kick_auth.WRITE_SCOPE
    token = tokens.account
    if token is None:
        # Why there is none, where a token was asked for and none could be
        # had: a sign-in that lapsed says so.
        return health.BotAccount(platform, sign_in=tokens.trouble)
    return health.BotAccount(platform, login=token.login, may_write=token.may(scope))


def _bot_facts(streamers) -> health.ChatBot:
    """Where the bot in chat stands, for the Health page: the channels it
    writes in the chat of, as which accounts."""
    bot = _bot()
    channels = []
    for row in streamers:
        owner = row["broadcaster_user_id"]
        if not _tuning_now(owner).bot_writes:
            continue
        written = bot.written.get(owner) or chat_bot.Written()
        channels.append(health.BotChannel(row["slug"], row["platform"], written.sent_at, written.trouble))
    return health.ChatBot(
        channels=tuple(channels),
        accounts=(_bot_account(platforms.KICK), _bot_account(platforms.TWITCH)),
        sign_in_url=KICK_SIGN_IN_PATH,
        redirect_uri=settings.kick_redirect_uri,
    )


def _youtube_facts(channels: int) -> health.YouTubeChat | None:
    """Where reading YouTube's chat stands, for the Health page: None while
    no YouTube channel is watched."""
    if not channels:
        return None
    reader = _youtube_reader
    if reader is None:
        return health.YouTubeChat(channels=channels)
    reading, unavailable, failing = dict(reader.reading), dict(reader.unavailable), dict(reader.failing)
    return health.YouTubeChat(
        channels=channels,
        reading=tuple(sorted(reading.items())),
        unavailable=tuple(sorted(unavailable.items())),
        failing=tuple(sorted((slug, why, since) for slug, (why, since) in failing.items())),
        last_heard_at=reader.last_heard_at,
    )


def _twitch_facts(channels: int) -> health.TwitchChat | None:
    """Where reading Twitch's chat stands, for the Health page: None while
    the service neither is set up for Twitch nor watches a Twitch channel."""
    if not settings.twitch:
        return health.TwitchChat(set_up=False, channels=channels) if channels else None
    reader = _twitch_reader
    if reader is None:
        return health.TwitchChat(set_up=True, channels=channels)
    return health.TwitchChat(
        set_up=True,
        channels=channels,
        account=reader.account,
        reading=len(reader.subscribed),
        connected_since=reader.connected_since,
        last_heard_at=reader.last_heard_at,
        trouble=reader.trouble,
        trouble_since=reader.trouble_since,
        needs_sign_in=reader.needs_sign_in,
        refused=tuple(reader.refused_channels()),
    )


def _page_context(conn, nav: str) -> dict:
    """What every dashboard page needs: which page it is, and the state of
    the service shown in the bar across its top."""
    streamers = get_streamers(conn)
    tracked = {row["slug"] for row in streamers if row["tracking_enabled"]}
    recording = recording_manager.recording_channels()
    watching = _flags.get("watching_enabled", True)
    now = _right_now(streamers)
    troubles = health.report(_health_facts(streamers, now["channels"]), _today())["troubles"]
    return {
        "nav": nav,
        "asset_version": _asset_version(),
        "watching_enabled": watching,
        "streamers": streamers,
        "now": now,
        # Beside "Health" in the bar: how much there is trouble with.
        "health_flag": health.flag_words(troubles),
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
        offset = dashboard_view.page_offset(offset, MOMENTS_PAGE_SIZE, counts[show])
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
                "paging": dashboard_view.pages(
                    offset,
                    MOMENTS_PAGE_SIZE,
                    counts[show],
                    lambda other: dashboard_view.review_url(show, channel, offset=other),
                ),
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
        moments = count_moments_by_channel(conn)
        recordings = Counter(row["broadcaster_user_id"] for row in get_stream_recordings(conn))
        context.update(
            {
                "watchlist": [
                    {
                        "slug": row["slug"],
                        "broadcaster_user_id": row["broadcaster_user_id"],
                        "platform": platforms.TITLES.get(row["platform"], row["platform"]),
                        "platform_user_id": row["platform_user_id"],
                        "added": dashboard_view.date_words(to_local_datetime(row["added_at"]).date(), today),
                        "now": context["now"]["channels"][row["slug"]],
                        "tracking_enabled": bool(row["tracking_enabled"]),
                        # Never tracked: there is no switch for it.
                        "recorded_only": row["platform"] in platforms.RECORDED_ONLY,
                        "always_record": bool(row["always_record"]),
                        # Its stream being kept whole, or the last such
                        # recording still being saved - None if neither.
                        "whole_stream": whole_streams.get(row["slug"]),
                        # What removing it can delete as well.
                        "kept": dashboard_view.kept_words(
                            moments.get(row["broadcaster_user_id"], 0), recordings[row["broadcaster_user_id"]]
                        ),
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


# What takes the room on the disk is found by walking the folders, which
# takes a moment once there are thousands of clips - and the Health page
# asks again every few seconds. So what was measured is kept for this long.
DISK_USE_SECONDS = 300
_disk_use: tuple[float, tuple[tuple[str, int], ...]] | None = None


def _size_of(path: Path) -> int:
    """How much room a file takes, or everything under a folder. What can't
    be looked at counts for nothing."""
    try:
        if not path.is_dir():
            return path.stat().st_size
    except OSError:
        return 0
    total = 0
    folders = [path]
    while folders:
        try:
            with os.scandir(folders.pop()) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            folders.append(Path(entry.path))
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _measure_disk_use() -> tuple[tuple[str, int], ...]:
    return (
        ("Clips", _size_of(CLIPS_DIR)),
        ("Stream recordings", _size_of(STREAMS_DIR)),
        ("Buffers clips are cut from", _size_of(recorder.RECORDINGS_DIR)),
        ("Database", _size_of(db.DB_PATH)),
        ("Logs", _size_of(LOG_FILE.parent)),
    )


async def _disk_use_now() -> tuple[tuple[str, int], ...]:
    global _disk_use
    if _disk_use is None or time.monotonic() - _disk_use[0] >= DISK_USE_SECONDS:
        _disk_use = (time.monotonic(), await asyncio.to_thread(_measure_disk_use))
    return _disk_use[1]


@app.get("/dashboard/health", response_class=HTMLResponse)
async def dashboard_health(request: Request):
    """The Health page: whether what the service depends on is there -
    webhooks arriving, Kick's chat subscriptions, the bot in chat, the
    recorders, room on the disk, the tunnel (see health.py). Its script
    fetches it again every few seconds and puts what it says now in place of
    what it said."""
    global _served_port, _tunnel_look
    server = request.scope.get("server")
    if server and server[1]:
        _served_port = server[1]
    if _funnel is None:
        await _look_at_tunnel()
    elif (datetime.now(timezone.utc) - _funnel[0]).total_seconds() >= TUNNEL_FRESH_SECONDS:
        # Looked at again behind the page rather than before it: a client
        # that takes its time to answer must not hold the page up.
        if _tunnel_look is None or _tunnel_look.done():
            _tunnel_look = asyncio.create_task(_look_at_tunnel())
    use = await _disk_use_now()

    conn = get_connection()
    try:
        context = _page_context(conn, "health")
        stored_chat = {}
        for row in context["streamers"]:
            span = get_chat_span(conn, row["broadcaster_user_id"])
            if span is not None:
                stored_chat[row["broadcaster_user_id"]] = to_local_datetime(span[1])
        facts = _health_facts(
            context["streamers"],
            context["now"]["channels"],
            stored_chat=stored_chat,
            drops=health.read_drops(get_subscription_drops(conn)),
            renames=health.read_renames(get_channel_renames(conn)),
            use=use,
        )
    finally:
        conn.close()

    report = health.report(facts, _today())
    context.update(
        {
            "health": report,
            "health_flag": health.flag_words(report["troubles"]),
            "log_file": LOG_FILE.as_posix(),
        }
    )
    return templates.TemplateResponse(request, "health.html", context)


# The tabs of the Settings page after the ones of its sections
# (tuning.GROUPS): where notifications go - on the page for all channels, as
# they are the service's - and trying what is in the form on stored chat.
SETTINGS_NOTIFICATIONS = "notifications"
SETTINGS_TRIAL = "trial"


@app.get("/dashboard/settings", response_class=HTMLResponse)
async def dashboard_settings(request: Request, channel: str | None = None, section: str | None = None):
    """The Settings page: what of detection and of clips can be set (see
    tuning.py) - for all channels, or with `channel` for one of the
    watchlist's, which then shows what it has of its own. Its sections are
    tabs, of which `section` is the one shown (the first, unless it names
    one the page has); the script keeps it in the address as they are
    switched, so the page fetched again after a save shows the same one."""
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
            mine = _own_values(conn, row)
            every, rest, rest_label = tuning.values(for_all, mine), tuning.values(for_all), "All channels"
            address = f"/channels/{quote(channel)}/tuning"

        sections = tuning.form(every, rest, rest_label, for_channel=row is not None)
        editable = row is None or own
        # There is chat to try them on once there is a channel to have it.
        trial = editable and bool(streamers)
        notifications = _notification_form() if row is None else None
        tabs = [
            *(item["key"] for item in sections),
            *([SETTINGS_NOTIFICATIONS] if notifications else []),
            *([SETTINGS_TRIAL] if trial else []),
        ]

        context = _page_context(conn, "settings")
        context.update(
            {
                "channel": channel,
                "channels": [
                    {"slug": streamer["slug"], "own": bool(streamer["own_tuning"])} for streamer in streamers
                ],
                "own": own,
                "editable": editable,
                "sections": sections,
                "trial": trial,
                "notifications": notifications,
                "shown": section if section in tabs else tabs[0],
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
                "bot": _bot_settings_words(row) if row is not None else None,
            }
        )
    finally:
        conn.close()

    return templates.TemplateResponse(request, "settings.html", context)


def _notification_form() -> dict:
    """The Settings page's Notifications tab: where notes go - each value
    and whether it was saved here or comes from .env, a secret only as
    whether one is set - and which kinds go out."""
    notes, saved, env = _notes(), _saved_setup(), _env_setup()
    groups = []
    for title, names in dashboard_view.NOTIFY_SETUP_GROUPS:
        fields = []
        for name in names:
            value = getattr(_note_setup, name)
            secret = name in notify.SECRETS
            fields.append(
                {
                    "name": name,
                    "label": notify.SETUP_LABELS[name],
                    "value": "" if secret else value,
                    "secret": secret,
                    "set": bool(value),
                    "saved": name in saved,
                    "placeholder": dashboard_view.setup_placeholder(name, bool(value), name in saved),
                    "caption": dashboard_view.setup_caption(name, bool(getattr(env, name)), name in saved),
                }
            )
        groups.append({"title": title, "fields": fields})
    going = [destination.name for destination in notes.destinations]
    return {
        "lead": dashboard_view.notify_lead_words(going),
        "going": bool(going),
        "groups": groups,
        "kinds": [
            {
                "name": name,
                "label": label,
                "caption": dashboard_view.NOTIFY_CAPTIONS[name],
                "enabled": _flags.get(SETTING_KEYS[name], SETTING_DEFAULTS[SETTING_KEYS[name]]),
            }
            for name, label in NOTIFY_SETTINGS
        ],
    }


def _bot_settings_words(row) -> dict:
    """What a channel's Settings say of the bot beside its own section: as
    whom it writes on the channel's platform - and, on Kick, where another
    account signs in."""
    platform = row["platform"]
    account = None if platform in health.NOT_WRITTEN else _bot_account(platform)
    kick = platform == platforms.KICK
    return {
        "words": health.bot_account_words(account, platform),
        "sign_in": (
            {"url": KICK_SIGN_IN_PATH, "words": "Sign in another account" if account.login else "Sign one in"}
            if kick
            else None
        ),
    }


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


def _recording_chat_lines(
    conn, row, parts: list[stream_recording.Part], first: float, last: float, limit: int
) -> list[dict]:
    """The chat lines that go with the stretch of a recording's file from
    its second `first` to its second `last`, each with the second of the
    file it belongs to - `limit` of them at most, the earliest. A message is
    put against the picture its writer was watching, as on the review page
    (see chat_trace.py). The channel's chat is told by the channel's id: its
    name on the watchlist, which is the one a recording has, is not the one
    chat is stored under."""
    delay = timedelta(seconds=_recording_chat_delay(conn, row))
    messages = get_channel_chat_between(
        conn,
        row["broadcaster_user_id"],
        stream_recording.broadcast_time(parts, first) + delay,
        stream_recording.broadcast_time(parts, last) + delay,
        limit,
    )
    chatters = {message["sender_username"] for message in messages if message["sender_username"]}
    return chat_trace.chat_lines(
        messages,
        lambda arrived: stream_recording.file_second(parts, arrived - delay),
        get_chat_emotes(conn, row["broadcaster_user_id"]),
        get_chat_identities(conn, row["broadcaster_user_id"], chatters),
        badges=get_channel_badges(conn, row["broadcaster_user_id"]),
    )


def _recording_chat(conn, row, parts: list[stream_recording.Part], at: float) -> dict:
    """The chat that goes with a stretch of a recording's file, around its
    second `at`: the lines (see _recording_chat_lines) and the stretch they
    cover."""
    whole = stream_recording.length(parts)
    at = min(max(at, 0.0), whole)
    first = max(at - RECORDING_CHAT_BEFORE_SECONDS, 0.0)
    last = min(at + RECORDING_CHAT_AFTER_SECONDS, whole)
    lines = _recording_chat_lines(conn, row, parts, first, last, RECORDING_CHAT_LIMIT)
    if len(lines) == RECORDING_CHAT_LIMIT:
        last = max(float(lines[-1]["at"]), at)
    return {
        "url": f"/dashboard/recordings/{row['id']}/chat",
        "first": dashboard_view.number_words(first, 1),
        "last": dashboard_view.number_words(last, 1),
        "whole": last >= whole,
        "lines": lines,
    }


# Messages in each step of the trace over the whole of a recording, by
# recording, with what they were counted under: going through the chat of a
# stream of hours takes a second or two, and none of it changes once the
# recording is over. A stretch of one is counted each time it is asked for -
# that is a few minutes of chat.
_recording_counts: dict[int, tuple[tuple, tuple[list[int], list[int]]]] = {}


def _recording_strip(
    row,
    parts: list[stream_recording.Part],
    first: float,
    last: float,
    delay_seconds: int,
    laugh_names: tuple[str, ...],
    laugh_patterns: tuple[str, ...],
) -> dict:
    """The strip over the stretch of a recording's file from its second
    `first` to its second `last` (see chat_trace.stretch_trace): what chat
    did there, where the file goes from one part to the next, and where the
    channel's moments were. Chat is placed as beside the recording, and a
    message from a break in the stream is not counted - the file has no
    picture for it to belong to. Goes through all the chat of the stretch,
    so it is run in a thread of its own, with a connection of its own."""
    whole = (first, last) == (0.0, stream_recording.length(parts))
    step = chat_trace.stretch_step(last - first)
    delay = timedelta(seconds=delay_seconds)
    arrived_from = stream_recording.broadcast_time(parts, first) + delay
    arrived_to = stream_recording.broadcast_time(parts, last) + delay
    counted_under = (row["parts"], delay_seconds, laugh_names, laugh_patterns)
    kept = _recording_counts.get(row["id"]) if whole else None
    conn = get_connection()
    try:
        if kept is not None and kept[0] == counted_under:
            counts = kept[1]
        else:
            counts = chat_trace.stretch_counts(
                iter_channel_chat_between(conn, row["broadcaster_user_id"], arrived_from, arrived_to),
                lambda arrived: stream_recording.second_in_file(parts, arrived - delay),
                first,
                last,
                step,
                laugh_names,
                laugh_patterns,
            )
            if whole:
                _recording_counts[row["id"]] = (counted_under, counts)
        windows = get_channel_moment_windows(conn, row["broadcaster_user_id"], arrived_from, arrived_to)
    finally:
        conn.close()
    moments = [
        (
            stream_recording.file_second(parts, datetime.fromisoformat(window["window_start"]) - delay),
            stream_recording.file_second(parts, datetime.fromisoformat(window["window_end"]) - delay),
        )
        for window in windows
    ]
    return chat_trace.stretch_trace(
        first,
        last,
        step,
        *counts,
        breaks=stream_recording.breaks(parts),
        moments=[(began, ended) for began, ended in moments if ended > began],
    )


# As _trims, for the stretches cut out of recordings: by recording id.
_recording_trims: dict[int, dict] = {}

# What the Trim dialog shows of a recording, whose strip can't hold hours at
# a width a handle could be set on: a window of at least this much footage
# around the stretch, which to begin with is this long either side of where
# the recording was being watched.
RECORDING_TRIM_WINDOW_SECONDS = 300
RECORDING_TRIM_AROUND_SECONDS = 30
# The most chat lines the chat of a stretch of a recording is made of: a
# few hours of a busy chat. Each is laid out once (see chat_video.py), but
# all of them at once, on one page of a browser.
RECORDING_CHAT_VIDEO_LIMIT = 50_000


def _stream_url(file: Path) -> str | None:
    """The address of a file under STREAMS_DIR, or None if it is not there."""
    if not file.exists():
        return None
    # Made again, it is a different file under the same name.
    return f"/streams/{quote(file.relative_to(STREAMS_DIR).as_posix())}?v={int(file.stat().st_mtime)}"


def _recording_cuts(file: Path) -> list[tuple[Path, tuple[int, int]]]:
    """The stretches cut out of a recording's file that are kept next to
    it, the newest first, each with the seconds of the recording it holds
    (see clip_trim.recording_video_name). There is one at most, unless
    removing the one before did not work."""
    if not file.parent.is_dir():
        return []
    cuts = []
    for path in file.parent.iterdir():
        stretch = clip_trim.recording_stretch(file.name, path.name)
        if stretch is not None:
            cuts.append((path, stretch))
    return sorted(cuts, key=lambda cut: cut[0].stat().st_mtime, reverse=True)


def _recording_cut_files(file: Path) -> tuple[Path, ...]:
    """Every file that is a stretch cut out of a recording's file, and where
    the chat video of each is or would be."""
    videos = [video for video, _stretch in _recording_cuts(file)]
    return (
        *videos,
        *(video.with_name(chat_video.video_name(video.name)) for video in videos),
        *(_cut_record_path(video) for video in videos),
    )


def _recording_trim(row) -> dict:
    """Where the stretch cut out of a recording stands (see _trim_standing)."""
    file = _stream_file(row)
    known = _recording_trims.get(row["id"], {})
    cuts = _recording_cuts(file) if file is not None else []
    if not cuts:
        return _trim_standing(known, None, None)
    video, stretch = cuts[0]
    chat = video.with_name(chat_video.video_name(video.name))
    record = _cut_record(video)
    held = _held(record, 0.0)
    # Where it is known to the tenth of a second, the stretch is not also
    # named by the seconds in the file's name.
    return _trim_standing(
        known, _stream_url(video), _stream_url(chat), None if held else stretch, held, bool(record and record.burned)
    )


def _recording_has_chat(conn, row, parts: list[stream_recording.Part]) -> bool:
    """Whether any chat was stored for a recording."""
    delay = timedelta(seconds=_recording_chat_delay(conn, row))
    first, last = parts[0].started_at + delay, parts[-1].ended_at + delay
    return bool(get_channel_chat_between(conn, row["broadcaster_user_id"], first, last, 1))


def _recording_trim_dialog(conn, row, parts: list[stream_recording.Part], video_url: str) -> dict:
    """What the Trim dialog of a recording is told (see _trim.html)."""
    recording_id = row["id"]
    return {
        "id": f"recording-{recording_id}",
        "url": f"/recordings/{recording_id}/trim",
        "video_url": video_url,
        # Times in it are seconds of the file, which begins where it begins.
        "lead": "0",
        "last": dashboard_view.number_words(stream_recording.length(parts), 2),
        "chat": _recording_has_chat(conn, row, parts),
        "chat_rest_words": "Chat where it is beside the recording",
        "time_hint": "A time in the recording, written like 1:23:45 or 83:45.5.",
        "window": {
            "seconds": RECORDING_TRIM_WINDOW_SECONDS,
            "around": RECORDING_TRIM_AROUND_SECONDS,
            "strip_url": f"/dashboard/recordings/{recording_id}/trace",
            "chat_url": f"/dashboard/recordings/{recording_id}/chat",
            "chat_most": dashboard_view.duration_words(clip_trim.CHAT_VIDEO_MAX_SECONDS),
        },
        **_trim_dialog(),
    }


# -- the chat of a whole recording ---------------------------------------------
#
# Beside a recording's Download is Download chat: its chat as a video as long
# as the recording, on the colour the Trim dialog starts out with, to put next
# to the picture in an editor. It is made the first time it is asked for -
# chat is laid out once (see chat_video.py), so an hour of stream takes a few
# minutes - and kept next to the recording's file, which it is removed with.
# Not see-through: that would take gigabytes an hour (see
# clip_trim.CHAT_VIDEO_MAX_SECONDS), where on a colour it is tens of megabytes.

# Where making it stands, by recording, while it is made or after it failed.
# Once it is there, the file says so.
_recording_chat_files: dict[int, dict] = {}

# What the chat video of a recording is taken to need of the disk, a second
# of the recording - a busy chat on a colour comes to about 20 kB, measured.
RECORDING_CHAT_FILE_BYTES_PER_SECOND = 100_000


def _recording_chat_file_paths(file: Path) -> tuple[Path, Path]:
    """Where the chat video of a recording's file is kept, and where it is
    written while it is made - so that one cut short is never taken for
    it."""
    chat = file.with_name(chat_video.video_name(file.name))
    return chat, chat.with_name(f"{chat.stem}.part{chat.suffix}")


def _recording_chat_file(conn, row, parts: list[stream_recording.Part], file: Path | None) -> dict:
    """What Download chat on the Recordings page is told of a recording
    (see _recording_chat_file.html)."""
    recording_id = row["id"]
    known = _recording_chat_files.get(recording_id, {})
    state = known.get("state", dashboard_view.CHAT_FILE_NONE)
    download_url = None
    if state != dashboard_view.CHAT_FILE_MAKING and file is not None:
        download_url = _stream_url(_recording_chat_file_paths(file)[0])
        if download_url:
            state = dashboard_view.CHAT_FILE_READY
    return {
        "url": f"/recordings/{recording_id}/chat_video",
        "page_url": f"/dashboard/recordings/{recording_id}/chat_video",
        "state": state,
        "busy": state == dashboard_view.CHAT_FILE_MAKING,
        "download_url": download_url,
        "chat": bool(parts) and _recording_has_chat(conn, row, parts),
        "words": dashboard_view.chat_file_words(state, known.get("done")),
    }


async def _recording_chat_file_background(
    recording_id: int, channel: str, lines: list[dict], duration: float, chat: Path, partial: Path
) -> None:
    # Off the event loop, and as a tracked task, like the chat of a cut
    # (see _trim_chat_background): minutes for a long recording.
    what = f"stream recording {recording_id}"
    try:
        partial.unlink(missing_ok=True)
        progress = _making(_recording_chat_files, recording_id, dashboard_view.CHAT_FILE_MAKING)
        frames = await asyncio.to_thread(
            chat_video.render,
            chat_video.lines_from(lines, 0.0),
            duration,
            partial,
            clip_trim.DEFAULT_CHAT_BACKGROUND,
            progress,
        )
        partial.replace(chat)
    except _GivenUp:
        partial.unlink(missing_ok=True)
        _recording_chat_files.pop(recording_id, None)
        logger.info("[%s] making the chat video of %s given up: shutting down", channel, what)
        return
    except Exception:
        logger.exception("[%s] making the chat video of the whole of %s failed", channel, what)
        partial.unlink(missing_ok=True)
        _recording_chat_files[recording_id] = {"state": dashboard_view.CHAT_FILE_FAILED}
        return
    _recording_chat_files.pop(recording_id, None)
    logger.info("[%s] chat video of the whole of %s saved: %s (%d frames)", channel, what, chat.name, frames)


# -- moments found in a recording after the fact ------------------------------
#
# The chat stored with a recording can be replayed through the detector once
# the stream is over (see recording_moments.py). The Recordings page lists
# what would have been a moment under the channel's settings as they are
# saved, plays the recording from any of them, and makes the ones asked for
# into moments, each with a file cut out of the recording's.

# Where that stands, by recording: its chat being replayed, what was found
# and when, clips being cut, or that it failed. A restart forgets what was
# found; looking again finds it again.
_recording_finds: dict[int, dict] = {}


class _GivenUp(Exception):
    """Work that was not seen through - a replay, chat being made for a
    cut: the service is on its way down."""


def _look_through(
    broadcaster_user_id: int, parts: list[stream_recording.Part], tuned: tuning.Tuning, standing: dict
) -> dict:
    """What replaying a recording's chat under `tuned` comes to: the moments
    found in its file, how many more the file has too little footage of,
    and how many messages were replayed. How far it has got is kept in
    `standing` as it goes, for the page to say. Runs in a thread of its own,
    so with a connection of its own - and gives up (_GivenUp) once the
    service is shutting down, which would otherwise wait minutes for it."""
    conn = get_connection()
    try:
        messages, keywords = replay.load_between(conn, broadcaster_user_id, *recording_moments.chat_span(parts))
    finally:
        conn.close()
    standing["of"] = len(messages)

    def progress(done: int) -> None:
        if _shutdown_requested:
            raise _GivenUp
        standing["done"] = done

    found, missing = recording_moments.find(replay.replay(messages, keywords, tuned, progress), parts, tuned)
    return {"found": found, "missing": missing, "messages": len(messages)}


async def _find_moments_background(
    recording_id: int, channel: str, broadcaster_user_id: int, parts: list[stream_recording.Part], tuned: tuning.Tuning
) -> None:
    # The replay of a long stream with a busy chat takes minutes (see
    # replay.PROGRESS_EVERY): off the event loop, and as a tracked task, so
    # that the page can come and go while it runs.
    global _replay_running
    standing = _recording_finds[recording_id]
    try:
        result = await asyncio.to_thread(_look_through, broadcaster_user_id, parts, tuned, standing)
    except _GivenUp:
        _recording_finds.pop(recording_id, None)
        logger.info("[%s] looking for moments in stream recording %d given up: shutting down", channel, recording_id)
        return
    except Exception:
        logger.exception("[%s] looking for moments in stream recording %d failed", channel, recording_id)
        _recording_finds[recording_id] = {"state": dashboard_view.FIND_FAILED}
        return
    finally:
        _replay_running = False
    # Each moment found is kept with where its clip lies, framed by the
    # settings the chat was replayed under: what is saved later changes
    # nothing of it until the recording is looked through again.
    _recording_finds[recording_id] = {
        "state": dashboard_view.FIND_FOUND,
        **result,
        "looked": datetime.now(timezone.utc),
    }
    logger.info(
        "[%s] stream recording %d: %d moment(s) found in %d messages, %d more without footage",
        channel, recording_id, len(result["found"]), result["messages"], result["missing"],
    )


def _moment_from_recording(
    recording_id: int, broadcaster_user_id: int, channel: str, file: Path, found: recording_moments.Found
) -> tuple[int, Path, ClipExtent]:
    """Makes a moment of one found in a recording, and returns its id, its
    file and where the clip lies in that. The file is cut out of the
    recording's, clip and context in one as for any moment, and the moment
    is stored as the service stores one it detected - with what the replay
    measured, at the time it would have fired.

    The file is cut first, so a cut that fails leaves no moment behind, and
    a moment whose file can then not be put in its place is taken back: it
    is all of a moment or none of it. Blocks for as long as the cut takes:
    run in a thread of its own, hence the connection of its own."""
    cut = CLIPS_DIR / channel / f".recording_{recording_id}_{found.key}.mp4"
    try:
        begins, duration = recorder.trim_clip(file, *found.footage, cut)
        before, length, after = recording_moments.extent(found, begins, duration)
        spike = found.spike
        conn = get_connection()
        try:
            moment_id = insert_moment(
                conn,
                broadcaster_user_id=broadcaster_user_id,
                channel_slug=channel,
                window_start=found.window_start.isoformat(),
                window_end=found.window_end.isoformat(),
                reason=",".join(spike.reasons),
                score=spike.score,
                message_count=spike.message_count,
                baseline_message_rate=spike.baseline_message_rate,
                current_message_rate=spike.current_message_rate,
                emote_count=spike.emote_count,
                keyword_hits=spike.keyword_hits,
                detected_at=found.fired_at.isoformat(),
            )
            clip = cut.with_name(f"moment_{moment_id}.mp4")
            try:
                cut.replace(clip)
                update_moment_clip_path(
                    conn,
                    moment_id,
                    clip.relative_to(CLIPS_DIR).as_posix(),
                    clip_start=found.clip_started_at.isoformat(),
                    clip_duration=length,
                    context_before=before,
                    context_after=after,
                )
            except Exception:
                delete_moment(conn, moment_id)
                clip.unlink(missing_ok=True)
                raise
        finally:
            conn.close()
    finally:
        cut.unlink(missing_ok=True)
    return moment_id, clip, (before, length, after)


async def _make_moments_background(
    recording_id: int, broadcaster_user_id: int, channel: str, file: Path, chosen: list[recording_moments.Found]
) -> None:
    # One cut after another, each off the event loop, and as a tracked task
    # so a shutdown waits for them. How far it has got is kept with what was
    # found, which is what the page asks about.
    standing = _recording_finds[recording_id]
    made: list[tuple[int, Path, ClipExtent]] = []
    for found in chosen:
        try:
            made.append(
                await asyncio.to_thread(
                    _moment_from_recording, recording_id, broadcaster_user_id, channel, file, found
                )
            )
        except Exception:
            logger.exception(
                "[%s] the moment at %s of stream recording %d could not be made",
                channel, dashboard_view.stream_time_words(found.clip[0]), recording_id,
            )
        else:
            moment_id, clip, (before, length, after) = made[-1]
            logger.info(
                "[%s] moment %d made from stream recording %d: %s (%.0f s, with %.0f s before it and %.0f s after)",
                channel, moment_id, recording_id, clip, length, before, after,
            )
        standing["done"] += 1
    standing.update(state=dashboard_view.FIND_FOUND, made=len(made), failed=len(chosen) - len(made))
    if made and _analysis_steps():
        _track_task(_analyse_in_turn(channel, made))


async def _analyse_in_turn(channel: str, clips: list[tuple[int, Path, ClipExtent]]) -> None:
    # A clip at a time: each analysis step keeps a processor busy, and a
    # recording can give dozens of clips at once.
    for moment_id, clip, extent in clips:
        await asyncio.gather(*(analyse(moment_id, channel, clip, extent) for analyse in _analysis_steps()))


def _recording_finding(conn, row, parts: list[stream_recording.Part], today: date) -> dict:
    """What the Recordings page shows of the moments looked for in a
    recording (see _recording_moments.html): where that stands, in words,
    and the moments found - where in the file each is, what set it off, and
    the moment it is, if it is one already. That last is looked up every
    time, so a moment the service made of it while the stream was on counts
    as much as one made from here."""
    recording_id = row["id"]
    known = _recording_finds.get(recording_id, {})
    state = known.get("state", dashboard_view.FIND_NONE)
    found: list[recording_moments.Found] = known.get("found", [])
    rows = []
    if found:
        delay = timedelta(seconds=_recording_chat_delay(conn, row))
        windows = get_channel_moment_windows(
            conn, row["broadcaster_user_id"], found[0].window_start, max(one.window_end for one in found)
        )
        for one in found:
            moment_id = recording_moments.moment_of(one, windows)
            rows.append(
                {
                    "key": one.key,
                    # Where its clip begins, which is where the recording is
                    # played from, and where its reaction shows on the trace:
                    # placed as the trace places a moment's.
                    "at": dashboard_view.number_words(one.clip[0], 1),
                    "time": dashboard_view.stream_time_words(one.clip[0]),
                    "began": dashboard_view.number_words(
                        stream_recording.file_second(parts, one.window_start - delay), 1
                    ),
                    "ended": dashboard_view.number_words(
                        stream_recording.file_second(parts, one.window_end - delay), 1
                    ),
                    "words": dashboard_view.found_words(
                        ",".join(one.spike.reasons), one.spike.score, one.clip_seconds
                    ),
                    "moment": (
                        {
                            "id": moment_id,
                            "href": dashboard_view.review_url(dashboard_view.SHOW_ALL, moment=moment_id),
                        }
                        if moment_id is not None
                        else None
                    ),
                }
            )
    new = sum(1 for item in rows if item["moment"] is None)
    looked = known.get("looked")
    return {
        "url": f"/recordings/{recording_id}/moments",
        "page_url": f"/dashboard/recordings/{recording_id}/moments",
        "trace_url": f"/dashboard/recordings/{recording_id}/trace",
        "state": state,
        "busy": state in dashboard_view.FIND_BUSY,
        "looked_before": state in (dashboard_view.FIND_FOUND, dashboard_view.FIND_FAILED),
        "words": dashboard_view.find_words(
            state,
            found=len(rows),
            already=len(rows) - new,
            missing=known.get("missing", 0),
            messages=known.get("messages", 0),
            looked=looked.astimezone() if looked else None,
            today=today,
            done=known.get("done", 0),
            of=known.get("of", 0),
            made=known.get("made", 0),
            failed=known.get("failed", 0),
        ),
        "rows": rows,
        "new": new,
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
    video_url = f"/streams/{quote(row['file_path'])}" if standing == "playable" else None
    return {
        "id": row["id"],
        "channel": row["channel_slug"],
        "standing": standing,
        "when": dashboard_view.recording_span_words(
            started, to_local_datetime(row["ended_at"]) if row["ended_at"] else None, today
        ),
        "length": dashboard_view.stream_time_words(row["duration"]),
        "words": not_over["words"] if not_over else None,
        "video_url": video_url,
        # What chat did over all of it is counted once the page is there
        # (see dashboard_recording_trace): the script fetches it from here.
        "trace_url": f"/dashboard/recordings/{row['id']}/trace" if video_url else None,
        "trim": _recording_trim_dialog(conn, row, parts, video_url) if video_url else None,
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
        "finding": _recording_finding(conn, row, parts, today) if video_url else None,
        "chat_file": _recording_chat_file(conn, row, parts, file) if video_url else None,
    }


@app.get("/dashboard/recordings", response_class=HTMLResponse)
async def dashboard_recordings(request: Request, recording: int | None = None, offset: int | None = None):
    """The Recordings page: the streams that were kept whole, the newest
    first and a page of them at a time from `offset` on, and the one that is
    open - its video, with the chat that went with it replayed beside it.
    `recording` is the one to open; left out, it is the first of the page.
    With no `offset`, the page is the one that lists the recording to open."""
    today = _today()
    conn = get_connection()
    try:
        opened = get_stream_recording(conn, recording) if recording is not None else None
        total = count_stream_recordings(conn)
        if offset is None:
            offset = get_stream_recording_offset(conn, opened["id"]) if opened is not None else 0
        offset = dashboard_view.page_offset(offset, RECORDINGS_PAGE_SIZE, total)
        rows = get_stream_recordings(conn, limit=RECORDINGS_PAGE_SIZE, offset=offset)
        not_over = _stream_recordings_not_over()
        if opened is None and rows:
            opened = rows[0]
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
                        "href": dashboard_view.recordings_url(row["id"], offset=offset),
                        "current": opened is not None and row["id"] == opened["id"],
                    }
                    for row in rows
                ],
                "recording": (
                    _open_recording(conn, opened, today, not_over.get(opened["id"])) if opened is not None else None
                ),
                "paging": dashboard_view.pages(
                    offset, RECORDINGS_PAGE_SIZE, total, lambda other: dashboard_view.recordings_url(offset=other)
                ),
                "mark": _recordings_mark(not_over, total),
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


@app.get("/dashboard/recordings/{recording_id}/trace", response_class=HTMLResponse)
async def dashboard_recording_trace(
    request: Request, recording_id: int, first: float = 0.0, last: float | None = None
):
    """What chat did over a recording, as the strip the Recordings page has
    under its video - or, with `first` and `last`, over the stretch of the
    file between those two seconds, which is what the Trim dialog draws its
    own strip from. The page fetches it once it is there: going through the
    chat of a stream of hours takes a moment."""
    conn = get_connection()
    try:
        row = get_stream_recording(conn, recording_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown recording {recording_id}")
        parts = stream_recording.decoded(row["parts"])
        if not parts:
            raise HTTPException(status_code=409, detail="the recording has no footage to put chat against yet")
        whole = stream_recording.length(parts)
        first = min(max(first, 0.0), whole) if math.isfinite(first) else 0.0
        last = min(max(last, 0.0), whole) if last is not None and math.isfinite(last) else whole
        if last - first < clip_trim.MIN_SECONDS:
            # Nothing to draw a stretch like that on: all of it, then.
            first, last = 0.0, whole
        # What tells a laughing message is looked up here, where what is
        # kept of the channel's settings may be touched.
        tuned = _tuning(conn, row["broadcaster_user_id"])
        laugh_patterns = tuple(tuned.detector.added_laugh_emote_patterns)
        laugh_names = tuple(
            detector.laugh_emote_names(_channel_keywords(conn, row["broadcaster_user_id"]), laugh_patterns)
        )
        delay = _recording_chat_delay(conn, row)
    finally:
        conn.close()
    strip = await asyncio.to_thread(_recording_strip, row, parts, first, last, delay, laugh_names, laugh_patterns)
    return templates.TemplateResponse(
        request, "_recording_trace.html", {"strip": strip, "asset_version": _asset_version()}
    )


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
    badges = get_channel_badges(conn, owner)
    for rows in tables:
        for row in rows:
            row["who"] = chat_stats.chatter_name(row["name"], identities, badges)


def _analyze_lines(conn, owner: int, rows, emotes, today: date, url, asked: str = "") -> list[dict]:
    """Stored messages as the page lists them: a line of chat each, with
    when it arrived, where its writer's other messages are and where the
    chat around it is. The one whose surroundings were asked for (`asked`,
    its id) is marked."""
    chatters = {row["sender_username"] for row in rows if row["sender_username"]}
    lines = chat_trace.chat_lines(
        rows,
        lambda arrived: 0.0,
        emotes,
        get_chat_identities(conn, owner, chatters),
        badges=get_channel_badges(conn, owner),
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
    offset = dashboard_view.page_offset(asked.offset, size, total)
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
        # Another page opens at its messages, not at what is said of the
        # chatter above them.
        "paging": dashboard_view.pages(
            offset, size, total, lambda other: f"{page(offset=other)}#{chat_stats.MESSAGES_ANCHOR}"
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


def _moment_files(row) -> list[Path]:
    """Every file a moment has, or may have, next to its clip: the clip,
    what was cut out of it with Trim and the chat video that went with
    that, what was kept of the cut, and the context clips of a moment from
    before a moment had one file. Only within the clips folder, whatever
    the row says."""
    if not row["clip_path"]:
        return []
    clip = CLIPS_DIR / row["clip_path"]
    if not clip.resolve().is_relative_to(CLIPS_DIR.resolve()):
        return []
    video, chat = (CLIPS_DIR / path for path in _trim_paths(row["clip_path"]))
    context = [clip.with_name(name) for name in recorder.context_clip_names(clip.name).values()]
    return [clip, video, chat, _cut_record_path(video), *context]


def _moment_busy(moment_id: int) -> str | None:
    """Why a moment can't be removed right now - something is being made of
    its file - or None if it can be."""
    if _trims.get(moment_id, {}).get("state") in dashboard_view.TRIM_BUSY:
        return "A stretch of this moment is being cut out. Try again once that is done."
    if _transcripts.get(moment_id, {}).get("state") == dashboard_view.TRANSCRIPT_MAKING:
        return "This moment is being transcribed. Try again once that is done."
    return None


def _remove_moment(conn, row) -> None:
    """Removes a moment for good: its files, then its row. Raises OSError,
    with the row left where it is, if a file can't be removed - on Windows,
    one that something still has open."""
    for file in _moment_files(row):
        file.unlink(missing_ok=True)
    delete_moment(conn, row["id"])
    _trims.pop(row["id"], None)
    _transcripts.pop(row["id"], None)


@app.post("/moments/{moment_id}/delete")
async def remove_moment(moment_id: int):
    """Removes a moment for good - its clip, whatever was cut out of it,
    and its row. A moment whose clip is still being cut can be removed too:
    the clip is thrown away once it is there (see _create_clip_background)."""
    conn = get_connection()
    try:
        row = get_moment(conn, moment_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown moment {moment_id}")
        if (busy := _moment_busy(moment_id)) is not None:
            raise HTTPException(status_code=409, detail=busy)
        try:
            _remove_moment(conn, row)
        except OSError:
            logger.warning("moment %d could not be removed", moment_id, exc_info=True)
            raise HTTPException(
                status_code=409, detail="The clip could not be removed. Something may still have its file open."
            ) from None
    finally:
        conn.close()
    logger.info("[%s] moment %d deleted", row["channel_slug"], moment_id)
    return {"deleted": moment_id}


async def _asked_trim(request: Request) -> dict:
    """What a Trim dialog asks to have cut, as it was posted - unless the
    service is on its way down, or what was posted says nothing of the
    kind."""
    if _shutdown_requested:
        raise HTTPException(status_code=409, detail="The service is shutting down.")
    try:
        asked = await request.json()
    except ValueError:
        asked = None
    if not isinstance(asked, dict):
        raise HTTPException(status_code=400, detail="Expected start, end, chat and chat_where as JSON.")
    return asked


@app.post("/moments/{moment_id}/trim")
async def trim_moment(moment_id: int, request: Request):
    """Starts cutting a stretch of a moment's footage out as a video of its
    own (see clip_trim.py), and with `chat` its chat (chat_video.py): drawn
    into its picture if `chat_where` says "video" - in `chat_box`, its
    left, top, width and height as shares of the picture, or at the bottom
    on the right without one - otherwise a video of its own to lay over it.
    `start` and `end` are clip time; `chat_shift` moves chat
    that many seconds later than the channel's chat delay has it,
    `chat_background` ("#rrggbb") puts a colour behind it instead of
    nothing. Its animated emotes move. What was cut
    for the moment before is replaced."""
    asked = await _asked_trim(request)
    conn = get_connection()
    try:
        row = get_moment(conn, moment_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown moment {moment_id}")
        row = await _with_clip_length(conn, row)
        if not row["clip_path"] or not row["clip_duration"] or not (CLIPS_DIR / row["clip_path"]).exists():
            raise HTTPException(status_code=409, detail="The moment has no clip to trim.")
        try:
            span = clip_trim.file_span(row, asked.get("start"), asked.get("end"))
            look = clip_trim.chat_look(asked)
            if asked.get("chat"):
                clip_trim.with_chat(span[1] - span[0], look.where, look.background)
        except clip_trim.TrimError as e:
            raise _trim_refused(e) from None
        lines = None
        if asked.get("chat"):
            _strip, lines = _chat_against_clip(conn, row, _chat_delay(conn, row["channel_slug"]))
            if not lines:
                raise HTTPException(status_code=409, detail="No chat was stored for this moment.")
    finally:
        conn.close()

    if _trims.get(moment_id, {}).get("state") not in dashboard_view.TRIM_BUSY:
        _trims[moment_id] = {"state": _first_trim_state(lines, look.where)}
        video, chat = (CLIPS_DIR / path for path in _trim_paths(row["clip_path"]))
        _track_task(
            _trim_background(
                _trims,
                moment_id,
                f"moment {moment_id}",
                row["channel_slug"],
                CLIPS_DIR / row["clip_path"],
                span,
                row["context_before"] or 0.0,
                lines,
                look,
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
            "subtitles_url": None,
            "cut": None,
            "chat_later": False,
        }
    return {"moment_id": moment_id, **_trim(row)}


def _no_cut_record() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail="Where this video begins was not kept when it was cut, so its chat can't be made now. Export it again.",
    )


def _asked_chat_look(asked: dict, seconds: float) -> clip_trim.ChatLook:
    """How the chat of a cut `seconds` long that is there already is to
    be, as asked for (see clip_trim.chat_look), refused in words where it
    can't."""
    try:
        look = clip_trim.chat_look(asked)
        clip_trim.with_chat(seconds, look.where, look.background)
    except clip_trim.TrimError as e:
        raise _trim_refused(e) from None
    return look


def _first_trim_state(lines: list[dict] | None, where: str) -> str:
    """What a cut that is asked for is doing to begin with: being cut - or,
    with its chat to be drawn into it, made together with that."""
    if lines is not None and where != clip_trim.CHAT_APART:
        return dashboard_view.TRIM_BURNING
    return dashboard_view.TRIM_CUTTING


def _burned_already() -> HTTPException:
    return HTTPException(status_code=409, detail="This video has its chat in it already.")


def _chat_later(
    standing: dict[int, dict],
    key: int,
    what: str,
    channel: str,
    video: Path,
    chat: Path,
    record: clip_trim.Cut,
    lines: list[dict],
    start: float,
    look: clip_trim.ChatLook,
) -> None:
    """Starts making the chat of a cut video that is there (see
    trim_moment_chat): `record` is what was kept of it, `start` where its
    first frame is on the time its chat `lines` are placed on."""
    if look.where == clip_trim.CHAT_APART:
        early = standing.get(key, {}).get("early", 0.0)
        standing[key] = {"state": dashboard_view.TRIM_CHAT, "early": early}
        task = _trim_chat_background(standing, key, what, channel, lines, start, record.duration, look, chat, early)
    else:
        standing[key] = {"state": dashboard_view.TRIM_BURNING, "done": 0.0}
        task = _burn_background(
            standing, key, what, channel, source=video, first=0.0, cut=record, start=start,
            lines=lines, look=look, video=video, old=(chat,),
        )
    _track_task(task)


@app.post("/moments/{moment_id}/trim/chat")
async def trim_moment_chat(moment_id: int, request: Request):
    """Starts making the chat of the video already cut out of a moment's
    footage, for an editor who took the video without it at first: for the
    stretch that video holds, as trim_moment would have made it then.
    `chat_where`, `chat_shift` and `chat_background` are as there - drawn
    into the picture, the chat takes the place of the video there was once
    it is made."""
    asked = await _asked_trim(request)
    conn = get_connection()
    try:
        row = get_moment(conn, moment_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown moment {moment_id}")
        row = await _with_clip_length(conn, row)
        if not row["clip_path"] or not row["clip_duration"]:
            raise HTTPException(status_code=409, detail="The moment has no clip to trim.")
        video, chat = (CLIPS_DIR / path for path in _trim_paths(row["clip_path"]))
        if _trims.get(moment_id, {}).get("state") in dashboard_view.TRIM_BUSY:
            return {"moment_id": moment_id, **_trim(row)}
        if not video.exists():
            raise HTTPException(status_code=409, detail="Nothing has been cut out of this moment yet.")
        record = _cut_record(video)
        if record is None:
            raise _no_cut_record()
        if record.burned:
            raise _burned_already()
        look = _asked_chat_look(asked, record.duration)
        _strip, lines = _chat_against_clip(conn, row, _chat_delay(conn, row["channel_slug"]))
        if not lines:
            raise HTTPException(status_code=409, detail="No chat was stored for this moment.")
    finally:
        conn.close()

    # Chat lines are placed in clip time.
    start = record.begins - (row["context_before"] or 0.0)
    what = f"moment {moment_id}"
    channel = row["channel_slug"]
    _chat_later(_trims, moment_id, what, channel, video, chat, record, lines, start, look)
    return {"moment_id": moment_id, **_trim(row)}


@app.get("/moments/{moment_id}/trim/{what}")
async def download_trim(moment_id: int, what: str):
    """The video cut out of a moment's footage (`video`), its chat video
    (`chat`) or the moment's transcript as subtitles timed to that video
    (`subtitles`), to save. Downloading any of them marks the moment as
    taken by an editor, which the review queue shows."""
    if what not in ("video", "chat", "subtitles"):
        raise HTTPException(status_code=404, detail=f"no such file of a cut: {what}")
    conn = get_connection()
    try:
        row = get_moment(conn, moment_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown moment {moment_id}")
        file = None
        if row["clip_path"]:
            video, chat = _trim_paths(row["clip_path"])
            file = CLIPS_DIR / (chat if what == "chat" else video)
        if file is None or not file.exists():
            raise HTTPException(status_code=404, detail="That file has not been made.")
        answer = _cut_subtitles(row, file) if what == "subtitles" else FileResponse(file, filename=file.name)
        update_moment_downloaded(conn, moment_id, datetime.now(timezone.utc).isoformat())
    finally:
        conn.close()
    return answer


def _cut_subtitles(row, video: Path) -> Response:
    """A moment's transcript as subtitles timed to the video cut out of its
    footage: from where that video really begins, for as long as it runs
    (see clip_trim.cut_record)."""
    transcript = subtitles.read(row["subtitles"])
    if transcript is None:
        raise HTTPException(status_code=404, detail="The moment has no transcript yet.")
    record = _cut_record(video)
    if record is None:
        raise HTTPException(
            status_code=409,
            detail="Where this video begins was not kept when it was cut, so subtitles can't be timed to it. "
            "Export it again.",
        )
    return _subtitles_file(transcript, record.begins - (row["context_before"] or 0.0), record.duration, video.name)


@app.post("/moments/{moment_id}/subtitles")
async def transcribe_moment(moment_id: int):
    """Starts writing down what is said in a moment's footage, line by line
    with its times (see subtitles.py): all of its file, the clip and the
    footage either side of it, to be corrected on the review page and taken
    into an editor as subtitles. Asked again while that goes on, nothing
    more is started; a transcript that is there is kept."""
    if _shutdown_requested:
        raise HTTPException(status_code=409, detail="The service is shutting down.")
    conn = get_connection()
    try:
        row = get_moment(conn, moment_id)
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown moment {moment_id}")
    if not row["clip_path"] or not (CLIPS_DIR / row["clip_path"]).exists():
        raise HTTPException(status_code=409, detail="The moment has no clip to transcribe.")
    if subtitles.read(row["subtitles"]) is not None:
        raise HTTPException(status_code=409, detail="The moment has its transcript already.")
    if _transcripts.get(moment_id, {}).get("state") != dashboard_view.TRANSCRIPT_MAKING:
        _transcripts[moment_id] = {"state": dashboard_view.TRANSCRIPT_MAKING}
        _track_task(
            _transcribe_background(
                moment_id, row["channel_slug"], CLIPS_DIR / row["clip_path"], row["context_before"] or 0.0
            )
        )
    known = _transcripts[moment_id]
    return {
        "moment_id": moment_id,
        "state": known["state"],
        "words": dashboard_view.transcript_words(known["state"], known.get("done")),
    }


@app.get("/moments/{moment_id}/subtitles")
async def download_subtitles(moment_id: int):
    """A moment's transcript as subtitles timed to its whole file - the
    clip and the footage either side of it - to save beside that."""
    conn = get_connection()
    try:
        row = get_moment(conn, moment_id)
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown moment {moment_id}")
    transcript = subtitles.read(row["subtitles"])
    if transcript is None or not row["clip_path"]:
        raise HTTPException(status_code=404, detail="The moment has no transcript yet.")
    return _subtitles_file(transcript, -(row["context_before"] or 0.0), None, Path(row["clip_path"]).name)


@app.post("/moments/{moment_id}/subtitles/{line}")
async def correct_transcript(moment_id: int, line: int, request: Request):
    """Puts a line of a moment's transcript right: `text` is what it says
    instead (see subtitles.corrected). It keeps the times it was heard at;
    left empty, it is left out of the subtitles."""
    try:
        asked = await request.json()
    except ValueError:
        asked = None
    text = asked.get("text") if isinstance(asked, dict) else None
    if not isinstance(text, str):
        raise HTTPException(status_code=400, detail="Expected the line's text as JSON.")
    conn = get_connection()
    try:
        row = get_moment(conn, moment_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown moment {moment_id}")
        transcript = subtitles.read(row["subtitles"])
        if transcript is None:
            raise HTTPException(status_code=409, detail="The moment has no transcript yet.")
        try:
            transcript = subtitles.corrected(transcript, line, text)
        except IndexError:
            raise HTTPException(status_code=404, detail=f"The transcript has no line {line}.") from None
        update_moment_subtitles(conn, moment_id, subtitles.stored(transcript))
    finally:
        conn.close()
    return {"moment_id": moment_id, "line": line, "text": transcript[line].text}


@app.get("/dashboard/moments/{moment_id}/subtitles", response_class=HTMLResponse)
async def dashboard_transcript(request: Request, moment_id: int):
    """A moment's transcript as the review page shows it beside the clip,
    on its own - what the page fetches after asking for one to be made, and
    every couple of seconds while it is."""
    conn = get_connection()
    try:
        row = get_moment(conn, moment_id)
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown moment {moment_id}")
    if not row["clip_path"]:
        raise HTTPException(status_code=404, detail="The moment has no clip, so nothing to transcribe.")
    return templates.TemplateResponse(request, "_transcript.html", {"transcript": _transcript(row)})


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


# Where Kick sends the browser back to once the bot's account has signed in:
# the redirect URL the Kick app has (see config.DEFAULT_KICK_REDIRECT_URI).
KICK_SIGNED_IN_PATH = "/bot/kick/signed_in"
KICK_SIGN_IN_PATH = "/bot/kick/sign_in"


@app.get(KICK_SIGN_IN_PATH)
async def sign_in_kick_bot():
    """Sends the browser to Kick, for the account the bot is to write in
    Kick's chat as to sign in (see kick_auth.py). Kick sends it back to
    KICK_SIGNED_IN_PATH."""
    now = time.monotonic()
    for state, started in list(_kick_sign_ins.items()):
        if started.gone_by(now):
            del _kick_sign_ins[state]
    address, sign_in = kick_auth.start_sign_in(settings.kick_client_id, settings.kick_redirect_uri)
    _kick_sign_ins[sign_in.state] = sign_in
    logger.info("bot: an account is signing in on Kick")
    return RedirectResponse(address, status_code=303)


@app.get(KICK_SIGNED_IN_PATH, response_class=HTMLResponse)
async def kick_bot_signed_in(
    request: Request, code: str = "", state: str = "", error: str = "", error_description: str = ""
):
    """Where Kick sends the browser back to once the account has signed in -
    or has not, which the page says. The tokens Kick hands out for the code
    are kept, and the bot writes in Kick's chat as that account from then
    on."""
    sign_in = _kick_sign_ins.pop(state, None)
    account = why = None
    if sign_in is None or sign_in.gone_by(time.monotonic()):
        why = "This sign-in was not started here, or it took too long. Start it again."
    elif error:
        why = f"Kick did not sign the account in ({error_description or error})."
    elif not code:
        why = "Kick sent no code back."
    else:
        try:
            token = await kick_auth.finish_sign_in(
                settings.kick_client_id, settings.kick_client_secret, settings.kick_redirect_uri, sign_in, code
            )
        except kick_auth.SignInNeeded as refused:
            why = str(refused)
        except httpx.HTTPError as failed:
            logger.warning("bot: Kick could not be asked to sign the account in", exc_info=True)
            why = f"Kick could not be asked ({type(failed).__name__}). Try again."
        else:
            _kick_bot_tokens().keep(token)
            account = token.login
    if account:
        logger.info("bot: signed in on Kick as %s", account)
    else:
        logger.warning("bot: signing in on Kick failed: %s", why)
    conn = get_connection()
    try:
        context = _page_context(conn, "health")
    finally:
        conn.close()
    context.update({"account": account, "why": why, "sign_in_url": KICK_SIGN_IN_PATH})
    return templates.TemplateResponse(request, "signed_in.html", context, status_code=200 if account else 400)


@app.post("/subscriptions/check")
async def check_subscriptions():
    """Checks Kick's chat subscriptions now rather than at the next round,
    and subscribes what is missing - the button on the Health page."""
    if _subscription_check_running:
        raise HTTPException(status_code=409, detail="A check is under way already.")
    logger.info("chat subscriptions checked from the dashboard")
    await _ensure_chat_subscriptions(report_all_present=False)
    return {"status": "checked"}


@app.post("/notifications/test")
async def test_notifications():
    """Sends a note everywhere notifications go, whatever the switches say -
    the button on the Health page - and says to how many of them it went.
    The Health page says why not, where it did not."""
    notes = _notes()
    if not notes.destinations:
        raise HTTPException(status_code=409, detail="Notifications go nowhere: the Settings page sets up none.")
    logger.info("a test notification was asked for from the dashboard")
    sent = await notes.notify(notify.test_note(link=_link("/dashboard/health")))
    return {"sent": sent, "of": len(notes.destinations)}


async def _json_object(request: Request) -> dict:
    """A request's JSON, where it is an object; an empty one otherwise."""
    try:
        asked = await request.json()
    except ValueError:
        return {}
    return asked if isinstance(asked, dict) else {}


@app.post("/notifications/setup")
async def set_notifications(request: Request):
    """Saves where notifications go and which kinds go out - the Settings
    page's Notifications tab: `setup`, the fields of notify.Setup by name (a
    secret left out stays as it is; an empty value says there is none), and
    `kinds`, each kind's switch by name (NOTIFY_SETTINGS). A value that is
    what .env has goes back to following .env. It holds at once: the next
    note goes by it."""
    global _notifier
    asked = await _json_object(request)
    setup, kinds = asked.get("setup", {}), asked.get("kinds", {})
    names = {name for name, _label in NOTIFY_SETTINGS}
    if (
        not isinstance(setup, dict)
        or not isinstance(kinds, dict)
        or not all(name in notify.SETUP_LABELS and isinstance(value, str) for name, value in setup.items())
        or not all(name in names and isinstance(value, bool) for name, value in kinds.items())
    ):
        raise HTTPException(status_code=400, detail="expected where notifications go and which kinds, as JSON")

    env = _env_setup()
    conn = get_connection()
    try:
        saved = get_notify_setup(conn)
        for name, value in setup.items():
            if value.strip() == getattr(env, name):
                saved.pop(name, None)
            else:
                saved[name] = value.strip()
        try:
            notify.check_setup(notify.in_force(env, saved))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        replace_notify_setup(conn, saved)
        for name, value in kinds.items():
            set_flag(conn, SETTING_KEYS[name], value)
            _flags[SETTING_KEYS[name]] = value
    finally:
        conn.close()
    _notifier = None
    going = [destination.name for destination in _notes().destinations]
    logger.info(
        "notifications set up from the dashboard -> %s", dashboard_view.list_words(going) if going else "nowhere"
    )
    return {"destinations": going}


@app.post("/notifications/telegram/chats")
async def find_telegram_chats(request: Request):
    """The chats that wrote to a Telegram bot lately, for the Settings page
    to pick the one notes go to (see notify.telegram_chats): the bot of the
    token given (`token`), or of the one in force, asked through the Bot API
    server given (`api_url`) or the one in force."""
    asked = await _json_object(request)
    _notes()
    token = asked.get("token") if isinstance(asked.get("token"), str) else ""
    token = token.strip() or _note_setup.telegram_bot_token
    api_url = asked.get("api_url")
    api_url = api_url.strip() if isinstance(api_url, str) else _note_setup.telegram_api_url
    token_label, server_label = notify.SETUP_LABELS["telegram_bot_token"], notify.SETUP_LABELS["telegram_api_url"]
    if not token:
        raise HTTPException(status_code=400, detail=f'"{token_label}" is needed first: @BotFather has it.')
    if not notify.TELEGRAM_TOKEN_SHAPE.fullmatch(token):
        raise HTTPException(status_code=400, detail=f"\"{token_label}\" is not a bot's token.")
    if api_url and not notify.WEB_ADDRESS.fullmatch(api_url):
        raise HTTPException(status_code=400, detail=f'"{server_label}" is not a web address.')
    try:
        bot = await notify.telegram_bot(token, api_url)
        chats = await notify.telegram_chats(token, api_url)
    except notify.CannotSend as e:
        raise HTTPException(status_code=502, detail=str(e)) from None
    return {"bot": bot, "chats": [{"id": str(chat.id), "kind": chat.kind, "name": chat.name} for chat in chats]}


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
    """Puts a channel on the watchlist: `slug` is what was typed - a Kick
    channel's name, a link to a channel's page, `login@twitch`,
    `handle@youtube`, `handle@tiktok`."""
    slug = slug.strip()
    if not slug:
        raise HTTPException(status_code=400, detail="slug must not be empty")
    try:
        result = await add_to_watchlist(slug)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except twitch_client.NotSetUp as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
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
        result["slug"], result["broadcaster_user_id"], result["emote_count"],
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
        if row["platform"] in platforms.RECORDED_ONLY and value:
            title = platforms.TITLES[row["platform"]]
            raise HTTPException(
                status_code=409, detail=f"{title} channels are only recorded: no clips are looked for in them."
            )
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


async def _unsubscribe_kick_chat(broadcaster_user_id: int, slug: str) -> None:
    """Has Kick stop sending the chat of a channel that is no longer
    watched. Chat that still arrives is dropped (see _take_in), so a
    failure here is logged and nothing more."""
    try:
        token = await get_app_access_token(settings.kick_client_id, settings.kick_client_secret)
        ids = [
            sub["id"]
            for sub in await get_event_subscriptions(token)
            if sub.get("broadcaster_user_id") == broadcaster_user_id and sub.get("id")
        ]
        if ids:
            await kick_client.unsubscribe(ids, token)
    except Exception:
        logger.warning("[%s] its chat subscription on Kick could not be removed", slug, exc_info=True)


async def _remove_everything_of(conn, broadcaster_user_id: int, slug: str) -> dict:
    """Removes what came of watching a channel: its moments with their
    files, its stream recordings with theirs, and its chat. Raises
    HTTPException (409) before removing anything if something is being
    made of one of them, and OSError if a file can't be removed - what was
    removed by then stays removed."""
    moments = get_channel_moments(conn, broadcaster_user_id)
    recordings = [row for row in get_stream_recordings(conn) if row["broadcaster_user_id"] == broadcaster_user_id]
    busy = [row["id"] for row in moments if _moment_busy(row["id"]) is not None]
    if busy:
        raise HTTPException(
            status_code=409, detail=f"Something is being made of moment {busy[0]}. Try again once that is done."
        )
    busy = [row["id"] for row in recordings if _recording_busy(row["id"]) is not None]
    if busy:
        raise HTTPException(
            status_code=409, detail=f"Something is being made of recording {busy[0]}. Try again once that is done."
        )
    for row in moments:
        _remove_moment(conn, row)
    for row in recordings:
        await _remove_recording(conn, row)
    # What is kept by the name its chat comes under.
    for name in {row["channel_slug"] for row in moments} | {get_chat_name(conn, broadcaster_user_id) or slug}:
        forget_chat_delay(conn, name)

    def remove_chat() -> int:
        # A connection of its own: a busy channel's chat takes a while.
        own = get_connection()
        try:
            return remove_channel_chat(own, broadcaster_user_id)
        finally:
            own.close()

    messages = await asyncio.to_thread(remove_chat)
    for key in [key for key in _chat_identity_cache if key[0] == broadcaster_user_id]:
        del _chat_identity_cache[key]
    return {"moments": len(moments), "recordings": len(recordings), "messages": messages}


@app.post("/channels/{slug}/delete")
async def remove_channel(slug: str, everything: int = 0):
    """Takes a channel off the watchlist: it is watched no more, its chat no
    longer taken in, and what it had for being on it - its emotes, badges,
    own settings - is gone. What came of watching it stays, unless
    `everything`: then its moments, stream recordings and chat are removed
    for good as well. Not while its stream is being recorded, or its last
    recording saved."""
    conn = get_connection()
    try:
        row = get_streamer_by_slug(conn, slug)
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown channel {slug!r}")
        broadcaster_user_id = row["broadcaster_user_id"]
        whole = _by_channel(_stream_recordings_not_over()).get(slug)
        if recording_manager.stream_recording_of(slug) is not None or whole is not None:
            raise HTTPException(
                status_code=409,
                detail=f"{slug} is being recorded, or its recording saved. Stop it, or wait until it is saved.",
            )
        removed = None
        if everything:
            try:
                removed = await _remove_everything_of(conn, broadcaster_user_id, slug)
            except OSError:
                logger.warning("[%s] what came of watching it could not all be removed", slug, exc_info=True)
                raise HTTPException(
                    status_code=409,
                    detail="Not everything could be removed. Something may still have a file open.",
                ) from None
        remove_streamer(conn, broadcaster_user_id)
    finally:
        conn.close()
    _forget_tuning()
    _stream_start_cache.pop(broadcaster_user_id, None)
    await recording_manager.forget(slug)
    if row["platform"] == platforms.KICK:
        await _unsubscribe_kick_chat(broadcaster_user_id, slug)
    if removed is None:
        logger.info("[%s] taken off the watchlist", slug)
    else:
        logger.info(
            "[%s] taken off the watchlist, with %d moment(s), %d recording(s) and %d chat message(s)",
            slug, removed["moments"], removed["recordings"], removed["messages"],
        )
    return {"slug": slug, "removed": removed}


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


def _recording_busy(recording_id: int) -> str | None:
    """Why a recording that is over can't be removed right now - something
    is being made of its file - or None if it can be."""
    if _recording_trims.get(recording_id, {}).get("state") in dashboard_view.TRIM_BUSY:
        return "A stretch of this recording is being cut out. Try again once that is done."
    if _recording_chat_files.get(recording_id, {}).get("state") == dashboard_view.CHAT_FILE_MAKING:
        return "The chat video of this recording is being made. Try again once that is done."
    if _recording_finds.get(recording_id, {}).get("state") in dashboard_view.FIND_BUSY:
        return "Moments are being looked for in this recording, or cut out of it. Try again once that is done."
    return None


async def _remove_recording(conn, row) -> None:
    """Removes a stream recording that is over for good: its file, what was
    cut out of it, whatever is left of its parts, then its row. Raises
    OSError, with the row left where it is, if a file can't be removed."""
    recording_id = row["id"]
    file = _stream_file(row)
    if file is not None:
        # What was cut out of it goes with it.
        for cut in (*_recording_cut_files(file), *_recording_chat_file_paths(file)):
            cut.unlink(missing_ok=True)
        file.unlink(missing_ok=True)
    folder = stream_recording.parts_folder(row["channel_slug"], recording_id)
    if folder.exists():
        await asyncio.to_thread(shutil.rmtree, folder)
    delete_stream_recording(conn, recording_id)
    _recording_trims.pop(recording_id, None)
    _recording_counts.pop(recording_id, None)
    _recording_finds.pop(recording_id, None)
    _recording_chat_files.pop(recording_id, None)


@app.post("/recordings/{recording_id}/delete")
async def delete_recording(recording_id: int):
    """Removes a stream recording for good: its file, whatever is left of
    its parts, and its row. Not one that is still running or being saved."""
    conn = get_connection()
    try:
        row = _stream_recording_at_rest(conn, recording_id)
        if (busy := _recording_busy(recording_id)) is not None:
            raise HTTPException(status_code=409, detail=busy)
        try:
            await _remove_recording(conn, row)
        except OSError:
            logger.warning("stream recording %d could not be removed", recording_id, exc_info=True)
            raise HTTPException(
                status_code=409, detail="The recording could not be removed. Something may still have its file open."
            ) from None
    finally:
        conn.close()
    logger.info("[%s] stream recording %d deleted", row["channel_slug"], recording_id)
    return {"deleted": recording_id}


@app.post("/recordings/{recording_id}/trim")
async def trim_recording(recording_id: int, request: Request):
    """Starts cutting a stretch of a recording out as a video of its own,
    as trim_moment does of a moment's footage - `start` and `end` are
    seconds of the recording's file here - and with `chat` its chat, drawn
    into the picture or as a video of its own, which a stretch can be too
    long for as a see-through one (see clip_trim.with_chat). The file is
    kept next to the recording's, named for the stretch it holds; what was
    cut out of the recording before is replaced."""
    asked = await _asked_trim(request)
    conn = get_connection()
    try:
        row, parts, file = _recording_with_file(conn, recording_id, "cut from")
        whole = stream_recording.length(parts)
        try:
            start, end = clip_trim.stretch(0.0, whole, asked.get("start"), asked.get("end"))
            look = clip_trim.chat_look(asked)
            if asked.get("chat"):
                clip_trim.with_chat(end - start, look.where, look.background)
        except clip_trim.TrimError as e:
            raise _trim_refused(e) from None
        lines = None
        if asked.get("chat"):
            # From far enough back to fill the column the video starts out
            # with, and on for as far as its lines can be moved.
            lines = _recording_chat_lines(
                conn,
                row,
                parts,
                start - clip_trim.CHAT_VIDEO_LEAD_SECONDS,
                end + clip_trim.CHAT_SHIFT_LIMIT_SECONDS,
                RECORDING_CHAT_VIDEO_LIMIT,
            )
            if not lines:
                raise HTTPException(status_code=409, detail="No chat was stored for this stretch of the recording.")
    finally:
        conn.close()
    # A cut is a copy of that share of the file, on the disk recordings are
    # kept on - which must not be what fills it under one that is running.
    takes = (row["size_bytes"] or 0) * (end - start) / whole
    if recording_manager.room_left() - takes < recording_manager.MIN_FREE_BYTES:
        raise HTTPException(
            status_code=409,
            detail=f"The disk is too full to cut that out: it takes about {dashboard_view.size_words(round(takes))}.",
        )

    if _recording_trims.get(recording_id, {}).get("state") not in dashboard_view.TRIM_BUSY:
        _recording_trims[recording_id] = {"state": _first_trim_state(lines, look.where)}
        video = file.with_name(clip_trim.recording_video_name(file.name, start, end))
        _track_task(
            _trim_background(
                _recording_trims,
                recording_id,
                f"stream recording {recording_id}",
                row["channel_slug"],
                file,
                (start, end),
                0.0,
                lines,
                look,
                video,
                video.with_name(chat_video.video_name(video.name)),
                _recording_cut_files(file),
            )
        )
    return {"recording_id": recording_id, **_recording_trim(row)}


@app.get("/recordings/{recording_id}/trim")
async def recording_trim_state(recording_id: int):
    """Where the stretch cut out of a recording stands - what its Trim
    dialog asks when it is opened and while one is being made."""
    conn = get_connection()
    try:
        row = get_stream_recording(conn, recording_id)
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown recording {recording_id}")
    return {"recording_id": recording_id, **_recording_trim(row)}


@app.post("/recordings/{recording_id}/trim/chat")
async def trim_recording_chat(recording_id: int, request: Request):
    """Starts making the chat of the stretch already cut out of a
    recording, as trim_moment_chat does for a moment's."""
    asked = await _asked_trim(request)
    conn = get_connection()
    try:
        row, parts, file = _recording_with_file(conn, recording_id, "make chat for")
        if _recording_trims.get(recording_id, {}).get("state") in dashboard_view.TRIM_BUSY:
            return {"recording_id": recording_id, **_recording_trim(row)}
        cuts = _recording_cuts(file)
        if not cuts:
            raise HTTPException(status_code=409, detail="Nothing has been cut out of this recording yet.")
        video = cuts[0][0]
        record = _cut_record(video)
        if record is None:
            raise _no_cut_record()
        if record.burned:
            raise _burned_already()
        look = _asked_chat_look(asked, record.duration)
        # As trim_recording takes them, for the stretch the video holds.
        lines = _recording_chat_lines(
            conn,
            row,
            parts,
            record.begins - clip_trim.CHAT_VIDEO_LEAD_SECONDS,
            record.begins + record.duration + clip_trim.CHAT_SHIFT_LIMIT_SECONDS,
            RECORDING_CHAT_VIDEO_LIMIT,
        )
        if not lines:
            raise HTTPException(status_code=409, detail="No chat was stored for this stretch of the recording.")
    finally:
        conn.close()

    chat = video.with_name(chat_video.video_name(video.name))
    what = f"stream recording {recording_id}"
    channel = row["channel_slug"]
    _chat_later(_recording_trims, recording_id, what, channel, video, chat, record, lines, record.begins, look)
    return {"recording_id": recording_id, **_recording_trim(row)}


def _chat_file_answer(recording_id: int) -> dict:
    """Where the chat video of a whole recording stands, as its requests
    answer."""
    conn = get_connection()
    try:
        row = get_stream_recording(conn, recording_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown recording {recording_id}")
        parts = stream_recording.decoded(row["parts"])
        return {"recording_id": recording_id, **_recording_chat_file(conn, row, parts, _stream_file(row))}
    finally:
        conn.close()


@app.post("/recordings/{recording_id}/chat_video")
async def make_recording_chat_video(recording_id: int):
    """Starts making the chat of a whole recording as a video of its own,
    on a colour, unless it is there or being made already (see
    _recording_chat_file). Lines are placed as beside the recording: by its
    parts, with the channel's chat delay."""
    conn = get_connection()
    try:
        row, parts, file = _recording_with_file(conn, recording_id, "make chat for")
        chat, partial = _recording_chat_file_paths(file)
        making = _recording_chat_files.get(recording_id, {}).get("state") == dashboard_view.CHAT_FILE_MAKING
        if making or chat.exists():
            return {"recording_id": recording_id, **_recording_chat_file(conn, row, parts, file)}
        whole = stream_recording.length(parts)
        lines = _recording_chat_lines(conn, row, parts, 0.0, whole, RECORDING_CHAT_VIDEO_LIMIT + 1)
    finally:
        conn.close()
    if not lines:
        raise HTTPException(status_code=409, detail="No chat was stored for this recording.")
    if len(lines) > RECORDING_CHAT_VIDEO_LIMIT:
        raise HTTPException(
            status_code=409,
            detail=(
                f"This recording has more chat than one chat video can hold ({RECORDING_CHAT_VIDEO_LIMIT:,} lines). "
                "Cut a stretch of it out with Trim and take its chat with it."
            ),
        )
    takes = whole * RECORDING_CHAT_FILE_BYTES_PER_SECOND
    if recording_manager.room_left() - takes < recording_manager.MIN_FREE_BYTES:
        raise HTTPException(
            status_code=409,
            detail=(
                "The disk is too full to make the chat video: "
                f"it takes up to {dashboard_view.size_words(round(takes))}."
            ),
        )
    _recording_chat_files[recording_id] = {"state": dashboard_view.CHAT_FILE_MAKING, "done": 0.0}
    _track_task(_recording_chat_file_background(recording_id, row["channel_slug"], lines, whole, chat, partial))
    return _chat_file_answer(recording_id)


@app.get("/recordings/{recording_id}/chat_video")
async def recording_chat_video_state(recording_id: int):
    """Where the chat video of a whole recording stands."""
    return _chat_file_answer(recording_id)


@app.get("/dashboard/recordings/{recording_id}/chat_video", response_class=HTMLResponse)
async def dashboard_recording_chat_video(request: Request, recording_id: int):
    """Download chat as the Recordings page shows it - what the page fetches
    after asking for the video, and every couple of seconds while it is
    made."""
    conn = get_connection()
    try:
        row = get_stream_recording(conn, recording_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown recording {recording_id}")
        parts = stream_recording.decoded(row["parts"])
        context = {"chat_file": _recording_chat_file(conn, row, parts, _stream_file(row))}
    finally:
        conn.close()
    return templates.TemplateResponse(request, "_recording_chat_file.html", context)


def _find_standing(recording_id: int) -> dict:
    """Where looking for moments in a recording stands, as its two
    requests answer. What was found is on the page (see
    dashboard_recording_moments)."""
    known = _recording_finds.get(recording_id, {})
    return {"recording_id": recording_id, "state": known.get("state", dashboard_view.FIND_NONE)}


@app.post("/recordings/{recording_id}/moments")
async def find_recording_moments(recording_id: int):
    """Starts looking for moments in a recording: the chat stored with it
    is replayed through the detector under the channel's settings as they
    are saved now (see recording_moments.py). Nothing is stored - what was
    found is kept until the service stops or the recording is looked through
    again, and shown by dashboard_recording_moments. Asked again while it is
    being looked through, or while clips are being cut, nothing more is
    started."""
    global _replay_running
    if _shutdown_requested:
        raise HTTPException(status_code=409, detail="The service is shutting down.")
    conn = get_connection()
    try:
        row, parts, _file = _recording_with_file(conn, recording_id, "look for moments in")
        tuned = _tuning(conn, row["broadcaster_user_id"])
    finally:
        conn.close()
    if _recording_finds.get(recording_id, {}).get("state") not in dashboard_view.FIND_BUSY:
        _one_replay_at_a_time()
        _replay_running = True
        _recording_finds[recording_id] = {"state": dashboard_view.FIND_LOOKING, "done": 0, "of": 0}
        _track_task(
            _find_moments_background(recording_id, row["channel_slug"], row["broadcaster_user_id"], parts, tuned)
        )
    return _find_standing(recording_id)


@app.post("/recordings/{recording_id}/moments/clips")
async def make_recording_moments(recording_id: int, request: Request):
    """Makes moments of the ones found in a recording, each with its file
    cut out of the recording's: clip and context, framed by the settings the
    chat was replayed under. `moments` says which, by the key the page has
    for each; left out, it is all of them. One that is a moment already is
    left as it is. They are stored like moments the service detected, at the
    time they would have fired, so they are in the review queue from then
    on."""
    if _shutdown_requested:
        raise HTTPException(status_code=409, detail="The service is shutting down.")
    try:
        asked = await request.json()
    except ValueError:
        asked = None
    keys = asked.get("moments") if isinstance(asked, dict) else ()
    if keys is not None and (not isinstance(keys, list) or not all(type(key) is int for key in keys)):
        raise HTTPException(status_code=400, detail="Expected the moments to make clips of as JSON.")

    conn = get_connection()
    try:
        row, parts, file = _recording_with_file(conn, recording_id, "cut clips from")
        known = _recording_finds.get(recording_id, {})
        if known.get("state") == dashboard_view.FIND_CUTTING:
            raise HTTPException(status_code=409, detail="Clips are still being cut. Try again once that is done.")
        if known.get("state") != dashboard_view.FIND_FOUND:
            raise HTTPException(status_code=409, detail="Look for moments in the recording first.")
        found: list[recording_moments.Found] = known["found"]
        if keys is not None and (unknown := set(keys) - {one.key for one in found}):
            raise HTTPException(
                status_code=400, detail=f"{len(unknown)} of the moments asked for were not found in this recording."
            )
        windows = (
            get_channel_moment_windows(
                conn, row["broadcaster_user_id"], found[0].window_start, max(one.window_end for one in found)
            )
            if found
            else []
        )
        chosen = [
            one
            for one in found
            if (keys is None or one.key in keys) and recording_moments.moment_of(one, windows) is None
        ]
        # Moments are kept under the name the channel's chat comes with.
        channel = get_chat_name(conn, row["broadcaster_user_id"]) or row["channel_slug"]
    finally:
        conn.close()
    if not chosen:
        raise HTTPException(status_code=409, detail="There is nothing here that is not a moment already.")
    # Each file is a copy of its share of the recording's. Clips are not
    # what may fill the disk under a recording that is running.
    whole = stream_recording.length(parts)
    takes = (row["size_bytes"] or 0) * sum(one.footage[1] - one.footage[0] for one in chosen) / whole
    if recording_manager.room_left() - takes < recording_manager.MIN_FREE_BYTES:
        raise HTTPException(
            status_code=409,
            detail=f"The disk is too full to cut that: it takes about {dashboard_view.size_words(round(takes))}.",
        )

    known.update(state=dashboard_view.FIND_CUTTING, done=0, of=len(chosen), made=0, failed=0)
    _track_task(_make_moments_background(recording_id, row["broadcaster_user_id"], channel, file, chosen))
    return _find_standing(recording_id)


@app.get("/dashboard/recordings/{recording_id}/moments", response_class=HTMLResponse)
async def dashboard_recording_moments(request: Request, recording_id: int):
    """What looking for moments in a recording has come to, as the
    Recordings page shows it - what the page fetches after asking for
    something, and every couple of seconds while that is being done."""
    conn = get_connection()
    try:
        row = get_stream_recording(conn, recording_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown recording {recording_id}")
        parts = stream_recording.decoded(row["parts"])
        context = {"finding": _recording_finding(conn, row, parts, _today())}
    finally:
        conn.close()
    return templates.TemplateResponse(request, "_recording_moments.html", context)


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


async def _posted_values(request: Request, for_all: bool = False) -> dict[str, tuning.Value]:
    """The settings' values a request brings (`values`, by name), each as
    the value its setting takes. One that can't be taken is a 400 that says
    why in words for the page - as is, among values `for_all` channels, one
    that is a channel's alone."""
    try:
        asked = await request.json()
    except ValueError:
        asked = None
    posted = asked.get("values") if isinstance(asked, dict) else None
    if not isinstance(posted, dict):
        raise HTTPException(status_code=400, detail="expected the settings' values as JSON")
    try:
        given = tuning.cleaned(posted)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    if for_all and (apart := sorted(tuning.per_channel(given))):
        label = tuning.BY_NAME[apart[0]].label
        raise HTTPException(status_code=400, detail=f'"{label}" is set for each channel by itself.')
    return given


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
    given = await _posted_values(request, for_all=True)
    conn = get_connection()
    try:
        for_all = tuning.shared({**tuning.stored(get_tuning(conn)), **given})
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
    kept either way - all but what is a channel's alone, which is in force
    either way."""
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
    global _replay_running
    _one_replay_at_a_time()
    _replay_running = True
    try:
        result = await asyncio.to_thread(_replay_both, slug, saved, proposed)
    finally:
        _replay_running = False
    if result is None:
        raise HTTPException(status_code=409, detail=f"No chat is stored for {slug} yet.")
    return result


@app.post("/tuning/trial")
async def try_tuning(request: Request, chat: str):
    """What `values` would do if they were saved for all channels, shown on
    the newest stored chat of the channel `chat`: how many moments it would
    have made with the settings as they are saved, and with these. Nothing
    is saved."""
    given = await _posted_values(request, for_all=True)
    conn = get_connection()
    try:
        row = get_streamer_by_slug(conn, chat)
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown channel {chat!r}")
        owner = row["broadcaster_user_id"]
        for_all = {**tuning.stored(get_tuning(conn)), **given}
        mine = _own_values(conn, row)
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
        _heard.refused += 1
        _heard.last_refused_at = datetime.now(timezone.utc)
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    payload = await request.json()
    # For the Health page: that Kick got through, whatever is done with it.
    _heard.last_at = datetime.now(timezone.utc)

    if kick_event_type != "chat.message.sent":
        logger.info("Received %s event: %s", kick_event_type, payload)
        return {"status": "received"}
    return {"status": await _take_chat(kick_chat.chat_message(payload))}


async def _take_chat(message: ChatMessage) -> str:
    """What becomes of one chat message (see _take_chats)."""
    (status,) = await _take_chats([message])
    return status


async def _take_chats(messages: Sequence[ChatMessage]) -> list[str]:
    """What becomes of chat messages, whichever platform they came from:
    stored with their channel's chat, counted by the detector, made a moment
    of - or dropped. Returns what was done with each, in a word or two.

    However many there are, they are stored in one go: a commit takes a
    tenth of a second on a spinning disk, longer than a busy chat leaves
    between two messages. A platform that delivers chat in bulk hands it
    over a batch at a time; Kick's webhooks come one message each.

    The bot's commands among them are carried out once they are stored
    (_carry_out) - whatever the switches say, as the buttons on the
    Channels page work whatever they say. Not if they could not be stored:
    the platform delivers them again."""
    asked: list[tuple[ChatMessage, str]] = []
    statuses = await _take_in(messages, asked)
    for message, command in asked:
        _carry_out(message, command)
    return statuses


async def _take_in(messages: Sequence[ChatMessage], asked: list[tuple[ChatMessage, str]]) -> list[str]:
    """_take_chats but for the bot's commands, which are put in `asked`
    for it to carry out."""
    now = datetime.now(timezone.utc)
    statuses = ["watching disabled"] * len(messages)
    watching = _flags.get("watching_enabled", True)
    taken: list[tuple[int, ChatMessage, bool]] = []
    tracked: dict[int, bool] = {}
    watched: dict[int, bool] = {}
    conn = get_connection()
    try:
        for index, message in enumerate(messages):
            owner = message.broadcaster_user_id
            # For the Health page: that the channel's chat is coming in, kept
            # or not.
            _heard.chat_at[owner] = message.received_at or now
            _notice_chat_name(conn, message)
            if not _shutdown_requested and (command := _bot_command(conn, message)) is not None:
                asked.append((message, command))
            # The switches are about looking for clips. A channel whose stream
            # is being kept whole has its chat kept with it whatever they say.
            kept_whole = recording_manager.keeps_chat_of(owner)
            if _shutdown_requested or not (watching or kept_whole):
                # Watching paused (or a shutdown is in progress) - drop the
                # message without storing or running detection, so no new
                # moments pile up.
                continue
            if owner not in tracked:
                streamer = get_streamer_by_id(conn, owner)
                # A channel missing from the watchlist is processed - Kick
                # delivers only what was subscribed to, so the watchlist is
                # behind - unless it was taken off it, after which a
                # platform may go on sending its chat for a while.
                watched[owner] = streamer is not None or not was_removed(conn, owner)
                tracked[owner] = (
                    watching
                    and platforms.platform_of(message.channel) not in platforms.RECORDED_ONLY
                    and (streamer is None or bool(streamer["tracking_enabled"]))
                )
            if not watched[owner] and not kept_whole:
                statuses[index] = "not on the watchlist"
                continue
            if not tracked[owner] and not kept_whole:
                # Tracking paused for this one channel - same drop as the
                # global pause, scoped to it.
                statuses[index] = "channel tracking disabled"
                continue
            logger.info("[%s] %s: %s", message.channel, message.sender or "?", message.content)
            taken.append((index, message, tracked[owner]))
        if not taken:
            return statuses

        new = insert_chat_messages(conn, [_chat_row(message, now) for _, message, _ in taken])
        fresh = []
        for index, message, judged in taken:
            if message.message_id not in new:
                # A platform delivers a message again when it doesn't see it
                # acknowledged in time - i.e. exactly when this service is
                # struggling. The message is already stored and counted;
                # feeding it to the detector a second time would inflate the
                # very burst it is trying to measure.
                statuses[index] = "duplicate"
                continue
            new.discard(message.message_id)
            fresh.append((index, message, judged))
        _remember_chat_identities(conn, (message for _, message, _ in fresh))

        for index, message, judged in fresh:
            if not judged:
                # Kept to go with the recording of the stream. Nobody is
                # looking for moments in it.
                statuses[index] = "stored"
                continue
            await _detect(conn, message)
            statuses[index] = "received"
    finally:
        conn.close()
    return statuses


def _chat_row(message: ChatMessage, now: datetime) -> ChatRow:
    """A message as it is stored, arrived when it says or else `now`."""
    return (
        message.message_id,
        message.broadcaster_user_id,
        message.channel,
        message.sender,
        message.content,
        message.emotes_json(),
        message.created_at,
        (message.received_at or now).isoformat(),
    )


async def _detect(conn, message: ChatMessage) -> None:
    """Counts a stored message of a tracked channel, and makes a moment of
    the reaction it completes - or of the clip it asks for."""
    channel, broadcaster_user_id = message.channel, message.broadcaster_user_id
    # Everything from here on is judged by the channel's settings as they
    # stand for this message.
    tuned = _tuning(conn, broadcaster_user_id)
    if tuned.bot_command(message.content) is not None:
        # Asks the bot for something (see _carry_out). No reaction either.
        return
    commanded = False
    if tuned.is_clip_command(message.content):
        # One of the channel's clip commands. It is no reaction, so the
        # detector never sees it, whoever posted it; from someone who may ask,
        # it is a moment by itself, with none of the detector's figures to its
        # name.
        commanded = tuned.clip_command_from_anyone or message.runs_channel
        spike = _COMMANDED if commanded else None
    else:
        emote_weight = detector.classify_native_emotes(message.content, tuned.detector)
        laugh_weight, mention_weight = detector.classify_message(
            message.content, _channel_keywords(conn, broadcaster_user_id), tuned.detector
        )
        spike = detector.record_message(
            channel,
            sender=message.sender,
            content=message.content,
            emote_count=message.emote_count,
            emote_weight=emote_weight,
            laugh_weight=laugh_weight,
            mention_weight=mention_weight,
            settings=tuned.detector,
        )
    if spike is not None and channel in _moment_sessions:
        # A moment is already open for this channel (a re-fire past the
        # cooldown) - it's the same reaction continuing, so just keep it alive
        # rather than opening a duplicate.
        session = _moment_sessions[channel]
        session.last_active = datetime.now(timezone.utc)
        if commanded:
            # What was asked for is what was on screen just now: the open
            # moment's clip is made to reach it.
            session.window_end = session.last_active
            logger.info("[%s] clip asked for by %s: moment %d extended", channel, message.sender, session.moment_id)
    elif spike is not None:
        await _open_moment_session(conn, message, spike, tuned)


async def _open_moment_session(conn, message: ChatMessage, spike: detector.Spike, tuned: tuning.Tuning) -> None:
    """Stores the moment a message set off and holds it open while the
    reaction lasts; its clip is cut once it closes (_run_moment_session)."""
    channel = message.channel
    window_end = datetime.now(timezone.utc)
    window_start = window_end - timedelta(seconds=detector.SHORT_WINDOW_SECONDS)
    reason = ",".join(spike.reasons)
    try:
        stream_elapsed = await _stream_elapsed_seconds(channel, message.broadcaster_user_id)
    except Exception:
        # Stream time is a label on the dashboard. The detector has already
        # started its cooldown, so a moment dropped here because the
        # platform's API hiccuped would simply be gone.
        logger.warning("[%s] could not look up the stream's start time", channel, exc_info=True)
        stream_elapsed = None
    moment_id = insert_moment(
        conn,
        broadcaster_user_id=message.broadcaster_user_id,
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
    # Open a moment session: hold it open and extend the clip while the
    # reaction lasts, then cut one clip covering all of it.
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
    # Tracked (not just held via _moment_sessions) so there's no gap between
    # the session closing and its clip-cut task existing - both would
    # otherwise look "idle" to _shutdown_when_idle for the moment in between.
    _track_task(_run_moment_session(session))
    stream_time = str(timedelta(seconds=stream_elapsed)) if stream_elapsed is not None else "unknown"
    logger.info(
        "MOMENT detected in [%s] (%s) at stream time %s: %d msgs, %d emotes, %d keyword hits in %ds, score=%.2f",
        channel,
        reason,
        stream_time,
        spike.message_count,
        spike.emote_count,
        spike.keyword_hits,
        detector.SHORT_WINDOW_SECONDS,
        spike.score,
    )
