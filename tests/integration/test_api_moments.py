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
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from kick_clip_hunter import db, detector, recorder, tuning
from kick_clip_hunter.platforms import StreamUrlError
from kick_clip_hunter.recorder import RecorderError
from tests.support.webhook import chat_payload

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
        owner = service.watch(CHANNEL)

        first = await service.main._stream_elapsed_seconds(CHANNEL, owner)
        second = await service.main._stream_elapsed_seconds(CHANNEL, owner)

        assert first == pytest.approx(3600, abs=5) and second == pytest.approx(3600, abs=5)
        assert len(service.api.calls("GET", "/channels")) == 1

    async def test_the_cached_stream_start_expires(self, service, monkeypatch):
        owner = service.watch(CHANNEL)
        await service.main._stream_elapsed_seconds(CHANNEL, owner)

        monkeypatch.setattr(service.main, "STREAM_INFO_CACHE_SECONDS", -1)
        await service.main._stream_elapsed_seconds(CHANNEL, owner)

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
    async def test_a_moment_stays_open_until_the_reaction_has_been_quiet(self, service):
        # Long enough that the session is certainly still open when checked.
        service.quiet_seconds = 0.5
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

    async def test_new_people_joining_in_after_the_trigger_extend_it(self, service):
        # A fresh crowd keeps the reaction "active" for as long as their
        # laughs stay in the detector's short window, so this session ends at
        # its hard cap - which is shortened here to keep the test quick.
        service.quiet_seconds = 5.0
        service.max_open_seconds = 1.0
        service.watch(CHANNEL)
        service.warm_up(CHANNEL)
        await service.crowd_laughs(CHANNEL, people=crowd_needed())
        await service.clock_tick()

        for number in range(crowd_needed()):
            await service.chat(CHANNEL, f"latecomer{number}", "xDDD")
        await service.settle()

        (moment,) = service.moments()
        assert window(moment) > WINDOW

    async def test_one_straggler_does_not_extend_it(self, service):
        service.quiet_seconds = 0.2
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
        service.quiet_seconds = 1.0
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


