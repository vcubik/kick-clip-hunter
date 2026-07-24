"""CLI to add a channel to the watchlist: subscribes to its chat.message.sent
events and records the streamer in the local database.

Usage: python scripts/subscribe.py <channel_slug>
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kick_clip_hunter.watchlist import add_channel_to_watchlist


async def main(slug: str) -> None:
    result = await add_channel_to_watchlist(slug)
    print(f"Found channel {slug!r}: broadcaster_user_id={result['broadcaster_user_id']}")
    print(
        f"Fetched {result['emote_count']} 7TV emote name(s) for {slug!r} "
        f"({result['laugh_emote_count']} classified as laugh-related)"
    )
    print(f"Added {slug!r} to the watchlist.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slug", help="Kick channel slug/username")
    args = parser.parse_args()
    asyncio.run(main(args.slug))
