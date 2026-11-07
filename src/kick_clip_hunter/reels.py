"""A moment's clip made into a 9:16 reel - by GitHub Actions in a repository of
the reel tooling's own (REELS_REPO, reached with REELS_GITHUB_TOKEN), not
here: transcribing a clip and rendering a reel need more than the small
machine the service runs on.

How it goes, a moment at a time, each step started from the Reels page:

1. "Make a reel": the moment's file - or the stretch of it that was chosen,
   cut out of it as the Trim dialog cuts one, nothing re-encoded - goes up
   into a release of that
   repository, tagged `moment-<id>` (`tag`), as `clip.mp4`, and with it
   `context.json` (`context`): the channel, how it is credited, the
   moment's name and note, a comment written for the storyboard on the
   page (what to make of the clip), where in the file the clip the service
   cut begins and ends, and the chat over the whole file - each message at
   the second of the file it belongs to, and the second most of chat
   laughed at. Then the `storyboard` workflow is dispatched. It transcribes the
   clip (`clip.srt`, and `clip.words.json` with the time of every word)
   and has Claude Code write `storyboard.json`: two or three variants of
   the reel, each with what the render script is to be run with, and three
   hooks to choose from - and `contact.jpg`, a sheet of frames from the
   clip, and where it proposes subtitles, `clip.subs.srt`: the transcript
   put right and the speakers told apart ("A: ", "B: ").
2. The service looks at the release every `POLL_SECONDS`; once
   `storyboard.json` is there it is fetched and kept, with the contact
   sheet and the transcript: the subtitles Claude proposed where there are
   some, otherwise what the speech model heard (kept as well, as `heard`).
3. The transcript is put right on the page as needed - a line's words, its
   times, lines added and taken out (`checked_transcript`) - and the reel's
   subtitles are made of it from then on.
4. "Render" with a variant and a hook - one of the storyboard's, or words
   of one's own - and with or without subtitles, and a comment on what to
   change in the variant where one was written: the transcript goes up as
   `clip.subs.srt` and the `render` workflow is dispatched, which has
   Claude rework the variant by the comment, where there is one, and puts
   `reel.mp4` into the same release, with `render.json`: what it was
   rendered with, and what Claude made of the comment (`rendered_note`).
   The service fetches them the same way and keeps the reel next to the
   moment's clip.
5. Feedback on the reel, once it is back, is kept with it and goes up as
   `feedback.json` (`feedback`) - with what the reel was made of and with -
   and the `feedback` workflow is dispatched, which records it in the
   repository, for later storyboards to go by. A new render or storyboard
   is of another reel, so its feedback starts afresh.

Times on the page - the transcript's, the stretch's - are seconds of the
moment's file, whatever was sent: what comes back is moved by where the
stretch begins in it (`moved`), and the subtitles that go up are cut to the
stretch and timed to it (subtitles.srt). So a transcript put right stays
right where another stretch of the same file is sent, and only the lines in
that stretch go up with it.

What was there of an earlier storyboard or reel is taken out of the release
before a step is asked again, so what the service finds there is always the
answer to what it last asked - but not the transcript, while the moment's
file is the one it was heard from (`stamp`): a new storyboard is made from
the same transcript, and one put right by hand goes up with it (and
`context.json` says so), for Claude to go by. A stretch other than the one
sent before is heard anew, as another file would be. A workflow that fails is
noticed by its run (each run is named `run_name`), where the token may read
the repository's Actions; otherwise a step that has not come back within
its limit (`STORYBOARD_LIMIT`, `RENDER_LIMIT`) is given up on.

The repository's own side - the workflows, the skill, the render script -
lives in that repository, not here.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from . import platforms, subtitles

logger = logging.getLogger(__name__)

API_URL = "https://api.github.com"
TIMEOUT_SECONDS = 30.0
# Moving a clip up, or a reel down, is given longer: a moment's file with
# minutes of footage around its clip runs to a hundred megabytes and more.
TRANSFER_TIMEOUT_SECONDS = 600.0
CHUNK_BYTES = 1024 * 1024

POLL_SECONDS = 30
# A storyboard takes a few minutes (most of it transcribing), a render one
# or two; both are given far longer before the service gives up on them.
STORYBOARD_LIMIT = timedelta(minutes=40)
RENDER_LIMIT = timedelta(minutes=30)

# The states of a moment's reel, in the order it goes through them.
SENDING = "sending"
STORYBOARDING = "storyboarding"
CHOOSING = "choosing"
RENDERING = "rendering"
DONE = "done"
FAILED = "failed"
BUSY = frozenset({SENDING, STORYBOARDING, RENDERING})

CLIP = "clip.mp4"
CONTEXT = "context.json"
STORYBOARD = "storyboard.json"
CONTACT = "contact.jpg"
REEL = "reel.mp4"
RENDERED = "render.json"
FEEDBACK = "feedback.json"
HEARD = "clip.srt"
WORDS = "clip.words.json"
SUBTITLES = "clip.subs.srt"
# What a step leaves in the release, taken out before it is asked again -
# the transcript only where the moment's file is not the one it was heard
# from any more (see the module's docstring). Feedback is on the reel that
# was there, so it goes with it.
STORYBOARD_ANSWERS = (STORYBOARD, CONTACT, REEL, RENDERED, FEEDBACK)
TRANSCRIPT_FILES = (HEARD, WORDS, SUBTITLES)
RENDER_ANSWERS = (REEL, RENDERED, FEEDBACK)

# How a render is asked to treat subtitles: as the variant has them, or
# with them or without them whatever it has.
SUBTITLES_AS_THE_VARIANT = "auto"
WITH_SUBTITLES = "on"
WITHOUT_SUBTITLES = "off"
SUBTITLE_CHOICES = (SUBTITLES_AS_THE_VARIANT, WITH_SUBTITLES, WITHOUT_SUBTITLES)

# Hook words of one's own are kept to what fits on two lines of the reel.
HOOK_MOST_CHARACTERS = 80
# A comment written on the page to go with a step - for the storyboard, for
# the render - or feedback on the reel: a few paragraphs at most.
COMMENT_MOST_CHARACTERS = 2000
# A transcript put right on the page: as many lines as hours of talk would
# make, each no longer than a subtitle can hold, and its times may run this
# far past the end of the file (its length is known to the tenth).
TRANSCRIPT_MOST_LINES = 1000
TRANSCRIPT_SLACK_SECONDS = 0.5
# The chat that goes along with the clip: enough for minutes of a busy one.
CONTEXT_CHAT_LIMIT = 1500
# A stretch of the moment's file sent in its place is at least this long; one
# that leaves out less than this of the file at either end is the whole file.
PART_LEAST_SECONDS = 1.0
PART_SLACK_SECONDS = 0.05

_CREDIT = {
    platforms.KICK: "kick.com/{login}",
    platforms.TWITCH: "twitch.tv/{login}",
    platforms.YOUTUBE: "youtube.com/@{login}",
    platforms.TIKTOK: "tiktok.com/@{login}",
}


def tag(moment_id: int) -> str:
    """The release a moment's reel is made in."""
    return f"moment-{moment_id}"


