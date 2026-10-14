"""Chat as video, for the stretch of footage an editor cut out with Trim
(see clip_trim.py): either a video of its own, to lay over the cut or put
beside it in a video editor, or drawn into the cut's own picture.

The chat beside a clip is a web page; this turns it into frames. A browser
without a window lays out every chat line of the stretch once, one under
another in a column as wide as the video, and takes pictures of that
column a screenful at a time, background left out. Each frame is then put
together here from those pictures: the part of the column chat showed at
that moment - the lines that had arrived by then, the newest at the
bottom, the oldest fading out at the top. A new line is there at once,
the ones before it moved up to make room, as in chat itself. Nothing is
drawn twice and nothing runs on a clock: no frame is ever dropped, and
half an hour of a busy chat is ready in a minute or two - when the browser
drew every frame, it took twice as long as the video runs.

An animated emote would stand still in those pictures, on whichever of
its frames the browser drew. So the browser leaves the animated ones out
of its pictures, their pictures are taken apart into their frames here
(see Animation), and each frame of the video has them drawn in, on the
frame of their animation it shows. That takes longer than a column that
stands still: a frame with an emote moving on it is seldom the one before
it over again.

The frames go to ffmpeg as raw pictures, for one of three files:

- a video of its own that is see-through where there is no chat: ProRes
  4444, the format editors read transparency from. Every frame of it is
  whole, so it is large however little changes - a few megabytes for every
  second of a busy chat (see clip_trim.CHAT_VIDEO_MAX_SECONDS);
- a video of its own on a colour, to put next to the picture: H.264,
  which keeps only what changes from one frame to the next - a fraction of
  the size;
- the cut itself with its chat drawn into a corner of the picture (see
  burn). The picture is encoded anew for that, on the graphics card where
  ffmpeg can use one (see video_encoder): that is the slow part, at the
  pace the card - or the processor - encodes the stream's picture.

The browser only ever opens this project's own page: it is given the page,
the stylesheet, the script and the badge pictures from memory and from
`static/`, may fetch emote pictures from the hosts they are on, and is
refused everything else. That is also why it can be headless, unlike the
browser that looks at kick.com (kick_stream.py).
"""

from __future__ import annotations

import bisect
import io
import json
import logging
import math
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlsplit

import numpy as np
from jinja2 import Environment, FileSystemLoader
from patchright.sync_api import Error as BrowserError
from patchright.sync_api import sync_playwright
from PIL import Image

logger = logging.getLogger("kick_clip_hunter")

FFMPEG_BIN = "ffmpeg"
FFPROBE_BIN = "ffprobe"

# A video of its own: a column of chat. An editor can scale it down without
# harm; scaled up, the text goes soft.
WIDTH = 480
HEIGHT = 720
FPS = 30
# ffmpeg's quantiser for the ProRes picture, 0 (best) to 31; the
# transparency is kept exactly whatever this is. Text this size shows no
# loss here, and the file is a good deal smaller than at the encoder's own
# setting.
QUALITY = 11
# The H.264 one on a colour: x264's constant quality, lower is better.
# Text on a flat colour is easy to keep sharp; this keeps it so.
COLOUR_QUALITY = 18

# Chat already on screen when the video starts: the last lines from before
# its first frame. More than fit, so the column starts out full.
PREFILL_LINES = 40
# Over how much of the column's top the oldest lines fade out.
FADE_PIXELS = 72
# Room under the newest line.
BOTTOM_PIXELS = 10
# How much of the column the browser takes a picture of at a time: well
# under what it can draw in one go, and a hundred lines of chat or more.
PAGE_PIXELS = 4096

# Chat drawn into a cut's picture: a column of the shape of the video of
# its own, as tall as two thirds of the picture's shorter side - so in a
# picture 1080 lines high, exactly that video - at the bottom, this far in
# from the side (at that size).
SIDES = ("right", "left")
MARGIN_PIXELS = 24
# The picture of a burned cut is encoded at about the bitrate the stream
# was sent at - a little over, to make up for its being encoded a second
# time - and never under the least.
LEAST_BITRATE = 2_000_000
BITRATE_OVER = 1.25
# What a picture is taken to have been sent at when ffprobe can't say, per
# pixel of it: 8 Mbit/s for 1080p, about what the platforms send.
BITRATE_PER_PIXEL = 8_000_000 / (1920 * 1080)
AUDIO_BITRATE = "192k"

