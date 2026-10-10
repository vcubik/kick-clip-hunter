"""From a chat reaction to a stored moment, an open moment session, and the
background work that turns it into a clip.

Chat arrives as signed webhook deliveries; the detector is given five minutes
of history first so it has a baseline. The app's waits are time-compressed by
the `service` fixture, so sessions close and clips are "cut" within
milliseconds - the recorder buffer is empty here, which is exactly the
no-footage path (real clip cutting is covered in tests/e2e).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kick_clip_hunter import db, detector, recorder
from kick_clip_hunter.kick_stream import StreamUrlError
from kick_clip_hunter.recorder import RecorderError

pytestmark = pytest.mark.anyio

CHANNEL = "some_channel"
WINDOW = timedelta(seconds=detector.SHORT_WINDOW_SECONDS)


def crowd_needed() -> int:
    """The smallest crowd that fires on the 10-chatter history `warm_up` builds."""
    return detector._dynamic_min_reaction_unique(10)


def window(moment) -> timedelta:
    return datetime.fromisoformat(moment["window_end"]) - datetime.fromisoformat(moment["window_start"])


class TestDetection:
    async def test_a_crowd_laughing_becomes_one_stored_moment(self, service):
        user_id = service.watch(CHANNEL)
        service.warm_up(CHANNEL)

        await service.crowd_laughs(CHANNEL, people=8)

        (moment,) = service.moments()
        assert moment["channel_slug"] == CHANNEL
        assert moment["broadcaster_user_id"] == user_id
        assert moment["reason"] == "laugh"
        assert moment["score"] > 0
        assert moment["keyword_hits"] >= crowd_needed()
        assert moment["clip_path"] is None and moment["rating"] is None

    async def test_ordinary_chat_does_not(self, service):
        service.watch(CHANNEL)
        service.warm_up(CHANNEL)

        for number in range(20):
            await service.chat(CHANNEL, f"viewer{number % 10}", f"nothing special {number}")

        assert service.moments() == []

    async def test_no_moment_while_the_channel_has_no_history_yet(self, service):
        service.watch(CHANNEL)

        await service.crowd_laughs(CHANNEL, people=30)

        assert service.moments() == []

    async def test_the_moment_window_starts_as_the_detection_window(self, service):
        service.watch(CHANNEL)
        service.warm_up(CHANNEL)
        before = datetime.now(timezone.utc)

        await service.crowd_laughs(CHANNEL, people=crowd_needed())

        (moment,) = service.moments()
        assert window(moment) == WINDOW
        assert before <= datetime.fromisoformat(moment["window_end"]) <= datetime.now(timezone.utc)

    async def test_stream_time_comes_from_when_kick_says_the_stream_started(self, service):
        service.watch(CHANNEL, live_for_seconds=5400)
        service.warm_up(CHANNEL)

        await service.crowd_laughs(CHANNEL)

        (moment,) = service.moments()
        assert moment["stream_elapsed_seconds"] == pytest.approx(5400, abs=5)

    async def test_stream_time_is_unknown_when_kick_reports_the_channel_offline(self, service):
        service.watch(CHANNEL, live=False)
        service.warm_up(CHANNEL)

        await service.crowd_laughs(CHANNEL)

        (moment,) = service.moments()
        assert moment["stream_elapsed_seconds"] is None

    async def test_a_moment_is_kept_even_if_kick_cannot_be_asked_when_the_stream_started(self, service, caplog):
        # The lookup only labels the moment. The detector's cooldown has
        # already started by then, so a moment dropped here would be gone.
        service.watch(CHANNEL)
        service.warm_up(CHANNEL)
        service.api.failures["/channels"] = 503

        with caplog.at_level(logging.WARNING, logger="kick_clip_hunter"):
            await service.crowd_laughs(CHANNEL)  # every delivery is answered with a 200

        (moment,) = service.moments()
        assert moment["reason"] == "laugh"
        assert moment["stream_elapsed_seconds"] is None
        assert any("could not look up the stream's start time" in r.getMessage() for r in caplog.records)

    async def test_the_lookup_is_tried_again_for_the_next_moment(self, service, monkeypatch):
        monkeypatch.setattr(detector, "COOLDOWN_SECONDS", 0)
        service.watch(CHANNEL, live_for_seconds=1200)
        service.warm_up(CHANNEL)
        service.api.failures["/channels"] = 503
        await service.crowd_laughs(CHANNEL, people=crowd_needed())
        await service.settle()

        service.api.failures.clear()
        await service.chat(CHANNEL, "latecomer", "xDDD")

        first, second = service.moments()
        assert first["stream_elapsed_seconds"] is None
        assert second["stream_elapsed_seconds"] == pytest.approx(1200, abs=5)

    async def test_the_stream_start_is_looked_up_once_and_cached(self, service):
        service.watch(CHANNEL)

        first = await service.main._stream_elapsed_seconds(CHANNEL)
        second = await service.main._stream_elapsed_seconds(CHANNEL)

        assert first == pytest.approx(3600, abs=5) and second == pytest.approx(3600, abs=5)
        assert len(service.api.calls("GET", "/channels")) == 1

    async def test_the_cached_stream_start_expires(self, service, monkeypatch):
        service.watch(CHANNEL)
        await service.main._stream_elapsed_seconds(CHANNEL)

        monkeypatch.setattr(service.main, "STREAM_INFO_CACHE_SECONDS", -1)
        await service.main._stream_elapsed_seconds(CHANNEL)

        assert len(service.api.calls("GET", "/channels")) == 2

    async def test_moments_of_different_channels_are_independent(self, service):
        service.watch("channel_a")
        service.watch("channel_b")
        service.warm_up("channel_a")
        service.warm_up("channel_b")

        await service.crowd_laughs("channel_a")
        await service.crowd_laughs("channel_b")

        assert [moment["channel_slug"] for moment in service.moments()] == ["channel_a", "channel_b"]


class TestMomentSession:
    async def test_a_moment_stays_open_until_the_reaction_has_been_quiet(self, service, monkeypatch):
        # Long enough that the session is certainly still open when checked.
        monkeypatch.setattr(service.main, "MOMENT_SESSION_QUIET_SECONDS", 0.5)
        service.watch(CHANNEL)
        service.warm_up(CHANNEL)

        await service.crowd_laughs(CHANNEL, people=crowd_needed())
        assert list(service.main._moment_sessions) == [CHANNEL]

        await service.settle()
        assert service.main._moment_sessions == {}

    async def test_a_reaction_that_stops_at_the_trigger_is_not_extended(self, service):
        service.watch(CHANNEL)
        service.warm_up(CHANNEL)

        await service.crowd_laughs(CHANNEL, people=crowd_needed())
        await service.settle()

        (moment,) = service.moments()
        assert window(moment) == WINDOW

    async def test_new_people_joining_in_after_the_trigger_extend_it(self, service, monkeypatch):
        # A fresh crowd keeps the reaction "active" for as long as their
        # laughs stay in the detector's short window, so this session ends at
        # its hard cap - which is shortened here to keep the test quick.
        monkeypatch.setattr(service.main, "MOMENT_SESSION_QUIET_SECONDS", 5.0)
        monkeypatch.setattr(service.main, "MOMENT_SESSION_MAX_SECONDS", 1.0)
        service.watch(CHANNEL)
        service.warm_up(CHANNEL)
        await service.crowd_laughs(CHANNEL, people=crowd_needed())
        await service.clock_tick()

        for number in range(crowd_needed()):
            await service.chat(CHANNEL, f"latecomer{number}", "xDDD")
        await service.settle()

        (moment,) = service.moments()
        assert window(moment) > WINDOW

    async def test_one_straggler_does_not_extend_it(self, service, monkeypatch):
        monkeypatch.setattr(service.main, "MOMENT_SESSION_QUIET_SECONDS", 0.2)
        service.watch(CHANNEL)
        service.warm_up(CHANNEL)
        await service.crowd_laughs(CHANNEL, people=crowd_needed())
        await service.clock_tick()

        await service.chat(CHANNEL, "straggler", "xd")
        await service.settle()

        (moment,) = service.moments()
        assert window(moment) == WINDOW

    async def test_a_re_fire_while_a_moment_is_open_does_not_open_a_second_one(self, service, monkeypatch):
        monkeypatch.setattr(detector, "COOLDOWN_SECONDS", 0)  # let every further laugh re-fire
        monkeypatch.setattr(service.main, "MOMENT_SESSION_QUIET_SECONDS", 1.0)
        service.watch(CHANNEL)
        service.warm_up(CHANNEL)

        await service.crowd_laughs(CHANNEL, people=12)

        assert len(service.moments()) == 1
        assert list(service.main._moment_sessions) == [CHANNEL]

    async def test_once_it_has_closed_a_new_reaction_is_a_new_moment(self, service, monkeypatch):
        monkeypatch.setattr(detector, "COOLDOWN_SECONDS", 0)
        service.watch(CHANNEL)
        service.warm_up(CHANNEL)
        await service.crowd_laughs(CHANNEL, people=crowd_needed())
        await service.settle()

        await service.chat(CHANNEL, "latecomer", "xDDD")
        await service.settle()

        assert len(service.moments()) == 2

    async def test_without_buffered_footage_the_moment_is_kept_without_a_clip(self, service, caplog):
        service.watch(CHANNEL)
        service.warm_up(CHANNEL)

        with caplog.at_level(logging.INFO, logger="kick_clip_hunter"):
            await service.crowd_laughs(CHANNEL, people=crowd_needed())
            await service.settle()

        (moment,) = service.moments()
        assert moment["clip_path"] is None
        assert any("no buffered footage yet for moment" in record.getMessage() for record in caplog.records)
        assert not any(record.levelno >= logging.ERROR for record in caplog.records)


@pytest.fixture
def moment(service) -> int:
    """A stored moment to cut a clip for; returns its id."""
    user_id = service.watch(CHANNEL)
    conn = db.get_connection()
    try:
        return db.insert_moment(
            conn,
            broadcaster_user_id=user_id,
            channel_slug=CHANNEL,
            window_start=START.isoformat(),
            window_end=END.isoformat(),
            reason="laugh",
            score=10.0,
            message_count=12,
            baseline_message_rate=1.0,
            current_message_rate=1.2,
            emote_count=0,
            keyword_hits=8,
        )
    finally:
        conn.close()


START = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
END = START + timedelta(seconds=10)
# What the stand-in cutter says its clip holds: whole segments around the
# window asked for, on the stream's clock.
CLIP_HOLDS = (START - timedelta(seconds=26), END + timedelta(seconds=1))


@pytest.fixture
def clip_cutter(service, monkeypatch):
    """Replaces the two calls into the recorder with stand-ins that write
    placeholder files and remember how they were called."""

    class Cutter:
        def __init__(self) -> None:
            self.clip_error: Exception | None = None
            self.context_error: Exception | None = None
            self.clip_calls: list = []
            self.context_calls: list = []

    cutter = Cutter()

    async def create_clip(channel, start, end, name, post_roll_seconds):
        cutter.clip_calls.append((channel, start, end, name, post_roll_seconds))
        if cutter.clip_error is not None:
            raise cutter.clip_error
        path = recorder.CLIPS_DIR / channel / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"clip")
        return recorder.CutClip(path, CLIP_HOLDS[0], CLIP_HOLDS[1])

    async def create_context(channel, start, end, name, post_roll_seconds):
        cutter.context_calls.append((channel, start, end, name, post_roll_seconds))
        if cutter.context_error is not None:
            raise cutter.context_error
        return {"before": Path("before.mp4"), "after": Path("after.mp4")}

    monkeypatch.setattr(service.main.recording_manager, "create_clip_for_moment", create_clip)
    monkeypatch.setattr(service.main.recording_manager, "create_context_clips_for_moment", create_context)
    return cutter


class TestCuttingTheClip:
    async def cut(self, service, moment_id: int, post_roll: int = 10) -> None:
        await service.main._create_clip_background(moment_id, CHANNEL, START, END, post_roll_seconds=post_roll)
        await service.settle()

    async def test_the_clip_is_cut_for_the_moments_window_and_recorded_on_it(self, service, moment, clip_cutter):
        await self.cut(service, moment, post_roll=7)

        assert clip_cutter.clip_calls == [(CHANNEL, START, END, f"moment_{moment}.mp4", 7)]
        (row,) = service.moments()
        assert row["clip_path"] == f"{CHANNEL}/moment_{moment}.mp4"

    async def test_the_stretch_of_the_broadcast_the_clip_holds_is_recorded_with_it(self, service, moment, clip_cutter):
        # What was cut, not what was asked for: the dashboard lines chat up
        # with the picture by it.
        await self.cut(service, moment)

        (row,) = service.moments()
        assert datetime.fromisoformat(row["clip_start"]) == CLIP_HOLDS[0]
        assert row["clip_duration"] == (CLIP_HOLDS[1] - CLIP_HOLDS[0]).total_seconds()

    async def test_it_waits_for_the_post_roll_to_be_broadcast_before_cutting(
        self, service, moment, clip_cutter, monkeypatch
    ):
        slept: list[float] = []

        class RecordingSleep:
            """main.py's view of asyncio, with sleeps noted instead of slept."""

            def __getattr__(self, name):
                return getattr(asyncio, name)

            async def sleep(self, seconds):
                if not slept:
                    assert clip_cutter.clip_calls == [], "the clip was cut before waiting for its post-roll"
                slept.append(seconds)

        monkeypatch.setattr(service.main, "asyncio", RecordingSleep())

        await self.cut(service, moment, post_roll=7)

        assert slept == [
            7 + recorder.CLIP_SETTLE_SECONDS,
            recorder.CONTEXT_AFTER_SECONDS + recorder.CLIP_SETTLE_SECONDS,
        ]

    async def test_context_clips_are_cut_with_the_same_window(self, service, moment, clip_cutter):
        await self.cut(service, moment, post_roll=7)

        assert clip_cutter.context_calls == [(CHANNEL, START, END, f"moment_{moment}.mp4", 7)]

    @pytest.mark.parametrize(
        "error",
        [RecorderError("No buffered segments"), StreamUrlError("not live"), RuntimeError("ffmpeg exploded")],
        ids=["no-footage", "stream-not-live", "unexpected-error"],
    )
    async def test_a_failed_cut_leaves_the_moment_without_a_clip_and_nothing_else_runs(
        self, service, moment, clip_cutter, error
    ):
        clip_cutter.clip_error = error

        await self.cut(service, moment)

        (row,) = service.moments()
        assert row["clip_path"] is None
        assert clip_cutter.context_calls == []

    async def test_only_an_unexpected_failure_is_logged_as_an_error(self, service, moment, clip_cutter, caplog):
        with caplog.at_level(logging.INFO, logger="kick_clip_hunter"):
            clip_cutter.clip_error = RecorderError("No buffered segments")
            await self.cut(service, moment)
            assert not any(record.levelno >= logging.ERROR for record in caplog.records)

            clip_cutter.clip_error = RuntimeError("ffmpeg exploded")
            await self.cut(service, moment)

        errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
        assert len(errors) == 1
        assert "clip creation failed" in errors[0].getMessage() and errors[0].exc_info is not None

    async def test_failing_context_clips_do_not_take_the_clip_with_them(self, service, moment, clip_cutter, caplog):
        clip_cutter.context_error = RuntimeError("disk full")

        with caplog.at_level(logging.ERROR, logger="kick_clip_hunter"):
            await self.cut(service, moment)

        (row,) = service.moments()
        assert row["clip_path"] == f"{CHANNEL}/moment_{moment}.mp4"
        assert any("context clips failed" in record.getMessage() for record in caplog.records)


