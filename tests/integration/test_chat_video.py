"""The chat video, from the browser's pictures on: real ffmpeg joins them
into a file an editor can read transparency from.

The browser is the one thing replaced - `capture_frames` is given frames
made here instead of drawing a page.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kick_clip_hunter import chat_video
from tests.support.media import FFMPEG, decodes_cleanly, png_frame, require_ffmpeg, video_stream

pytestmark = pytest.mark.usefixtures("ffmpeg")

HALF_SEEN = png_frame(rgba=(255, 0, 0, 128))


@pytest.fixture(scope="module")
def ffmpeg() -> None:
    require_ffmpeg()


def first_pixel(path: Path) -> tuple[int, int, int, int]:
    """Red, green, blue and alpha of the top left pixel of a video's first frame."""
    # fmt: off
    result = subprocess.run(
        [FFMPEG, "-v", "error", "-i", str(path), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
        check=True, capture_output=True,
    )
    # fmt: on
    return tuple(result.stdout[:4])


def line(at: float, text: str = "hello") -> dict:
    return {
        "nick": "alice",
        "colour": 1,
        "own_colour": None,
        "badges": [],
        "parts": [{"text": text}],
        "at": f"{at:g}",
        "in_moment": False,
    }


class TestEncoding:
    def test_frames_become_a_prores_4444_video_at_the_videos_frame_rate(self, tmp_path):
        output = tmp_path / "moment_1_chat.mov"

        count = chat_video.encode([HALF_SEEN] * 45, output)

        stream = video_stream(output)
        assert count == 45
        assert (stream["codec_name"], stream["profile"]) == ("prores", "4444")
        assert stream["r_frame_rate"] == f"{chat_video.FPS}/1"
        assert int(stream["nb_frames"]) == 45
        assert float(stream["duration"]) == pytest.approx(45 / chat_video.FPS, abs=0.01)
        assert decodes_cleanly(output)

    def test_what_was_see_through_stays_see_through(self, tmp_path):
        output = tmp_path / "chat.mov"

        chat_video.encode([HALF_SEEN] * 3, output)

        red, green, blue, alpha = first_pixel(output)
        assert video_stream(output)["pix_fmt"].startswith("yuva444p")
        assert alpha == pytest.approx(128, abs=2)
        assert red > 240 and green < 15 and blue < 15

    def test_nothing_at_all_and_everything_stay_what_they_are(self, tmp_path):
        clear, solid = tmp_path / "clear.mov", tmp_path / "solid.mov"

        chat_video.encode([png_frame(rgba=(0, 0, 0, 0))] * 2, clear)
        chat_video.encode([png_frame(rgba=(255, 255, 255, 255))] * 2, solid)

        assert first_pixel(clear)[3] == 0
        assert first_pixel(solid)[3] == 255

    def test_the_folder_is_made_and_only_the_finished_file_is_left_in_it(self, tmp_path):
        output = tmp_path / "some_channel" / "moment_1_chat.mov"

        chat_video.encode([HALF_SEEN] * 3, output)

        assert [path.name for path in output.parent.iterdir()] == ["moment_1_chat.mov"]

    def test_a_video_rendered_again_takes_the_place_of_the_one_before(self, tmp_path):
        output = tmp_path / "chat.mov"
        chat_video.encode([HALF_SEEN] * 9, output)

        chat_video.encode([HALF_SEEN] * 4, output)

        assert int(video_stream(output)["nb_frames"]) == 4
        assert [path.name for path in tmp_path.iterdir()] == ["chat.mov"]

    def test_pictures_that_are_not_pictures_are_an_error_and_leave_nothing(self, tmp_path):
        output = tmp_path / "chat.mov"

        with pytest.raises(chat_video.ChatVideoError, match=r"chat\.mov"):
            chat_video.encode([b"this is not a PNG"] * 3, output)

        assert list(tmp_path.iterdir()) == []

    def test_no_frames_are_an_error_and_leave_nothing(self, tmp_path):
        with pytest.raises(chat_video.ChatVideoError, match="no frames"):
            chat_video.encode([], tmp_path / "chat.mov")

        assert list(tmp_path.iterdir()) == []

    def test_a_failed_rendering_keeps_the_video_there_was(self, tmp_path):
        output = tmp_path / "chat.mov"
        chat_video.encode([HALF_SEEN] * 9, output)

        with pytest.raises(chat_video.ChatVideoError):
            chat_video.encode([b"this is not a PNG"], output)

        assert int(video_stream(output)["nb_frames"]) == 9
        assert [path.name for path in tmp_path.iterdir()] == ["chat.mov"]

    def test_a_browser_that_fails_part_way_leaves_nothing_and_no_encoder_running(self, tmp_path):
        def frames():
            yield HALF_SEEN
            yield HALF_SEEN
            raise RuntimeError("the page went away")

        with pytest.raises(RuntimeError, match="the page went away"):
            chat_video.encode(frames(), tmp_path / "chat.mov")

        assert list(tmp_path.iterdir()) == []


class TestRendering:
    @pytest.fixture
    def browser(self, monkeypatch) -> dict:
        """Stands in for the browser: remembers the page and the seconds it
        was asked to draw, and hands back a picture for each."""
        seen: dict = {"closed": False}

        def capture_frames(html, times):
            seen["html"], seen["times"] = html, list(times)
            try:
                for _ in times:
                    yield HALF_SEEN
            finally:
                seen["closed"] = True

        monkeypatch.setattr(chat_video, "capture_frames", capture_frames)
        return seen

    def test_a_clip_gets_a_video_as_long_as_itself(self, browser, tmp_path):
        output = tmp_path / "chat.mov"

        frames = chat_video.render([line(0.5)], 1.5, output)

        assert frames == round(1.5 * chat_video.FPS) == int(video_stream(output)["nb_frames"])
        assert browser["times"] == chat_video.frame_times(1.5)
        assert browser["closed"]

    def test_the_browser_is_given_the_lines_of_the_clip(self, browser, tmp_path):
        lines = [line(-3, "before it"), line(0.2, "during it"), line(9, "after it")]

        chat_video.render(lines, 1.0, tmp_path / "chat.mov")

        assert "before it" in browser["html"] and "during it" in browser["html"]
        assert "after it" not in browser["html"]

    def test_without_ffmpeg_it_fails_and_leaves_nothing(self, browser, monkeypatch, tmp_path):
        monkeypatch.setattr(chat_video, "FFMPEG_BIN", str(tmp_path / "no-such-ffmpeg"))

        with pytest.raises(OSError):
            chat_video.render([line(0.5)], 1.0, tmp_path / "chat.mov")

        assert list(tmp_path.iterdir()) == []
