"""CLI to create a real Kick clip for a channel's current livestream.

Requires a saved login session (see scripts/kick_login.py). Briefly opens a
visible browser window against the channel's page - see clip_creator.py for
why headless doesn't work here.

Usage: python scripts/create_clip.py <channel_slug> [--duration 30] [--start-time 150] [--title "..."]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kick_clip_hunter.clip_creator import create_clip
from kick_clip_hunter.kick_session import has_saved_session

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slug", help="Kick channel slug/username")
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--start-time", type=int, default=150)
    parser.add_argument("--title", default="")
    args = parser.parse_args()

    if not has_saved_session():
        print("No saved session found - run scripts/kick_login.py first.")
        sys.exit(1)

    clip = create_clip(args.slug, args.start_time, args.duration, args.title)
    print("Clip created:")
    for key, value in clip.items():
        print(f"  {key}: {value}")
