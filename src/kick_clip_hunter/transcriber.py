"""Local speech-to-text for cut clips, via faster-whisper.

For stream comedy, what the streamer actually says carries much of the
signal, so the transcript is stored and shown per moment. Like the other
per-clip analysis steps it is pure data capture for a future learned
classifier - nothing here judges or scores a clip.

Runs on CPU: the host has no usable GPU accelerator, but transcription is a
task faster-whisper handles fine without one.

MODEL_SIZE is int8-quantized large-v3-turbo: the full multilingual model
(needed for Czech - the English-only distil/Parakeet/Canary variants aren't
an option here) at a size and speed that comfortably runs as a background
task on a 4-core CPU (~5-7x real-time once the model is loaded).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from .ml_loading import HEAVY_IMPORT_LOCK

if TYPE_CHECKING:
    from faster_whisper import WhisperModel

logger = logging.getLogger("kick_clip_hunter")

# Whisper is well known to hallucinate boilerplate subtitle-credit lines
# ("Titulky vytvořil...", "Přeložil...") on quiet/unclear stretches, for
# lower-resource languages like Czech where a lot of training data came from
# amateur-subtitled YouTube videos. Matched per-segment (not the whole
# transcript) since real speech can surround one hallucinated segment.
# Extend this list as new hallucinated phrases turn up in real transcripts.
_HALLUCINATION_RE = re.compile(
    r"titulky\s+(vytvoř|přelož)|^přelož(il|ila)\b|^titulky\s*[:\-]", re.IGNORECASE
)

MODEL_SIZE = "large-v3-turbo"
# Downloaded once (~1.6GB) and cached here rather than in HF's default
# ~/.cache, which lives on the small system drive on this machine.
MODEL_DOWNLOAD_ROOT = Path(__file__).resolve().parent.parent.parent / "data" / "whisper_models"
# Both streamers watched so far speak Czech (Slovak transcribes fine under
# the "cs" code too - the two are mutually intelligible and whisper doesn't
# distinguish them). Pinning the language skips a detection pass and avoids
# it ever guessing wrong on a quiet or music-heavy clip.
LANGUAGE = "cs"

_model: WhisperModel | None = None


def _get_model() -> WhisperModel:
    global _model
    with HEAVY_IMPORT_LOCK:
        if _model is None:
            # Imported on first use rather than at module level - see
            # ml_loading.py.
            from faster_whisper import WhisperModel

            MODEL_DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
            logger.info("loading whisper model %r (first use only)...", MODEL_SIZE)
            _model = WhisperModel(
                MODEL_SIZE, device="cpu", compute_type="int8", download_root=str(MODEL_DOWNLOAD_ROOT)
            )
    return _model


def transcribe_clip(clip_path: Path) -> str:
    """Runs synchronously (CPU-bound) - call via asyncio.to_thread.

    Returns the clip's speech as a single string, or "" if no speech was
    detected (vad_filter drops silence/music-only stretches rather than
    hallucinating text over them). Segments matching a known hallucinated
    boilerplate phrase (see _HALLUCINATION_RE) are dropped individually.
    """
    model = _get_model()
    segments, _info = model.transcribe(
        str(clip_path), language=LANGUAGE, vad_filter=True, beam_size=1
    )
    texts = (segment.text.strip() for segment in segments)
    return " ".join(text for text in texts if text and not _HALLUCINATION_RE.search(text)).strip()
