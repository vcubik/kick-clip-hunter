import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from . import detector
from .config import load_settings
from .db import (
    get_channel_keywords,
    get_chat_snippet,
    get_connection,
    get_recent_moments,
    get_streamers,
    insert_chat_message,
    insert_moment,
)
from .kick_client import get_app_access_token, get_channel_by_slug
from .timeutil import to_local
from .webhook_security import get_kick_public_key, verify_signature

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("kick_clip_hunter")

app = FastAPI()
settings = load_settings()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

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


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
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

        moments = []
        for row in get_recent_moments(conn, limit=50):
            stream_elapsed = row["stream_elapsed_seconds"]
            snippet = get_chat_snippet(conn, row["channel_slug"], row["window_start"], row["window_end"])
            moments.append(
                {
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
                }
            )
    finally:
        conn.close()

    return templates.TemplateResponse(
        request, "dashboard.html", {"streamers": streamers, "moments": moments}
    )


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
                insert_moment(
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
