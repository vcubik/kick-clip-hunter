"""Backfill transcript/audio-event tags/frame embedding/sound-event tags for
any moment that has a clip but is missing one or more of them.

The live pipeline fills these in automatically right after a clip is
recorded (main.py's _create_clip_background and its transcribe/audio-events/
frame-encoding/sound-events siblings), but a manually imported clip
(import_clip.py, import_clips_dir.py) never goes through that path - it
just inserts a moment row with clip_path set. This script closes that gap,
one moment at a time, using the same functions the live pipeline calls.

Pure data capture, same as the live pipeline - this doesn't judge or score
anything, just fills in the same features for manually-imported clips that
live-detected ones already get automatically. Run it after any import.

Usage: python scripts/backfill_taste.py [--limit N]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kick_clip_hunter import audio_events, frame_encoder, sound_events, transcriber
from kick_clip_hunter.db import (
    get_connection,
    get_moments_missing_taste_data,
    update_moment_audio_events,
    update_moment_frame_embedding,
    update_moment_sound_embedding,
    update_moment_sound_events,
    update_moment_transcript,
)
from kick_clip_hunter.recorder import CLIPS_DIR


def main(limit: int | None) -> None:
    conn = get_connection()
    try:
        rows = get_moments_missing_taste_data(conn)
        if limit is not None:
            rows = rows[:limit]
        if not rows:
            print("nothing to backfill")
            return

        for row in rows:
            clip_path = CLIPS_DIR / row["clip_path"]
            if not clip_path.exists():
                print(f"moment {row['id']}: clip file missing on disk ({clip_path}), skipping")
                continue

            if row["transcript"] is None:
                try:
                    transcript = transcriber.transcribe_clip(clip_path)
                    update_moment_transcript(conn, row["id"], transcript)
                    print(f"moment {row['id']}: transcript saved ({len(transcript)} chars)")
                except Exception as e:
                    print(f"moment {row['id']}: transcription failed: {e}")

            if row["audio_events"] is None:
                try:
                    tags = audio_events.detect_audio_events(clip_path)
                    update_moment_audio_events(conn, row["id"], tags)
                    print(f"moment {row['id']}: audio events saved: {tags}")
                except Exception as e:
                    print(f"moment {row['id']}: audio event detection failed: {e}")

            if row["frame_embedding"] is None:
                try:
                    embedding = frame_encoder.encode_clip(clip_path)
                    if embedding:
                        update_moment_frame_embedding(conn, row["id"], embedding)
                        print(f"moment {row['id']}: frame embedding saved ({len(embedding)} bytes)")
                    else:
                        print(f"moment {row['id']}: no frames extracted, skipping")
                except Exception as e:
                    print(f"moment {row['id']}: frame encoding failed: {e}")

            if row["sound_events"] is None or row["sound_embedding"] is None:
                try:
                    tags, embedding = sound_events.tag_sound_events(clip_path)
                    if row["sound_events"] is None:
                        update_moment_sound_events(conn, row["id"], tags)
                    if row["sound_embedding"] is None and embedding:
                        update_moment_sound_embedding(conn, row["id"], embedding)
                    print(f"moment {row['id']}: sound events saved: {tags}")
                except Exception as e:
                    print(f"moment {row['id']}: sound event tagging failed: {e}")
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N moments (for testing)")
    args = parser.parse_args()
    main(args.limit)
