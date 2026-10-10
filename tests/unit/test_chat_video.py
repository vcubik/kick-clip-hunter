"""The chat video, up to the browser: which lines go into it, the page they
are put on, which second each frame shows, and what the browser that draws
the page is allowed to load.

Driving the browser itself is not covered (see docs/testing.md); encoding
the frames it returns is, in tests/integration/test_chat_video.py.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kick_clip_hunter import chat_video
from tests.support.html import parse

STATIC = chat_video.STATIC_DIR


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


class TestFrames:
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

    def test_a_chatter_has_their_colour_and_badges(self):
        known = line(1, own_colour="#9ad8ff", badges=[{"icon": "moderator", "words": "Moderator"}])

        name = parse(chat_video.page_html([known])).one("p", class_="ch-chat").one("b")

        assert name.attrs["style"] == "color: #9ad8ff"
        (badge,) = name.find("img", class_="ch-badge")
        assert badge.attrs["src"].startswith("/static/badges/moderator.svg") and badge.attrs["alt"] == "Moderator"

    def test_pictures_are_loaded_whether_or_not_their_line_is_showing(self):
        # A lazy picture in a hidden line would never load, and the page is
        # never scrolled.
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
        "url", ["https://cdn.7tv.app/emote/ID1/2x.webp", "https://files.kick.com/emotes/37226/fullsize"]
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
