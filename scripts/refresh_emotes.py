"""CLI to refresh a watched channel's 7TV emote keywords without touching
its Kick event subscription (subscribing again would create a duplicate).

Usage: python scripts/refresh_emotes.py <channel_slug>
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kick_clip_hunter.config import load_settings
from kick_clip_hunter.db import get_connection, replace_channel_keywords
from kick_clip_hunter.detector import EMOTE_MENTION_LAUGH_WEIGHT, classify_emote_names
from kick_clip_hunter.kick_client import get_app_access_token, get_channel_by_slug
from kick_clip_hunter.seventv_client import get_channel_emote_names


async def main(slug: str) -> None:
    settings = load_settings()
    token = await get_app_access_token(settings.kick_client_id, settings.kick_client_secret)

    channel = await get_channel_by_slug(slug, token)
    broadcaster_id = channel["broadcaster_user_id"]

    emote_names = await get_channel_emote_names(broadcaster_id)
    keyword_weights = classify_emote_names(emote_names)
    laugh_count = sum(1 for weight in keyword_weights.values() if weight == EMOTE_MENTION_LAUGH_WEIGHT)
    print(
        f"Fetched {len(emote_names)} 7TV emote name(s) for {slug!r} "
        f"({laugh_count} classified as laugh-related)"
    )

    conn = get_connection()
    try:
        replace_channel_keywords(conn, broadcaster_id, keyword_weights)
    finally:
        conn.close()
    print(f"Refreshed keywords for {slug!r}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slug", help="Kick channel slug/username")
    args = parser.parse_args()
    asyncio.run(main(args.slug))
