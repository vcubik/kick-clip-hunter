"""CLI to refresh a watched channel's emotes - 7TV's, and on Twitch also
BetterTTV's and FrankerFaceZ's: the keywords the detector listens for and the
pictures the dashboard shows - and its subscriber badges, without touching
its Kick event subscription at all.

Usage: python scripts/refresh_emotes.py <channel>

<channel> is a Kick channel's name, or a Twitch channel on the watchlist: a
link to it, or its name followed by @twitch.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kick_clip_hunter import platforms
from kick_clip_hunter.config import load_settings
from kick_clip_hunter.db import get_connection, get_streamer_by_slug
from kick_clip_hunter.kick_client import get_app_access_token, get_channel_by_slug
from kick_clip_hunter.seventv_client import get_channel_emotes
from kick_clip_hunter.watchlist import EMOTE_SOURCES, channel_emotes, refresh_badge_pictures, store_channel_emotes


def _watched_elsewhere(platform: str, name: str) -> tuple[str, int, str]:
    """A channel of another platform than Kick is refreshed by what the
    watchlist keeps of it: its name there, the id it is kept by, its id on
    its platform."""
    slug = platforms.channel_name(platform, name.lower())
    conn = get_connection()
    try:
        row = get_streamer_by_slug(conn, slug)
    finally:
        conn.close()
    if row is None:
        raise SystemExit(f"{slug!r} is not on the watchlist - add it with subscribe.py first.")
    return slug, row["broadcaster_user_id"], row["platform_user_id"]


async def main(channel: str) -> None:
    platform, name = platforms.parse(channel)
    if platform == platforms.KICK:
        settings = load_settings()
        token = await get_app_access_token(settings.kick_client_id, settings.kick_client_secret)
        broadcaster_id = (await get_channel_by_slug(name, token))["broadcaster_user_id"]
        slug, user_id, emotes = name, None, await get_channel_emotes(broadcaster_id)
    else:
        slug, broadcaster_id, user_id = _watched_elsewhere(platform, name)
        emotes = await channel_emotes(platform, user_id)

    conn = get_connection()
    try:
        laugh_count = store_channel_emotes(conn, broadcaster_id, emotes)
    finally:
        conn.close()
    print(
        f"Fetched {len(emotes)} {EMOTE_SOURCES[platform]} emote name(s) for {slug!r} "
        f"({laugh_count} classified as laugh-related)"
    )
    print(f"Refreshed keywords for {slug!r}.")
    try:
        print(f"Fetched {await refresh_badge_pictures(broadcaster_id, slug, user_id)} subscriber badge(s).")
    except Exception as error:
        print(f"Could not fetch its subscriber badges: {error!r}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("channel", help="Kick channel name, or a watched Twitch channel's link or <name>@twitch")
    args = parser.parse_args()
    asyncio.run(main(args.channel))
