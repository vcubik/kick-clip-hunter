import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Header, HTTPException, Request

from . import detector
from .db import get_connection, insert_chat_message, insert_moment
from .webhook_security import get_kick_public_key, verify_signature

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("kick_clip_hunter")

app = FastAPI()


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

        conn = get_connection()
        try:
            insert_chat_message(
                conn,
                message_id=payload["message_id"],
                broadcaster_user_id=broadcaster_user_id,
                channel_slug=channel,
                sender_username=sender.get("username", ""),
                content=content,
                emotes_json=json.dumps(payload.get("emotes", [])),
                created_at=payload.get("created_at", ""),
            )

            spike = detector.record_message(channel)
            if spike is not None:
                window_end = datetime.now(timezone.utc)
                window_start = window_end - timedelta(seconds=detector.SHORT_WINDOW_SECONDS)
                insert_moment(
                    conn,
                    broadcaster_user_id=broadcaster_user_id,
                    channel_slug=channel,
                    window_start=window_start.isoformat(),
                    window_end=window_end.isoformat(),
                    message_count=spike.message_count,
                    baseline_rate=spike.baseline_rate,
                    current_rate=spike.current_rate,
                    score=spike.score,
                )
                logger.info(
                    "MOMENT detected in [%s]: %d messages in %ds (%.2f msg/s vs baseline %.2f msg/s, score=%.2f)",
                    channel,
                    spike.message_count,
                    detector.SHORT_WINDOW_SECONDS,
                    spike.current_rate,
                    spike.baseline_rate,
                    spike.score,
                )
        finally:
            conn.close()
    else:
        logger.info("Received %s event: %s", kick_event_type, payload)

    return {"status": "received"}