class TestPerClipAnalysis:
    """Four optional steps, each behind its own switch, each independent."""

    @pytest.fixture
    def analysis(self, service, monkeypatch):
        """Stand-ins for the four model wrappers, recording which ran."""
        ran: list[str] = []
        results = {
            "transcript": "to je konec",
            "audio_events": "cs, HAPPY, Laughter",
            "frames": b"\x01\x02\x03\x04",
            "sound_events": ("Laughter:0.80, Speech:0.55", b"\x09\x08\x07"),
        }

        def step(name):
            def run(clip_path):
                ran.append(name)
                result = results[name]
                if isinstance(result, Exception):
                    raise result
                return result

            return run

        main = service.main
        monkeypatch.setattr(main.transcriber, "transcribe_clip", step("transcript"))
        monkeypatch.setattr(main.audio_events, "detect_audio_events", step("audio_events"))
        monkeypatch.setattr(main.frame_encoder, "encode_clip", step("frames"))
        monkeypatch.setattr(main.sound_events, "tag_sound_events", step("sound_events"))
        return ran, results

    async def cut(self, service, moment_id: int):
        await service.main._create_clip_background(moment_id, CHANNEL, START, END, post_roll_seconds=10)
        await service.settle()
        (row,) = service.moments()
        return row

    async def test_nothing_is_analysed_unless_switched_on(self, service, moment, clip_cutter, analysis):
        ran, _results = analysis

        row = await self.cut(service, moment)

        assert ran == []
        assert row["clip_path"] is not None
        assert [
            row[c] for c in ("transcript", "audio_events", "frame_embedding", "sound_events", "sound_embedding")
        ] == [None] * 5

    @pytest.mark.parametrize(
        ("setting", "step", "stored"),
        [
            ("transcript", "transcript", {"transcript": "to je konec"}),
            ("audio_events", "audio_events", {"audio_events": "cs, HAPPY, Laughter"}),
            ("frames", "frames", {"frame_embedding": b"\x01\x02\x03\x04"}),
            (
                "sound_events",
                "sound_events",
                {"sound_events": "Laughter:0.80, Speech:0.55", "sound_embedding": b"\x09\x08\x07"},
            ),
        ],
    )
    async def test_each_switch_turns_on_exactly_its_own_step(
        self, service, moment, clip_cutter, analysis, setting, step, stored
    ):
        ran, _results = analysis
        await service.client.post(f"/settings/{setting}?enabled=1")

        row = await self.cut(service, moment)

        assert ran == [step]
        assert {column: row[column] for column in stored} == stored

    async def test_all_four_can_run_for_the_same_clip(self, service, moment, clip_cutter, analysis):
        ran, _results = analysis
        for setting in ("transcript", "audio_events", "frames", "sound_events"):
            await service.client.post(f"/settings/{setting}?enabled=1")

        row = await self.cut(service, moment)

        assert sorted(ran) == ["audio_events", "frames", "sound_events", "transcript"]
        assert row["transcript"] and row["audio_events"] and row["frame_embedding"] and row["sound_events"]

    @pytest.mark.parametrize(
        ("failing", "columns", "logged"),
        [
            ("transcript", ["transcript"], "transcription failed"),
            ("audio_events", ["audio_events"], "audio event detection failed"),
            ("frames", ["frame_embedding"], "frame encoding failed"),
            ("sound_events", ["sound_events", "sound_embedding"], "sound event tagging failed"),
        ],
    )
    async def test_one_step_failing_does_not_stop_the_others(
        self, service, moment, clip_cutter, analysis, caplog, failing, columns, logged
    ):
        _ran, results = analysis
        results[failing] = RuntimeError("model file is corrupt")
        for setting in ("transcript", "audio_events", "frames", "sound_events"):
            await service.client.post(f"/settings/{setting}?enabled=1")

        with caplog.at_level(logging.ERROR, logger="kick_clip_hunter"):
            row = await self.cut(service, moment)

        every = ["transcript", "audio_events", "frame_embedding", "sound_events", "sound_embedding"]
        assert [column for column in every if row[column] is None] == columns
        errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
        assert [logged in record.getMessage() for record in errors] == [True]
        assert errors[0].exc_info is not None  # logged with its traceback

    async def test_a_clip_with_no_extractable_frames_stores_no_embedding(self, service, moment, clip_cutter, analysis):
        _ran, results = analysis
        results["frames"] = b""
        await service.client.post("/settings/frames?enabled=1")

        row = await self.cut(service, moment)

        assert row["frame_embedding"] is None

    async def test_sound_tags_are_stored_even_without_an_embedding(self, service, moment, clip_cutter, analysis):
        _ran, results = analysis
        results["sound_events"] = ("", b"")
        await service.client.post("/settings/sound_events?enabled=1")

        row = await self.cut(service, moment)

        assert row["sound_events"] == ""
        assert row["sound_embedding"] is None

    async def test_a_switch_turned_off_again_stops_its_step(self, service, moment, clip_cutter, analysis):
        ran, _results = analysis
        await service.client.post("/settings/transcript?enabled=1")
        await service.client.post("/settings/transcript?enabled=0")

        await self.cut(service, moment)

        assert ran == []
