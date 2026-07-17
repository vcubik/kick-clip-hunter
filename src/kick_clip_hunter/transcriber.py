"""Local speech-to-text for cut clips, via faster-whisper.

Step one of the moment-judge pipeline described in docs/moment-judge-design.md:
for stream comedy, what the streamer actually says carries most of the
signal, and it's the judge's primary planned evidence. Shipped standalone
first (transcript stored and shown per moment, no judge yet) to check that
cheaply, before spending API budget wiring up the judge itself.

This is the one piece of that pipeline that stays fully local - the host has
no usable LLM accelerator (see the design doc), but transcription is a CPU
task faster-whisper handles fine regardless of the GPU situation.

MODEL_SIZE is int8-quantized large-v3-turbo: the full multilingual model
(needed for Czech - the English-only distil/Parakeet/Canary variants aren't
an option here) at a size and speed that comfortably runs as a background
task on a 4-core CPU (~5-7x real-time once the model is loaded).
"""

import logging
from pathlib import Path

from faster_whisper import WhisperModel

logger = logging.getLogger("kick_clip_hunter")

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
    if _model is None:
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
    hallucinating text over them).
    """
    model = _get_model()
    segments, _info = model.transcribe(
        str(clip_path), language=LANGUAGE, vad_filter=True, beam_size=1
    )
    return " ".join(segment.text.strip() for segment in segments).strip()