def run_name(workflow: str, release: str) -> str:
    """What a workflow's run for a release is called (its `run-name`)."""
    return f"{workflow} {release}"


def credit(channel: str) -> str:
    """How a reel names the channel it is from, as its platform's address."""
    platform, login = platforms.split(channel)
    return _CREDIT[platform].format(login=login)


# -- what goes up with the clip -------------------------------------------------


@dataclass(frozen=True)
class ChatLine:
    """One chat message, at the second of the moment's file it belongs to."""

    second: float
    sender: str
    text: str
    laughing: bool


def peak_second(lines: Iterable[ChatLine]) -> int | None:
    """The second of the file most of chat laughed in - the earliest of
    them, where several tie; None where nobody laughed."""
    counts: dict[int, int] = {}
    for line in lines:
        if line.laughing:
            second = math.floor(line.second)
            counts[second] = counts.get(second, 0) + 1
    if not counts:
        return None
    most = max(counts.values())
    return min(second for second, count in counts.items() if count == most)


def context(
    *,
    moment_id: int,
    channel: str,
    title: str | None,
    notes: str | None,
    rating: int | None,
    clip_start: float | None,
    clip_end: float | None,
    chat: Iterable[ChatLine],
    transcript_corrected: bool = False,
    comment: str = "",
) -> dict[str, Any]:
    """What goes up with a moment's file, for whoever makes its storyboard:
    see the module's docstring. Times are seconds into the file.
    `transcript_corrected` says that the subtitles that go up with it are
    the transcript put right by hand; `comment` is what was written on the
    page for the storyboard to go by."""
    lines = [line for line in chat if line.second >= 0][:CONTEXT_CHAT_LIMIT]
    platform, login = platforms.split(channel)
    return {
        "moment_id": moment_id,
        "streamer": login,
        "platform": platform,
        "credit": credit(channel),
        "title": title or "",
        "notes": notes or "",
        "rating": rating,
        "clip": None if clip_start is None or clip_end is None else {"start": clip_start, "end": clip_end},
        "peak": peak_second(lines),
        "chat": [{"t": round(line.second, 1), "user": line.sender, "text": line.text} for line in lines],
        "transcript_corrected": transcript_corrected,
        "comment": comment,
    }


