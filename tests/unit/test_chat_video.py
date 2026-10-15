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
from collections.abc import Iterable
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from kick_clip_hunter import chat_video
from tests.support.browser import animated_picture
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

RED, BLUE, GREEN = (255, 0, 0, 255), (0, 0, 255, 255), (0, 255, 0, 255)
# Where an animated emote of a made-up column is in its line, and how big
# it is drawn.
EMOTE_TOP, EMOTE = 4, 28


class MadeUpColumn:
    """A column as the browser would hand it over, made up: a line every
    LINE pixels from the top, each a band of opaque white, arriving at the
    given seconds - and animated emotes, each given as the line it is in,
    how far in from the left it is and its picture, which are left out of
    the column's pictures as the browser leaves them out (unless
    `set_aside` is False). Its pictures are `width` wide. Remembers which
    pictures of it were asked for."""

    def __init__(
        self,
        arrivals: list[float],
        line_pixels: int = LINE,
        emotes: Iterable[tuple[int, int, bytes]] = (),
        set_aside: bool = True,
        width: int = WIDTH,
    ) -> None:
        self.width = width
        self.tops = [index * line_pixels for index in range(len(arrivals))]
        self.bottoms = [top + line_pixels for top in self.tops]
        self.arrivals = arrivals
        self.emotes = [
            chat_video.Emote(self.tops[line] + EMOTE_TOP, left, EMOTE, EMOTE, picture) for line, left, picture in emotes
        ]
        self.set_aside = set_aside
        self.asked: list[int] = []

    def picture(self, index: int) -> bytes | None:
        self.asked.append(index)
        page = chat_video.PAGE_PIXELS
        if not 0 <= index < max(1, math.ceil((self.bottoms[-1] if self.bottoms else 0) / page)):
            return None
        image = Image.new("RGBA", (self.width, page))
        for top, bottom in zip(self.tops, self.bottoms, strict=True):
            upper, lower = max(top - index * page, 0), min(bottom - index * page, page)
            if lower > upper:
                image.paste((255, 255, 255, 255), (0, upper, self.width, lower))
        for emote in self.emotes if self.set_aside else ():
            upper, lower = max(emote.top - index * page, 0), min(emote.top + emote.height - index * page, page)
            if lower > upper:
                image.paste((0, 0, 0, 0), (emote.left, upper, emote.left + emote.width, lower))
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        return buffer.getvalue()

    def column(self) -> chat_video.Column:
        return chat_video.Column(self.arrivals, self.tops, self.bottoms, self.picture, self.emotes)


def pixels(frame: bytes) -> np.ndarray:
    return np.frombuffer(frame, np.uint8).reshape(HEIGHT, WIDTH, 4)


