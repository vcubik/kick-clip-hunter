"""Local video-frame embedding for cut clips, via SigLIP2.

Video-side encoder in the "lightweight local encoder -> embedding -> stored
on the moment" pattern described in docs/moment-judge-design.md's
learned-classifier path; see audio_events.py for the audio counterpart. Pure
data capture for now - nothing here judges or scores a clip, that's later
work once enough rated data exists to train against.

Runs entirely on CPU - see moment-judge-design.md for why (no usable local
GPU accelerator on this host). A handful of frames per clip through a base-
size SigLIP2 keeps this fast enough as a background task even on CPU.
"""

import logging
import os
import subprocess
import tempfile
from pathlib import Path

# Downloaded once and cached here rather than in HF's default ~/.cache, which
# lives on the small system drive on this machine (same reasoning as
# transcriber.py's MODEL_DOWNLOAD_ROOT). Must be set before transformers/
# huggingface_hub touch the default cache location on import.
MODEL_DOWNLOAD_ROOT = Path(__file__).resolve().parent.parent.parent / "data" / "siglip2_models"
os.environ.setdefault("HF_HOME", str(MODEL_DOWNLOAD_ROOT))

import numpy as np
import torch
from PIL import Image
from transformers import AutoModel, AutoProcessor  # noqa: E402 - must follow HF_HOME setdefault above

from .recorder import FFMPEG_BIN

logger = logging.getLogger("kick_clip_hunter")

MODEL_ID = "google/siglip2-base-patch16-224"
FFPROBE_BIN = "ffprobe"
# 3 uniformly-sampled frames (roughly start/middle/end), each kept as its
# own vector rather than averaged into one - averaging across more frames
# doesn't add information, it just blurs together whatever happened in each
# one, which throws away exactly the temporal detail a future classifier
# might want (e.g. a fail near the end vs. calm throughout).
FRAME_COUNT = 3
EMBED_DIM = 768  # SigLIP2 base's pooled-output width; see encode_clip's blob layout
# Clips include a pre-roll (recorder.PRE_ROLL_SECONDS) before the moment
# that triggered them - the first several seconds are usually just lead-up,
# not the interesting part, so sampling starts a bit past the very beginning
# instead of "wasting" a sample point there.
SKIP_START_SECONDS = 10

_model = None
_processor = None


def _get_model():
    global _model, _processor
    if _model is None:
        MODEL_DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
        logger.info("loading SigLIP2 model %r (first use only)...", MODEL_ID)
        _model = AutoModel.from_pretrained(MODEL_ID).eval()
        _processor = AutoProcessor.from_pretrained(MODEL_ID)
    return _model, _processor


def _clip_duration_seconds(clip_path: Path) -> float:
    result = subprocess.run(
        [
            FFPROBE_BIN, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(clip_path),
        ],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def _extract_frames(clip_path: Path, count: int = FRAME_COUNT) -> list[Image.Image]:
    """Uniformly samples `count` frames across the clip via ffmpeg, skipping
    the first SKIP_START_SECONDS (falls back to sampling the whole clip if
    it's too short for that to leave anything).
    """
    duration = _clip_duration_seconds(clip_path)
    start = SKIP_START_SECONDS if duration > SKIP_START_SECONDS else 0.0
    sample_span = duration - start
    fps = count / sample_span if sample_span > 0 else count
    with tempfile.TemporaryDirectory() as tmp_dir:
        pattern = str(Path(tmp_dir) / "%02d.jpg")
        subprocess.run(
            [
                FFMPEG_BIN, "-y",
                "-ss", f"{start:.3f}",
                "-i", str(clip_path),
                "-vf", f"fps={fps:.6f}",
                "-frames:v", str(count),
                pattern,
            ],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        # .copy() detaches the image from the lazily-read file handle so it
        # survives past the tempdir being cleaned up below.
        return [Image.open(p).convert("RGB").copy() for p in sorted(Path(tmp_dir).glob("*.jpg"))]


def encode_clip(clip_path: Path) -> bytes:
    """Runs synchronously (CPU-bound) - call via asyncio.to_thread.

    Returns FRAME_COUNT L2-normalized float32 embeddings (one per sampled
    frame, no pooling), concatenated as raw bytes for the
    moments.frame_embedding BLOB column - or b"" if no frames could be
    extracted. There's no separate column for shape: a reader recovers the
    frame count as len(blob) // (EMBED_DIM * 4) and reshapes to
    (-1, EMBED_DIM).
    """
    frames = _extract_frames(clip_path)
    if not frames:
        return b""
    model, processor = _get_model()
    inputs = processor(images=frames, return_tensors="pt")
    with torch.no_grad():
        # This transformers version's get_image_features() returns the full
        # BaseModelOutputWithPooling rather than a bare tensor - the pooled
        # per-image embedding is .pooler_output ([num_frames, hidden_size]).
        features = model.get_image_features(**inputs).pooler_output
    normalized = features / features.norm(dim=-1, keepdim=True)
    return normalized.numpy().astype(np.float32).tobytes()
