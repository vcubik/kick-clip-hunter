"""A stand-in for the browser a chat video is drawn with (chat_video.py),
for the tests that have one made: no browser is ever launched by the suite.
"""

from __future__ import annotations

import threading

from tests.support.media import png_frame


class FakeBrowser:
    """Remembers the pages it was given and hands back a picture for every
    frame asked for. `hold()` makes it wait before the first picture until
    `release()`. Put in the place of `chat_video.capture_frames`."""

    def __init__(self) -> None:
        self.pages: list[str] = []
        self.frames: list[int] = []
        self.failure: Exception | None = None
        self._waiting = threading.Event()
        self._waiting.set()

    def hold(self) -> None:
        self._waiting.clear()

    def release(self) -> None:
        self._waiting.set()

    def capture_frames(self, html, times):
        self.pages.append(html)
        self.frames.append(len(times))
        assert self._waiting.wait(timeout=20), "the test never released the browser"
        if self.failure is not None:
            raise self.failure
        for _ in times:
            yield png_frame(rgba=(255, 255, 255, 128))
