"""Chat as video, short of a browser and an encoder: which lines go into
it, the page they are laid out on, what the browser that lays them out is
allowed to load, how each frame is put together from its pictures of the
column, and where in a cut's picture chat is drawn.

Driving the browser itself is not covered (see docs/testing.md): the column
here is made up, with a band of white where each line would be. Encoding
the frames is tested with real ffmpeg in tests/integration/test_chat_video.py.
"""

from __future__ import annotations

import io
import math
import re
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from kick_clip_hunter import chat_video
from tests.support.html import parse

STATIC = chat_video.STATIC_DIR
WIDTH, HEIGHT, BOTTOM = chat_video.WIDTH, chat_video.HEIGHT, chat_video.BOTTOM_PIXELS
# How tall each line of a made-up column is.
LINE = 36


def line(at: float, nick: str = "alice", text: str = "hello", **more) -> dict:
    """A chat line as chat_trace.chat_replay makes them."""
    return {
        "nick": nick,
        "colour": 3,
        "own_colour": None,
        "badges": [],
        "parts": [{"text": text}],
        "at": f"{at:g}",
        "in_moment": False,
        **more,
    }


def ats(lines) -> list[float]:
    return [float(each["at"]) for each in lines]


class TestName:
    def test_the_video_is_named_after_its_clip(self):
        assert chat_video.video_name("moment_12.mp4") == "moment_12_chat.mov"


class TestLines:
    def test_everything_that_arrives_while_the_clip_runs_is_in(self):
        lines = [line(0), line(3.5), line(40), line(40.1), line(75)]

        assert ats(chat_video.lines_for_clip(lines, 40.0)) == [0, 3.5, 40]

    def test_the_last_lines_from_before_the_clip_are_on_screen_from_the_start(self):
        earlier = [line(-30 + step * 0.25) for step in range(120)]

        kept = chat_video.lines_for_clip([*earlier, line(1)], 40.0)

        assert len(kept) == chat_video.PREFILL_LINES + 1
        assert ats(kept) == [*ats(earlier[-chat_video.PREFILL_LINES :]), 1]

    def test_no_chat_no_lines(self):
        assert chat_video.lines_for_clip([], 40.0) == []


class TestLinesOfAVideoCutOutOfTheFootage:
    """A chat video is made for a stretch cut out of a moment's footage,
    which starts where the editor put it, not where the clip does."""

    def test_lines_are_counted_from_where_the_video_starts(self):
        lines = [line(-4), line(0), line(12.5)]

        assert ats(chat_video.lines_from(lines, 10.0)) == [-14, -10, 2.5]

    def test_a_video_that_starts_before_the_clip_has_the_clips_lines_later(self):
        assert ats(chat_video.lines_from([line(-4), line(0)], -6.0)) == [2, 6]

    def test_chat_can_be_shown_later_than_where_it_stands_or_earlier(self):
        lines = [line(3), line(8)]

        assert ats(chat_video.lines_from(lines, 2.0, later=1.5)) == [2.5, 7.5]
        assert ats(chat_video.lines_from(lines, 2.0, later=-1.5)) == [-0.5, 4.5]

    def test_the_lines_are_otherwise_as_they_were_and_the_ones_given_are_untouched(self):
        given = [line(3, nick="bob", text="KEKW", in_moment=True)]

        (shown,) = chat_video.lines_from(given, 1.0)

        assert shown == {**given[0], "at": "2"}
        assert given[0]["at"] == "3"

    def test_times_are_written_as_the_page_writes_them(self):
        (shown,) = chat_video.lines_from([line(0.1)], 0.2, later=0.3)

        assert shown["at"] == "0.2"

    def test_what_arrives_while_the_cut_runs_is_what_its_video_shows(self):
        lines = [line(-20), line(-3), line(0), line(9), line(30)]

        # Cut from 5 s before the clip, 12 s long.
        shown = chat_video.lines_for_clip(chat_video.lines_from(lines, -5.0), 12.0)

        assert ats(shown) == [-15, 2, 5]


class TestFrameTimes:
    def test_one_frame_every_thirtieth_of_a_second_from_the_first(self):
        times = chat_video.frame_times(2.0)

        assert len(times) == 2 * chat_video.FPS
        assert times[:3] == [0, 1 / chat_video.FPS, 2 / chat_video.FPS]
        assert times[-1] < 2.0

    def test_a_clip_that_ends_between_two_frames_gets_the_frame_it_ends_in(self):
        assert len(chat_video.frame_times(1.01, fps=10)) == 11

    def test_even_a_clip_of_no_length_has_a_frame(self):
        assert chat_video.frame_times(0.0) == [0.0]


