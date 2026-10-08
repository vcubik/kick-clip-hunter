"""Starting the service must not import the ML libraries.

faster-whisper, funasr, transformers/torch and panns_inference take anywhere
from ten seconds to two minutes to import, and every analysis step that
needs them is off by default. They are loaded on first use (ml_loading.py);
this guards against an import creeping back to module level, which would
make every restart slow again without any test failing.

The check runs in a fresh interpreter, because by the time this test runs
other tests may have loaded all sorts of things into this one.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
HEAVY = ["torch", "transformers", "funasr", "faster_whisper", "panns_inference", "soundfile", "ctranslate2", "librosa"]


def modules_loaded_by(statements: str, cwd: Path) -> list[str]:
    code = textwrap.dedent(
        f"""
        import json, sys
        {statements}
        print("LOADED " + json.dumps(sorted(name for name in {HEAVY!r} if name in sys.modules)))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": str(SRC)},
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    (line,) = [line for line in result.stdout.splitlines() if line.startswith("LOADED ")]
    return json.loads(line.removeprefix("LOADED "))


def test_importing_the_app_loads_no_ml_library(tmp_path):
    assert modules_loaded_by("import kick_clip_hunter.main", tmp_path) == []


def test_importing_the_analysis_modules_themselves_loads_none_either(tmp_path):
    statements = "from kick_clip_hunter import audio_events, frame_encoder, sound_events, transcriber"

    assert modules_loaded_by(statements, tmp_path) == []


def test_importing_the_app_does_not_touch_the_network_or_need_model_files(tmp_path):
    # sound_events used to download its label file as a side effect of being
    # imported; with an unusable proxy any such attempt fails the import.
    code = "import kick_clip_hunter.main"
    blocked = {"HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9", "NO_PROXY": ""}
    home = tmp_path / "empty-home"
    home.mkdir()

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env={**os.environ, **blocked, "PYTHONPATH": str(SRC), "HOME": str(home), "USERPROFILE": str(home)},
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert result.returncode == 0, result.stderr
    assert list(home.iterdir()) == []
