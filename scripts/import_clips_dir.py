"""Bulk-import every video file in a folder as moments, e.g. a folder of
clips saved outside this pipeline whose original streamer doesn't matter.

Usage: python scripts/import_clips_dir.py <folder> [channel_slug]

channel_slug defaults to "unknown" if omitted - same as any channel_slug not
on the watchlist (see import_clip.py), it's fine for rating/taste-model
purposes since broadcaster_user_id just falls back to 0.

Reuses import_clip.import_clip() per file - see that script's docstring for
what an import actually does (moves, not copies, the source file into
data/clips/; bypasses live chat detection; zeroed-out detector signals,
reason="manual_import"). One DB connection is shared across the whole
folder rather than reopened per file.

After importing, run scripts/backfill_taste.py to transcribe + tag + embed
everything that was just added.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from import_clip import import_clip  # noqa: E402 - same scripts/ dir, needs sys.path above first

from kick_clip_hunter.db import get_connection

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".ts"}


def main(folder: Path, channel_slug: str) -> None:
    if not folder.is_dir():
        print(f"error: {folder} is not a directory")
        sys.exit(1)

    files = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS)
    if not files:
        print(f"no video files found in {folder} (looked for {sorted(VIDEO_EXTENSIONS)})")
        return

    conn = get_connection()
    try:
        imported = 0
        for source in files:
            try:
                moment_id, clip_path = import_clip(conn, source, channel_slug)
                print(f"imported {source.name} -> moment {moment_id} ({clip_path})")
                imported += 1
            except Exception as e:
                print(f"failed to import {source.name}: {e}")
    finally:
        conn.close()

    print(f"\nImported {imported}/{len(files)} file(s).")
    print("Next steps:")
    print("  1. PYTHONPATH=src python scripts/backfill_taste.py   (transcribes + tags + embeds them)")
    print("  2. Rate them on the dashboard: http://localhost:8000/dashboard")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path, help="Folder containing video files to import")
    parser.add_argument(
        "channel_slug", nargs="?", default="unknown", help="Streamer slug to tag these with (default: unknown)"
    )
    args = parser.parse_args()
    main(args.folder, args.channel_slug)