class TestPage:
    def test_every_line_is_there_with_the_second_it_arrives(self):
        page = parse(chat_video.page_html([line(-2, "alice", "before"), line(3.5, "bob", "during")]))

        lines = page.one("div", class_="chat-lines").find("p", class_="ch-chat")

        assert [each.attrs["data-at"] for each in lines] == ["-2", "3.5"]
        assert [each.one("b").text for each in lines] == ["alice", "bob"]

    def test_there_is_nothing_behind_the_lines(self):
        # A colour behind them is put in with the frames, not on the page.
        assert "style" not in parse(chat_video.page_html([line(1)])).one("html").attrs

    def test_a_chatter_has_their_colour_and_badges(self):
        known = line(1, own_colour="#9ad8ff", badges=[{"icon": "moderator", "words": "Moderator"}])

        name = parse(chat_video.page_html([known])).one("p", class_="ch-chat").one("b")

        assert name.attrs["style"] == "color: #9ad8ff"
        (badge,) = name.find("img", class_="ch-badge")
        assert badge.attrs["src"].startswith("/static/badges/moderator.svg") and badge.attrs["alt"] == "Moderator"

    def test_every_picture_is_loaded_before_the_column_is_drawn(self):
        # A lazy picture would load only once scrolled to - after its line
        # was drawn without it.
        emote = {"emote": "KEKW", "image": "https://cdn.7tv.app/emote/ID1/2x.webp", "width": 20}

        (picture,) = parse(chat_video.page_html([line(1, parts=[emote])])).find("img", class_="ch-emote")

        assert picture.attrs["src"] == "https://cdn.7tv.app/emote/ID1/2x.webp"
        assert "loading" not in picture.attrs

    def test_what_a_viewer_typed_cannot_become_markup(self):
        nasty = '<script>alert(1)</script><img src=x onerror="alert(2)">'

        page = parse(chat_video.page_html([line(1, nick=nasty, text=nasty)]))

        assert [script.attrs.get("src") for script in page.find("script")] == ["/static/chat_video.js"]
        assert page.find("img") == []
        assert page.one("p", class_="ch-chat").one("b").text == nasty

    def test_it_is_dressed_and_driven_by_files_that_exist(self):
        page = parse(chat_video.page_html([line(1)]))

        (stylesheet,) = [link.attrs["href"] for link in page.find("link", rel="stylesheet")]
        (script,) = [script.attrs["src"] for script in page.find("script")]

        for address in (stylesheet, script):
            assert chat_video.answer(f"{chat_video.ORIGIN}{address}", "other")[0] == "file", address

    def test_names_without_a_colour_of_their_own_get_the_review_pages_colours(self):
        def nicks(stylesheet: str) -> dict[str, str]:
            text = (STATIC / stylesheet).read_text(encoding="utf-8")
            return dict(re.findall(r"(--nick-\d):\s*(#[0-9A-Fa-f]{6})", text))

        assert nicks("chat_video.css") == nicks("dashboard.css") != {}