def seen(frame: bytes) -> np.ndarray:
    """How much of each pixel row of a frame is seen, in its middle."""
    return pixels(frame)[:, WIDTH // 2, 3]


def frame_at(made_up: MadeUpColumn, time: float) -> bytes:
    (frame,) = chat_video.frames(made_up.column(), [time])
    return frame


# Where the top of the newest line is in a frame, and of the one before it.
NEWEST = HEIGHT - BOTTOM - LINE
BEFORE_IT = NEWEST - LINE


def emote_middle(frame: bytes, line_top: int, left: int) -> tuple[int, ...]:
    """The pixel in the middle of an emote `left` pixels in from the left,
    in the line whose top is `line_top` rows down the frame."""
    return tuple(pixels(frame)[line_top + EMOTE_TOP + EMOTE // 2, left + EMOTE // 2].tolist())


class TestWhereTheFrameIs:
    def test_nothing_is_shown_before_the_first_line_arrives(self):
        assert chat_video.frame_top(MadeUpColumn([2.0]).column(), 1.9) is None

    def test_the_newest_line_is_at_the_bottom_from_the_frame_it_arrives_in(self):
        column = MadeUpColumn([1.0, 2.0]).column()

        assert chat_video.frame_top(column, 1.99) == LINE + BOTTOM - HEIGHT
        assert chat_video.frame_top(column, 2.0) == 2 * LINE + BOTTOM - HEIGHT

    def test_lines_arriving_together_are_there_together(self):
        column = MadeUpColumn([1.0, 3.0, 3.0]).column()

        assert chat_video.frame_top(column, 3.0) == 3 * LINE + BOTTOM - HEIGHT

    def test_the_column_only_ever_moves_on_down(self):
        column = MadeUpColumn([0.5 + step * 0.1 for step in range(60)]).column()

        tops = [chat_video.frame_top(column, time) for time in chat_video.frame_times(8.0)[30:]]

        assert tops == sorted(tops)


class TestComposition:
    def test_before_any_chat_a_frame_is_nothing_at_all(self):
        assert frame_at(MadeUpColumn([3.0]), 1.0) == bytes(WIDTH * HEIGHT * 4)

    def test_a_line_that_has_arrived_is_whole_just_above_the_bottom(self):
        rows = seen(frame_at(MadeUpColumn([1.0]), 2.0))

        assert rows[NEWEST : HEIGHT - BOTTOM].tolist() == [255] * LINE
        assert rows[HEIGHT - BOTTOM :].tolist() == [0] * BOTTOM
        assert rows[:NEWEST].max() == 0

    def test_a_new_line_is_there_whole_at_once_and_moves_the_one_before_up(self):
        made_up = MadeUpColumn([1.0, 1.5])

        before, arriving = (seen(frame_at(made_up, time)) for time in (1.49, 1.5))

        assert before[NEWEST:].tolist() == [255] * LINE + [0] * BOTTOM
        assert arriving[BEFORE_IT:].tolist() == [255] * 2 * LINE + [0] * BOTTOM
        assert arriving[:BEFORE_IT].max() == 0

    def test_lines_arriving_together_come_in_together(self):
        rows = seen(frame_at(MadeUpColumn([1.0, 3.0, 3.0, 3.0]), 3.0))

        assert rows[HEIGHT - BOTTOM - 4 * LINE :].tolist() == [255] * 4 * LINE + [0] * BOTTOM

    def test_a_line_still_to_come_does_not_show_under_the_newest(self):
        # In the column, the next line starts right under the newest one,
        # in the room the frame leaves below it.
        frame = pixels(frame_at(MadeUpColumn([1.0, 5.0]), 2.0))

        assert frame[HEIGHT - BOTTOM :, :, 3].max() == 0

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

    def test_a_column_that_ends_on_the_edge_of_a_picture_needs_none_past_it(self, monkeypatch):
        # The room under the newest line would be the next picture's.
        monkeypatch.setattr(chat_video, "PAGE_PIXELS", 2 * LINE)
        made_up = MadeUpColumn([0.1, 0.2])

        rows = seen(frame_at(made_up, 5.0))

        assert rows[BEFORE_IT:].tolist() == [255] * 2 * LINE + [0] * BOTTOM
        assert made_up.asked == [0]

    def test_as_many_frames_as_times(self):
        times = chat_video.frame_times(2.0)

        assert len(list(chat_video.frames(MadeUpColumn([0.5]).column(), times))) == len(times)


class TestShapes:
    """Frames of a column of chat drawn into a picture where the Trim dialog
    put it: of any height, and drawn twice as fine for chat larger than the
    page has it."""

    def test_a_shorter_column_shows_the_newest_lines_and_fades_over_less(self):
        shape = chat_video.Shape(200)
        made_up = MadeUpColumn([0.1 * step for step in range(20)])

        (frame,) = chat_video.frames(made_up.column(), [5.0], shape)

        rows = np.frombuffer(frame, np.uint8).reshape(200, WIDTH, 4)[:, WIDTH // 2, 3]
        assert shape.fade == 50
        assert rows[0] == 0 and rows[25] == pytest.approx(128, abs=2)
        assert rows[50 : 200 - BOTTOM].min() == 255 and rows[200 - BOTTOM :].max() == 0

    def test_twice_as_fine_it_has_twice_as_many_pixels_every_way(self):
        shape = chat_video.Shape(HEIGHT, density=2)
        made_up = MadeUpColumn([0.5], line_pixels=2 * LINE, width=2 * WIDTH)

        (frame,) = chat_video.frames(made_up.column(), [1.0], shape)

        assert shape.size == (2 * WIDTH, 2 * HEIGHT)
        rows = np.frombuffer(frame, np.uint8).reshape(2 * HEIGHT, 2 * WIDTH, 4)[:, -1, 3]
        newest = 2 * HEIGHT - 2 * BOTTOM - 2 * LINE
        assert rows[newest:].tolist() == [255] * 2 * LINE + [0] * 2 * BOTTOM
        assert rows[:newest].max() == 0


class TestAnimatedEmotes:
    """Emotes that move: drawn into each frame by the column's frames, on
    the frame of their animation the video is at."""

    # Red for a tenth of a second, blue for one, green for two, over and
    # over from the video's first frame: green from 0.2 s to 0.4 s, red
    # again from 0.4 s.
    PICTURE = animated_picture([RED, BLUE, GREEN], [100, 100, 200])

    def test_one_is_drawn_where_it_is_on_the_frame_its_animation_is_at(self):
        made_up = MadeUpColumn([0.5], emotes=[(0, 100, self.PICTURE)])

        drawn = chat_video.frames(made_up.column(), [1.25, 1.35, 1.45, 1.55, 1.65])

        assert [emote_middle(frame, NEWEST, 100) for frame in drawn] == [RED, BLUE, GREEN, GREEN, RED]

    def test_every_copy_of_a_picture_is_on_the_same_frame_of_it(self):
        made_up = MadeUpColumn([0.5, 1.0], emotes=[(0, 100, self.PICTURE), (1, 300, self.PICTURE)])

        for frame, colour in zip(chat_video.frames(made_up.column(), [1.25, 1.35]), [RED, BLUE], strict=True):
            assert emote_middle(frame, BEFORE_IT, 100) == emote_middle(frame, NEWEST, 300) == colour

    def test_it_moves_up_with_its_line(self):
        made_up = MadeUpColumn([0.5, 1.0], emotes=[(0, 100, self.PICTURE)])

        before, after = chat_video.frames(made_up.column(), [0.65, 1.05])

        assert emote_middle(before, NEWEST, 100) == GREEN
        assert emote_middle(after, BEFORE_IT, 100) == GREEN
        # Where it was is the line that has arrived since.
        assert emote_middle(after, NEWEST, 100) == (255, 255, 255, 255)

    def test_one_in_a_line_still_to_come_is_not_drawn(self):
        made_up = MadeUpColumn([0.5, 5.0], emotes=[(1, 100, self.PICTURE)])

        frame = pixels(frame_at(made_up, 1.0))

        assert frame[HEIGHT - BOTTOM :, :, 3].max() == 0

    def test_it_fades_out_at_the_top_like_its_line(self):
        arrivals = [0.1 * step for step in range(40)]
        # The line whose top is 26 rows down the frame once all forty have
        # arrived.
        line = (40 * LINE + BOTTOM - HEIGHT + 26) // LINE
        made_up = MadeUpColumn(arrivals, emotes=[(line, 100, self.PICTURE)])

        frame = pixels(frame_at(made_up, 9.05))

        row = 26 + EMOTE_TOP + EMOTE // 2
        assert tuple(frame[row, 100 + EMOTE // 2].tolist()) == (0, 255, 0, round(255 * row / chat_video.FADE_PIXELS))

    def test_what_is_see_through_in_it_shows_what_is_under_it(self):
        half_seen = animated_picture([(255, 0, 0, 128), (0, 0, 255, 128)], [500, 500], "WEBP")
        on_its_own = MadeUpColumn([0.5], emotes=[(0, 100, half_seen)])
        over_its_line = MadeUpColumn([0.5], emotes=[(0, 100, half_seen)], set_aside=False)

        alone, over = (emote_middle(frame_at(made_up, 1.2), NEWEST, 100) for made_up in (on_its_own, over_its_line))

        assert alone == (255, 0, 0, 128)
        # Half of the white line under it shows through.
        assert over == pytest.approx((255, 127, 127, 255), abs=1)

    def test_a_frame_stays_the_one_before_until_an_emote_on_it_moves_on(self):
        made_up = MadeUpColumn([0.5], emotes=[(0, 100, self.PICTURE)])

        first, second, third, moved = chat_video.frames(made_up.column(), [1.0, 1.0 + 1 / 30, 1.0 + 2 / 30, 1.2])

        assert second is first and third is first
        assert moved != first

    def test_each_picture_is_taken_apart_once_however_often_it_is_shown(self, monkeypatch):
        taken: list[bytes] = []

        def taking_apart(picture: bytes, width: int, height: int) -> chat_video.Animation:
            taken.append(picture)
            return animation(picture, width, height)

        animation = chat_video.animation
        monkeypatch.setattr(chat_video, "animation", taking_apart)
        made_up = MadeUpColumn([0.5, 1.0, 1.5], emotes=[(line, 100, self.PICTURE) for line in range(3)])

        for _frame in chat_video.frames(made_up.column(), chat_video.frame_times(3.0)):
            pass

        assert taken == [self.PICTURE]


class TestAnimations:
    """An emote's picture taken apart into its frames, as hosts of emotes
    hand them out."""

    @pytest.mark.parametrize("kind", ["GIF", "WEBP", "PNG"])
    def test_a_picture_of_more_than_one_frame_is_animated(self, kind):
        assert chat_video.is_animated(animated_picture([RED, BLUE], [100, 100], kind))

    def test_one_of_one_frame_is_not(self):
        assert not chat_video.is_animated(animated_picture([RED], [100], "WEBP"))

    def test_nor_is_one_that_cannot_be_read_whole(self):
        picture = animated_picture([RED, BLUE, GREEN], [100, 100, 100])

        assert not chat_video.is_animated(picture[: len(picture) // 2])
        assert not chat_video.is_animated(b"<html>not found</html>")

    def test_each_frame_lasts_as_long_as_the_picture_says(self):
        made = chat_video.animation(animated_picture([RED, BLUE, GREEN], [100, 250, 50]), EMOTE, EMOTE)

        assert made.ends == [100, 350, 400]
        assert [tuple(frame[0, 0].tolist()) for frame in made.frames] == [RED, BLUE, GREEN]

    def test_a_frame_that_says_it_flashes_by_lasts_a_tenth_of_a_second(self):
        made = chat_video.animation(animated_picture([RED, BLUE, GREEN], [0, 5, 20], "WEBP"), EMOTE, EMOTE)

        assert made.ends == [100, 200, 220]

    def test_it_runs_through_its_frames_over_and_over(self):
        made = chat_video.animation(animated_picture([RED, BLUE, GREEN], [100, 100, 200]), EMOTE, EMOTE)

        indexes = [made.index(second) for second in (0.0, 0.099, 0.1, 0.2, 0.399, 0.4, 1.35, 3600.25)]

        assert indexes == [0, 0, 1, 2, 2, 0, 1, 2]

    def test_its_frames_are_the_size_it_is_drawn_at(self):
        made = chat_video.animation(animated_picture([RED, BLUE], [100, 100], size=(112, 64)), 49, 28)

        assert [frame.shape for frame in made.frames] == [(28, 49, 4)] * 2

    def test_made_smaller_its_edges_keep_their_colour(self):
        # Red on the left half, nothing on the right: made smaller, the
        # pixels between them are partly seen - and red, not darkened.
        frame = Image.new("RGBA", (8, 8))
        frame.paste(RED, (0, 0, 4, 8))
        buffer = io.BytesIO()
        frame.save(buffer, "WEBP", save_all=True, append_images=[frame.copy()], duration=[100, 100], lossless=True)

        made = chat_video.animation(buffer.getvalue(), 3, 3)

        middle = made.frames[0][1, 1]
        assert 0 < middle[3] < 255
        assert middle[:3].tolist() == [255, 0, 0]


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


Placed, Shape = chat_video.Placed, chat_video.Shape


class TestPlacementWhereNothingIsSaid:
    def test_in_a_1080p_picture_it_is_the_video_of_its_own_at_the_bottom_right(self):
        assert chat_video.placement(1920, 1080) == Placed(1920 - WIDTH - 24, 1080 - HEIGHT - 24, WIDTH, HEIGHT, Shape())

    def test_it_takes_the_same_share_of_a_smaller_picture(self):
        assert chat_video.placement(1280, 720) == Placed(1280 - 320 - 16, 720 - 480 - 16, 320, 480, Shape())

    def test_an_upright_picture_is_measured_by_its_width(self):
        assert chat_video.placement(720, 1280) == Placed(720 - 320 - 16, 1280 - 480 - 16, 320, 480, Shape())

    def test_in_a_larger_picture_it_is_drawn_twice_as_fine(self):
        assert chat_video.placement(3840, 2160).shape == Shape(HEIGHT, density=2)

    def test_sizes_and_corners_are_even_for_the_picture_s_colour_to_line_up(self):
        placed = chat_video.placement(854, 480)

        assert all(value % 2 == 0 for value in placed[:4])


class TestPlacementInABox:
    """Where the Trim dialog put chat: its box's width says how large its
    text is, its height how much of the column shows."""

    def test_it_is_where_the_box_is_and_as_large(self):
        placed = chat_video.placement(1920, 1080, (0.05, 0.1, 0.25, 0.5))

        # A quarter of 1920 is the page's own width: text as large as there.
        assert placed == Placed(96, 108, 480, 540, Shape(540))

    def test_a_narrower_box_has_smaller_text_and_more_of_the_column_in_the_same_height(self):
        placed = chat_video.placement(1920, 1080, (0.05, 0.1, 0.125, 0.5))

        assert placed == Placed(96, 108, 240, 540, Shape(1080))

    def test_chat_larger_than_the_page_has_it_is_drawn_twice_as_fine(self):
        placed = chat_video.placement(1920, 1080, (0.05, 0.1, 0.375, 0.5))

        assert placed == Placed(96, 108, 720, 540, Shape(360, density=2))

    @pytest.mark.parametrize(
        ("width", "across"),
        [(0.05, round(WIDTH * chat_video.SCALE_LEAST)), (0.9, round(WIDTH * chat_video.SCALE_MOST))],
        ids=["too-small", "too-large"],
    )
    def test_its_text_is_never_smaller_or_larger_than_it_may_be(self, width, across):
        assert chat_video.placement(1920, 1080, (0.0, 0.0, width, 0.5)).across == across

    def test_at_least_a_few_lines_of_it_show(self):
        placed = chat_video.placement(1920, 1080, (0.0, 0.5, 0.25, 0.02))

        assert placed.shape.height == chat_video.COLUMN_LEAST and placed.down == chat_video.COLUMN_LEAST

    def test_it_is_kept_within_the_picture(self):
        # Made larger than the box, the text takes it past the right edge.
        placed = chat_video.placement(1920, 1080, (0.97, 0.9, 0.03, 0.1))

        assert placed.left + placed.across <= 1920 and placed.top + placed.down <= 1080

    def test_it_never_reaches_past_a_small_picture(self):
        placed = chat_video.placement(320, 180, (0.0, 0.0, 1.0, 1.0))

        assert (placed.across, placed.down) == (320, 180)

    def test_sizes_and_corners_are_even(self):
        placed = chat_video.placement(1366, 768, (0.31, 0.27, 0.233, 0.411))

        assert all(value % 2 == 0 for value in placed[2:4])


class TestBitrate:
    def test_a_little_over_what_the_stream_was_sent_at(self):
        assert chat_video.bitrate(chat_video.Footage(1920, 1080, 8_000_000)) == 10_000_000

    def test_what_a_picture_that_size_is_sent_at_when_that_is_not_known(self):
        assert chat_video.bitrate(chat_video.Footage(1920, 1080, None)) == 10_000_000

    def test_never_too_little_for_the_text_to_stay_sharp(self):
        assert chat_video.bitrate(chat_video.Footage(160, 120, 50_000)) == chat_video.LEAST_BITRATE


class TestFilters:
    def test_a_1080p_picture_takes_the_frames_as_they_are(self):
        graph = chat_video._graph(Placed(1416, 336, WIDTH, HEIGHT, Shape()), None)

        assert graph == "[0:v][1:v]overlay=1416:336:format=auto[picture]"

    def test_a_smaller_one_has_them_made_smaller_first(self):
        graph = chat_video._graph(Placed(944, 224, 320, 480, Shape()), None)

        assert graph == "[1:v]scale=320:480:flags=area[chat];[0:v][chat]overlay=944:224:format=auto[picture]"

    def test_frames_drawn_twice_as_fine_are_made_the_size_of_the_box(self):
        graph = chat_video._graph(Placed(96, 108, 720, 540, Shape(360, density=2)), None)

        assert graph == "[1:v]scale=720:540:flags=area[chat];[0:v][chat]overlay=96:108:format=auto[picture]"

    def test_a_background_is_drawn_where_the_chat_goes_before_it_is_laid_over(self):
        graph = chat_video._graph(Placed(24, 336, WIDTH, HEIGHT, Shape()), "#0a2540")

        assert graph == (
            f"[0:v]drawbox=24:336:{WIDTH}:{HEIGHT}:0x0a2540:t=fill[base];[base][1:v]overlay=24:336:format=auto[picture]"
        )
