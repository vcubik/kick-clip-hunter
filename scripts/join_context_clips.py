"""CLI to give moments from before a moment had one video file the same:
joins each clip with the context clips kept next to it
(`moment_<id>_before.mp4`, `moment_<id>_after.mp4`) into one file, in the
clip's place, and records where the clip lies in it.

The dashboard plays one file per moment. A moment that still has its context
in separate files plays as its clip alone until this has been run; nothing is
re-encoded, and running it again does nothing more. Safe to run while the
server is up.

Usage: python scripts/join_context_clips.py [--dry-run]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kick_clip_hunter.db import get_connection, get_moments_with_clip_alone, update_moment_context
from kick_clip_hunter.recorder import CLIPS_DIR, context_clip_names, join_context_clips


def main(dry_run: bool = False) -> None:
    conn = get_connection()
    try:
        joined = 0
        for row in get_moments_with_clip_alone(conn):
            clip_path = CLIPS_DIR / row["clip_path"]
            names = context_clip_names(clip_path.name).values()
            context = [name for name in names if clip_path.with_name(name).exists()]
            if not clip_path.exists() or not context:
                continue
            if dry_run:
                print(f"moment {row['id']}: would join {clip_path.name} with {', '.join(context)}")
                joined += 1
                continue
            try:
                lengths = join_context_clips(clip_path)
            except Exception as e:
                print(f"moment {row['id']}: joining failed, files left as they were: {e}")
                continue
            if lengths is None:
                print(f"moment {row['id']}: could not be measured, files left as they were")
                continue
            before, clip, after = lengths
            update_moment_context(conn, row["id"], row["clip_duration"] or clip, before, after)
            print(f"moment {row['id']}: one file now, {before:.0f} s before the clip and {after:.0f} s after it")
            joined += 1
        print(f"{joined} moment(s) {'to join' if dry_run else 'joined'}")
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Only list what would be joined")
    args = parser.parse_args()
    main(args.dry_run)