class TestWhatTheBrowserMayLoad:
    def test_the_page_itself(self):
        assert chat_video.answer(f"{chat_video.ORIGIN}/", "document") == ("page", None)

    @pytest.mark.parametrize(
        "path", ["chat_video.css", "chat_video.js", "fonts/Archivo-Variable.ttf", "badges/moderator.svg"]
    )
    def test_its_own_files(self, path):
        kind, file = chat_video.answer(f"{chat_video.ORIGIN}/static/{path}?v=12", "other")

        assert kind == "file"
        assert file == (STATIC / path).resolve() and file.is_file()

    @pytest.mark.parametrize(
        "path",
        [
            "/static/no_such_file.css",
            "/static/",
            "/static/badges",
            "/static/../chat_video.py",
            "/static/..%2Fchat_video.py",
            "/static/badges/../../main.py",
            "/dashboard",
            "/clips/some_channel/moment_1.mp4",
        ],
    )
    def test_nothing_else_of_its_own_address(self, path):
        assert chat_video.answer(f"{chat_video.ORIGIN}{path}", "other") == ("missing", None)

    def test_a_file_outside_static_is_not_handed_out_even_when_it_exists(self):
        outside = Path(chat_video.__file__).name

        assert (STATIC / ".." / outside).resolve().is_file()
        assert chat_video.answer(f"{chat_video.ORIGIN}/static/../{outside}", "other") == ("missing", None)

    @pytest.mark.parametrize(
        "url",
        [
            "https://cdn.7tv.app/emote/ID1/2x.webp",
            "https://files.kick.com/emotes/37226/fullsize",
            "https://static-cdn.jtvnw.net/emoticons/v2/25/default/dark/2.0",
            "https://cdn.betterttv.net/emote/5f1b0186cf6d2144653d2970/2x",
            "https://cdn.frankerfacez.com/emote/139407/2",
        ],
    )
    def test_emote_pictures_from_where_they_are_kept(self, url):
        assert chat_video.answer(url, "image") == ("fetch", None)

    @pytest.mark.parametrize(
        ("url", "resource_type"),
        [
            ("https://cdn.7tv.app/emote/ID1/2x.webp", "script"),
            ("https://cdn.7tv.app/emote/ID1/2x.webp", "fetch"),
            ("http://cdn.7tv.app/emote/ID1/2x.webp", "image"),
            ("https://cdn.7tv.app.evil.example/emote/ID1/2x.webp", "image"),
            ("https://evil.example/cdn.7tv.app/x.webp", "image"),
            ("https://kick.com/some_channel", "document"),
            ("http://127.0.0.1:8000/dashboard", "document"),
            ("http://chat-video.invalid.evil.example/", "document"),
            ("https://chat-video.invalid/", "document"),
            ("file:///C:/Windows/win.ini", "other"),
            ("data:text/html,<script>alert(1)</script>", "document"),
        ],
    )
    def test_and_nothing_from_anywhere_else(self, url, resource_type):
        assert chat_video.answer(url, resource_type) == ("refuse", None)


# -- the frames ----------------------------------------------------------------


class MadeUpColumn:
    """A column as the browser would hand it over, made up: a line every
    LINE pixels from the top, each a band of opaque white, arriving at the
    given seconds. Remembers which pictures of it were asked for."""

    def __init__(self, arrivals: list[float], line_pixels: int = LINE) -> None:
        self.tops = [index * line_pixels for index in range(len(arrivals))]
        self.bottoms = [top + line_pixels for top in self.tops]
        self.arrivals = arrivals
        self.asked: list[int] = []

    def picture(self, index: int) -> bytes | None:
        self.asked.append(index)
        page = chat_video.PAGE_PIXELS
        if not 0 <= index < max(1, math.ceil((self.bottoms[-1] if self.bottoms else 0) / page)):
            return None
        image = Image.new("RGBA", (WIDTH, page))
        for top, bottom in zip(self.tops, self.bottoms, strict=True):
            upper, lower = max(top - index * page, 0), min(bottom - index * page, page)
            if lower > upper:
                image.paste((255, 255, 255, 255), (0, upper, WIDTH, lower))
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        return buffer.getvalue()

    def column(self) -> chat_video.Column:
        return chat_video.Column(self.arrivals, self.tops, self.bottoms, self.picture)


