"""CLI to add a channel to the watchlist and record the streamer in the local
database. A Kick channel's chat.message.sent events are subscribed (unless
they already are); a Twitch channel's chat is subscribed by the running
service. Safe to run again for a channel that is already watched.

Usage: python scripts/subscribe.py <channel>

<channel> is a Kick channel's name, a link to a Kick or Twitch channel, or a
Twitch channel's name followed by @twitch.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kick_clip_hunter.watchlist import add_to_watchlist


async def main(channel: str) -> None:
    result = await add_to_watchlist(channel)
    slug = result["slug"]
    print(f"Found channel {slug!r}: broadcaster_user_id={result['broadcaster_user_id']}")
    print(
        f"Fetched {result['emote_count']} 7TV emote name(s) for {slug!r} "
        f"({result['laugh_emote_count']} classified as laugh-related)"
    )
    print(f"Added {slug!r} to the watchlist.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("channel", help="Kick channel name, a Kick or Twitch link, or <name>@twitch")
    args = parser.parse_args()
    asyncio.run(main(args.channel))
