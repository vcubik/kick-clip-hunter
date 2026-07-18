"""Local audio event & emotion detection for cut clips, via SenseVoice (FunASR).

Runs alongside transcriber.py (faster-whisper) rather than replacing it:
SenseVoice's own transcribed words are discarded here, only its
language/emotion/event tags are kept. Actual speech-to-text stays whisper's
job, since that's the model tuned and verified for Czech quality.

Second local encoder in the "lightweight local encoder -> embedding/tags ->
stored on the moment" pattern described in docs/moment-judge-design.md's
learned-classifier path; see frame_encoder.py for the video counterpart.
Like transcription, this is pure data capture for now - nothing here judges
or scores a clip, that's later work once enough rated data exists to check
which tags actually correlate with a good moment.

Runs entirely on CPU - see moment-judge-design.md for why (no usable local
GPU accelerator on this host: AMD RX 480, no practical CUDA/ROCm on Windows).
"""

import logging
import os
import re
from pathlib import Path

# Downloaded once (~900MB) and cached here rather than in HF's default
# ~/.cache, which lives on the small system drive on this machine (same
# reasoning as transcriber.py's MODEL_DOWNLOAD_ROOT). Must be set before
# funasr/huggingface_hub touch the default cache location on import.
MODEL_DOWNLOAD_ROOT = Path(__file__).resolve().parent.parent.parent / "data" / "sensevoice_models"
os.environ.setdefault("HF_HOME", str(MODEL_DOWNLOAD_ROOT))

from funasr import AutoModel  # noqa: E402 - must follow the HF_HOME setdefault above

logger = logging.getLogger("kick_clip_hunter")

MODEL_ID = "FunAudioLLM/SenseVoiceSmall"

# SenseVoice's raw output embeds one <|lang|><|emotion|><|event|><|itn-flag|>
# tag group per VAD-detected segment before that segment's transcribed
# words - e.g. <|en|><|NEUTRAL|><|Speech|><|withitn|>. Confidence gaps show
# up as real tag values too (EMO_UNKNOWN, Event_UNK, nospeech), not just
# clean labels (HAPPY, Laughter, BGM, Applause, Cry, Sneeze, Cough, Breath,
# ...) - a real clip commonly yields several segments' worth of tags, e.g.
# "nospeech, NEUTRAL, Event_UNK, woitn, en, EMO_UNKNOWN, Speech, withitn".
_TAG_RE = re.compile(r"<\|([^|]+)\|>")

_model: AutoModel | None = None


def _get_model() -> AutoModel:
    global _model
    if _model is None:
        MODEL_DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
        logger.info("loading SenseVoice model %r (first use only)...", MODEL_ID)
        _model = AutoModel(
            model=MODEL_ID,
            vad_model="fsmn-vad",
            vad_kwargs={"max_single_segment_time": 30000},
            device="cpu",
            hub="hf",
            disable_update=True,
        )
    return _model


def detect_audio_events(clip_path: Path) -> str:
    """Runs synchronously (CPU-bound) - call via asyncio.to_thread.

    Returns the model's tags as a comma-separated string (see _TAG_RE above
    for what they look like), deduplicated in first-seen order. Kept as
    opaque raw tags rather than parsed into a judgment - deciding which ones
    matter, and how to handle multi-segment/low-confidence noise, is later
    work, once there's enough rated data to check against.
    """
    model = _get_model()
    result = model.generate(
        input=str(clip_path), cache={}, language="auto", use_itn=False, merge_vad=True
    )
    raw_text = result[0]["text"] if result else ""
    tags = _TAG_RE.findall(raw_text)
    return ", ".join(dict.fromkeys(tags))