def seen(frame: bytes) -> np.ndarray:
    """How much of each pixel row of a frame is seen, in its middle."""
    return np.frombuffer(frame, np.uint8).reshape(HEIGHT, WIDTH, 4)[:, WIDTH // 2, 3]


def frame_at(made_up: MadeUpColumn, time: float) -> bytes:
    (frame,) = chat_video.frames(made_up.column(), [time])
    return frame


class TestView:
    def test_nothing_is_shown_before_the_first_line_arrives(self):
        assert chat_video.view_at(MadeUpColumn([2.0]).column(), 1.9) is None

    def test_the_newest_line_sits_at_the_bottom_once_it_has_come_in(self):
        column = MadeUpColumn([1.0, 2.0]).column()

        view = chat_video.view_at(column, 5.0)

        assert view == chat_video.View(2 * LINE + BOTTOM - HEIGHT, ())

    def test_a_line_coming_in_is_still_partly_below_the_frame_and_partly_seen(self):
        column = MadeUpColumn([1.0, 2.0]).column()

        view = chat_video.view_at(column, 2.0 + chat_video.ENTER_SECONDS / 2)

        assert view == chat_video.View(round(2 * LINE + BOTTOM - HEIGHT - LINE / 2), ((1, 128),))

    def test_lines_arriving_together_come_in_together(self):
        column = MadeUpColumn([1.0, 3.0, 3.0]).column()

        view = chat_video.view_at(column, 3.0)

        # Neither of them seen yet, both still below the frame.
        assert view == chat_video.View(3 * LINE + BOTTOM - HEIGHT - 2 * LINE, ((2, 0), (1, 0)))

    def test_the_column_only_ever_moves_on_down(self):
        column = MadeUpColumn([0.5 + step * 0.1 for step in range(60)]).column()

        tops = [chat_video.view_at(column, time).top for time in chat_video.frame_times(8.0)[30:]]

        assert tops == sorted(tops)


class TestComposition:
    def test_before_any_chat_a_frame_is_nothing_at_all(self):
        assert frame_at(MadeUpColumn([3.0]), 1.0) == bytes(WIDTH * HEIGHT * 4)

    def test_a_line_that_has_come_in_is_whole_just_above_the_bottom(self):
        rows = seen(frame_at(MadeUpColumn([1.0]), 2.0))

        assert rows[HEIGHT - BOTTOM - LINE : HEIGHT - BOTTOM].tolist() == [255] * LINE
        assert rows[HEIGHT - BOTTOM :].tolist() == [0] * BOTTOM
        assert rows[: HEIGHT - BOTTOM - LINE].max() == 0

    def test_the_next_line_moves_it_up(self):
        rows = seen(frame_at(MadeUpColumn([1.0, 1.5]), 2.0))

        assert rows[HEIGHT - BOTTOM - 2 * LINE : HEIGHT - BOTTOM].tolist() == [255] * 2 * LINE

    def test_a_line_eases_in_rising_from_under_the_bottom(self):
        halfway = 1.5 + chat_video.ENTER_SECONDS / 2

        rows = seen(frame_at(MadeUpColumn([1.0, 1.5]), halfway))

        # The column is half a line lower than it will be, the new line
        # half seen - and its lower half under the frame's bottom edge.
        first = HEIGHT - BOTTOM - 2 * LINE + LINE // 2
        assert rows[first : first + LINE].tolist() == [255] * LINE
        assert set(rows[first + LINE :].tolist()) == {128}

    def test_the_oldest_lines_fade_out_at_the_top(self):
        rows = seen(frame_at(MadeUpColumn([0.1 * step for step in range(40)]), 9.0))

        fade = chat_video.FADE_PIXELS
        assert rows[0] == 0
        assert rows[fade // 2] == pytest.approx(128, abs=2)
        assert rows[fade : HEIGHT - BOTTOM].min() == 255

    def test_a_frame_like_the_one_before_is_that_one(self):
        times = [2.0, 2.0 + 1 / 30, 2.0 + 2 / 30]

        first, second, third = chat_video.frames(MadeUpColumn([1.0]).column(), times)

        assert second is first and third is first

    def test_pictures_are_taken_as_the_column_reaches_them_and_each_only_once(self, monkeypatch):
        monkeypatch.setattr(chat_video, "PAGE_PIXELS", 100)
        made_up = MadeUpColumn([0.2 * step for step in range(100)])

        for _frame in chat_video.frames(made_up.column(), chat_video.frame_times(20.0)):
            pass

        # 100 lines of 36 pixels are 36 pictures of 100.
        assert made_up.asked == list(range(36))

    def test_a_line_across_two_pictures_is_drawn_whole(self, monkeypatch):
        monkeypatch.setattr(chat_video, "PAGE_PIXELS", 50)

        rows = seen(frame_at(MadeUpColumn([0.1, 0.2, 0.3]), 5.0))

        assert rows[HEIGHT - BOTTOM - 3 * LINE : HEIGHT - BOTTOM].tolist() == [255] * 3 * LINE

    def test_a_column_that_ends_on_the_edge_of_a_picture_is_drawn_whole(self, monkeypatch):
        # The frame reaches past the column's last picture by the room under
        # the newest line: there is no picture there.
        monkeypatch.setattr(chat_video, "PAGE_PIXELS", 2 * LINE)

        rows = seen(frame_at(MadeUpColumn([0.1, 0.2]), 5.0))

        assert rows[HEIGHT - BOTTOM - 2 * LINE : HEIGHT - BOTTOM].tolist() == [255] * 2 * LINE

    def test_lines_arriving_together_rise_in_together(self):
        made_up = MadeUpColumn([1.0, 3.0, 3.0, 3.0])

        arriving, halfway = (frame_at(made_up, time) for time in (3.0, 3.0 + chat_video.ENTER_SECONDS / 2))

        # As they arrive, all three are still below the frame, and only the
        # line before them is seen...
        assert seen(arriving)[HEIGHT - BOTTOM - LINE : HEIGHT - BOTTOM].tolist() == [255] * LINE
        assert seen(arriving)[HEIGHT - BOTTOM :].max() == 0
        # ... and halfway in, half of the three is above the bottom, half seen.
        rows = seen(halfway)
        top_of_first = HEIGHT - BOTTOM - 4 * LINE + 3 * LINE // 2
        assert rows[top_of_first : top_of_first + LINE].tolist() == [255] * LINE
        assert set(rows[top_of_first + LINE :].tolist()) == {128}

    def test_as_many_frames_as_times(self):
        times = chat_video.frame_times(2.0)

        assert len(list(chat_video.frames(MadeUpColumn([0.5]).column(), times))) == len(times)


class TestProgress:
    def test_it_is_told_as_the_frames_go_by_once_a_second(self):
        said: list[float] = []

        handed = list(chat_video._told(iter([b"x"] * 75), 75, said.append))

        assert len(handed) == 75
        assert said == [0.0, 30 / 75, 60 / 75]

    def test_what_it_raises_stops_the_frames(self):
        def give_up(done: float) -> None:
            if done > 0:
                raise RuntimeError("enough")

        told = chat_video._told(iter([b"x"] * 75), 75, give_up)

        with pytest.raises(RuntimeError, match="enough"):
            list(told)


# -- chat drawn into a cut ------------------------------------------------------


class TestPlacement:
    def test_in_a_1080p_picture_the_column_is_the_video_of_its_own(self):
        assert chat_video.placement(1920, 1080, "right") == (WIDTH, HEIGHT, 1920 - WIDTH - 24, 1080 - HEIGHT - 24)

    def test_on_the_left_it_is_as_far_in_from_that_side(self):
        assert chat_video.placement(1920, 1080, "left") == (WIDTH, HEIGHT, 24, 1080 - HEIGHT - 24)

    def test_it_takes_the_same_share_of_a_smaller_picture(self):
        assert chat_video.placement(1280, 720, "right") == (320, 480, 1280 - 320 - 16, 720 - 480 - 16)

    def test_an_upright_picture_is_measured_by_its_width(self):
        assert chat_video.placement(720, 1280, "right") == (320, 480, 720 - 320 - 16, 1280 - 480 - 16)

    def test_sizes_and_corners_are_even_for_the_picture_s_colour_to_line_up(self):
        assert all(value % 2 == 0 for value in chat_video.placement(854, 480, "right"))


class TestBitrate:
    def test_a_little_over_what_the_stream_was_sent_at(self):
        assert chat_video.bitrate(chat_video.Footage(1920, 1080, 8_000_000)) == 10_000_000

    def test_what_a_picture_that_size_is_sent_at_when_that_is_not_known(self):
        assert chat_video.bitrate(chat_video.Footage(1920, 1080, None)) == 10_000_000

    def test_never_too_little_for_the_text_to_stay_sharp(self):
        assert chat_video.bitrate(chat_video.Footage(160, 120, 50_000)) == chat_video.LEAST_BITRATE


class TestFilters:
    def test_a_1080p_picture_takes_the_frames_as_they_are(self):
        assert chat_video._graph(WIDTH, HEIGHT, 1416, 336, None) == "[0:v][1:v]overlay=1416:336:format=auto[picture]"

    def test_a_smaller_one_has_them_made_smaller_first(self):
        graph = chat_video._graph(320, 480, 944, 224, None)

        assert graph == "[1:v]scale=320:480:flags=area[chat];[0:v][chat]overlay=944:224:format=auto[picture]"

    def test_a_background_is_drawn_where_the_chat_goes_before_it_is_laid_over(self):
        graph = chat_video._graph(WIDTH, HEIGHT, 24, 336, "#0a2540")

        assert graph == (
            f"[0:v]drawbox=24:336:{WIDTH}:{HEIGHT}:0x0a2540:t=fill[base];[base][1:v]overlay=24:336:format=auto[picture]"
        )
