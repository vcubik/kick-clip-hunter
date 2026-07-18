"""Local sound-event tagging for cut clips, via PANNs (AudioSet CNN14).

Third local audio encoder, added alongside transcriber.py (speech-to-text)
and audio_events.py (SenseVoice language/emotion/event tags): SenseVoice's
event vocabulary is narrow (~8 classes - Speech/BGM/Applause/Laughter/Cry/
Sneeze/Breath/Cough) and is a secondary feature of an ASR model, not built
for noisy multi-source audio (game sound effects + mic + music all at once,
as in stream clips) - in practice it defaults to "unknown" on most real
clips (see the audio-events analysis this was built after).

PANNs (Cnn14) is trained on Google's AudioSet - 527 sound classes, purpose-
built for tagging exactly this kind of real-world polyphonic audio, and
distinguishes finer-grained reaction sounds (several Laughter subtypes,
Applause, Cheering, Shout, Screaming, Crowd, ...) that SenseVoice's 8-class
vocabulary can't.

Pure data capture, same as the other two encoders - nothing here judges or
scores a clip, that's later work once enough rated data exists.

Runs entirely on CPU - see moment-judge-design.md for why (no usable local
GPU accelerator on this host).

Windows quirk: panns_inference fetches its label list and model checkpoint
via a hardcoded `os.system('wget ...')` call. There's no wget on Windows,
so that call silently does nothing (os.system's return code is never
checked) - the library then crashes trying to read/load a file that was
never actually downloaded. Both are pre-downloaded here via urllib instead,
before panns_inference's own download logic ever gets a chance to run.
"""

import logging
import subprocess
import tempfile
import urllib.request
from pathlib import Path

logger = logging.getLogger("kick_clip_hunter")

# The label CSV's path is hardcoded inside panns_inference (Path.home() /
# "panns_data", not overridable) and is read at *import time* - so it must
# already exist before the first `from panns_inference import ...` anywhere
# in the process. It's tiny (a few KB of class names), so living outside
# data/ (unlike the checkpoint below) isn't worth fighting the library over.
_LABELS_CSV_PATH = Path.home() / "panns_data" / "class_labels_indices.csv"
_LABELS_CSV_URL = "http://storage.googleapis.com/us_audioset/youtube_corpus/v1/csv/class_labels_indices.csv"

# The model checkpoint (~300MB) *is* redirected here via AudioTagging's
# checkpoint_path argument - same reasoning as transcriber.py's
# MODEL_DOWNLOAD_ROOT (avoids the small system drive).
MODEL_DOWNLOAD_ROOT = Path(__file__).resolve().parent.parent.parent / "data" / "panns_models"
_CHECKPOINT_PATH = MODEL_DOWNLOAD_ROOT / "Cnn14_mAP=0.431.pth"
_CHECKPOINT_URL = "https://zenodo.org/record/3987831/files/Cnn14_mAP%3D0.431.pth?download=1"
_CHECKPOINT_MIN_BYTES = 3 * 10**8  # matches panns_inference's own corrupt-download check


def _ensure_downloaded(url: str, dest: Path, min_bytes: int = 1) -> None:
    if dest.exists() and dest.stat().st_size >= min_bytes:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    logger.info("downloading %s -> %s (first use only)...", url, dest)
    urllib.request.urlretrieve(url, dest)


_ensure_downloaded(_LABELS_CSV_URL, _LABELS_CSV_PATH)

from panns_inference import AudioTagging  # noqa: E402 - must follow the labels-CSV download above
from panns_inference import labels as AUDIOSET_LABELS  # noqa: E402

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

from .recorder import FFMPEG_BIN  # noqa: E402

MODEL_ID = "PANNs Cnn14 (AudioSet)"
SAMPLE_RATE = 32000  # Cnn14's expected input rate
TOP_N = 5  # how many top-scoring AudioSet labels to keep per clip
MIN_CONFIDENCE = 0.1  # drop tags the model itself isn't confident about, rather than always reporting exactly TOP_N

_model: AudioTagging | None = None


def _get_model() -> AudioTagging:
    global _model
    if _model is None:
        _ensure_downloaded(_CHECKPOINT_URL, _CHECKPOINT_PATH, _CHECKPOINT_MIN_BYTES)
        logger.info("loading %s model (first use only)...", MODEL_ID)
        _model = AudioTagging(checkpoint_path=str(_CHECKPOINT_PATH), device="cpu")
    return _model


def _extract_audio(clip_path: Path, dest: Path) -> None:
    subprocess.run(
        [
            FFMPEG_BIN, "-y", "-i", str(clip_path),
            "-ac", "1", "-ar", str(SAMPLE_RATE), "-vn",
            str(dest),
        ],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def tag_sound_events(clip_path: Path) -> tuple[str, bytes]:
    """Runs synchronously (CPU-bound) - call via asyncio.to_thread.

    Returns (tags, embedding):
    - tags: comma-separated "Label:confidence" for AudioSet classes scoring
      at least MIN_CONFIDENCE, highest first, capped at TOP_N - e.g.
      "Music:0.82, Speech:0.61, Laughter:0.34" - or "" if nothing cleared
      the confidence floor.
    - embedding: PANNs' own 2048-dim clip-level embedding, packed as
      float32 raw bytes for the moments.sound_embedding BLOB column, or
      b"" if audio extraction failed.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        wav_path = Path(tmp_dir) / "audio.wav"
        try:
            _extract_audio(clip_path, wav_path)
        except subprocess.CalledProcessError:
            return "", b""
        audio, _sr = sf.read(str(wav_path), dtype="float32")

    if audio.ndim > 1:
        audio = audio.mean(axis=1)

    model = _get_model()
    clipwise_output, embedding = model.inference(audio[None, :])
    scores = clipwise_output[0]
    embedding_vec = embedding[0].astype(np.float32)

    top_idx = np.argsort(scores)[::-1][:TOP_N]
    tags = ", ".join(
        f"{AUDIOSET_LABELS[i]}:{scores[i]:.2f}" for i in top_idx if scores[i] >= MIN_CONFIDENCE
    )
    return tags, embedding_vec.tobytes()
