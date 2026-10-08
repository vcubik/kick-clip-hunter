"""Shared guard for the heavy ML libraries the per-clip analysis steps use.

faster-whisper, funasr, transformers/torch and panns_inference together take
anywhere from ten seconds to a couple of minutes just to import, and every
analysis step is off by default - so nothing imports them at startup anymore.
Each wrapper (transcriber.py, audio_events.py, frame_encoder.py,
sound_events.py) imports its libraries and loads its model the first time
it's actually asked for a result.

That first use happens on worker threads (asyncio.to_thread), potentially
several at once right after a clip is saved. HEAVY_IMPORT_LOCK makes those
first imports and model loads happen one at a time, the way they did when
everything was imported on the main thread at startup: these libraries were
never written to be imported for the first time from several threads at
once, and loading several multi-hundred-MB models in parallel on a CPU only
makes each of them slower. It is not held during inference.
"""

import threading

# Reentrant, so a wrapper can take it for its own imports and again inside
# its model loader.
HEAVY_IMPORT_LOCK = threading.RLock()