# The address the browser is given for the page. It never leaves the
# machine - every request to it is answered here (see answer()).
ORIGIN = "http://chat-video.invalid"
# Where emote pictures are: Kick's own, Twitch's own, 7TV's, BetterTTV's and
# FrankerFaceZ's.
IMAGE_HOSTS = frozenset(
    {"cdn.7tv.app", "files.kick.com", "static-cdn.jtvnw.net", "cdn.betterttv.net", "cdn.frankerfacez.com"}
)
# How long the page may wait for its pictures before the column is drawn
# without the ones still missing (their names are drawn instead).
PICTURES_TIMEOUT_SECONDS = 20

PACKAGE_DIR = Path(__file__).parent
STATIC_DIR = PACKAGE_DIR / "static"

# Chat is typed by viewers: everything of it is escaped on its way in.
_templates = Environment(loader=FileSystemLoader(PACKAGE_DIR / "templates"), autoescape=True)

# Making one keeps a browser and an encoder busy - and a burn the graphics
# card, or every core; one at a time.
_one_at_a_time = threading.Lock()


class ChatVideoError(RuntimeError):
    pass


def video_name(clip_name: str) -> str:
    """The chat video's file name, next to the video it goes with."""
    return f"{Path(clip_name).stem}_chat.mov"


def lines_for_clip(lines: Sequence[Mapping[str, Any]], duration: float) -> list[Mapping[str, Any]]:
    """The chat lines (see chat_trace.chat_replay) a video that long shows:
    everything that arrives while it runs, and the last few from before it
    starts, which are on screen from the first frame."""
    before = [line for line in lines if float(line["at"]) < 0]
    during = [line for line in lines if 0 <= float(line["at"]) <= duration]
    return before[-PREFILL_LINES:] + during


def lines_from(lines: Sequence[Mapping[str, Any]], start: float, later: float = 0.0) -> list[dict[str, Any]]:
    """The chat lines of a clip as the lines of a video that starts `start`
    seconds into it - before its first frame, if negative - with every line
    `later` seconds after where it stands against the picture (before, if
    negative)."""
    return [{**line, "at": f"{round(float(line['at']) - start + later, 2):g}"} for line in lines]


def frame_times(duration: float, fps: int = FPS) -> list[float]:
    """The second of the video each of its frames shows."""
    return [frame / fps for frame in range(max(1, math.ceil(duration * fps)))]


def page_html(lines: Sequence[Mapping[str, Any]]) -> str:
    """The page the browser lays the chat out on: every line, one under
    another, in the order they arrive."""
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


# -- the column ---------------------------------------------------------------


class Emote(NamedTuple):
    """An animated emote in the column, left out of its pictures for every
    frame to have it drawn in moving (see drawn_column): where it is, in
    pixels from the column's top and left, the size it is drawn at, and its
    picture as it was fetched."""

    top: int
    left: int
    width: int
    height: int
    picture: bytes


class Column(NamedTuple):
    """Chat laid out once (see drawn_column): for each line, in the order
    they arrive, the second of the video it arrives at and where it starts
    and ends in the column, in pixels from the column's top. `picture(n)`
    is a PNG of the column from PAGE_PIXELS * n down, PAGE_PIXELS of it -
    or None past its end. `emotes` are the animated emotes left out of
    those pictures, if they were asked to move."""

    arrivals: Sequence[float]
    tops: Sequence[float]
    bottoms: Sequence[float]
    picture: Callable[[int], bytes | None]
    emotes: Sequence[Emote] = ()


