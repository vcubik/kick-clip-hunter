"""The four per-clip analysis wrappers (transcript, audio events, frame
embeddings, sound events).

The models themselves are multi-gigabyte downloads and are not run here.
What is tested is everything the wrappers add around them: how each library
is loaded (once, lazily, on CPU, from the project's own cache directory) and
how a model's raw output is turned into what gets stored on a moment. Each
library is replaced by a small stand-in registered under its import name.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
import wave
from pathlib import Path
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest

from kick_clip_hunter import audio_events, frame_encoder, sound_events, transcriber
from kick_clip_hunter.ml_loading import HEAVY_IMPORT_LOCK
from tests.support.media import FFMPEG

CLIP = Path("data/clips/some_channel/moment_1.mp4")


class Loads:
    """Records how a stand-in model was constructed, and takes a moment over
    it so that concurrent first uses really do overlap."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    def record(self, *args, **kwargs) -> None:
        time.sleep(0.02)
        self.calls.append((args, kwargs))


def stand_in_module(monkeypatch, name: str, **attributes) -> ModuleType:
    module = ModuleType(name)
    for attribute, value in attributes.items():
        setattr(module, attribute, value)
    monkeypatch.setitem(sys.modules, name, module)
    return module


@pytest.fixture
def whisper(monkeypatch, tmp_path) -> Loads:
    loads = Loads()

    class WhisperModel:
        def __init__(self, *args, **kwargs):
            loads.record(*args, **kwargs)

    stand_in_module(monkeypatch, "faster_whisper", WhisperModel=WhisperModel)
    monkeypatch.setattr(transcriber, "_model", None)
    monkeypatch.setattr(transcriber, "MODEL_DOWNLOAD_ROOT", tmp_path / "whisper_models")
    return loads


@pytest.fixture
def sensevoice(monkeypatch, tmp_path) -> Loads:
    loads = Loads()

    class AutoModel:
        def __init__(self, *args, **kwargs):
            loads.record(*args, **kwargs)

    stand_in_module(monkeypatch, "funasr", AutoModel=AutoModel)
    monkeypatch.setattr(audio_events, "_model", None)
    monkeypatch.setattr(audio_events, "MODEL_DOWNLOAD_ROOT", tmp_path / "sensevoice_models")
    return loads


@pytest.fixture
def siglip(monkeypatch, tmp_path) -> Loads:
    loads = Loads()

    class Pretrained:
        @classmethod
        def from_pretrained(cls, *args, **kwargs):
            loads.record(cls.__name__, *args, **kwargs)
            return cls()

        def eval(self):
            return self

    class AutoModel(Pretrained):
        pass

    class AutoProcessor(Pretrained):
        pass

    stand_in_module(monkeypatch, "transformers", AutoModel=AutoModel, AutoProcessor=AutoProcessor)
    monkeypatch.setattr(frame_encoder, "_model", None)
    monkeypatch.setattr(frame_encoder, "_processor", None)
    monkeypatch.setattr(frame_encoder, "MODEL_DOWNLOAD_ROOT", tmp_path / "siglip2_models")
    return loads


@pytest.fixture
def panns(monkeypatch, tmp_path) -> Loads:
    loads = Loads()
    downloads: list[str] = []
    loads.downloads = downloads

    class AudioTagging:
        def __init__(self, *args, **kwargs):
            loads.record(*args, **kwargs)

    class PannsModule(ModuleType):
        """Like the real library, unusable until its label file exists."""

        def __getattr__(self, name):
            if name.startswith("__"):  # the import system probing the module
                raise AttributeError(name)
            assert sound_events._LABELS_CSV_URL in downloads, (
                "panns_inference imported before its label CSV was ensured"
            )
            return {"AudioTagging": AudioTagging, "labels": ["Speech", "Laughter", "Music"]}[name]

    monkeypatch.setitem(sys.modules, "panns_inference", PannsModule("panns_inference"))
    monkeypatch.setattr(sound_events, "_ensure_downloaded", lambda url, dest, min_bytes=1: downloads.append(url))
    monkeypatch.setattr(sound_events, "_model", None)
    monkeypatch.setattr(sound_events, "_labels", [])
    monkeypatch.setattr(sound_events, "_CHECKPOINT_PATH", tmp_path / "panns_models" / "checkpoint.pth")
    return loads


