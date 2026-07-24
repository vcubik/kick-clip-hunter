"""Add a channel to the watchlist: subscribes to its chat.message.sent
events, fetches its 7TV emote keywords, and records the streamer locally.

Shared by scripts/subscribe.py (CLI) and the dashboard's "add channel" form
in main.py, so the two stay in lockstep instead of drifting apart.
"""

from .config import load_settings
from .db import add_streamer, get_connection, replace_channel_keywords
from .detector import EMOTE_MENTION_LAUGH_WEIGHT, classify_emote_names
from .kick_client import get_app_access_token, get_channel_by_slug, subscribe_chat_messages
from .seventv_client import get_channel_emote_names


async def add_channel_to_watchlist(slug: str) -> dict:
    """Raises ValueError (via get_channel_by_slug) if no such Kick channel exists."""
    settings = load_settings()
    token = await get_app_access_token(settings.kick_client_id, settings.kick_client_secret)

    channel = await get_channel_by_slug(slug, token)
    broadcaster_id = channel["broadcaster_user_id"]

    await subscribe_chat_messages(broadcaster_id, token)

    emote_names = await get_channel_emote_names(broadcaster_id)
    keyword_weights = classify_emote_names(emote_names)
    laugh_count = sum(1 for weight in keyword_weights.values() if weight == EMOTE_MENTION_LAUGH_WEIGHT)

    conn = get_connection()
    try:
        add_streamer(conn, broadcaster_id, slug)
        replace_channel_keywords(conn, broadcaster_id, keyword_weights)
    finally:
        conn.close()

    return {
        "slug": slug,
        "broadcaster_user_id": broadcaster_id,
        "emote_count": len(emote_names),
        "laugh_emote_count": laugh_count,
    }
