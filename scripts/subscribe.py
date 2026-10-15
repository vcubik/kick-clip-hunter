"""CLI to add a channel to the watchlist and record the streamer in the local
database. A Kick channel's chat.message.sent events are subscribed (unless
they already are); a Twitch channel's chat is subscribed by the running
service, and a YouTube channel's chat is read by it while the channel is
live. Safe to run again for a channel that is already watched.

Usage: python scripts/subscribe.py <channel>

<channel> is a Kick channel's name, a link to a Kick, Twitch or YouTube
channel (or to a YouTube video of the channel), or a Twitch or YouTube
channel's name followed by @twitch or @youtube - a YouTube channel's name
being its handle.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kick_clip_hunter import platforms
from kick_clip_hunter.watchlist import EMOTE_SOURCES, add_to_watchlist


async def main(channel: str) -> None:
    result = await add_to_watchlist(channel)
    slug = result["slug"]
    print(f"Found channel {slug!r}: broadcaster_user_id={result['broadcaster_user_id']}")
    sources = EMOTE_SOURCES.get(platforms.platform_of(slug))
    if sources:
        print(
            f"Fetched {result['emote_count']} {sources} emote name(s) for {slug!r} "
            f"({result['laugh_emote_count']} classified as laugh-related)"
        )
    print(f"Added {slug!r} to the watchlist.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "channel", help="Kick channel name, a Kick, Twitch or YouTube link, <name>@twitch or <handle>@youtube"
    )
    args = parser.parse_args()
    asyncio.run(main(args.channel))