@contextmanager
def drawn_column(html: str) -> Iterator[Column]:
    """The page's chat laid out by a browser without a window, which stays
    open for as long as the `with` block runs: the column's pictures are
    taken as the frames come to need them. The emotes that are animated are
    left out of those pictures and handed over apart, to be drawn in
    moving."""
    fetched: dict[str, bytes] = {}

    def serve(route) -> None:
        kind, file = answer(route.request.url, route.request.resource_type)
        if kind == "page":
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
        elif kind == "file":
            route.fulfill(path=str(file))
        elif kind == "fetch":
            # Fetched here and handed on to the browser, to keep the picture:
            # whether an emote is animated is told from that.
            try:
                response = route.fetch()
                picture = response.body()
            except BrowserError:
                route.abort()
                return
            if response.ok:
                fetched[route.request.url] = picture
            route.fulfill(response=response, body=picture)
        elif kind == "missing":
            route.fulfill(status=404, body="")
        else:
            route.abort()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": WIDTH, "height": PAGE_PIXELS})
            page.route("**/*", serve)
            # Not "load": that waits for every picture, and how long those
            # may take is the page's own ready() to decide.
            page.goto(f"{ORIGIN}/", wait_until="domcontentloaded")
            # The page's own script is in the page's world; patchright runs
            # what it is given apart from that unless told otherwise.
            page.evaluate(
                "limit => chatVideo.ready(limit)", PICTURES_TIMEOUT_SECONDS * 1000, isolated_context=False
            )
            emotes = _set_aside(page, fetched)
            laid_out = page.evaluate("page => chatVideo.layOut(page)", PAGE_PIXELS, isolated_context=False)
            pages = int(laid_out["pages"])

            def picture(index: int) -> bytes | None:
                if not 0 <= index < pages:
                    return None
                page.evaluate("y => window.scrollTo(0, y)", index * PAGE_PIXELS, isolated_context=False)
                return page.screenshot(type="png", omit_background=True)

            lines = laid_out["lines"]
            yield Column(
                [line[0] for line in lines],
                [line[1] for line in lines],
                [line[2] for line in lines],
                picture,
                emotes,
            )
        finally:
            browser.close()


def _set_aside(page, fetched: Mapping[str, bytes]) -> list[Emote]:
    """Has the browser leave the emotes whose pictures are animated out of
    the column's pictures - of the pictures it was given, by their
    addresses - and says where each of those emotes is."""
    animated = {url: picture for url, picture in fetched.items() if is_animated(picture)}
    boxes = page.evaluate("urls => chatVideo.setAside(urls)", list(animated), isolated_context=False)
    return [Emote(top, left, width, height, animated[url]) for top, left, width, height, url in boxes]


def frame_top(column: Column, time: float) -> int | None:
    """Where in the column the top of the frame `time` seconds into the
    video is - above the column's own while the lines so far do not fill a
    frame - or None before the first line has arrived. The newest line is
    at the bottom from the frame it arrives in."""
    arrived = bisect.bisect_right(column.arrivals, time)
    if not arrived:
        return None
    return round(column.bottoms[arrived - 1]) + BOTTOM_PIXELS - HEIGHT


