"""Import an externally-sourced clip (e.g. a Kick clip you downloaded outside
this pipeline) as a moment, so it can be rated and feed the taste model.

Usage: python scripts/import_clip.py <path-to-mp4> <channel_slug>

This bypasses live chat detection entirely - there's no real-time chat window
to measure, so the moment is stored with zeroed-out detector signals
(reason="manual_import", score=0, etc.) rather than fabricated ones. The taste
model still benefits from the transcript/embedding side; the numeric-signal
side is honestly "no signal available" for these.

After importing, run scripts/backfill_taste.py to transcribe + embed it (it
picks up any moment with a clip_path and no transcript yet), then rate it on
the dashboard like any other moment.
"""

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kick_clip_hunter.db import get_connection, get_streamers, insert_moment
from kick_clip_hunter.recorder import CLIPS_DIR


def main(source: Path, channel_slug: str) -> None:
    if not source.exists():
        print(f"error: {source} does not exist")
        sys.exit(1)

    conn = get_connection()
    try:
        broadcaster_user_id = 0
        for row in get_streamers(conn):
            if row["slug"].lower() == channel_slug.lower():
                broadcaster_user_id = row["broadcaster_user_id"]
                break
        else:
            print(
                f"note: {channel_slug!r} isn't on the current watchlist - "
                f"storing with broadcaster_user_id=0 (fine for rating/taste-model purposes)"
            )

        dest_dir = CLIPS_DIR / channel_slug
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_name = f"manual_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}_{source.name}"
        dest_path = dest_dir / dest_name
        shutil.copy2(source, dest_path)

        now = datetime.now(timezone.utc).isoformat()
        moment_id = insert_moment(
            conn,
            broadcaster_user_id=broadcaster_user_id,
            channel_slug=channel_slug,
            window_start=now,
            window_end=now,
            reason="manual_import",
            score=0.0,
            message_count=0,
            baseline_message_rate=0.0,
            current_message_rate=0.0,
            emote_count=0,
            keyword_hits=0,
        )
        clip_path = dest_path.relative_to(CLIPS_DIR).as_posix()
        conn.execute("UPDATE moments SET clip_path = ? WHERE id = ?", (clip_path, moment_id))
        conn.commit()
    finally:
        conn.close()

    print(f"Imported as moment {moment_id} (clip: {clip_path})")
    print("Next steps:")
    print("  1. PYTHONPATH=src python scripts/backfill_taste.py   (transcribes + embeds it)")
    print("  2. Rate it on the dashboard: http://localhost:8000/dashboard")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Path to the clip's mp4 file")
    parser.add_argument("channel_slug", help="Streamer slug this clip belongs to")
    args = parser.parse_args()
    main(args.source, args.channel_slug)