class TestKeepingAMoment:
    """Once its session has closed, a moment is kept only if enough people
    laughed around it (detector.laughing_needed) - on a channel whose own
    reacting share puts that bar above what fires a moment. As recommended
    the two are the same share of the chat, and a moment fired by laughs
    always has enough people laughing."""

    CHATTERS = 40
    SHARE = detector.MIN_REACTION_UNIQUE_FRACTION / 2

    def sizes(self) -> tuple[int, int]:
        """How many people fire a moment in that chat, and how many have to
        laugh for it to be kept."""
        settings = replace(detector.recommended(), min_reaction_unique_fraction=self.SHARE)
        fires = detector._dynamic_min_reaction_unique(self.CHATTERS, settings)
        keeps = detector.laughing_needed(self.CHATTERS, settings)
        assert fires < keeps
        return fires, keeps

    async def test_a_burst_nobody_joins_is_dropped_before_its_clip_is_cut(self, service, monkeypatch, caplog):
        cut = []

        async def create_clip(*args, **kwargs):
            cut.append(args)
            raise RecorderError("not asked for in this test")

        monkeypatch.setattr(service.main.recording_manager, "create_clip_for_moment", create_clip)
        fires, _keeps = self.sizes()
        service.watch(CHANNEL)
        await set_up(service, reacting_share=self.SHARE)
        service.warm_up(CHANNEL, chatters=self.CHATTERS)

        with caplog.at_level(logging.INFO, logger="kick_clip_hunter"):
            await service.crowd_laughs(CHANNEL, people=fires)
            assert len(service.moments()) == 1  # stored while it is open
            await service.settle()

        assert service.moments() == []
        assert cut == []
        assert any("dropped" in record.getMessage() for record in caplog.records)

    async def test_people_who_join_in_after_it_fired_keep_it(self, service):
        fires, keeps = self.sizes()
        service.quiet_seconds = 0.5
        service.watch(CHANNEL)
        await set_up(service, reacting_share=self.SHARE)
        service.warm_up(CHANNEL, chatters=self.CHATTERS)
        await service.crowd_laughs(CHANNEL, people=fires)
        await service.clock_tick()

        for number in range(keeps - fires):
            await service.chat(CHANNEL, f"latecomer{number}", "xd")
        await service.settle()

        assert len(service.moments()) == 1

    async def test_it_is_kept_with_how_many_people_laughed_around_it(self, service):
        fires, keeps = self.sizes()
        service.quiet_seconds = 0.5
        service.watch(CHANNEL)
        await set_up(service, reacting_share=self.SHARE)
        service.warm_up(CHANNEL, chatters=self.CHATTERS)
        await service.crowd_laughs(CHANNEL, people=fires)
        await service.clock_tick()

        for number in range(keeps + 2 - fires):
            await service.chat(CHANNEL, f"latecomer{number}", "xd")
        await service.settle()

        (moment,) = service.moments()
        assert moment["laughing"] == keeps + 2

    async def test_a_channel_that_keeps_every_moment_keeps_it(self, service):
        fires, _keeps = self.sizes()
        service.watch(CHANNEL)
        await set_up(service, reacting_share=self.SHARE, laughing_share=0)
        service.warm_up(CHANNEL, chatters=self.CHATTERS)

        await service.crowd_laughs(CHANNEL, people=fires)
        await service.settle()

        assert len(service.moments()) == 1

    async def test_one_the_streamer_asked_for_while_it_was_open_is_kept(self, service):
        fires, _keeps = self.sizes()
        service.quiet_seconds = 0.5
        service.watch(CHANNEL)
        await set_up(service, reacting_share=self.SHARE, clip_commands=["!clip"])
        service.warm_up(CHANNEL, chatters=self.CHATTERS)
        await service.crowd_laughs(CHANNEL, people=fires)

        await service.chat(CHANNEL, "the_streamer", "!clip", identity=STREAMER)
        await service.settle()

        (moment,) = service.moments()
        assert moment["reason"] == "laugh"
        # Counted all the same, for the queue to be ordered by.
        assert moment["laughing"] == fires


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
    """Replaces the call into the recorder with a stand-in that writes a
    placeholder file and remembers how it was called."""

    class Cutter:
        def __init__(self) -> None:
            self.clip_error: Exception | None = None
            self.clip_calls: list = []
            # How each clip was asked to be framed, as the recorder takes it.
            self.framings: list[dict] = []
            # Seconds of footage the file is said to hold before and after
            # the clip. None by default: the placeholder is not a video a
            # clip could be cut out of for the analysis steps.
            self.context: tuple[float, float] = (0.0, 0.0)

    cutter = Cutter()

    async def create_clip(channel, start, end, name, **framing):
        cutter.clip_calls.append((channel, start, end, name, framing["post_roll_seconds"]))
        cutter.framings.append(framing)
        if cutter.clip_error is not None:
            raise cutter.clip_error
        path = recorder.CLIPS_DIR / channel / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"clip")
        return recorder.CutClip(path, CLIP_HOLDS[0], CLIP_HOLDS[1], *cutter.context)

    monkeypatch.setattr(service.main.recording_manager, "create_clip_for_moment", create_clip)
    return cutter