class _Pages:
    """The column's pictures, taken as a frame first needs them and let go
    of once the frames have gone past them: the column only ever moves on
    down, and at hours of chat it would not fit in memory whole."""

    def __init__(self, picture: Callable[[int], bytes | None]) -> None:
        self._picture = picture
        self._kept: dict[int, np.ndarray | None] = {}

    def _page(self, index: int) -> np.ndarray | None:
        if index not in self._kept:
            png = self._picture(index)
            self._kept[index] = None if png is None else np.asarray(Image.open(io.BytesIO(png)).convert("RGBA"))
        return self._kept[index]

    def window(self, top: int) -> np.ndarray:
        """The column from `top` down to the bottom of its newest line, as
        a frame whose top is there (see frame_top): HEIGHT rows of WIDTH
        pixels of red, green, blue and how much of it is seen - nothing in
        the room under that line, where the lines still to come are."""
        rows = HEIGHT - BOTTOM_PIXELS
        first = max(top, 0) // PAGE_PIXELS
        for index in [index for index in self._kept if index < first]:
            del self._kept[index]
        frame = np.zeros((HEIGHT, WIDTH, 4), np.uint8)
        for index in range(first, max(top + rows - 1, 0) // PAGE_PIXELS + 1):
            page = self._page(index)
            if page is None:
                break
            start = index * PAGE_PIXELS
            upper, lower = max(top, start), min(top + rows, start + len(page))
            if lower > upper:
                frame[upper - top : lower - top] = page[upper - start : lower - start, :WIDTH]
        return frame


# How much of each pixel row at the top of a frame is kept, out of 255.
_FADE = np.array([[round(255 * row / FADE_PIXELS)] for row in range(FADE_PIXELS)], np.uint16)


def frames(column: Column, times: Sequence[float]) -> Iterator[bytes]:
    """The frames showing the column's chat at `times`, as raw pictures
    WIDTH x HEIGHT of red, green, blue and how much of it is seen - nothing
    where there is no chat. A frame that would be the same as the one
    before it is that one."""
    pages = _Pages(column.picture)
    moving = _Moving(column.emotes)
    blank = bytes(WIDTH * HEIGHT * 4)
    shown: tuple[int, tuple[tuple[int, int], ...]] | None = None
    picture = blank
    for second in times:
        top = frame_top(column, second)
        now = None if top is None else (top, moving.shown(top, second))
        if now != shown:
            shown = now
            picture = blank if now is None else _frame(pages.window(now[0]), moving, *now)
        yield picture


def _frame(frame: np.ndarray, moving: _Moving, top: int, shown: Iterable[tuple[int, int]]) -> bytes:
    moving.draw(frame, top, shown)
    frame[:FADE_PIXELS, :, 3] = frame[:FADE_PIXELS, :, 3] * _FADE // 255
    return frame.tobytes()


# -- animated emotes ------------------------------------------------------------

# A frame of an animated picture that says it lasts this many milliseconds
# or less - as one can, to flash by as fast as it can be shown - is shown
# for BLINK_SHOWN_MILLISECONDS, as browsers show it.
BLINK_MILLISECONDS = 10
BLINK_SHOWN_MILLISECONDS = 100


class Animation(NamedTuple):
    """An animated emote's picture as it is drawn: its frames, at the size
    it is drawn at, and when each of them ends, in milliseconds from the
    start of a run through them. It runs through them over and over from
    the video's first frame on, every copy of it at once - as a browser
    runs the copies of a picture."""

    frames: Sequence[np.ndarray]
    ends: Sequence[int]

    def index(self, second: float) -> int:
        """Which of its frames is shown `second` seconds into the video."""
        return bisect.bisect_right(self.ends, round(second * 1000) % self.ends[-1])


def is_animated(picture: bytes) -> bool:
    """Whether an emote's picture is one that can be drawn moving here: of
    more than one frame, each of which can be read. Any other is left to
    the browser, which draws it standing still."""
    try:
        with Image.open(io.BytesIO(picture)) as image:
            count = getattr(image, "n_frames", 1)
            if count < 2:
                return False
            for index in range(count):
                image.seek(index)
                image.load()
    except Exception:  # whatever an emote's host may hand out
        return False
    return True


def animation(picture: bytes, width: int, height: int) -> Animation:
    """An animated emote's picture - a GIF, a WebP, ... - as it is drawn,
    `width` x `height`."""
    frames: list[np.ndarray] = []
    ends: list[int] = []
    elapsed = 0
    with Image.open(io.BytesIO(picture)) as image:
        for index in range(image.n_frames):
            image.seek(index)
            # A WebP frame says how long it lasts once it has been read.
            image.load()
            lasts = round(image.info.get("duration") or 0)
            elapsed += lasts if lasts > BLINK_MILLISECONDS else BLINK_SHOWN_MILLISECONDS
            ends.append(elapsed)
            # Made smaller with each colour weighed by how much of it is
            # seen, or its edges would go dark.
            drawn = image.convert("RGBA").convert("RGBa").resize((width, height), Image.Resampling.LANCZOS)
            frames.append(np.asarray(drawn.convert("RGBA")))
    return Animation(frames, ends)


class _Moving:
    """A column's animated emotes, for its frames: which of them a frame
    shows and on which frame of their animation, and drawing them into it.
    An emote's picture is taken apart into its frames the first time a
    frame shows it."""

    def __init__(self, emotes: Sequence[Emote]) -> None:
        self._emotes = sorted(emotes, key=lambda emote: emote.top)
        self._tops = [emote.top for emote in self._emotes]
        self._tallest = max((emote.height for emote in self._emotes), default=0)
        self._animations: dict[tuple[bytes, int, int], Animation] = {}

    def _animation(self, emote: Emote) -> Animation:
        key = (emote.picture, emote.width, emote.height)
        if key not in self._animations:
            self._animations[key] = animation(*key)
        return self._animations[key]

    def shown(self, top: int, second: float) -> tuple[tuple[int, int], ...]:
        """The ones the frame `second` seconds into the video shows, its top
        being at `top` in the column: each as where it is in their order,
        with the frame of its animation it is on."""
        first = bisect.bisect_left(self._tops, top - self._tallest + 1)
        last = bisect.bisect_left(self._tops, top + HEIGHT - BOTTOM_PIXELS)
        return tuple(
            (index, self._animation(emote).index(second))
            for index, emote in enumerate(self._emotes[first:last], start=first)
            if emote.top + emote.height > top
        )

    def draw(self, frame: np.ndarray, top: int, shown: Iterable[tuple[int, int]]) -> None:
        """Draws the ones a frame shows (see shown) into it."""
        for index, at in shown:
            emote = self._emotes[index]
            _lay_over(frame, self._animation(emote).frames[at], emote.top - top, emote.left)


def _lay_over(frame: np.ndarray, picture: np.ndarray, top: int, left: int) -> None:
    """Draws a picture over a frame, its top left corner `top` and `left`
    pixels into it - as much of it as is above the room under the newest
    line, where the lines still to come are."""
    height, width = picture.shape[:2]
    upper, lower = max(top, 0), min(top + height, HEIGHT - BOTTOM_PIXELS)
    first, last = max(left, 0), min(left + width, WIDTH)
    if lower <= upper or last <= first:
        return
    over = picture[upper - top : lower - top, first - left : last - left].astype(np.float32) / 255
    under = frame[upper:lower, first:last].astype(np.float32) / 255
    over_seen, under_seen = over[:, :, 3:], under[:, :, 3:]
    seen = over_seen + under_seen * (1 - over_seen)
    colour = (over[:, :, :3] * over_seen + under[:, :, :3] * under_seen * (1 - over_seen)) / np.maximum(seen, 1e-6)
    frame[upper:lower, first:last] = np.rint(np.concatenate((colour, seen), axis=2) * 255).astype(np.uint8)


def _told(pictures: Iterable[bytes], count: int, progress: Callable[[float], None] | None) -> Iterator[bytes]:
    """`pictures`, telling `progress` what share of the `count` of them has
    been handed on, every second of video. It may raise, which stops the
    encoding (see _encode)."""
    if progress is not None:
        progress(0.0)
    for done, picture in enumerate(pictures, start=1):
        yield picture
        if progress is not None and done % FPS == 0:
            progress(min(done / count, 1.0))


# -- the encoding --------------------------------------------------------------


def _raw_frames(fps: int = FPS) -> tuple[str, ...]:
    """ffmpeg's input, as frames() hands them on: from its standard input."""
    return ("-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{WIDTH}x{HEIGHT}", "-framerate", str(fps), "-i", "-")


def _encode(arguments: Sequence[str], frames: Iterable[bytes], output: Path) -> int:
    """Runs ffmpeg with `arguments` into `output`, the frames written to
    its standard input as they come; returns how many it was given. The
    file appears under its name only once it is complete. Whatever `frames`
    raises stops ffmpeg and leaves nothing behind."""
    output.parent.mkdir(parents=True, exist_ok=True)
    unfinished = output.with_name(f"{output.stem}.part{output.suffix}")
    count = 0
    # ffmpeg's complaints go to a file, not a pipe: nothing reads a pipe
    # while frames are being written, and a full one would stall both sides.
    with tempfile.TemporaryFile() as complaints:
        process = subprocess.Popen(
            [FFMPEG_BIN, "-y", "-loglevel", "error", *arguments, str(unfinished)],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=complaints,
        )
        pipe = process.stdin
        assert pipe is not None
        try:
            for frame in frames:
                try:
                    pipe.write(frame)
                except OSError:
                    # ffmpeg has stopped reading: it has all the picture it
                    # needs, or it gave up - which its status says.
                    break
                count += 1
            _close(pipe)
            status = process.wait()
        except BaseException:
            _stop(process, pipe)
            _remove(unfinished)
            raise
        if status != 0 or count == 0:
            unfinished.unlink(missing_ok=True)
            complaints.seek(0)
            said = complaints.read().decode(errors="replace").strip()
            reason = "there were no frames to encode" if count == 0 else said or f"it exited with status {status}"
            raise ChatVideoError(f"ffmpeg could not write {output.name}: {reason}")
    unfinished.replace(output)
    return count


# How long ffmpeg is given to end by itself once its input is closed, and
# how long a file it had open may take to be let go of on Windows.
STOP_SECONDS = 10
RELEASE_SECONDS = 5


def _stop(process: subprocess.Popen, pipe) -> None:
    """Ends an ffmpeg that is no longer wanted. Its input is closed first,
    so that it ends by itself: where `ffmpeg` is a launcher that starts the
    real one (as package managers on Windows install it), killing the
    launcher would leave the real one running, holding its file open."""
    _close(pipe)
    try:
        process.wait(timeout=STOP_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _remove(path: Path) -> None:
    """Removes a file, waiting a little for whatever had it open to let go."""
    deadline = time.monotonic() + RELEASE_SECONDS
    while True:
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.1)


def _close(pipe) -> None:
    try:
        pipe.close()
    except OSError:
        pass  # ffmpeg is gone already


def _colour(background: str) -> str:
    """A colour written "#rrggbb", as ffmpeg's filters take it."""
    return f"0x{background.removeprefix('#')}"


def encode(frames: Iterable[bytes], output: Path, background: str | None = None, fps: int = FPS) -> int:
    """Joins raw frames (see frames()) into a chat video of its own and
    returns how many there were: ProRes 4444, which keeps what is
    see-through in them so - or, with a `background` colour put behind
    them, so that nothing is see-through any more, H.264."""
    # fmt: off
    if background is None:
        codec = (
            "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", "-vendor", "apl0",
            "-qscale:v", str(QUALITY), "-alpha_bits", "8",
        )
    else:
        # The colour is painted over a copy of the frames themselves, not
        # taken from a source of its own: that one never ends, and an ffmpeg
        # given no frames at all (encoding given up before the first) would
        # then wait for one forever.
        behind = f"[0:v]split[text][copy];[copy]drawbox=c={_colour(background)}:t=fill[behind]"
        codec = (
            # Laid over the colour in full colour, before the picture is
            # given the H.264 colour resolution: a name's colour stays clean.
            "-filter_complex", f"{behind};[behind][text]overlay=format=rgb:shortest=1[chat]",
            "-map", "[chat]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(COLOUR_QUALITY), "-pix_fmt", "yuv420p",
        )
    # fmt: on
    return _encode((*_raw_frames(fps), *codec, "-an"), frames, output)


def render(
    lines: Sequence[Mapping[str, Any]],
    duration: float,
    output: Path,
    background: str | None = None,
    progress: Callable[[float], None] | None = None,
) -> int:
    """Renders the chat video of a video `duration` seconds long from its
    chat lines (see chat_trace.chat_replay) and returns its frame count.
    With a `background` colour the video is that colour behind the text, to
    put next to the picture; without one it is see-through, to lay over it.
    `progress` is told how far it has got, as a share, and may raise to
    give up. Animated emotes move in it."""
    html = page_html(lines_for_clip(lines, duration))
    times = frame_times(duration)
    with _one_at_a_time, drawn_column(html) as column:
        return encode(_told(frames(column, times), len(times), progress), output, background)


# -- chat drawn into the picture -----------------------------------------------

# Encoders of H.264 on a graphics card, in the order they are tried, with
# what they are told. On the processor alone, encoding 1080p60 anew takes
# about as long as the video runs and every core there is; a card does it
# in about half that and leaves the processor to the service. ffmpeg's
# builds have them whether or not there is such a card, so each is tried
# on a few frames, and the first that works is used (see video_encoder).
HARDWARE_ENCODERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("h264_amf", ("-quality", "speed", "-rc", "vbr_peak")),
    ("h264_nvenc", ("-preset", "p3", "-rc", "vbr")),
    ("h264_qsv", ("-preset", "veryfast")),
)
SOFTWARE_ENCODER: tuple[str, tuple[str, ...]] = ("libx264", ("-preset", "veryfast"))
# How long trying an encoder may take before it counts as not working.
ENCODER_TRIAL_SECONDS = 30

