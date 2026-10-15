"""A stand-in for the browser chat is laid out by for a chat video
(chat_video.py), for the tests that have one made: no browser is ever
launched by the suite.
"""

from __future__ import annotations

import io
import math
import re
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from PIL import Image

from kick_clip_hunter import chat_video

# How tall each line of the column is, and what it is drawn as: a band of
# white across the column, opaque, so that where chat is can be told in
# any frame - and in any picture it is drawn into.
LINE_PIXELS = 36
LINE_COLOUR = (255, 255, 255, 255)
# Where an animated emote is in a line - this far down from its top and in
# from the column's left - and how big it is drawn.
EMOTE_TOP = 4
EMOTE_LEFT = 60
EMOTE_PIXELS = 28


def animated_picture(
    colours: Sequence[tuple[int, int, int, int]],
    milliseconds: Sequence[int],
    kind: str = "GIF",
    size: tuple[int, int] = (EMOTE_PIXELS, EMOTE_PIXELS),
) -> bytes:
    """An animated emote's picture as its host hands it out: a frame all of
    each colour in turn, each lasting as many milliseconds as given."""
    frames = [Image.new("RGBA", size, colour) for colour in colours]
    buffer = io.BytesIO()
    exact = {"lossless": True} if kind == "WEBP" else {}
    frames[0].save(buffer, kind, save_all=True, append_images=frames[1:], duration=list(milliseconds), loop=0, **exact)
    return buffer.getvalue()


class FakeBrowser:
    """Lays a page of chat out as the browser would, as far as the frames
    are concerned - a line every LINE_PIXELS from the top, in the order of
    the page, each arriving at the second the page gives it - and remembers
    the pages it was given, and the density it was asked for. `hold()`
    makes it wait before laying a page out until `release()`; with
    `failure` set it raises that instead. With `emote` set to an animated
    emote's picture, every line has that emote in it, left out of the
    pictures and handed over apart, as the browser does. Asked for a
    density, it lays the page out that many times as large every way, as
    the browser draws it then. Put in the place of
    `chat_video.drawn_column`."""

    def __init__(self) -> None:
        self.pages: list[str] = []
        self.densities: list[int] = []
        self.emote: bytes | None = None
        self.failure: Exception | None = None
        self._waiting = threading.Event()
        self._waiting.set()

    def hold(self) -> None:
        self._waiting.clear()

    def release(self) -> None:
        self._waiting.set()

    @contextmanager
    def drawn_column(self, html: str, density: int = 1) -> Iterator[chat_video.Column]:
        self.pages.append(html)
        self.densities.append(density)
        assert self._waiting.wait(timeout=20), "the test never released the browser"
        if self.failure is not None:
            raise self.failure
        arrivals = [float(at) for at in re.findall(r'data-at="([^"]*)"', html)]
        # Everything is `density` times as many pixels, every way.
        line, width = LINE_PIXELS * density, chat_video.WIDTH * density
        tops = [index * line for index in range(len(arrivals))]
        bottoms = [top + line for top in tops]
        emotes = []
        if self.emote is not None:
            at, size = (EMOTE_TOP * density, EMOTE_LEFT * density), EMOTE_PIXELS * density
            emotes = [chat_video.Emote(top + at[0], at[1], size, size, self.emote) for top in tops]
        page = chat_video.PAGE_PIXELS
        count = max(1, math.ceil((bottoms[-1] if bottoms else 0) / page))

        def picture(index: int) -> bytes | None:
            if not 0 <= index < count:
                return None
            image = Image.new("RGBA", (width, page))
            spans = [(top, bottom, LINE_COLOUR, 0, width) for top, bottom in zip(tops, bottoms, strict=True)]
            # Where an emote that moves is, there is nothing.
            spans += [(e.top, e.top + e.height, (0, 0, 0, 0), e.left, e.left + e.width) for e in emotes]
            for top, bottom, colour, left, right in spans:
                upper, lower = max(top - index * page, 0), min(bottom - index * page, page)
                if lower > upper:
                    image.paste(colour, (left, upper, right, lower))
            buffer = io.BytesIO()
            image.save(buffer, "PNG")
            return buffer.getvalue()

        yield chat_video.Column(arrivals, tops, bottoms, picture, emotes)