class TestCuttingTheClip:
    async def cut(self, service, moment_id: int, post_roll: int = 10, **settings) -> None:
        tuned = replace(tuning.recommended(), post_roll_seconds=post_roll, **settings)
        await service.main._create_clip_background(moment_id, CHANNEL, START, END, tuned)
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

    async def test_how_much_footage_the_file_has_around_the_clip_is_recorded_too(self, service, moment, clip_cutter):
        clip_cutter.context = (31.5, 58.25)

        await self.cut(service, moment)

        (row,) = service.moments()
        assert (row["context_before"], row["context_after"]) == (31.5, 58.25)
        # The clip's own start and length are the clip's, not the file's.
        assert datetime.fromisoformat(row["clip_start"]) == CLIP_HOLDS[0]
        assert row["clip_duration"] == (CLIP_HOLDS[1] - CLIP_HOLDS[0]).total_seconds()

    async def test_it_waits_for_the_post_roll_and_the_context_after_it_before_cutting(
        self, service, moment, clip_cutter, monkeypatch
    ):
        slept: list[float] = []

        class RecordingSleep:
            """main.py's view of asyncio, with sleeps noted instead of slept."""

            def __getattr__(self, name):
                return getattr(asyncio, name)

            async def sleep(self, seconds):
                if not slept:
                    assert clip_cutter.clip_calls == [], "the clip was cut before waiting for its footage"
                slept.append(seconds)

        monkeypatch.setattr(service.main, "asyncio", RecordingSleep())

        await self.cut(service, moment, post_roll=7)

        # One wait, one cut: the file is written once everything that goes
        # into it has been broadcast.
        assert slept == [7 + recorder.CONTEXT_AFTER_SECONDS + recorder.CLIP_SETTLE_SECONDS]
        assert len(clip_cutter.clip_calls) == 1

    @pytest.mark.parametrize(
        "error",
        [RecorderError("No buffered segments"), StreamUrlError("not live"), RuntimeError("ffmpeg exploded")],
        ids=["no-footage", "stream-not-live", "unexpected-error"],
    )
    async def test_a_failed_cut_leaves_the_moment_without_a_clip(self, service, moment, clip_cutter, error):
        clip_cutter.clip_error = error

        await self.cut(service, moment)

        (row,) = service.moments()
        assert (row["clip_path"], row["clip_start"], row["context_before"]) == (None, None, None)

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
        await service.main._create_clip_background(moment_id, CHANNEL, START, END, tuning.recommended())
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


MODERATOR = {"username_color": "#00ff00", "badges": [{"type": "moderator", "text": "Moderator"}]}
STREAMER = {"username_color": "#00ff00", "badges": [{"type": "broadcaster", "text": "Broadcaster"}]}
SUBSCRIBER = {"username_color": "#00ff00", "badges": [{"type": "subscriber", "text": "Subscriber", "count": 3}]}


async def set_up(service, **values) -> None:
    """Saves settings: what is a channel's alone for CHANNEL, the rest for
    all channels."""
    mine = tuning.per_channel(values)
    for address, given in ((f"/channels/{CHANNEL}/tuning", mine), ("/tuning", tuning.shared(values))):
        response = await service.client.post(address, json={"values": given})
        assert response.status_code == 200, response.text


