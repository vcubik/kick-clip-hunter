"""The command-line helpers in scripts/.

They are thin, but each one changes real state (the database, the clip
directory, subscriptions on Kick's side), so what they do - and what they
deliberately don't - is pinned down here. The two that drive a browser
(kick_login.py, create_clip.py) are out of reach of an automated test.
"""

from __future__ import annotations

import asyncio
import runpy
import subprocess
from pathlib import Path

import pytest

import backfill_taste
import check
import import_clip
import import_clips_dir
import refresh_emotes
import subscribe
from kick_clip_hunter import audio_events, db, detector, frame_encoder, recorder, sound_events, transcriber
from tests.support.data import T0, add_moment, add_streamer, minutes, moment

SCRIPTS = Path(import_clip.__file__).parent


@pytest.fixture
def conn():
    connection = db.get_connection()
    yield connection
    connection.close()


def video(directory: Path, name: str, content: bytes = b"video bytes") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(content)
    return path


class TestImportClip:
    def test_the_file_is_moved_into_the_channels_clip_directory(self, conn, tmp_path):
        source = video(tmp_path / "downloads", "best_of.mp4", b"the clip")

        _moment_id, clip_path = import_clip.import_clip(conn, source, "some_channel")

        imported = recorder.CLIPS_DIR / clip_path
        assert not source.exists()  # moved, not copied - clips are large
        assert imported.read_bytes() == b"the clip"
        assert imported.parent == recorder.CLIPS_DIR / "some_channel"
        assert imported.name.startswith("manual_") and imported.name.endswith("_best_of.mp4")

    def test_it_becomes_a_moment_without_invented_detector_signals(self, conn, tmp_path):
        moment_id, clip_path = import_clip.import_clip(conn, video(tmp_path, "clip.mp4"), "some_channel")

        row = moment(moment_id)
        assert row["reason"] == "manual_import"
        assert row["clip_path"] == clip_path and "\\" not in clip_path
        assert [
            row[c]
            for c in (
                "score",
                "message_count",
                "baseline_message_rate",
                "current_message_rate",
                "emote_count",
                "keyword_hits",
            )
        ] == [0, 0, 0, 0, 0, 0]
        assert row["rating"] is None and row["transcript"] is None

    def test_a_watched_channel_is_matched_whatever_the_case(self, conn, tmp_path):
        add_streamer("Some_Channel", 4242)

        moment_id, _clip_path = import_clip.import_clip(conn, video(tmp_path, "clip.mp4"), "some_channel")

        assert moment(moment_id)["broadcaster_user_id"] == 4242

    def test_a_channel_that_is_not_watched_is_stored_with_no_broadcaster(self, conn, tmp_path):
        moment_id, _clip_path = import_clip.import_clip(conn, video(tmp_path, "clip.mp4"), "someone_else")

        assert moment(moment_id)["broadcaster_user_id"] == 0

    def test_an_imported_clip_shows_up_for_backfilling(self, conn, tmp_path):
        moment_id, _clip_path = import_clip.import_clip(conn, video(tmp_path, "clip.mp4"), "some_channel")

        assert [row["id"] for row in db.get_moments_missing_taste_data(conn)] == [moment_id]

    def test_the_cli_refuses_a_file_that_does_not_exist(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as stopped:
            import_clip.main(tmp_path / "missing.mp4", "some_channel")

        assert stopped.value.code == 1
        assert "does not exist" in capsys.readouterr().out

    def test_the_cli_says_what_to_do_next(self, tmp_path, capsys):
        import_clip.main(video(tmp_path, "clip.mp4"), "someone_else")

        output = capsys.readouterr().out
        assert "isn't on the current watchlist" in output
        assert "Imported as moment 1" in output and "backfill_taste.py" in output


class TestImportFolder:
    def test_every_video_file_is_imported_and_everything_else_left_alone(self, tmp_path, capsys):
        folder = tmp_path / "clips"
        for name in ("b.mp4", "a.MKV", "c.webm"):
            video(folder, name)
        notes = video(folder, "notes.txt")
        (folder / "subfolder").mkdir()

        import_clips_dir.main(folder, "some_channel")

        output = capsys.readouterr().out
        assert "Imported 3/3 file(s)." in output
        assert sorted(path.name for path in folder.iterdir()) == ["notes.txt", "subfolder"]
        assert notes.exists()
        conn = db.get_connection()
        try:
            assert db.count_moments_with_clip(conn) == 3
        finally:
            conn.close()

    def test_files_are_imported_in_name_order(self, tmp_path, capsys):
        folder = tmp_path / "clips"
        for name in ("03.mp4", "01.mp4", "02.mp4"):
            video(folder, name)

        import_clips_dir.main(folder, "unknown")

        lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("imported ")]
        assert [line.split()[1] for line in lines] == ["01.mp4", "02.mp4", "03.mp4"]

    def test_one_file_failing_does_not_stop_the_rest(self, tmp_path, capsys, monkeypatch):
        folder = tmp_path / "clips"
        video(folder, "good.mp4")
        video(folder, "bad.mp4")
        real_import = import_clips_dir.import_clip

        def flaky(conn, source, channel_slug):
            if source.name == "bad.mp4":
                raise OSError("file is locked")
            return real_import(conn, source, channel_slug)

        monkeypatch.setattr(import_clips_dir, "import_clip", flaky)

        import_clips_dir.main(folder, "some_channel")

        output = capsys.readouterr().out
        assert "failed to import bad.mp4: file is locked" in output
        assert "Imported 1/2 file(s)." in output

    def test_a_folder_without_videos_imports_nothing(self, tmp_path, capsys):
        video(tmp_path / "clips", "readme.txt")

        import_clips_dir.main(tmp_path / "clips", "some_channel")

        assert "no video files found" in capsys.readouterr().out

    def test_a_path_that_is_not_a_folder_is_refused(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as stopped:
            import_clips_dir.main(tmp_path / "nowhere", "some_channel")

        assert stopped.value.code == 1


class TestBackfill:
    @pytest.fixture
    def models(self, monkeypatch) -> list[tuple[str, str]]:
        """Stand-ins for the four analysis steps; records (step, clip name)."""
        ran: list[tuple[str, str]] = []

        def step(name, result):
            def run(clip_path):
                ran.append((name, clip_path.name))
                if isinstance(result, Exception):
                    raise result
                return result

            return run

        monkeypatch.setattr(transcriber, "transcribe_clip", step("transcript", "hello"))
        monkeypatch.setattr(audio_events, "detect_audio_events", step("audio", "en, HAPPY"))
        monkeypatch.setattr(frame_encoder, "encode_clip", step("frames", b"\x01\x02"))
        monkeypatch.setattr(sound_events, "tag_sound_events", step("sound", ("Laughter:0.80", b"\x03\x04")))
        return ran

    def clip_moment(self, name: str, **columns) -> int:
        moment_id = add_moment(clip_path=f"some_channel/{name}", **columns)
        video(recorder.CLIPS_DIR / "some_channel", name)
        return moment_id

    def test_fills_in_everything_a_clip_is_missing(self, models, capsys):
        moment_id = self.clip_moment("imported.mp4")

        backfill_taste.main(limit=None)

        row = moment(moment_id)
        assert (row["transcript"], row["audio_events"], row["frame_embedding"]) == ("hello", "en, HAPPY", b"\x01\x02")
        assert (row["sound_events"], row["sound_embedding"]) == ("Laughter:0.80", b"\x03\x04")
        assert sorted(step for step, _clip in models) == ["audio", "frames", "sound", "transcript"]

    def test_only_runs_the_steps_that_are_actually_missing(self, models):
        moment_id = self.clip_moment("half_done.mp4", transcript="already transcribed", frame_embedding=b"\x09")

        backfill_taste.main(limit=None)

        assert sorted(step for step, _clip in models) == ["audio", "sound"]
        assert moment(moment_id)["transcript"] == "already transcribed"

    def test_existing_sound_tags_are_kept_when_only_the_embedding_is_missing(self, models):
        moment_id = self.clip_moment(
            "tags_only.mp4", transcript="t", audio_events="a", frame_embedding=b"\x00", sound_events="kept as is"
        )

        backfill_taste.main(limit=None)

        row = moment(moment_id)
        assert row["sound_events"] == "kept as is"
        assert row["sound_embedding"] == b"\x03\x04"

    def test_moments_without_a_clip_and_complete_ones_are_not_touched(self, models, capsys):
        add_moment()
        self.clip_moment(
            "complete.mp4",
            transcript="t",
            audio_events="a",
            frame_embedding=b"\x00",
            sound_events="s",
            sound_embedding=b"\x00",
        )

        backfill_taste.main(limit=None)

        assert models == []
        assert "nothing to backfill" in capsys.readouterr().out

    def test_a_clip_whose_file_is_gone_is_skipped(self, models, capsys):
        add_moment(clip_path="some_channel/deleted.mp4")

        backfill_taste.main(limit=None)

        assert models == []
        assert "clip file missing on disk" in capsys.readouterr().out

    def test_the_limit_caps_how_many_moments_are_processed(self, models):
        for number in range(3):
            self.clip_moment(f"clip_{number}.mp4", detected_at=T0 + minutes(number))

        backfill_taste.main(limit=1)

        assert {clip for _step, clip in models} == {"clip_0.mp4"}

    def test_one_step_failing_is_reported_and_the_others_still_run(self, models, capsys, monkeypatch):
        monkeypatch.setattr(
            transcriber, "transcribe_clip", lambda clip_path: (_ for _ in ()).throw(RuntimeError("model missing"))
        )
        moment_id = self.clip_moment("clip.mp4")

        backfill_taste.main(limit=None)

        row = moment(moment_id)
        assert row["transcript"] is None
        assert row["audio_events"] == "en, HAPPY"
        assert "transcription failed: model missing" in capsys.readouterr().out

    def test_a_clip_with_no_extractable_frames_is_left_without_an_embedding(self, models, monkeypatch, capsys):
        monkeypatch.setattr(frame_encoder, "encode_clip", lambda clip_path: b"")
        moment_id = self.clip_moment("clip.mp4")

        backfill_taste.main(limit=None)

        assert moment(moment_id)["frame_embedding"] is None
        assert "no frames extracted" in capsys.readouterr().out


class TestWatchlistCommands:
    def stored_keywords(self, broadcaster_user_id: int) -> dict[str, float]:
        connection = db.get_connection()
        try:
            return db.get_channel_keywords(connection, broadcaster_user_id)
        finally:
            connection.close()

    def test_subscribe_adds_the_channel_and_reports_what_it_found(self, kick_api, capsys):
        kick_api.add_channel("new_channel", 4242, seventv_emotes=["KEKW", "Sadge"])

        asyncio.run(subscribe.main("new_channel"))

        output = capsys.readouterr().out
        assert "broadcaster_user_id=4242" in output
        assert "Fetched 2 7TV emote name(s)" in output and "(1 classified as laugh-related)" in output
        assert kick_api.subscribed == [4242]
        assert self.stored_keywords(4242) == {
            "kekw": detector.EMOTE_MENTION_LAUGH_WEIGHT,
            "sadge": detector.EMOTE_MENTION_OTHER_WEIGHT,
        }

    def test_refreshing_emotes_replaces_the_keywords_without_subscribing_again(self, kick_api, capsys):
        # Subscribing a second time would create a duplicate on Kick's side.
        channel = kick_api.add_channel("some_channel", 4242, seventv_emotes=["KEKW"])
        asyncio.run(subscribe.main("some_channel"))
        channel.seventv_emotes = ["OMEGALUL", "PogChamp"]

        asyncio.run(refresh_emotes.main("some_channel"))

        assert self.stored_keywords(4242) == {
            "omegalul": detector.EMOTE_MENTION_LAUGH_WEIGHT,
            "pogchamp": detector.EMOTE_MENTION_OTHER_WEIGHT,
        }
        assert len(kick_api.calls("POST", "/events/subscriptions")) == 1  # the original one only
        assert "Refreshed keywords for 'some_channel'." in capsys.readouterr().out


class TestCheckScript:
    """scripts/check.py promises to run "what CI runs"."""

    def test_its_steps_are_the_ones_in_the_ci_workflow(self):
        workflow = (SCRIPTS.parent / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")

        for name, command in check.steps([]):
            invocation = " ".join(command[2:])  # without "python -m"
            assert invocation in workflow, f"CI no longer runs the {name!r} step as `{invocation}`"

    def test_extra_arguments_are_passed_on_to_pytest_only(self):
        lint, formatting, tests = (command for _name, command in check.steps(["-x", "-k", "detector"]))

        assert tests[-3:] == ["-x", "-k", "detector"]
        assert "-x" not in lint and "-x" not in formatting

    def test_it_stops_at_the_first_failing_step_and_exits_with_its_status(self, monkeypatch, capsys):
        ran = []

        def run(command, cwd):
            ran.append(command[2])
            return subprocess.CompletedProcess(command, returncode=3 if command[2:4] == ["ruff", "format"] else 0)

        monkeypatch.setattr(check.subprocess, "run", run)

        assert check.main([]) == 3
        assert ran == ["ruff", "ruff"]  # the tests were never started
        assert "test formatting failed" in capsys.readouterr().out

    def test_all_steps_passing_is_success(self, monkeypatch, capsys):
        monkeypatch.setattr(check.subprocess, "run", lambda command, cwd: subprocess.CompletedProcess(command, 0))

        assert check.main([]) == 0
        assert "all checks passed" in capsys.readouterr().out

    def test_every_step_runs_from_the_repository_root(self, monkeypatch):
        directories = set()

        def run(command, cwd):
            directories.add(cwd)
            return subprocess.CompletedProcess(command, 0)

        monkeypatch.setattr(check.subprocess, "run", run)
        check.main([])

        assert directories == {SCRIPTS.parent}


class TestListingCommands:
    def run(self, script: str, capsys) -> str:
        runpy.run_path(str(SCRIPTS / script), run_name="__main__")
        return capsys.readouterr().out

    def test_an_empty_watchlist_says_so(self, capsys):
        assert self.run("list_watchlist.py", capsys).strip() == "Watchlist is empty."

    def test_the_watchlist_is_listed_one_channel_per_line(self, capsys):
        add_streamer("channel_a", 111)
        add_streamer("channel_b", 222)

        lines = self.run("list_watchlist.py", capsys).splitlines()

        assert [line.split("\t")[:2] for line in lines] == [
            ["channel_a", "broadcaster_user_id=111"],
            ["channel_b", "broadcaster_user_id=222"],
        ]

    def test_no_moments_says_so(self, capsys):
        assert self.run("list_moments.py", capsys).strip() == "No moments detected yet."

    def test_moments_are_listed_newest_first_with_their_numbers(self, capsys):
        add_moment("channel_a", detected_at=T0, reason="laugh", score=7.5, stream_elapsed_seconds=3723)
        add_moment("channel_b", detected_at=T0 + minutes(5), reason="emotes", stream_elapsed_seconds=None)

        newer, older = self.run("list_moments.py", capsys).splitlines()

        assert newer.startswith("[channel_b]") and "stream_time=unknown" in newer and "reason=emotes" in newer
        assert older.startswith("[channel_a]") and "stream_time=1:02:03" in older
        assert "score=7.50" in older and "msgs=12 (1.20/s vs baseline 0.50/s)" in older
