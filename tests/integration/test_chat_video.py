"""Chat as video, from the browser's pictures of the column on: real ffmpeg
makes a video of its own an editor can read transparency from, one on a
colour, and a cut with chat drawn into its picture.

The browser is the one thing replaced - `drawn_column` is a made-up column
(tests/support/browser.py), a band of white where each line would be - and
the graphics card: as for the whole suite, there is none (conftest.py), so
a cut's picture is encoded with x264.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import numpy as np
import pytest

from kick_clip_hunter import chat_video
from tests.support.browser import (
    EMOTE_LEFT,
    EMOTE_PIXELS,
    EMOTE_TOP,
    LINE_PIXELS,
    FakeBrowser,
    animated_picture,
)
from tests.support.media import FFMPEG, decodes_cleanly, make_video, probe, require_ffmpeg, video_stream

pytestmark = pytest.mark.usefixtures("ffmpeg")

WIDTH, HEIGHT = chat_video.WIDTH, chat_video.HEIGHT


@pytest.fixture(scope="module")
def ffmpeg() -> None:
    require_ffmpeg()


@pytest.fixture
def browser(monkeypatch) -> FakeBrowser:
    fake = FakeBrowser()
    monkeypatch.setattr(chat_video, "drawn_column", fake.drawn_column)
    return fake


def raw(rgba: tuple[int, int, int, int]) -> bytes:
    """A frame of one colour, as frames() hands them on."""
    return bytes(rgba) * WIDTH * HEIGHT


HALF_SEEN = raw((255, 0, 0, 128))

RED, BLUE = (255, 0, 0, 255), (0, 0, 255, 255)
# An emote that is red for half a second, then blue for one, over and over.
RED_THEN_BLUE = animated_picture([RED, BLUE], [500, 500])
# Where the middle of the newest line's emote is in a chat video of its own
# (see FakeBrowser).
EMOTE_ROW = HEIGHT - chat_video.BOTTOM_PIXELS - LINE_PIXELS + EMOTE_TOP + EMOTE_PIXELS // 2
EMOTE_COLUMN = EMOTE_LEFT + EMOTE_PIXELS // 2


def near(colour: np.ndarray, expected: tuple[int, ...]) -> bool:
    """Whether a pixel is the colour expected, give or take what encoding
    it changed."""
    return bool(np.abs(colour[:3] - np.array(expected[:3])).max() <= 24)


def picture(path: Path, at: float = 0.0, pixel_format: str = "rgba") -> np.ndarray:
    """The frame of a video `at` seconds into it, as rows of pixels - in
    numbers that can be taken from each other."""
    stream = video_stream(path)
    # fmt: off
    result = subprocess.run(
        [
            FFMPEG, "-v", "error", "-ss", f"{at:.3f}", "-i", str(path), "-frames:v", "1",
            "-f", "rawvideo", "-pix_fmt", pixel_format, "-",
        ],
        check=True, capture_output=True,
    )
    # fmt: on
    channels = 4 if pixel_format == "rgba" else 3
    width, height = (int(stream[key]) for key in ("width", "height"))
    return np.frombuffer(result.stdout, np.uint8).reshape(height, width, channels).astype(int)


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


class TestAVideoOfItsOwn:
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

        red, green, blue, alpha = picture(output)[0, 0]
        assert video_stream(output)["pix_fmt"].startswith("yuva444p")
        assert alpha == pytest.approx(128, abs=2)
        assert red > 240 and green < 15 and blue < 15

    def test_nothing_at_all_and_everything_stay_what_they_are(self, tmp_path):
        clear, solid = tmp_path / "clear.mov", tmp_path / "solid.mov"

        chat_video.encode([raw((0, 0, 0, 0))] * 2, clear)
        chat_video.encode([raw((255, 255, 255, 255))] * 2, solid)

        assert picture(clear)[0, 0, 3] == 0
        assert picture(solid)[0, 0, 3] == 255

    def test_on_a_background_it_is_h264_with_the_colour_behind_the_chat(self, tmp_path):
        output = tmp_path / "chat.mov"
        clear, white = raw((0, 0, 0, 0)), raw((255, 255, 255, 255))

        chat_video.encode([clear] * 30 + [white] * 30, output, background="#0a2540")

        stream = video_stream(output)
        assert (stream["codec_name"], stream["pix_fmt"]) == ("h264", "yuv420p")
        assert int(stream["nb_frames"]) == 60
        # Where there is no chat, the colour; where there is, the chat.
        assert picture(output, 0.0, "rgb24")[HEIGHT // 2, WIDTH // 2].tolist() == pytest.approx([10, 37, 64], abs=3)
        assert picture(output, 1.5, "rgb24")[HEIGHT // 2, WIDTH // 2].tolist() == pytest.approx([255, 255, 255], abs=3)

    def test_the_folder_is_made_and_only_the_finished_file_is_left_in_it(self, tmp_path):
        output = tmp_path / "some_channel" / "moment_1_chat.mov"

        chat_video.encode([HALF_SEEN] * 3, output)

        assert [path.name for path in output.parent.iterdir()] == ["moment_1_chat.mov"]

    def test_a_video_made_again_takes_the_place_of_the_one_before(self, tmp_path):
        output = tmp_path / "chat.mov"
        chat_video.encode([HALF_SEEN] * 9, output)

        chat_video.encode([HALF_SEEN] * 4, output)

        assert int(video_stream(output)["nb_frames"]) == 4
        assert [path.name for path in tmp_path.iterdir()] == ["chat.mov"]

    def test_no_frames_are_an_error_and_leave_nothing(self, tmp_path):
        with pytest.raises(chat_video.ChatVideoError, match="no frames"):
            chat_video.encode([], tmp_path / "chat.mov")

        assert list(tmp_path.iterdir()) == []

    def test_frames_that_fail_part_way_leave_nothing_and_no_encoder_running(self, tmp_path):
        def frames():
            yield HALF_SEEN
            yield HALF_SEEN
            raise RuntimeError("the page went away")

        with pytest.raises(RuntimeError, match="the page went away"):
            chat_video.encode(frames(), tmp_path / "chat.mov")

        assert list(tmp_path.iterdir()) == []

    def test_an_encoder_that_gives_up_says_why_and_leaves_nothing(self, tmp_path):
        output = tmp_path / "chat.mov"
        arguments = (*chat_video._raw_frames(), "-c:v", "no_such_encoder")

        # More frames than fit in the pipe while nothing reads them.
        with pytest.raises(chat_video.ChatVideoError, match="no_such_encoder"):
            chat_video._encode(arguments, [HALF_SEEN] * 60, output)

        assert list(tmp_path.iterdir()) == []

    def test_a_failed_one_keeps_the_video_there_was(self, tmp_path):
        output = tmp_path / "chat.mov"
        chat_video.encode([HALF_SEEN] * 9, output)

        with pytest.raises(chat_video.ChatVideoError):
            chat_video.encode([], output)

        assert int(video_stream(output)["nb_frames"]) == 9
        assert [path.name for path in tmp_path.iterdir()] == ["chat.mov"]


class TestRendering:
    def test_a_video_gets_chat_as_long_as_itself(self, browser, tmp_path):
        output = tmp_path / "chat.mov"

        frames = chat_video.render([line(0.5)], 1.5, output)

        assert frames == round(1.5 * chat_video.FPS) == int(video_stream(output)["nb_frames"])

    def test_the_lines_are_where_and_when_the_column_has_them(self, browser, tmp_path):
        output = tmp_path / "chat.mov"

        chat_video.render([line(0.5)], 2.0, output)

        bottom = HEIGHT - chat_video.BOTTOM_PIXELS
        # Before its line arrives the video is see-through all over...
        assert picture(output, 0.2)[:, WIDTH // 2, 3].max() == 0
        # ... and once it has come in, the line is just above the bottom.
        after = picture(output, 1.5)[:, WIDTH // 2, 3]
        assert after[bottom - 30 : bottom - 6].min() > 240
        assert after[: bottom - 40].max() == 0

    def test_the_browser_is_given_the_lines_of_the_video(self, browser, tmp_path):
        lines = [line(-3, "before it"), line(0.2, "during it"), line(9, "after it")]

        chat_video.render(lines, 1.0, tmp_path / "chat.mov")

        (html,) = browser.pages
        assert "before it" in html and "during it" in html
        assert "after it" not in html
        # Animated emotes standing still, unless asked otherwise.
        assert browser.animated == [False]

    def test_animated_emotes_move_in_it_when_asked_to(self, browser, tmp_path):
        browser.emote = RED_THEN_BLUE
        output = tmp_path / "chat.mov"

        chat_video.render([line(0.0)], 2.0, output, animated=True)

        assert browser.animated == [True]
        # On the frame of its animation the video is at, from its first frame.
        colours = [picture(output, at)[EMOTE_ROW, EMOTE_COLUMN] for at in (0.25, 0.75, 1.25)]
        assert [near(colour, expected) for colour, expected in zip(colours, [RED, BLUE, RED], strict=True)] == [
            True
        ] * 3

    def test_how_far_it_has_got_is_told_as_it_goes(self, browser, tmp_path):
        said: list[float] = []

        chat_video.render([line(0.5)], 2.0, tmp_path / "chat.mov", progress=said.append)

        assert said == [0.0, 0.5, 1.0]

    def test_it_can_be_given_up_part_way_and_leaves_nothing(self, browser, tmp_path):
        def give_up(done: float) -> None:
            if done >= 0.5:
                raise InterruptedError("shutting down")

        with pytest.raises(InterruptedError):
            chat_video.render([line(0.5)], 2.0, tmp_path / "chat.mov", progress=give_up)

        assert list(tmp_path.iterdir()) == []

    def test_on_a_colour_it_can_be_given_up_before_its_first_frame(self, browser, tmp_path, monkeypatch):
        # ffmpeg is then given no frames at all - which, with the colour
        # from a source of its own, kept it waiting for one forever.
        monkeypatch.setattr(chat_video, "STOP_SECONDS", 5)

        def give_up(done: float) -> None:
            raise InterruptedError("shutting down")

        started = time.monotonic()
        with pytest.raises(InterruptedError):
            chat_video.render([line(0.5)], 2.0, tmp_path / "chat.mov", "#18181b", progress=give_up)

        assert time.monotonic() - started < chat_video.STOP_SECONDS
        assert list(tmp_path.iterdir()) == []

    def test_without_ffmpeg_it_fails_and_leaves_nothing(self, browser, monkeypatch, tmp_path):
        monkeypatch.setattr(chat_video, "FFMPEG_BIN", str(tmp_path / "no-such-ffmpeg"))

        with pytest.raises(OSError):
            chat_video.render([line(0.5)], 1.0, tmp_path / "chat.mov")

        assert list(tmp_path.iterdir()) == []


# -- chat drawn into a cut -------------------------------------------------------


@pytest.fixture(scope="module")
def footage(tmp_path_factory) -> Path:
    """Twelve seconds of a test pattern with a tone, 640x360, with a frame a
    copied cut can begin on every two seconds."""
    return make_video(tmp_path_factory.mktemp("footage") / "footage.mp4", 12, keyframe_every=2.0, size="640x360")


# Where chat goes in that picture, on the right: its column at a third of
# the size of the video of its own, near the bottom right corner.
ACROSS, DOWN, LEFT, TOP = chat_video.placement(640, 360, "right")


def difference(burned: np.ndarray, plain: np.ndarray) -> np.ndarray:
    """How far apart two pictures are, pixel by pixel."""
    return np.abs(burned - plain).sum(axis=2)


class TestBurning:
    def test_the_cut_is_the_stretch_asked_for_to_the_frame_with_its_sound(self, browser, footage, tmp_path):
        output = tmp_path / "cut.mp4"

        # A copied cut from here would begin at second 2, on a keyframe.
        encoder = chat_video.burn([line(0.5)], footage, 3.3, 4.0, output, "right")

        assert encoder == "libx264"
        assert probe(output) == {"duration": pytest.approx(4.0, abs=0.1), "streams": ["audio", "video"]}
        stream = video_stream(output)
        assert (stream["codec_name"], stream["pix_fmt"]) == ("h264", "yuv420p")
        assert decodes_cleanly(output)
        # Its first frame is the footage's at 3.3 s, not before.
        assert difference(picture(output, 0.0, "rgb24"), picture(footage, 3.3, "rgb24"))[:, :LEFT].mean() < 8
        assert difference(picture(output, 0.0, "rgb24"), picture(footage, 2.0, "rgb24"))[:, :LEFT].mean() > 20

    def test_chat_is_drawn_where_it_goes_once_a_line_has_arrived(self, browser, footage, tmp_path):
        output = tmp_path / "cut.mp4"

        chat_video.burn([line(1.0)], footage, 0.0, 3.0, output, "right")

        before = difference(picture(output, 0.5, "rgb24"), picture(footage, 0.5, "rgb24"))
        after = difference(picture(output, 2.5, "rgb24"), picture(footage, 2.5, "rgb24"))
        chat = (slice(TOP, TOP + DOWN), slice(LEFT, LEFT + ACROSS))
        # Nothing until the line arrives; then the column has it, and the
        # rest of the picture is the footage as it was.
        assert before[chat].mean() < 8
        assert after[chat].max() > 200
        assert after[:, :LEFT].mean() < 8

    def test_on_the_left_it_is_drawn_there(self, browser, footage, tmp_path):
        output = tmp_path / "cut.mp4"

        chat_video.burn([line(0.0)], footage, 0.0, 2.0, output, "left")

        across, down, left, top = chat_video.placement(640, 360, "left")
        after = difference(picture(output, 1.0, "rgb24"), picture(footage, 1.0, "rgb24"))
        assert after[top : top + down, left : left + across].max() > 200
        assert after[:, left + across :].mean() < 8

    def test_animated_emotes_move_in_the_picture_too(self, browser, footage, tmp_path):
        browser.emote = RED_THEN_BLUE
        output = tmp_path / "cut.mp4"

        chat_video.burn([line(0.0)], footage, 0.0, 2.0, output, "right", animated=True)

        assert browser.animated == [True]
        row, column = TOP + round(EMOTE_ROW * DOWN / HEIGHT), LEFT + round(EMOTE_COLUMN * ACROSS / WIDTH)
        early, late = (picture(output, at, "rgb24")[row, column] for at in (0.25, 0.75))
        assert near(early, RED) and near(late, BLUE)

    def test_a_background_is_drawn_behind_the_chat(self, browser, footage, tmp_path):
        output = tmp_path / "cut.mp4"

        chat_video.burn([line(5.0)], footage, 0.0, 2.0, output, "right", background="#0a2540")

        # Before any line, the column is the colour all over.
        column = picture(output, 1.0, "rgb24")[TOP + 4 : TOP + DOWN - 4, LEFT + 4 : LEFT + ACROSS - 4]
        assert column.reshape(-1, 3).mean(axis=0).tolist() == pytest.approx([10, 37, 64], abs=4)

    def test_how_far_it_has_got_is_told_and_it_can_be_given_up(self, browser, footage, tmp_path):
        said: list[float] = []

        def give_up(done: float) -> None:
            said.append(done)
            if done >= 0.5:
                raise InterruptedError("shutting down")

        with pytest.raises(InterruptedError):
            chat_video.burn([line(0.5)], footage, 0.0, 4.0, tmp_path / "cut.mp4", "right", progress=give_up)

        assert said == [0.0, 0.25, 0.5]
        assert list(tmp_path.iterdir()) == []

    def test_a_video_made_again_from_itself_takes_its_place_once_whole(self, browser, footage, tmp_path):
        video = tmp_path / "cut.mp4"
        video.write_bytes(footage.read_bytes())

        chat_video.burn([line(0.0)], video, 0.0, 3.0, video, "right")

        assert probe(video)["duration"] == pytest.approx(3.0, abs=0.1)
        assert [path.name for path in tmp_path.iterdir()] == ["cut.mp4"]


class TestTheEncoder:
    @pytest.fixture
    def unmade(self, monkeypatch) -> None:
        monkeypatch.setattr(chat_video, "_encoder", None)

    def test_ffmpeg_can_tell_an_encoder_it_has_from_one_it_has_not(self):
        assert chat_video.encoder_works("libx264")
        assert not chat_video.encoder_works("h264_no_such_card")

    def test_without_ffmpeg_none_works(self, monkeypatch, tmp_path):
        monkeypatch.setattr(chat_video, "FFMPEG_BIN", str(tmp_path / "no-such-ffmpeg"))

        assert not chat_video.encoder_works("libx264")

    def test_the_first_card_that_works_is_used(self, monkeypatch, unmade):
        # libx264 stands in for a card that works.
        cards = (("h264_no_such_card", ("-x",)), ("libx264", ("-preset", "ultrafast")))
        monkeypatch.setattr(chat_video, "HARDWARE_ENCODERS", cards)

        assert chat_video.video_encoder() == ("libx264", ("-preset", "ultrafast"))

    def test_with_none_the_processor_does_it(self, monkeypatch, unmade):
        monkeypatch.setattr(chat_video, "HARDWARE_ENCODERS", (("h264_no_such_card", ()),))

        assert chat_video.video_encoder() == chat_video.SOFTWARE_ENCODER

    def test_what_was_found_is_kept(self, monkeypatch, unmade):
        monkeypatch.setattr(chat_video, "HARDWARE_ENCODERS", (("h264_no_such_card", ()),))
        found = chat_video.video_encoder()

        monkeypatch.setattr(chat_video, "HARDWARE_ENCODERS", (("libx264", ("-preset", "ultrafast")),))

        assert chat_video.video_encoder() == found == chat_video.SOFTWARE_ENCODER

    def test_a_videos_picture_is_measured(self, footage):
        found = chat_video.footage(footage)

        assert (found.width, found.height) == (640, 360)
        assert found.bitrate is not None and found.bitrate > 0