class TestAClipOnCommand:
    async def test_the_streamer_asking_makes_a_moment_in_a_chat_with_no_history_at_all(self, service):
        user_id = service.watch(CHANNEL)
        await set_up(service, clip_commands=["!clip"])
        before = datetime.now(timezone.utc)

        await service.chat(CHANNEL, "the_streamer", "!clip", identity=STREAMER)

        (moment,) = service.moments()
        assert (moment["channel_slug"], moment["broadcaster_user_id"]) == (CHANNEL, user_id)
        assert moment["reason"] == "command"
        assert (moment["score"], moment["message_count"], moment["keyword_hits"]) == (0, 0, 0)
        assert window(moment) == WINDOW
        assert before <= datetime.fromisoformat(moment["window_end"]) <= datetime.now(timezone.utc)

    async def test_a_moderator_may_ask_and_so_may_the_broadcasters_account_without_a_badge(self, service):
        user_id = service.watch(CHANNEL)
        await set_up(service, clip_commands=["!clip"])

        await service.chat(CHANNEL, "a_mod", " !CLIP ", identity=MODERATOR)
        await service.settle()
        payload = chat_payload(CHANNEL, user_id, "the_streamer", "!clip")
        payload["sender"]["user_id"] = user_id
        await service.deliver(payload)

        assert [moment["reason"] for moment in service.moments()] == ["command", "command"]

    async def test_a_viewer_asking_gets_nothing(self, service):
        service.watch(CHANNEL)
        await set_up(service, clip_commands=["!clip"])

        await service.chat(CHANNEL, "viewer", "!clip", identity=SUBSCRIBER)
        await service.chat(CHANNEL, "another", "!clip")

        assert service.moments() == []
        assert [content for _channel, _sender, content in service.chat_log()] == ["!clip", "!clip"]

    async def test_unless_anyone_may_ask(self, service):
        service.watch(CHANNEL)
        await set_up(service, clip_commands=["!clip"], clip_command_from_anyone=True)

        await service.chat(CHANNEL, "viewer", "!clip")

        assert [moment["reason"] for moment in service.moments()] == ["command"]

    async def test_a_command_is_its_channels_alone(self, service):
        # Without the channel going by settings of its own in anything else.
        service.watch(CHANNEL)
        service.watch("another_channel")
        await set_up(service, clip_commands=["!clip"])

        await service.chat("another_channel", "its_streamer", "!clip", identity=STREAMER)
        await service.chat(CHANNEL, "the_streamer", "!clip", identity=STREAMER)

        assert [moment["channel_slug"] for moment in service.moments()] == [CHANNEL]

    @pytest.mark.parametrize("own", [True, False])
    async def test_it_holds_whichever_way_the_channels_own_settings_are_switched(self, service, own):
        service.watch(CHANNEL)
        await set_up(service, clip_commands=["!clip"])
        await service.client.post(f"/channels/{CHANNEL}/own_tuning?enabled=1")
        await service.client.post(f"/channels/{CHANNEL}/own_tuning?enabled={int(own)}")

        await service.chat(CHANNEL, "the_streamer", "!clip", identity=STREAMER)

        assert [moment["reason"] for moment in service.moments()] == ["command"]

    async def test_with_no_command_set_the_message_is_chat_like_any_other(self, service):
        service.watch(CHANNEL)

        await service.chat(CHANNEL, "the_streamer", "!clip", identity=STREAMER)

        assert service.moments() == []

    async def test_talking_about_the_command_is_not_asking(self, service):
        service.watch(CHANNEL)
        await set_up(service, clip_commands=["!clip"])

        await service.chat(CHANNEL, "the_streamer", "type !clip and I get a clip", identity=STREAMER)

        assert service.moments() == []

    async def test_a_channel_that_is_not_tracked_gets_no_clip(self, service):
        service.watch(CHANNEL)
        await set_up(service, clip_commands=["!clip"])
        await service.client.post(f"/channels/{CHANNEL}/tracking?enabled=0")

        await service.chat(CHANNEL, "the_streamer", "!clip", identity=STREAMER)

        assert service.moments() == []

    async def test_asking_while_a_moment_is_open_extends_that_one(self, service):
        service.quiet_seconds = 0.5
        service.watch(CHANNEL)
        await set_up(service, clip_commands=["!clip"])
        await service.chat(CHANNEL, "the_streamer", "!clip", identity=STREAMER)
        opened = service.main._moment_sessions[CHANNEL].window_end
        await service.clock_tick()

        await service.chat(CHANNEL, "the_streamer", "!clip", identity=STREAMER)

        assert service.main._moment_sessions[CHANNEL].window_end > opened
        await service.settle()
        (moment,) = service.moments()
        assert window(moment) > WINDOW

    async def test_the_command_is_kept_from_the_detector_whoever_posts_it(self, service):
        # A crowd of viewers posting it is neither a clip nor a reaction.
        service.watch(CHANNEL)
        service.warm_up(CHANNEL)
        await set_up(service, clip_commands=["xDDD"])

        await service.crowd_laughs(CHANNEL, people=8)

        assert service.moments() == []

    async def test_its_clip_is_cut_like_any_other(self, service, monkeypatch):
        asked = []

        async def create_clip(channel, start, end, name, **framing):
            asked.append((channel, end - start, name, framing))
            raise RecorderError("nothing is being recorded in this test")

        monkeypatch.setattr(service.main.recording_manager, "create_clip_for_moment", create_clip)
        service.watch(CHANNEL)
        await set_up(service, clip_commands=["!clip"], pre_roll_seconds=40)

        await service.chat(CHANNEL, "the_streamer", "!clip", identity=STREAMER)
        await service.settle()

        (moment,) = service.moments()
        ((channel, length, name, framing),) = asked
        assert (channel, length, name) == (CHANNEL, WINDOW, f"moment_{moment['id']}.mp4")
        assert framing["pre_roll_seconds"] == 40
