"""Add a channel to the watchlist: fetches its 7TV emote keywords, makes
sure its chat.message.sent events are subscribed, and records the streamer
locally - in that order, so a failure part-way leaves nothing half-done.

Shared by scripts/subscribe.py (CLI) and the dashboard's "add channel" form
in main.py, so the two stay in lockstep instead of drifting apart.
"""

from .config import load_settings
from .db import add_streamer, get_connection, replace_channel_keywords
from .detector import EMOTE_MENTION_LAUGH_WEIGHT, classify_emote_names
from .kick_client import (
    get_app_access_token,
    get_channel_by_slug,
    get_event_subscriptions,
    subscribe_chat_messages,
)
from .seventv_client import get_channel_emote_names


async def add_channel_to_watchlist(slug: str) -> dict:
    """Raises ValueError (via get_channel_by_slug) if no such Kick channel exists."""
    settings = load_settings()
    token = await get_app_access_token(settings.kick_client_id, settings.kick_client_secret)

    channel = await get_channel_by_slug(slug, token)
    broadcaster_id = channel["broadcaster_user_id"]

    # Everything that can fail is fetched before anything is changed on
    # Kick's side: a 7TV outage used to leave a chat subscription behind for
    # a channel that never made it onto the local watchlist.
    emote_names = await get_channel_emote_names(broadcaster_id)
    keyword_weights = classify_emote_names(emote_names)
    laugh_count = sum(1 for weight in keyword_weights.values() if weight == EMOTE_MENTION_LAUGH_WEIGHT)

    # Subscribing twice creates a second subscription on Kick's side, so a
    # channel that is already subscribed (re-added, or re-run to refresh it)
    # is left as it is - same check-then-subscribe as the startup reconcile.
    subscribed = {subscription.get("broadcaster_user_id") for subscription in await get_event_subscriptions(token)}
    if broadcaster_id not in subscribed:
        await subscribe_chat_messages(broadcaster_id, token)

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
