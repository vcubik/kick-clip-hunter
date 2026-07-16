import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import detector, recorder, recording_manager
from .config import load_settings
from .db import (
    count_moments,
    get_channel_keywords,
    get_chat_snippet,
    get_connection,
    get_moment_channels,
    get_recent_moments,
    get_streamers,
    insert_chat_message,
    insert_moment,
    update_moment_clip_path,
    update_moment_notes,
    update_moment_rating,
)
from .kick_client import get_app_access_token, get_channel_by_slug
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(recording_manager.run_forever(_watchlist_slugs))
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
    moment_id: int, channel: str, window_start: datetime, window_end: datetime
) -> None:
    # The post-roll footage (and the segment covering window_end itself,
    # which ffmpeg's segment muxer doesn't flush to disk until it rotates to
    # the next one) doesn't exist yet at the instant a moment is detected -
    # wait for it to actually be recorded before looking for it, or the clip
    # comes out truncated right at the exciting part.
    await asyncio.sleep(recorder.POST_ROLL_SECONDS + recorder.SEGMENT_SECONDS + 2)
    try:
        clip_path = await recording_manager.create_clip_for_moment(
            channel, window_start, window_end, f"moment_{moment_id}.mp4"
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
            if spike is not None:
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
                asyncio.create_task(
                    _create_clip_background(moment_id, channel, window_start, window_end)
                )
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
