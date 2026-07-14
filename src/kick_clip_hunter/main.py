import logging

from fastapi import FastAPI, Header, HTTPException, Request

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
        channel = payload.get("broadcaster", {}).get("channel_slug", "?")
        sender = payload.get("sender", {}).get("username", "?")
        content = payload.get("content", "")
        logger.info("[%s] %s: %s", channel, sender, content)
    else:
        logger.info("Received %s event: %s", kick_event_type, payload)

    return {"status": "received"}
