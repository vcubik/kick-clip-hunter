"""A moment's chat as a video with a transparent background, to lay over
the clip in a video editor.

The chat beside a clip is a web page; this turns that page into a video.
A browser without a window opens a page holding the clip's chat lines and
is stepped through the clip one frame at a time: the page is told what
second of the clip it is, shows the lines that had arrived by then - each
new one easing in at the bottom, the older ones moving up - and a picture
of it is taken, background left out. ffmpeg joins the pictures into a
ProRes 4444 file, the format editors read transparency from. Nothing is
recorded as it happens, so no frame is ever dropped and how long the
rendering takes has no bearing on the result. The one thing that does run
on the browser's own clock is an animated emote: it moves, but not in time
with anything.

The video starts on the clip's first frame and is as long as the clip, so
in an editor it goes on the track above the clip, aligned to its start.

The browser only ever opens this project's own page: it is given the page,
the stylesheet, the script and the badge pictures from memory and from
`static/`, may fetch emote pictures from the two hosts they are on, and is
refused everything else. That is also why it can be headless, unlike the
browser that looks at kick.com (kick_stream.py).
"""

from __future__ import annotations

import math
import subprocess
import tempfile
import threading
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader
from patchright.sync_api import sync_playwright

FFMPEG_BIN = "ffmpeg"

# The video: a column of chat. An editor can scale it down without harm;
# scaled up, the text goes soft.
WIDTH = 480
HEIGHT = 720
FPS = 30
# ffmpeg's quantiser for the picture, 0 (best) to 31; the transparency is
# kept exactly whatever this is. Text this size shows no loss here, and the
# file is a good deal smaller than at the encoder's own setting.
QUALITY = 11

# Chat already on screen when the clip starts: the last lines from before
# its first frame. More than fit, so the column starts out full.
PREFILL_LINES = 40

# The address the browser is given for the page. It never leaves the
# machine - every request to it is answered here (see answer()).
ORIGIN = "http://chat-video.invalid"
# Where emote pictures are: 7TV's and Kick's own.
IMAGE_HOSTS = frozenset({"cdn.7tv.app", "files.kick.com"})
# How long the page may wait for its pictures before the rendering starts
# without the ones still missing (their names are drawn instead).
PICTURES_TIMEOUT_SECONDS = 20

PACKAGE_DIR = Path(__file__).parent
STATIC_DIR = PACKAGE_DIR / "static"

# Chat is typed by viewers: everything of it is escaped on its way in.
_templates = Environment(loader=FileSystemLoader(PACKAGE_DIR / "templates"), autoescape=True)

# A rendering keeps a browser and an encoder busy; one at a time.
_one_at_a_time = threading.Lock()


class ChatVideoError(RuntimeError):
    pass


def video_name(clip_name: str) -> str:
    """The chat video's file name, next to its clip like the context clips."""
    return f"{Path(clip_name).stem}_chat.mov"


def lines_for_clip(lines: Sequence[Mapping[str, Any]], duration: float) -> list[Mapping[str, Any]]:
    """The chat lines (see chat_trace.chat_replay) the video of a clip that
    long shows: everything that arrives while it runs, and the last few
    from before it starts, which are on screen from the first frame."""
    before = [line for line in lines if float(line["at"]) < 0]
    during = [line for line in lines if 0 <= float(line["at"]) <= duration]
    return before[-PREFILL_LINES:] + during


def frame_times(duration: float, fps: int = FPS) -> list[float]:
    """The second of the clip each frame of the video shows."""
    return [frame / fps for frame in range(max(1, math.ceil(duration * fps)))]


def page_html(lines: Sequence[Mapping[str, Any]]) -> str:
    """The page the browser is stepped through: every line, none shown yet."""
    return _templates.get_template("chat_video.html").render(lines=lines)


