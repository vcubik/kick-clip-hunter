"""CLI to add a channel to the watchlist: subscribes to its chat.message.sent
events and records the streamer in the local database.

Usage: python scripts/subscribe.py <channel_slug>
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kick_clip_hunter.config import load_settings
from kick_clip_hunter.db import add_streamer, get_connection
from kick_clip_hunter.kick_client import (
    get_app_access_token,
    get_channel_by_slug,
    subscribe_chat_messages,
)


async def main(slug: str) -> None:
    settings = load_settings()
    token = await get_app_access_token(settings.kick_client_id, settings.kick_client_secret)

    channel = await get_channel_by_slug(slug, token)
    broadcaster_id = channel["broadcaster_user_id"]
    print(f"Found channel {slug!r}: broadcaster_user_id={broadcaster_id}")

    result = await subscribe_chat_messages(broadcaster_id, token)
    print("Subscribed:", result)

    conn = get_connection()
    try:
        add_streamer(conn, broadcaster_id, slug)
    finally:
        conn.close()
    print(f"Added {slug!r} to the watchlist.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slug", help="Kick channel slug/username")
    args = parser.parse_args()
    asyncio.run(main(args.slug))
