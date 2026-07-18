import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import audio_events, detector, frame_encoder, recorder, recording_manager, transcriber
from .config import load_settings
from .db import (
    MOMENT_TYPES,
    STREAM_TYPES,
    count_moments,
    get_channel_keywords,
    get_chat_snippet,
    get_connection,
    get_flag,
    get_moment_channels,
    get_recent_moments,
    get_streamers,
    insert_chat_message,
    insert_moment,
    set_flag,
    update_moment_audio_events,
    update_moment_clip_path,
    update_moment_frame_embedding,
    update_moment_notes,
    update_moment_rating,
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
from .timeutil import to_local
from .webhook_security import get_kick_public_key, verify_signature

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("kick_clip_hunter")


def _watchlist_slugs() -> list[str]:
    conn = get_connection()
    try:
        return [row["slug"] for row in get_streamers(conn)]
    finally:
        conn.close()


# Runtime on/off switches, toggled from the dashboard and persisted in
# app_settings so they survive a restart. Held in memory too so the webhook
# hot path and the recording loop don't hit the DB on every message/tick.
# Maps the short dashboard name to its stored key.
SETTING_KEYS = {"chat": "chat_enabled", "recording": "recording_enabled"}
_flags: dict[str, bool] = {}


def _load_flags() -> None:
    conn = get_connection()
    try:
        for key in SETTING_KEYS.values():
            _flags[key] = get_flag(conn, key, default=True)
    finally:
        conn.close()


async def _ensure_chat_subscriptions() -> None:
    """Re-subscribe any watchlisted channel that has no chat.message.sent
    subscription on Kick.

    Kick silently drops event subscriptions to zero every so often with no
    error - chat just stops arriving for every channel until they're
    re-subscribed (see CLAUDE.md). Reconciling on every startup makes a
    restart self-healing instead of needing subscribe.py run by hand. It's
    check-then-subscribe (only the missing ones) so it never duplicates an
    existing subscription, and any API failure is logged but never blocks
    startup.
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
        logger.exception("could not check chat subscriptions on startup")
        return

    missing = [(bid, slug) for bid, slug in watch if bid not in subscribed]
    if not missing:
        logger.info("chat subscriptions present for all %d watched channels", len(watch))
        return

    logger.info(
        "re-subscribing %d channel(s) with no chat subscription: %s",
        len(missing), ", ".join(slug for _, slug in missing),
    )
    for bid, slug in missing:
        try:
            await subscribe_chat_messages(bid, token)
            logger.info("re-subscribed chat for %s", slug)
        except Exception:
            logger.exception("failed to re-subscribe chat for %s", slug)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_flags()
    await _ensure_chat_subscriptions()
    task = asyncio.create_task(
        recording_manager.run_forever(_watchlist_slugs, lambda: _flags.get("recording_enabled", True))
    )
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(lifespan=lifespan)
settings = load_settings()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

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
    # The post-roll footage (and the segment covering window_end itself,
    # which ffmpeg's segment muxer doesn't flush to disk until it rotates to
    # the next one) doesn't exist yet at the instant a moment closes - wait
    # for it to actually be recorded before looking for it, or the clip comes
    # out truncated right at the exciting part.
    await asyncio.sleep(post_roll_seconds + recorder.SEGMENT_SECONDS + 2)
    try:
        clip_path = await recording_manager.create_clip_for_moment(
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

    conn = get_connection()
    try:
        update_moment_clip_path(conn, moment_id, clip_path.relative_to(CLIPS_DIR).as_posix())
    finally:
        conn.close()
    logger.info("[%s] clip saved for moment %d: %s", channel, moment_id, clip_path)
    asyncio.create_task(_transcribe_clip_background(moment_id, channel, clip_path))
    asyncio.create_task(_detect_audio_events_background(moment_id, channel, clip_path))
    asyncio.create_task(_encode_frames_background(moment_id, channel, clip_path))


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


# A detected moment isn't cut into a fixed-length clip right away. Instead it
# stays "open" and its end is pushed out for as long as chat keeps reacting
# (detector.reaction_active), so one clip captures the whole reaction instead
# of getting chopped off before the payoff - the most common complaint when
# reviewing clips. The moment closes once the reaction has been quiet for
# MOMENT_SESSION_QUIET_SECONDS, or after MOMENT_SESSION_MAX_SECONDS as a hard
# cap, and only then is the clip cut.
MOMENT_SESSION_POLL_SECONDS = 3
MOMENT_SESSION_QUIET_SECONDS = 8
MOMENT_SESSION_MAX_SECONDS = 90
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
            if _session_should_close(session, detector.reaction_active(session.channel), now):
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
    asyncio.create_task(
        _create_clip_background(
            session.moment_id, session.channel, session.window_start, session.window_end,
            post_roll_seconds=DYNAMIC_POST_ROLL_SECONDS,
        )
    )


@app.get("/health")
async def health():
    return {"status": "ok"}


MOMENTS_PAGE_SIZE = 50


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, channel: str | None = None, offset: int = 0):
    offset = max(0, offset)
    conn = get_connection()
    try:
        streamers = [
            {
                "slug": row["slug"],
                "broadcaster_user_id": row["broadcaster_user_id"],
                "added_at_local": to_local(row["added_at"]),
            }
            for row in get_streamers(conn)
        ]

        channels = get_moment_channels(conn)
        total_moments = count_moments(conn, channel_slug=channel)

        moments = []
        for row in get_recent_moments(conn, limit=MOMENTS_PAGE_SIZE, offset=offset, channel_slug=channel):
            stream_elapsed = row["stream_elapsed_seconds"]
            snippet = get_chat_snippet(conn, row["channel_slug"], row["window_start"], row["window_end"])
            moments.append(
                {
                    "id": row["id"],
                    "channel_slug": row["channel_slug"],
                    "detected_at_local": to_local(row["detected_at"]),
                    "stream_time": str(timedelta(seconds=stream_elapsed)) if stream_elapsed is not None else "unknown",
                    "reason": row["reason"],
                    "score": row["score"],
                    "message_count": row["message_count"],
                    "baseline_message_rate": row["baseline_message_rate"],
                    "current_message_rate": row["current_message_rate"],
                    "emote_count": row["emote_count"],
                    "keyword_hits": row["keyword_hits"],
                    "snippet": snippet,
                    "clip_url": f"/clips/{row['clip_path']}" if row["clip_path"] else None,
                    "rating": row["rating"],
                    "notes": row["notes"] or "",
                    "transcript": row["transcript"] or "",
                    "audio_events": row["audio_events"] or "",
                    "stream_type": row["stream_type"],
                    "moment_type": row["moment_type"],
                }
            )
    finally:
        conn.close()

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "streamers": streamers,
            "moments": moments,
            "channels": channels,
            "selected_channel": channel,
            "offset": offset,
            "page_size": MOMENTS_PAGE_SIZE,
            "total_moments": total_moments,
            "chat_enabled": _flags.get("chat_enabled", True),
            "recording_enabled": _flags.get("recording_enabled", True),
            "stream_types": STREAM_TYPES,
            "moment_types": MOMENT_TYPES,
        },
    )


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
        if not _flags.get("chat_enabled", True):
            # Chat watching paused from the dashboard: drop the message without
            # storing or running detection, so no new moments pile up.
            return {"status": "chat watching disabled"}

        broadcaster = payload.get("broadcaster", {})
        sender = payload.get("sender", {})
        channel = broadcaster.get("channel_slug", "?")
        content = payload.get("content", "")
        logger.info("[%s] %s: %s", channel, sender.get("username", "?"), content)

        broadcaster_user_id = broadcaster["user_id"]
        sender_username = sender.get("username", "")
        emotes = payload.get("emotes", [])
        emote_count = sum(len(e.get("positions", [])) for e in emotes)
        emote_weight = detector.classify_native_emotes(content)

        conn = get_connection()
        try:
            laugh_weight, mention_weight = detector.classify_message(
                content, _channel_keywords(conn, broadcaster_user_id)
            )

            insert_chat_message(
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
                stream_elapsed = await _stream_elapsed_seconds(channel)
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
                )
                _moment_sessions[channel] = session
                asyncio.create_task(_run_moment_session(session))
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
        finally:
            conn.close()
    else:
        logger.info("Received %s event: %s", kick_event_type, payload)

    return {"status": "received"}
