"""Add a channel to the watchlist: fetches its 7TV emotes, makes
sure its chat.message.sent events are subscribed, and records the streamer
locally - in that order, so a failure part-way leaves nothing half-done.
Its subscriber badges are fetched too, but a channel is added without them
if they can't be: they are only pictures, and are fetched again on every
start.

A Twitch channel is looked up on Twitch and recorded with its emotes; its
chat is subscribed by the connection that reads it (twitch_chat.py), not
here. A YouTube channel is looked up on YouTube and recorded; it has no
emote set of its own anywhere - its chat's emoji come with every message -
and its chat is read by the service while it is live (youtube_chat.py).

Shared by scripts/subscribe.py (CLI) and the dashboard's "add channel" form
in main.py, so the two stay in lockstep instead of drifting apart.
"""

import logging

from . import channel_badges, platforms, twitch_client, twitch_emotes, youtube_client
from .config import load_settings
from .db import (
    GLOBAL_EMOTES_OWNER,
    add_platform_streamer,
    add_streamer,
    get_connection,
    replace_channel_badges,
    replace_channel_emotes,
    replace_channel_keywords,
)
from .detector import EMOTE_MENTION_LAUGH_WEIGHT, classify_emote_names
from .kick_client import (
    get_app_access_token,
    get_channel_by_slug,
    get_event_subscriptions,
    get_site_channel,
    subscribe_chat_messages,
)
from .seventv_client import Emote, emote_pictures, get_channel_emotes, get_global_emotes

logger = logging.getLogger(__name__)


def store_channel_emotes(conn, broadcaster_id: int, emotes: list[Emote]) -> int:
    """Stores what a channel's 7TV emote set means for it: the names the
    detector listens for, with their weights, and the picture each name is
    shown as. Returns how many of the names are laugh-related."""
    keyword_weights = classify_emote_names([emote.name for emote in emotes])
    replace_channel_keywords(conn, broadcaster_id, keyword_weights)
    replace_channel_emotes(conn, broadcaster_id, emote_pictures(emotes))
    return sum(1 for weight in keyword_weights.values() if weight == EMOTE_MENTION_LAUGH_WEIGHT)


# Which sets a channel's emotes beyond its platform's own come from, in words.
# A platform that is not here has none: 7TV, BetterTTV and FrankerFaceZ know
# no YouTube channels.
EMOTE_SOURCES = {platforms.KICK: "7TV", platforms.TWITCH: "7TV, BetterTTV and FrankerFaceZ"}


async def channel_emotes(platform: str, user_id: int | str) -> list[Emote]:
    """A channel's emotes beyond its platform's own, by its id there: its
    7TV set - and on Twitch its BetterTTV and FrankerFaceZ ones too (see
    twitch_emotes.py). Nothing for a platform whose channels have none (see
    EMOTE_SOURCES)."""
    if platform not in EMOTE_SOURCES:
        return []
    if platform == platforms.TWITCH:
        return await twitch_emotes.get_channel_emotes_on_twitch(str(user_id))
    return await get_channel_emotes(user_id, platform)


async def refresh_emote_pictures(
    broadcaster_id: int = GLOBAL_EMOTES_OWNER, platform: str = platforms.KICK, platform_user_id: str | None = None
) -> int:
    """Fetches a channel's emote sets again (see channel_emotes) and stores
    their pictures, leaving the detector's keywords as they are. A channel
    is asked about by its id on its own platform (a Kick channel's is the id
    it is kept by). Without a channel it is 7TV's global emotes, the ones
    every channel has, that are fetched. Returns how many pictures there
    are."""
    if broadcaster_id == GLOBAL_EMOTES_OWNER:
        emotes = await get_global_emotes()
    else:
        emotes = await channel_emotes(platform, platform_user_id or broadcaster_id)
    pictures = emote_pictures(emotes)
    conn = get_connection()
    try:
        replace_channel_emotes(conn, broadcaster_id, pictures)
    finally:
        conn.close()
    return len(pictures)


async def channel_badge_pictures(platform: str, name: str, user_id: int | str) -> list[channel_badges.Picture]:
    """A channel's own subscriber badges (see channel_badges.py), by its
    name and its id on its own platform."""
    if platform == platforms.TWITCH:
        settings = load_settings()
        token = await twitch_client.app_token(settings)
        sets = await twitch_client.get_chat_badges(str(user_id), settings.twitch_client_id, token)
        return channel_badges.from_twitch(sets)
    return channel_badges.from_kick(await get_site_channel(name))


