"""What is said in a moment's footage, line by line with its times: its
transcript, as subtitles.

A moment's transcript is made when it is asked for on the review page, of
all of its file - the clip and the footage either side of it, since a
stretch cut out of it for an editor (clip_trim.py) can begin and end
anywhere in there. Or of no more than that stretch, asked for in the Trim
dialog: a few seconds are heard in a moment, the whole file takes a while.
A transcript then knows which stretches of the file it was heard from (its
spans); another stretch heard later, or the rest of the file, is added to
it without touching a line already there, which may have been put right
(see merged). The speech model hears words, each with when it was said
(transcriber.transcribe_words); this module makes lines of them the size of
a subtitle, which is what the page shows to be corrected and what an editor
takes away: SubRip (.srt), which every editor reads, timed to the video it
goes with - the moment's whole file, or the cut the Trim dialog made of it.
SubRip is read back too (from_srt): a reel's transcript comes back from
where it is made as a file of it (see reels.py).

Times are clip time, as everywhere on the review page: seconds from the
clip's first frame, negative in the footage before it. A video's own times
start from its first frame.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import NamedTuple

# A line of a subtitle: as much as broadcasters put across the picture in
# one row, and read at a glance.
LINE_MAX_CHARACTERS = 42
# A line is on screen for no longer than this, however slowly it was said.
LINE_MAX_SECONDS = 6.0
# A pause this long between two words starts a new line: what follows is a
# new thought, or someone else talking.
PAUSE_SECONDS = 0.7
# A line stays on screen for at least this long where the next one leaves
# room for it: a word said quickly would be gone before it could be read.
LINE_MIN_SECONDS = 1.0
# A line that has grown too long is broken after a comma rather than before
# the word that did not fit - if what comes before the comma is at least
# this share of a line, so that no word is left on its own.
COMMA_BREAK_SHARE = 0.4
# The most a corrected line can hold.
TEXT_MAX_CHARACTERS = 300
# A line that a video's start or end cuts into is left out of its subtitles
# if less than this of it is left in the video: it would flash up and be
# gone, with words in it that the video does not have.
CUT_LINE_MIN_SECONDS = 0.5
# Two stretches heard this close together are one, and a stretch that is
# heard already to within this is not missing: where a cut begins and ends
# is kept to the hundredth of a second, a file's length is measured anew.
SPAN_SLACK_SECONDS = 0.05
# A subtitles file is written as UTF-8 with a byte order mark, which is how
# editors on Windows tell it from the local code page (without one, some of
# them mangle every Czech letter), and with Windows line ends.
ENCODING = "utf-8-sig"
_NEWLINE = "\r\n"

# What ends a sentence - a full stop, a question or exclamation mark, an
# ellipsis, with a closing quote or bracket after it - and what ends a
# clause: a comma, a semicolon, a colon, a dash.
_SENTENCE_END = re.compile("[.?!\u2026][\"')\u201d]*$")
_CLAUSE_END = re.compile("[,;:\u2013\u2014]$")


class Word(NamedTuple):
    """A word as the speech model heard it: when it began and ended, in
    seconds of the file it was heard in, and what it was - with the space
    the model writes in front of nearly every word."""

    start: float
    end: float
    text: str


class Line(NamedTuple):
    """A line of a transcript: when it is said, in clip time, and what."""

    start: float
    end: float
    text: str


def lines(heard: Iterable[Sequence[Word]], begins: float = 0.0) -> list[Line]:
    """The lines of what the speech model heard, in clip time. `heard` is
    its words in the stretches it heard as one - a line never runs from one
    into the next - and `begins` is the clip time of the first frame of the
    file it heard them in.

    A line runs to the end of a sentence, to a pause, or for as long as one
    fits on the screen and can be read - broken after a comma if there is
    one far enough into it."""
    made: list[list[Word]] = []
    for stretch in heard:
        line: list[Word] = []
        for word in stretch:
            if not word.text.strip():
                continue
            if line and word.start - line[-1].end >= PAUSE_SECONDS:
                made.append(line)
                line = []
            elif line and _too_long([*line, word]):
                kept, line = _broken(line)
                made.append(kept)
                if line and _too_long([*line, word]):
                    made.append(line)
                    line = []
            line.append(word)
            if _SENTENCE_END.search(word.text.strip()):
                made.append(line)
                line = []
        if line:
            made.append(line)
    return _spaced([_line(words, begins) for words in made])


def _text(words: Sequence[Word]) -> str:
    return " ".join("".join(word.text for word in words).split())


def _too_long(words: Sequence[Word]) -> bool:
    return len(_text(words)) > LINE_MAX_CHARACTERS or words[-1].end - words[0].start > LINE_MAX_SECONDS


def _broken(line: list[Word]) -> tuple[list[Word], list[Word]]:
    """A line that has grown too long, as the line it is to be and the
    start of the next one: broken after its last comma if what comes before
    that is long enough, otherwise where it is."""
    for index in range(len(line) - 2, -1, -1):
        before = line[: index + 1]
        if _CLAUSE_END.search(line[index].text.strip()) and len(_text(before)) >= (
            COMMA_BREAK_SHARE * LINE_MAX_CHARACTERS
        ):
            return before, line[index + 1 :]
    return line, []


def _line(words: Sequence[Word], begins: float) -> Line:
    return Line(words[0].start + begins, max(word.end for word in words) + begins, _text(words))


def _spaced(made: list[Line]) -> list[Line]:
    """Lines in the order they are said, none of them running into the next,
    each on screen long enough to be read where the next leaves room."""
    ordered = sorted(made, key=lambda line: line.start)
    spaced = []
    for index, line in enumerate(ordered):
        following = ordered[index + 1].start if index + 1 < len(ordered) else math.inf
        end = min(max(line.end, line.start + LINE_MIN_SECONDS), following)
        spaced.append(Line(round(line.start, 3), round(end, 3), line.text))
    return spaced


def stored(transcript: Sequence[Line]) -> str:
    """A transcript as it is kept on its moment (moments.subtitles)."""
    return json.dumps(
        [{"start": round(line.start, 3), "end": round(line.end, 3), "text": line.text} for line in transcript],
        ensure_ascii=False,
    )


def read(text: str | None) -> list[Line] | None:
    """A moment's transcript as it was kept (see stored) - or None where
    none has been made, or what is kept is not one."""
    if text is None:
        return None
    try:
        kept = json.loads(text)
        if not isinstance(kept, list):
            return None
        return [Line(float(line["start"]), float(line["end"]), str(line["text"])) for line in kept]
    except (ValueError, TypeError, KeyError):
        return None


# A stretch of a moment's file, from and to, in clip time.
Span = tuple[float, float]


def joined(spans: Iterable[Span]) -> list[Span]:
    """Stretches as few as they make together, in order: those that overlap
    or touch (to within SPAN_SLACK_SECONDS) as one."""
    together: list[Span] = []
    for start, end in sorted(spans):
        if together and start <= together[-1][1] + SPAN_SLACK_SECONDS:
            together[-1] = (together[-1][0], max(together[-1][1], end))
        else:
            together.append((start, end))
    return together


def spans_stored(spans: Iterable[Span]) -> str:
    """Which stretches a transcript was heard from, as they are kept on its
    moment (moments.subtitles_heard)."""
    return json.dumps([[round(start, 3), round(end, 3)] for start, end in joined(spans)])


def spans_read(text: str | None) -> list[Span] | None:
    """Which stretches of its moment's file a transcript was heard from (see
    spans_stored) - or None for all of the file: one made of it whole, or
    before a transcript could be made of less, or where what is kept is not
    stretches."""
    if text is None:
        return None
    try:
        kept = json.loads(text)
        if not isinstance(kept, list):
            return None
        return joined((float(start), float(end)) for start, end in kept)
    except (ValueError, TypeError):
        return None


def heard_in(spans: Sequence[Span] | None, start: float, end: float) -> bool:
    """Whether all of the stretch from `start` to `end` has been heard by a
    transcript heard from `spans` (None: all of its file)."""
    if spans is None:
        return True
    slack = SPAN_SLACK_SECONDS
    return any(begins - slack <= start and end <= ends + slack for begins, ends in joined(spans))


def merged(kept: Sequence[Line], spans: Sequence[Span], heard: Sequence[Line]) -> list[Line]:
    """A transcript heard from `spans` with the lines of another hearing
    added: those of `heard` that begin where it was not heard yet. Every
    line already there is kept as it is - put right, perhaps - so the
    footage it was heard in is not heard twice; one that would run into the
    line after it ends where that begins."""
    added = [line for line in heard if not any(start <= line.start < end for start, end in spans)]
    ordered = sorted([*kept, *added], key=lambda line: line.start)
    return [
        line._replace(end=min(line.end, ordered[index + 1].start)) if index + 1 < len(ordered) else line
        for index, line in enumerate(ordered)
    ]


def corrected(transcript: Sequence[Line], index: int, text: str) -> list[Line]:
    """The transcript with its line `index` (counted from 0) saying `text`
    instead - tidied as a subtitle has to be: each row of it trimmed, and no
    blank rows, which would end the subtitle there; cut to
    TEXT_MAX_CHARACTERS. An empty line is kept, and left out of subtitles.
    Raises IndexError for a line that is not there."""
    if not 0 <= index < len(transcript):
        raise IndexError(f"there is no line {index}")
    rows = (" ".join(row.split()) for row in text.splitlines())
    tidy = "\n".join(row for row in rows if row)[:TEXT_MAX_CHARACTERS].strip()
    return [*transcript[:index], transcript[index]._replace(text=tidy), *transcript[index + 1 :]]


def said(transcript: Iterable[Line]) -> bool:
    """Whether there is anything in a transcript to put in subtitles."""
    return any(line.text.strip() for line in transcript)


def srt(transcript: Iterable[Line], start: float, length: float | None = None) -> str:
    """The subtitles of a video whose first frame is at clip time `start`
    and which runs for `length` seconds (or on to the transcript's end), as
    SubRip, to be written in ENCODING: every line with something to say, on
    the video's own time and numbered from 1. A line that the video's start
    or end cuts into is cut to it, and left out if too little of it is left
    (CUT_LINE_MIN_SECONDS)."""
    last = math.inf if length is None else length
    cues = []
    for line in transcript:
        if not line.text.strip():
            continue
        whole = (line.start - start, line.end - start)
        shown = (max(whole[0], 0.0), min(whole[1], last))
        if shown[1] <= shown[0] or (shown != whole and shown[1] - shown[0] < CUT_LINE_MIN_SECONDS):
            continue
        text = _NEWLINE.join(line.text.splitlines())
        cues.append(f"{len(cues) + 1}{_NEWLINE}{_timestamp(shown[0])} --> {_timestamp(shown[1])}{_NEWLINE}{text}")
    return "".join(f"{cue}{_NEWLINE}{_NEWLINE}" for cue in cues)


def _timestamp(seconds: float) -> str:
    """A time as SubRip writes it: 00:01:02,345."""
    hours, rest = divmod(round(seconds * 1000), 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    whole, thousandths = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole:02d},{thousandths:03d}"


_CUE_TIMES = re.compile(r"(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})\s*-->\s*(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})")


def from_srt(text: str) -> list[Line]:
    """The lines of a SubRip file, on the video's own time, in the order
    they are in it: a cue's rows are one line, joined by a space. A block
    without times is skipped, and so is one with nothing to say."""
    found = []
    for block in re.split(r"\n\s*\n", text.lstrip("﻿").replace("\r", "").strip()):
        rows = [row.strip() for row in block.split("\n") if row.strip()]
        at = next((index for index, row in enumerate(rows) if _CUE_TIMES.search(row)), None)
        if at is None:
            continue
        times = _CUE_TIMES.search(rows[at]).groups()
        said = " ".join(rows[at + 1 :])
        if said:
            found.append(Line(_seconds(*times[:4]), _seconds(*times[4:]), said))
    return found


def _seconds(hours: str, minutes: str, seconds: str, fraction: str) -> float:
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(fraction.ljust(3, "0")) / 1000


def name(video_name: str) -> str:
    """The file name of a video's subtitles: the video's own, ending in
    .srt, so the two are seen to go together."""
    return f"{Path(video_name).stem}.srt"