def comment_asked(value: object) -> str:
    """A comment as it was written on the page, made fit to go along: its
    rows kept, the space at their ends and around it dropped - "" for none.
    Raises ValueError, in words for the page, for one that is not words, or
    is too long."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("Expected the comment as words.")
    rows = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    text = "\n".join(row.rstrip() for row in rows).strip()
    if len(text) > COMMENT_MOST_CHARACTERS:
        raise ValueError(f"A comment is at most {COMMENT_MOST_CHARACTERS} characters.")
    return text


def feedback(
    *,
    moment_id: int,
    channel: str,
    title: str | None,
    storyboard: Mapping[str, Any] | None,
    storyboard_comment: str | None,
    variant: str | None,
    hook: str | None,
    subtitles_asked: str | None,
    render_comment: str | None,
    render_note: str | None,
    rendered_at: str,
    words: str,
) -> dict[str, Any]:
    """What goes up as feedback on a moment's reel (see the module's
    docstring): the words, and what the reel was made of and with - so it
    can be recorded on its own. `rendered_at` is when the reel was asked
    for, which tells one reel of the moment from another."""
    platform, login = platforms.split(channel)
    offered = (storyboard or {}).get("variants", [])
    chosen = next((one for one in offered if isinstance(one, dict) and str(one.get("id")) == variant), {})
    return {
        "tag": tag(moment_id),
        "moment_id": moment_id,
        "streamer": login,
        "platform": platform,
        "credit": credit(channel),
        "title": title or "",
        "summary": str((storyboard or {}).get("summary") or ""),
        "storyboard_comment": storyboard_comment or "",
        "variant": variant or "",
        "variant_title": str(chosen.get("title") or chosen.get("structure") or ""),
        "hook": hook or "",
        "subtitles": subtitles_asked or SUBTITLES_AS_THE_VARIANT,
        "render_comment": render_comment or "",
        "render_note": render_note or "",
        "rendered_at": rendered_at,
        "feedback": words,
    }


# -- what comes back ------------------------------------------------------------


class BadStoryboard(ValueError):
    """A storyboard.json that is not what the workflow promises."""


def read_storyboard(raw: bytes) -> dict[str, Any]:
    """A storyboard as it came back, checked for what the page and the
    render step rely on. Raises BadStoryboard."""
    try:
        storyboard = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise BadStoryboard(f"The storyboard is not JSON: {error}") from None
    if not isinstance(storyboard, dict):
        raise BadStoryboard("The storyboard is not a JSON object.")
    variants = storyboard.get("variants")
    if not isinstance(variants, list) or not variants:
        raise BadStoryboard("The storyboard has no variants.")
    for variant in variants:
        if not isinstance(variant, dict) or not isinstance(variant.get("id"), str) or not variant["id"]:
            raise BadStoryboard("A variant of the storyboard has no id.")
    hooks = storyboard.get("hooks")
    if not isinstance(hooks, list) or not all(isinstance(hook, dict) and hook.get("text") for hook in hooks):
        raise BadStoryboard("The storyboard's hooks have no words.")
    return storyboard


def rendered_note(raw: bytes) -> str:
    """What the render workflow says Claude made of the comment the reel was
    rendered with (the `note` of its `render.json`): "" where it says
    nothing, or the file is not what the workflow promises - the reel is
    there all the same."""
    try:
        rendered = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return ""
    note = rendered.get("note") if isinstance(rendered, dict) else None
    return note.strip()[:COMMENT_MOST_CHARACTERS] if isinstance(note, str) else ""


def chosen_hook(storyboard: Mapping[str, Any], hook: str) -> str:
    """The words of the hook asked for: a storyboard hook's id stands for
    its words, anything else is the words themselves. Raises ValueError
    for none, or too many."""
    hook = hook.strip()
    for offered in storyboard.get("hooks", []):
        if str(offered.get("id")) == hook:
            return str(offered["text"])
    if not hook:
        raise ValueError("Choose a hook, or write one.")
    if len(hook) > HOOK_MOST_CHARACTERS:
        raise ValueError(f"A hook of your own is at most {HOOK_MOST_CHARACTERS} characters.")
    return hook


def chosen_variant(storyboard: Mapping[str, Any], variant: str) -> str:
    """The id of the variant asked for, as the storyboard has it. Raises
    ValueError for one it does not have."""
    for offered in storyboard.get("variants", []):
        if str(offered.get("id")).upper() == variant.strip().upper():
            return str(offered["id"])
    raise ValueError("The storyboard has no such variant.")


# -- the transcript --------------------------------------------------------------


def stamp(path: Path, part: tuple[float, float] | None = None) -> str | None:
    """Which file a moment's is: its size and when it was last written -
    what changes when footage is added to it - and the stretch of it that
    was sent, where only a stretch was. None for one that is not there."""
    try:
        found = path.stat()
    except OSError:
        return None
    whole = f"{found.st_size}:{found.st_mtime_ns}"
    return whole if part is None else f"{whole}@{part[0]:.3f}-{part[1]:.3f}"


def part_asked(start: object, end: object, length: float | None) -> tuple[float, float] | None:
    """The stretch of a moment's file to send in its place, from `start` to
    `end` in seconds of the file (`length` long, where that is known) - or
    None for the whole file: where neither was given, or the stretch leaves
    out next to nothing of it. Raises ValueError, in words that say why, for
    one that cannot be cut."""
    if start is None and end is None:
        return None
    first, last = _number(start), _number(end)
    if first is None or last is None:
        raise ValueError("Expected the start and end of the stretch, in seconds of the clip's file.")
    if first < 0 or (length is not None and last > length + TRANSCRIPT_SLACK_SECONDS):
        within = f", 0:00.0 to {clock(length)}" if length is not None else ""
        raise ValueError(f"The stretch has to lie within the clip's file{within}.")
    if last - first < PART_LEAST_SECONDS:
        raise ValueError("The stretch has to end at least a second after it starts.")
    if length is not None and first <= PART_SLACK_SECONDS and last >= length - PART_SLACK_SECONDS:
        return None
    return round(first, 3), round(last, 3)


def moved(lines: Iterable[subtitles.Line], seconds: float) -> list[subtitles.Line]:
    """Lines timed to a stretch of a file, timed to the file: `seconds` on."""
    return [subtitles.Line(round(line.start + seconds, 3), round(line.end + seconds, 3), line.text) for line in lines]


def clock(seconds: float) -> str:
    """A time in a file to the tenth of a second, as the page writes it:
    0:07.4, 12:05.0."""
    tenths = round(max(seconds, 0.0) * 10)
    minutes, tenths = divmod(tenths, 600)
    return f"{minutes}:{tenths // 10:02d}.{tenths % 10}"


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


def checked_transcript(posted: object, length: float | None) -> list[subtitles.Line]:
    """A transcript as it was put right on the page - a list of lines, each
    with its `start` and `end` in seconds of the file and its `text` - made
    fit to be kept: each line's words on one row, at most a subtitle's worth
    (subtitles.TEXT_MAX_CHARACTERS), a line left empty left out, the lines in
    the order they are said. `length` is the file's, which no line may run
    past. Raises ValueError, with which line is wrong and how - counted as
    the page has them, from 1."""
    if not isinstance(posted, list):
        raise ValueError("Expected the transcript's lines.")
    if len(posted) > TRANSCRIPT_MOST_LINES:
        raise ValueError(f"A transcript has at most {TRANSCRIPT_MOST_LINES} lines.")
    kept = []
    for number, line in enumerate(posted, start=1):
        if not isinstance(line, dict) or not isinstance(line.get("text"), str):
            raise ValueError(f"Line {number}: expected its start, end and words.")
        text = " ".join(line["text"].split())[: subtitles.TEXT_MAX_CHARACTERS].strip()
        if not text:
            continue
        start, end = _number(line.get("start")), _number(line.get("end"))
        if start is None or end is None:
            raise ValueError(f"Line {number}: write when it starts and ends, like 0:07.4.")
        if start < 0 or end <= start:
            raise ValueError(f"Line {number}: it has to end after it starts.")
        if length is not None and end > length + TRANSCRIPT_SLACK_SECONDS:
            raise ValueError(f"Line {number}: the clip ends at {clock(length)}.")
        kept.append(subtitles.Line(round(start, 3), round(end, 3), text))
    return sorted(kept, key=lambda kept_line: (kept_line.start, kept_line.end))


def subtitled(variant: Mapping[str, Any]) -> bool:
    """Whether a variant of the storyboard has subtitles in it."""
    return "--subs" in (variant.get("render_args") or [])


# -- GitHub ---------------------------------------------------------------------


class GitHubTrouble(RuntimeError):
    """GitHub did not do what was asked, in words for the page."""


@dataclass(frozen=True)
class Asset:
    id: int
    name: str
    size: int


@dataclass(frozen=True)
class Release:
    id: int
    upload_url: str
    assets: tuple[Asset, ...]

    def asset(self, name: str) -> Asset | None:
        return next((asset for asset in self.assets if asset.name == name), None)


def _release(found: Mapping[str, Any]) -> Release:
    return Release(
        id=int(found["id"]),
        upload_url=str(found["upload_url"]).split("{", 1)[0],
        assets=tuple(
            Asset(int(asset["id"]), str(asset["name"]), int(asset.get("size") or 0))
            for asset in found.get("assets", [])
        ),
    )


def _trouble(response: httpx.Response, what: str) -> GitHubTrouble:
    try:
        said = response.json().get("message")
    except ValueError:
        said = None
    return GitHubTrouble(f"GitHub refused to {what} ({response.status_code}{f': {said}' if said else ''}).")


async def _file_chunks(path: Path) -> AsyncIterator[bytes]:
    with path.open("rb") as file:
        while chunk := file.read(CHUNK_BYTES):
            yield chunk


class GitHub:
    """The few calls of GitHub's REST API the reels need, in one repository
    (`owner/name`), as the token's owner."""

    def __init__(self, repo: str, token: str, *, api_url: str = API_URL) -> None:
        self.repo = repo
        self._api = api_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _client(self, timeout: float = TIMEOUT_SECONDS) -> httpx.AsyncClient:
        return httpx.AsyncClient(headers=self._headers, timeout=timeout)

    async def release(self, release: str) -> Release | None:
        """The release tagged so, or None where there is none."""
        try:
            async with self._client() as client:
                response = await client.get(f"{self._api}/repos/{self.repo}/releases/tags/{release}")
        except httpx.HTTPError as error:
            raise GitHubTrouble(f"GitHub could not be reached: {error}") from None
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise _trouble(response, "say what is in the release")
        return _release(response.json())

    async def ensure_release(self, release: str, title: str) -> Release:
        """The release tagged so, made where there is none yet."""
        found = await self.release(release)
        if found is not None:
            return found
        try:
            async with self._client() as client:
                response = await client.post(
                    f"{self._api}/repos/{self.repo}/releases",
                    json={"tag_name": release, "name": title, "body": "Made by kick-clip-hunter."},
                )
        except httpx.HTTPError as error:
            raise GitHubTrouble(f"GitHub could not be reached: {error}") from None
        if response.status_code != 201:
            raise _trouble(response, "make the release")
        return _release(response.json())

    async def remove_assets(self, release: Release, names: Iterable[str]) -> None:
        """Takes the files so named out of the release, where it has them."""
        names = set(names)
        try:
            async with self._client() as client:
                for asset in release.assets:
                    if asset.name not in names:
                        continue
                    response = await client.delete(f"{self._api}/repos/{self.repo}/releases/assets/{asset.id}")
                    if response.status_code not in (204, 404):
                        raise _trouble(response, f"take {asset.name} out of the release")
        except httpx.HTTPError as error:
            raise GitHubTrouble(f"GitHub could not be reached: {error}") from None

    async def upload(self, release: Release, name: str, *, path: Path | None = None, data: bytes | None = None) -> None:
        """Puts a file into the release under `name` - from `path`, sent a
        piece at a time, or `data` - in place of one of that name."""
        await self.remove_assets(release, [name])
        size = path.stat().st_size if path is not None else len(data or b"")
        content_type = "application/json" if name.endswith(".json") else "application/octet-stream"
        body = _file_chunks(path) if path is not None else (data or b"")
        try:
            async with self._client(TRANSFER_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    release.upload_url,
                    params={"name": name},
                    content=body,
                    headers={"Content-Type": content_type, "Content-Length": str(size)},
                )
        except httpx.HTTPError as error:
            raise GitHubTrouble(f"GitHub could not be reached: {error}") from None
        if response.status_code != 201:
            raise _trouble(response, f"take {name}")

    async def download(self, asset: Asset, to: Path) -> None:
        """A file of a release, written to `to` (through a file beside it,
        so a download cut short leaves nothing behind)."""
        part = to.with_name(to.name + ".part")
        to.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with self._client(TRANSFER_TIMEOUT_SECONDS) as client:
                async with client.stream(
                    "GET",
                    f"{self._api}/repos/{self.repo}/releases/assets/{asset.id}",
                    headers={"Accept": "application/octet-stream"},
                    follow_redirects=True,
                ) as response:
                    if response.status_code != 200:
                        await response.aread()
                        raise _trouble(response, f"hand over {asset.name}")
                    with part.open("wb") as file:
                        async for chunk in response.aiter_bytes(CHUNK_BYTES):
                            file.write(chunk)
            part.replace(to)
        except httpx.HTTPError as error:
            part.unlink(missing_ok=True)
            raise GitHubTrouble(f"GitHub could not be reached: {error}") from None
        except BaseException:
            part.unlink(missing_ok=True)
            raise

    async def dispatch(self, event: str, payload: Mapping[str, Any]) -> None:
        """Starts the workflows that listen for `event`."""
        try:
            async with self._client() as client:
                response = await client.post(
                    f"{self._api}/repos/{self.repo}/dispatches",
                    json={"event_type": event, "client_payload": dict(payload)},
                )
        except httpx.HTTPError as error:
            raise GitHubTrouble(f"GitHub could not be reached: {error}") from None
        if response.status_code != 204:
            raise _trouble(response, f"start the {event} workflow")

    async def failed_run(self, name: str, since: datetime) -> str | None:
        """How the latest run so named that began after `since` ended, where
        it ended in anything but success ("failure", "cancelled", ...); None
        while it runs, after it succeeded, or where there is no such run -
        or where the token may not read the repository's Actions."""
        try:
            async with self._client() as client:
                response = await client.get(
                    f"{self._api}/repos/{self.repo}/actions/runs",
                    params={"event": "repository_dispatch", "per_page": 30},
                )
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        for run in response.json().get("workflow_runs", []):
            if run.get("display_title") != name:
                continue
            try:
                began = datetime.fromisoformat(str(run["created_at"]).replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            if began < since - timedelta(minutes=1):
                continue
            conclusion = run.get("conclusion")
            if run.get("status") == "completed" and conclusion not in (None, "success"):
                return str(conclusion)
            return None
        return None


# -- what the page says ---------------------------------------------------------


def hook_shown(hook: str) -> str:
    """A hook as words to read: the render script's line break ("|") as a
    slash, its marks around the highlighted words ("*") left out."""
    return " / ".join(part.strip() for part in hook.replace("*", "").split("|") if part.strip())


def row_words(state: str | None) -> str:
    """Where a reel stands in a word or two, for the Reels page's list."""
    return {
        SENDING: "Sending",
        STORYBOARDING: "Storyboard",
        CHOOSING: "Choose",
        RENDERING: "Rendering",
        DONE: "Ready",
        FAILED: "Failed",
    }.get(state or "", "")


def state_words(state: str | None, error: str | None = None) -> str:
    """What the Reels page says of where a moment's reel stands."""
    return {
        None: "",
        SENDING: "Sending the clip to GitHub.",
        STORYBOARDING: "The storyboard is being made. That takes a few minutes.",
        CHOOSING: "Choose a variant and a hook.",
        RENDERING: "The reel is being rendered. That takes a minute or two.",
        DONE: "The reel is ready.",
        FAILED: f"It did not work: {error}" if error else "It did not work.",
    }.get(state, "")