_encoder: tuple[str, tuple[str, ...]] | None = None
_encoder_lock = threading.Lock()


def encoder_works(name: str) -> bool:
    """Whether ffmpeg can encode H.264 with `name` on this machine."""
    # fmt: off
    command = [
        FFMPEG_BIN, "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=640x360:r=30",
        "-frames:v", "5", "-c:v", name, "-f", "null", "-",
    ]
    # fmt: on
    try:
        result = subprocess.run(command, capture_output=True, timeout=ENCODER_TRIAL_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def video_encoder() -> tuple[str, tuple[str, ...]]:
    """The encoder a burned cut's picture is encoded with, and what it is
    told: the first of HARDWARE_ENCODERS that works here, otherwise x264.
    Found out the first time it is asked for, and kept."""
    global _encoder
    with _encoder_lock:
        if _encoder is None:
            _encoder = next((each for each in HARDWARE_ENCODERS if encoder_works(each[0])), SOFTWARE_ENCODER)
            logger.info("chat drawn into a video is encoded with %s", _encoder[0])
        return _encoder


class Footage(NamedTuple):
    """A video file's picture: its size, and the bitrate it was encoded at
    where that is known."""

    width: int
    height: int
    bitrate: int | None


def footage(path: Path) -> Footage:
    """A video file's picture, as ffprobe tells it."""
    # fmt: off
    result = subprocess.run(
        [
            FFPROBE_BIN, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,bit_rate:format=bit_rate", "-of", "json", str(path),
        ],
        check=True, capture_output=True, text=True,
    )
    # fmt: on
    found = json.loads(result.stdout)
    (stream,) = found["streams"]
    bitrate = stream.get("bit_rate") or found.get("format", {}).get("bit_rate")
    return Footage(int(stream["width"]), int(stream["height"]), int(bitrate) if bitrate else None)


def _even(value: float) -> int:
    return max(2, 2 * round(value / 2))


def placement(width: int, height: int, side: str) -> tuple[int, int, int, int]:
    """Where chat goes in a picture `width` x `height`, on `side`: the size
    of its column, and where the column's top left corner is."""
    scale = min(width, height) / 1080
    across, down, margin = _even(WIDTH * scale), _even(HEIGHT * scale), _even(MARGIN_PIXELS * scale)
    left = width - across - margin if side == "right" else margin
    return across, down, max(left, 0), max(height - down - margin, 0)


def bitrate(picture: Footage) -> int:
    """What a burned cut's picture is encoded at (see BITRATE_OVER)."""
    sent = picture.bitrate or BITRATE_PER_PIXEL * picture.width * picture.height
    return max(LEAST_BITRATE, round(sent * BITRATE_OVER))


def burn(
    lines: Sequence[Mapping[str, Any]],
    source: Path,
    start: float,
    duration: float,
    output: Path,
    side: str,
    background: str | None = None,
    progress: Callable[[float], None] | None = None,
) -> str:
    """Cuts `duration` seconds of `source` out from its second `start`,
    with the chat of `lines` (counted from there, see lines_from) drawn
    into its picture on `side`, into `output`, and returns the encoder the
    picture was encoded with. Unlike a cut that is copied, it begins on the
    very frame at `start`. `progress` is as for render."""
    picture = footage(source)
    across, down, left, top = placement(picture.width, picture.height, side)
    encoder, options = video_encoder()
    rate = bitrate(picture)
    # fmt: off
    arguments = (
        # Asked to start at its second `start`, ffmpeg decodes from the frame
        # a cut could begin on before it and leaves out what comes before.
        "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(source),
        *_raw_frames(),
        "-filter_complex", _graph(across, down, left, top, background), "-map", "[picture]", "-map", "0:a?",
        # Laid over the footage, chat would keep its last picture going on
        # for as long as there are frames of chat after the footage's last.
        "-t", f"{duration:.3f}",
        "-c:v", encoder, *options, "-b:v", str(rate), "-maxrate", str(2 * rate), "-bufsize", str(2 * rate),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", AUDIO_BITRATE,
        "-movflags", "+faststart",
    )
    # fmt: on
    html = page_html(lines_for_clip(lines, duration))
    times = frame_times(duration)
    with _one_at_a_time, drawn_column(html) as column:
        _encode(arguments, _told(frames(column, times), len(times), progress), output)
    return encoder


def _graph(across: int, down: int, left: int, top: int, background: str | None) -> str:
    """ffmpeg's filters for chat drawn into a picture: the frames of chat
    made the size of its column there, the colour behind it if there is
    one, and the one laid over the other. Where the frames of chat run out,
    the last of them stays."""
    steps = []
    chat, base = "[1:v]", "[0:v]"
    if (across, down) != (WIDTH, HEIGHT):
        steps.append(f"{chat}scale={across}:{down}:flags=area[chat]")
        chat = "[chat]"
    if background:
        steps.append(f"{base}drawbox={left}:{top}:{across}:{down}:{_colour(background)}:t=fill[base]")
        base = "[base]"
    steps.append(f"{base}{chat}overlay={left}:{top}:format=auto[picture]")
    return ";".join(steps)