async def refresh_badge_pictures(broadcaster_id: int, slug: str, platform_user_id: str | None = None) -> int:
    """Fetches a watched channel's subscriber badges again and stores them.
    A channel is asked about by its name and its id on its own platform (a
    Kick channel's is the id it is kept by). Returns how many there are."""
    platform, name = platforms.split(slug)
    pictures = await channel_badge_pictures(platform, name, platform_user_id or broadcaster_id)
    conn = get_connection()
    try:
        replace_channel_badges(conn, broadcaster_id, pictures)
    finally:
        conn.close()
    return len(pictures)


async def _store_badges_if_any(broadcaster_id: int, slug: str, platform_user_id: str | None = None) -> None:
    """Fetches and stores the subscriber badges of a channel just added -
    or says why it couldn't and leaves it without them."""
    try:
        await refresh_badge_pictures(broadcaster_id, slug, platform_user_id)
    except Exception as error:
        logger.warning("could not fetch the subscriber badges of %s: %r", slug, error)


async def add_channel_to_watchlist(slug: str) -> dict:
    """Raises ValueError (via get_channel_by_slug) if no such Kick channel exists."""
    settings = load_settings()
    token = await get_app_access_token(settings.kick_client_id, settings.kick_client_secret)

    channel = await get_channel_by_slug(slug, token)
    broadcaster_id = channel["broadcaster_user_id"]

    # Everything that can fail is fetched before anything is changed on
    # Kick's side: a 7TV outage used to leave a chat subscription behind for
    # a channel that never made it onto the local watchlist.
    emotes = await get_channel_emotes(broadcaster_id)

    # Subscribing twice creates a second subscription on Kick's side, so a
    # channel that is already subscribed (re-added, or re-run to refresh it)
    # is left as it is - same check-then-subscribe as the startup reconcile.
    subscribed = {subscription.get("broadcaster_user_id") for subscription in await get_event_subscriptions(token)}
    if broadcaster_id not in subscribed:
        await subscribe_chat_messages(broadcaster_id, token)

    conn = get_connection()
    try:
        add_streamer(conn, broadcaster_id, slug)
        laugh_count = store_channel_emotes(conn, broadcaster_id, emotes)
    finally:
        conn.close()
    await _store_badges_if_any(broadcaster_id, slug)

    return {
        "slug": slug,
        "broadcaster_user_id": broadcaster_id,
        "emote_count": len(emotes),
        "laugh_emote_count": laugh_count,
    }


async def add_twitch_channel(login: str) -> dict:
    """Puts a Twitch channel on the watchlist, under its name with
    Twitch's (see platforms.py). Raises ValueError if Twitch has no such
    channel, twitch_client.NotSetUp if the service has no Twitch
    application to ask with."""
    settings = load_settings()
    token = await twitch_client.app_token(settings)
    user = await twitch_client.get_user(login, settings.twitch_client_id, token)
    emotes = await channel_emotes(platforms.TWITCH, user["id"])

    slug = platforms.channel_name(platforms.TWITCH, user["login"])
    conn = get_connection()
    try:
        broadcaster_id = add_platform_streamer(conn, platforms.TWITCH, user["id"], slug)
        laugh_count = store_channel_emotes(conn, broadcaster_id, emotes)
    finally:
        conn.close()
    await _store_badges_if_any(broadcaster_id, slug, user["id"])

    return {
        "slug": slug,
        "broadcaster_user_id": broadcaster_id,
        "emote_count": len(emotes),
        "laugh_emote_count": laugh_count,
    }


async def add_youtube_channel(where: str) -> dict:
    """Puts a YouTube channel on the watchlist, under its handle with
    YouTube's (see platforms.py). `where` is its handle, or a link to its
    page or one of its videos. Raises ValueError if YouTube has no channel
    there."""
    channel = await youtube_client.find_channel(where)
    slug = platforms.channel_name(platforms.YOUTUBE, channel.login)
    conn = get_connection()
    try:
        broadcaster_id = add_platform_streamer(conn, platforms.YOUTUBE, channel.channel_id, slug)
    finally:
        conn.close()

    return {"slug": slug, "broadcaster_user_id": broadcaster_id, "emote_count": 0, "laugh_emote_count": 0}


async def add_to_watchlist(text: str) -> dict:
    """Puts on the watchlist the channel someone typed: a link to its page,
    `login@twitch`, `handle@youtube`, or a Kick channel's name (see
    platforms.parse)."""
    platform, name = platforms.parse(text)
    if platform == platforms.TWITCH:
        return await add_twitch_channel(name)
    if platform == platforms.YOUTUBE:
        return await add_youtube_channel(name)
    return await add_channel_to_watchlist(name)
