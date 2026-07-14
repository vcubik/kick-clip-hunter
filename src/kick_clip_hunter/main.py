import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Header, HTTPException, Request

from . import detector
from .db import get_channel_keywords, get_connection, insert_chat_message, insert_moment
from .webhook_security import get_kick_public_key, verify_signature

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("kick_clip_hunter")

app = FastAPI()

# Channel keyword sets rarely change (only when re-subscribing), so we cache
# them per-process instead of hitting the DB on every single chat message.
_channel_keywords_cache: dict[int, set[str]] = {}


def _channel_keywords(conn, broadcaster_user_id: int) -> set[str]:
    if broadcaster_user_id not in _channel_keywords_cache:
        _channel_keywords_cache[broadcaster_user_id] = get_channel_keywords(conn, broadcaster_user_id)
    return _channel_keywords_cache[broadcaster_user_id]


@app.get("/health")
async def health():
    return {"status": "ok"}


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
        emotes = payload.get("emotes", [])
        emote_count = sum(len(e.get("positions", [])) for e in emotes)

        conn = get_connection()
        try:
            keyword_hit = detector.matches_keyword(content, _channel_keywords(conn, broadcaster_user_id))

            insert_chat_message(
                conn,
                message_id=payload["message_id"],
                broadcaster_user_id=broadcaster_user_id,
                channel_slug=channel,
                sender_username=sender.get("username", ""),
                content=content,
                emotes_json=json.dumps(emotes),
                created_at=payload.get("created_at", ""),
            )

            spike = detector.record_message(channel, emote_count=emote_count, keyword_hit=keyword_hit)
            if spike is not None:
                window_end = datetime.now(timezone.utc)
                window_start = window_end - timedelta(seconds=detector.SHORT_WINDOW_SECONDS)
                reason = ",".join(spike.reasons)
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
                )
                logger.info(
                    "MOMENT detected in [%s] (%s): %d msgs, %d emotes, %d keyword hits in %ds, score=%.2f",
                    channel,
                    reason,
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