def answer(url: str, resource_type: str) -> tuple[str, Path | None]:
    """What a request made by the page is answered with: ("page", None) for
    the page itself, ("file", path) for something of `static/`, ("fetch",
    None) for an emote picture that may be fetched from where it is,
    ("missing", None) for an address of the page's own that has nothing
    behind it and ("refuse", None) for everything else."""
    parts = urlsplit(url)
    if f"{parts.scheme}://{parts.netloc}" == ORIGIN:
        if parts.path == "/":
            return "page", None
        if parts.path.startswith("/static/"):
            root = STATIC_DIR.resolve()
            file = (root / parts.path.removeprefix("/static/")).resolve()
            if file.is_file() and root in file.parents:
                return "file", file
        return "missing", None
    if parts.scheme == "https" and parts.hostname in IMAGE_HOSTS and resource_type == "image":
        return "fetch", None
    return "refuse", None


def capture_frames(html: str, times: Sequence[float]) -> Iterator[bytes]:
    """A PNG of the page, background left out, for each of `times`."""

    def serve(route) -> None:
        kind, file = answer(route.request.url, route.request.resource_type)
        if kind == "page":
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
        elif kind == "file":
            route.fulfill(path=str(file))
        elif kind == "fetch":
            route.continue_()
        elif kind == "missing":
            route.fulfill(status=404, body="")
        else:
            route.abort()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
            page.route("**/*", serve)
            # Not "load": that waits for every picture, and how long those
            # may take is the page's own ready() to decide.
            page.goto(f"{ORIGIN}/", wait_until="domcontentloaded")
            # The page's own script is in the page's world; patchright runs
            # what it is given apart from that unless told otherwise.
            page.evaluate(
                "limit => chatVideo.ready(limit)", PICTURES_TIMEOUT_SECONDS * 1000, isolated_context=False
            )
            for time in times:
                page.evaluate("time => chatVideo.showAt(time)", time, isolated_context=False)
                yield page.screenshot(type="png", omit_background=True)
        finally:
            browser.close()


def encode(frames: Iterable[bytes], output: Path, fps: int = FPS) -> int:
    """Joins PNG frames into a ProRes 4444 video that keeps their
    transparency, and returns how many there were. The file appears under
    its name only once it is complete."""
    output.parent.mkdir(parents=True, exist_ok=True)
    unfinished = output.with_name(f"{output.stem}.part{output.suffix}")
    count = 0
    # ffmpeg's complaints go to a file, not a pipe: nothing reads a pipe
    # while frames are being written, and a full one would stall both sides.
    with tempfile.TemporaryFile() as complaints:
        # fmt: off
        process = subprocess.Popen(
            [
                FFMPEG_BIN, "-y", "-loglevel", "error",
                "-f", "image2pipe", "-framerate", str(fps), "-c:v", "png", "-i", "-",
                "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", "-vendor", "apl0",
                "-qscale:v", str(QUALITY), "-alpha_bits", "8",
                "-an", str(unfinished),
            ],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=complaints,
        )
        # fmt: on
        pipe = process.stdin
        assert pipe is not None
        try:
            try:
                for frame in frames:
                    pipe.write(frame)
                    count += 1
                pipe.close()
            except OSError:
                pass  # ffmpeg gave up; why is in its complaints
            status = process.wait()
        except BaseException:
            process.kill()
            process.wait()
            unfinished.unlink(missing_ok=True)
            raise
        finally:
            try:
                pipe.close()
            except OSError:
                pass
        if status != 0 or count == 0:
            unfinished.unlink(missing_ok=True)
            complaints.seek(0)
            said = complaints.read().decode(errors="replace").strip()
            reason = "there were no frames to encode" if count == 0 else said or f"it exited with status {status}"
            raise ChatVideoError(f"ffmpeg could not write {output.name}: {reason}")
    unfinished.replace(output)
    return count


def render(lines: Sequence[Mapping[str, Any]], duration: float, output: Path) -> int:
    """Renders the chat video of a clip `duration` seconds long from its
    chat lines (see chat_trace.chat_replay) and returns its frame count."""
    html = page_html(lines_for_clip(lines, duration))
    with _one_at_a_time:
        frames = capture_frames(html, frame_times(duration))
        try:
            return encode(frames, output)
        finally:
            close = getattr(frames, "close", None)
            if close:
                close()