class TestModelLoading:
    @pytest.mark.parametrize(
        ("fixture", "module", "loads_per_model"),
        [
            ("whisper", transcriber, 1),
            ("sensevoice", audio_events, 1),
            ("siglip", frame_encoder, 2),
            ("panns", sound_events, 1),
        ],
    )
    def test_a_model_is_loaded_once_even_when_first_used_from_several_threads(
        self, request, fixture, module, loads_per_model
    ):
        # Right after a clip is saved, analysis steps start on worker threads
        # at the same moment - and a second clip can arrive before the first
        # model has finished loading.
        loads = request.getfixturevalue(fixture)
        results = []
        barrier = threading.Barrier(8)

        def first_use():
            barrier.wait()
            results.append(module._get_model())

        threads = [threading.Thread(target=first_use) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert len(loads.calls) == loads_per_model
        assert len(results) == 8 and all(result is results[0] or result == results[0] for result in results)

    def test_models_of_different_wrappers_are_not_loaded_at_the_same_time(self, whisper, sensevoice, monkeypatch):
        overlapping = []
        loading = threading.Event()

        def exclusive(*args, **kwargs):
            if loading.is_set():
                overlapping.append("two loads at once")
            loading.set()
            time.sleep(0.05)
            loading.clear()

        monkeypatch.setattr(whisper, "record", exclusive)
        monkeypatch.setattr(sensevoice, "record", exclusive)

        threads = [threading.Thread(target=transcriber._get_model), threading.Thread(target=audio_events._get_model)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert overlapping == []

    def test_the_lock_is_reentrant(self):
        # A wrapper takes it for its imports and again inside its loader.
        with HEAVY_IMPORT_LOCK, HEAVY_IMPORT_LOCK:
            pass

    def test_whisper_runs_quantised_on_cpu_from_the_projects_cache(self, whisper):
        transcriber._get_model()

        ((args, kwargs),) = whisper.calls
        assert args == (transcriber.MODEL_SIZE,)
        assert kwargs == {
            "device": "cpu",
            "compute_type": "int8",
            "download_root": str(transcriber.MODEL_DOWNLOAD_ROOT),
        }
        assert transcriber.MODEL_DOWNLOAD_ROOT.is_dir()

    def test_sensevoice_runs_on_cpu_without_phoning_home_for_updates(self, sensevoice):
        audio_events._get_model()

        ((_args, kwargs),) = sensevoice.calls
        assert kwargs["model"] == audio_events.MODEL_ID
        assert kwargs["device"] == "cpu"
        assert kwargs["disable_update"] is True
        assert kwargs["hub"] == "hf"

    def test_siglip_loads_the_model_and_its_processor(self, siglip):
        model, processor = frame_encoder._get_model()

        assert [args for args, _kwargs in siglip.calls] == [
            ("AutoModel", frame_encoder.MODEL_ID),
            ("AutoProcessor", frame_encoder.MODEL_ID),
        ]
        assert type(model).__name__ == "AutoModel" and type(processor).__name__ == "AutoProcessor"

    def test_panns_gets_its_label_file_before_the_library_is_imported(self, panns):
        # The library reads that file at import time and, on Windows, cannot
        # download it itself (it shells out to wget).
        sound_events._get_model()

        assert panns.downloads == [sound_events._LABELS_CSV_URL, sound_events._CHECKPOINT_URL]
        ((_args, kwargs),) = panns.calls
        assert kwargs == {"checkpoint_path": str(sound_events._CHECKPOINT_PATH), "device": "cpu"}
        assert sound_events._labels == ["Speech", "Laughter", "Music"]


class TestDownloads:
    @pytest.fixture
    def fetched(self, monkeypatch) -> list[tuple[str, Path]]:
        calls: list[tuple[str, Path]] = []

        def urlretrieve(url, dest):
            calls.append((url, Path(dest)))
            Path(dest).write_bytes(b"x" * 100)

        monkeypatch.setattr(sound_events.urllib.request, "urlretrieve", urlretrieve)
        return calls

    def test_a_missing_file_is_downloaded_into_a_directory_created_for_it(self, fetched, tmp_path):
        target = tmp_path / "not" / "there" / "yet" / "labels.csv"

        sound_events._ensure_downloaded("http://127.0.0.1/labels.csv", target)

        assert fetched == [("http://127.0.0.1/labels.csv", target)]
        assert target.is_file()

    def test_a_file_already_there_is_not_downloaded_again(self, fetched, tmp_path):
        target = tmp_path / "labels.csv"
        target.write_bytes(b"already here")

        sound_events._ensure_downloaded("http://127.0.0.1/labels.csv", target)

        assert fetched == []

    def test_a_truncated_download_is_fetched_again(self, fetched, tmp_path):
        # An interrupted checkpoint download leaves a file that is far too small.
        target = tmp_path / "checkpoint.pth"
        target.write_bytes(b"partial")

        sound_events._ensure_downloaded("http://127.0.0.1/checkpoint.pth", target, min_bytes=50)

        assert len(fetched) == 1
        assert target.stat().st_size == 100


class TestTranscript:
    @pytest.fixture
    def transcribe(self, monkeypatch):
        calls = []

        def run(*texts: str) -> str:
            class Model:
                def transcribe(self, path, **options):
                    calls.append((path, options))
                    return iter([SimpleNamespace(text=text) for text in texts]), object()

            monkeypatch.setattr(transcriber, "_get_model", Model)
            return transcriber.transcribe_clip(CLIP)

        run.calls = calls
        return run

    def test_segments_are_joined_into_one_line(self, transcribe):
        assert transcribe(" to je konec ", "ne ne ne", " počkej ") == "to je konec ne ne ne počkej"

    def test_silence_gives_an_empty_transcript(self, transcribe):
        assert transcribe() == ""
        assert transcribe("", "   ") == ""

    @pytest.mark.parametrize(
        "hallucination",
        [
            "Titulky vytvořil JohnyX.",
            "titulky  přeložil někdo",
            "Přeložil John",
            "Přeložila Jana",
            "Titulky: studio",
            "Titulky - www",
        ],
    )
    def test_whispers_stock_subtitle_credits_are_dropped(self, transcribe, hallucination):
        assert transcribe("tak co", hallucination, "jedeme dál") == "tak co jedeme dál"

    @pytest.mark.parametrize("genuine", ["ty titulky jsou špatně", "kdo to přeložil", "nepřeložil bych to"])
    def test_real_speech_that_merely_mentions_those_words_is_kept(self, transcribe, genuine):
        assert transcribe(genuine) == genuine

    def test_the_clip_is_transcribed_as_czech_with_silence_filtered_out(self, transcribe):
        transcribe("ahoj")

        ((path, options),) = transcribe.calls
        assert path == str(CLIP)
        assert options == {"language": "cs", "vad_filter": True, "beam_size": 1}


class TestAudioEvents:
    @pytest.fixture
    def detect(self, monkeypatch):
        calls = []

        def run(result) -> str:
            class Model:
                def generate(self, **options):
                    calls.append(options)
                    return result

            monkeypatch.setattr(audio_events, "_get_model", Model)
            return audio_events.detect_audio_events(CLIP)

        run.calls = calls
        return run

    def test_keeps_the_tags_and_discards_the_words(self, detect):
        raw = "<|en|><|HAPPY|><|Laughter|><|withitn|>that was amazing"

        assert detect([{"text": raw}]) == "en, HAPPY, Laughter, withitn"

    def test_tags_repeated_across_segments_are_listed_once_in_first_seen_order(self, detect):
        raw = (
            "<|nospeech|><|NEUTRAL|><|Event_UNK|><|woitn|>"
            "<|en|><|NEUTRAL|><|Speech|><|woitn|>hi"
            "<|en|><|HAPPY|><|Laughter|><|woitn|>"
        )

        assert detect([{"text": raw}]) == "nospeech, NEUTRAL, Event_UNK, woitn, en, Speech, HAPPY, Laughter"

    @pytest.mark.parametrize("result", [[], [{"text": ""}], [{"text": "just words, no tags"}]])
    def test_nothing_recognisable_gives_an_empty_string(self, detect, result):
        assert detect(result) == ""

    def test_the_model_is_asked_to_detect_the_language_itself(self, detect):
        detect([{"text": ""}])

        (options,) = detect.calls
        assert options["input"] == str(CLIP)
        assert options["language"] == "auto"


class TestSoundEvents:
    LABELS = ("Speech", "Laughter", "Music", "Applause", "Silence", "Shout", "Clapping")

    @pytest.fixture
    def tag(self, monkeypatch):
        """Runs tag_sound_events with a stand-in model that returns the given
        per-class scores; `received` holds the audio the model was given."""
        received = []

        def run(scores, *, audio=None, extraction_fails=False):
            audio = np.zeros(32000, dtype=np.float32) if audio is None else audio
            embedding = np.arange(2048, dtype=np.float64) / 2048

            class Model:
                def inference(self, batch):
                    received.append(batch)
                    return np.array([scores], dtype=np.float32), np.array([embedding])

            def extract_audio(clip_path, dest):
                if extraction_fails:
                    raise subprocess.CalledProcessError(1, "ffmpeg")

            def get_model():
                received.append("model loaded")
                return Model()

            monkeypatch.setitem(sys.modules, "soundfile", SimpleNamespace(read=lambda path, dtype: (audio, 32000)))
            monkeypatch.setattr(sound_events, "_extract_audio", extract_audio)
            monkeypatch.setattr(sound_events, "_get_model", get_model)
            monkeypatch.setattr(sound_events, "_labels", self.LABELS)
            return sound_events.tag_sound_events(CLIP)

        run.received = received
        return run

    def test_lists_the_most_confident_labels_first(self, tag):
        tags, _embedding = tag([0.61, 0.34, 0.82, 0.05, 0.0, 0.2, 0.15])

        assert tags == "Music:0.82, Speech:0.61, Laughter:0.34, Shout:0.20, Clapping:0.15"

    def test_never_more_than_the_top_few(self, tag):
        tags, _embedding = tag([0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3])

        assert len(tags.split(", ")) == sound_events.TOP_N
        assert tags.startswith("Speech:0.90, Laughter:0.80")

    def test_labels_the_model_is_unsure_about_are_left_out(self, tag):
        tags, _embedding = tag([0.5, 0.09, 0.02, 0.0, 0.0, 0.0, 0.0])

        assert tags == "Speech:0.50"

    def test_a_clip_with_nothing_confident_gets_no_tags_but_still_an_embedding(self, tag):
        tags, embedding = tag([0.05, 0.02, 0.01, 0.0, 0.0, 0.0, 0.0])

        assert tags == ""
        assert len(embedding) == 2048 * 4

    def test_the_embedding_is_stored_as_float32(self, tag):
        _tags, embedding = tag([0.9, 0, 0, 0, 0, 0, 0])

        restored = np.frombuffer(embedding, dtype=np.float32)
        assert restored.shape == (2048,)
        assert restored[1024] == pytest.approx(0.5)

    def test_stereo_audio_is_mixed_down_before_tagging(self, tag):
        stereo = np.stack([np.ones(1000, dtype=np.float32), -np.ones(1000, dtype=np.float32)], axis=1)

        tag([0.9, 0, 0, 0, 0, 0, 0], audio=stereo)

        batch = tag.received[-1]
        assert batch.shape == (1, 1000)
        assert np.allclose(batch, 0.0)

    def test_a_clip_whose_audio_cannot_be_extracted_is_skipped_without_loading_the_model(self, tag):
        assert tag([0.9, 0, 0, 0, 0, 0, 0], extraction_fails=True) == ("", b"")
        assert tag.received == []


@pytest.mark.ffmpeg
class TestAudioExtraction:
    def test_a_clips_sound_is_extracted_as_mono_at_the_models_sample_rate(self, tmp_path):
        clip = tmp_path / "clip.mp4"
        # fmt: off
        subprocess.run(
            [
                FFMPEG, "-y", "-v", "error",
                "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=2",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=2",
                "-ac", "2", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
                str(clip),
            ],
            check=True,
        )
        # fmt: on
        wav = tmp_path / "audio.wav"

        sound_events._extract_audio(clip, wav)

        with wave.open(str(wav)) as audio:
            assert audio.getnchannels() == 1
            assert audio.getframerate() == sound_events.SAMPLE_RATE
            assert audio.getnframes() == pytest.approx(2 * sound_events.SAMPLE_RATE, rel=0.05)

    def test_a_file_that_is_not_media_is_an_error_for_the_caller_to_handle(self, tmp_path):
        not_media = tmp_path / "clip.mp4"
        not_media.write_bytes(b"definitely not a video")

        with pytest.raises(subprocess.CalledProcessError):
            sound_events._extract_audio(not_media, tmp_path / "audio.wav")


class TestFrameEmbedding:
    def test_a_clip_with_no_decodable_frames_gives_no_embedding_and_loads_no_model(self, monkeypatch):
        def must_not_load():
            raise AssertionError("the model must not be loaded when there is nothing to encode")

        monkeypatch.setattr(frame_encoder, "_extract_frames", lambda clip_path, count: [])
        monkeypatch.setattr(frame_encoder, "_get_model", must_not_load)

        assert frame_encoder.encode_clip(CLIP) == b""

    def test_frames_are_requested_in_groups_that_average_into_the_stored_vectors(self, monkeypatch):
        requested = []
        monkeypatch.setattr(frame_encoder, "_extract_frames", lambda clip_path, count: requested.append(count) or [])

        frame_encoder.encode_clip(CLIP)

        assert requested == [frame_encoder.FRAME_COUNT * frame_encoder.FRAMES_PER_VECTOR]


@pytest.mark.ffmpeg
class TestFrameExtraction:
    """Real ffmpeg, real images - only the embedding model is absent."""

    @pytest.fixture
    def clip(self, tmp_path):

        def make(seconds: int) -> Path:
            path = tmp_path / f"clip_{seconds}s.mp4"
            # fmt: off
            subprocess.run(
                [
                    FFMPEG, "-y", "-v", "error",
                    "-f", "lavfi", "-i", f"testsrc=size=160x120:rate=10:duration={seconds}",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    str(path),
                ],
                check=True,
            )
            # fmt: on
            return path

        return make

    def test_reads_the_clips_duration(self, clip):
        assert frame_encoder._clip_duration_seconds(clip(6)) == pytest.approx(6.0, abs=0.2)

    def test_samples_the_requested_number_of_frames_as_rgb_images(self, clip):
        frames = frame_encoder._extract_frames(clip(6), count=3)

        assert len(frames) == 3
        assert {frame.size for frame in frames} == {(160, 120)}
        assert {frame.mode for frame in frames} == {"RGB"}

    def test_a_long_clip_is_sampled_after_its_lead_in(self, clip):
        # The first seconds of a clip are pre-roll; sampling skips them.
        long_clip = clip(frame_encoder.SKIP_START_SECONDS + 8)

        frames = frame_encoder._extract_frames(long_clip, count=4)

        assert len(frames) == 4

    def test_frames_are_distinct_points_in_time(self, clip):
        frames = frame_encoder._extract_frames(clip(6), count=3)

        # The test pattern shows a running counter, so no two samples match.
        assert len({frame.tobytes() for frame in frames}) == 3

    def test_the_images_outlive_the_temporary_files_they_were_read_from(self, clip):
        frames = frame_encoder._extract_frames(clip(4), count=2)

        for frame in frames:
            frame.load()  # would fail if still tied to a deleted file
            assert frame.getpixel((0, 0)) is not None
