"""A moment's clip made into a 9:16 reel - by GitHub Actions in a repository of
the reel tooling's own (REELS_REPO, reached with REELS_GITHUB_TOKEN), not
here: transcribing a clip and rendering a reel need more than the small
machine the service runs on.

How it goes, a moment at a time, each step started from the review page:

1. "Make a reel": the moment's file goes up into a release of that
   repository, tagged `moment-<id>` (`tag`), as `clip.mp4`, and with it
   `context.json` (`context`): the channel, how it is credited, the
   moment's name and note, where in the file the clip the service cut
   begins and ends, and the chat over the whole file - each message at the
   second of the file it belongs to, and the second most of chat laughed
   at. Then the `storyboard` workflow is dispatched. It transcribes the
   clip and has Claude Code write `storyboard.json`: two or three variants
   of the reel, each with what the render script is to be run with, and
   three hooks to choose from - and `contact.jpg`, a sheet of frames from
   the clip.
2. The service looks at the release every `POLL_SECONDS` (`Reels.look`);
   once `storyboard.json` is there it is fetched and kept, with the contact
   sheet, and the page shows them.
3. "Render" with a variant and a hook - one of the storyboard's, or words
   of one's own - dispatches the `render` workflow, which puts `reel.mp4`
   into the same release; the service fetches it the same way and keeps it
   next to the moment's clip.

What was there of an earlier storyboard or reel is taken out of the release
before a step is asked again, so what the service finds there is always the
answer to what it last asked. A workflow that fails is noticed by its run
(each run is named `run_name`), where the token may read the repository's
Actions; otherwise a step that has not come back within its limit
(`STORYBOARD_LIMIT`, `RENDER_LIMIT`) is given up on.

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

from . import platforms

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
# What a step leaves in the release, taken out before it is asked again.
STORYBOARD_ANSWERS = (STORYBOARD, CONTACT, "clip.srt", "clip.words.json", "clip.subs.srt", REEL)
RENDER_ANSWERS = (REEL,)

# Hook words of one's own are kept to what fits on two lines of the reel.
HOOK_MOST_CHARACTERS = 80
# The chat that goes along with the clip: enough for minutes of a busy one.
CONTEXT_CHAT_LIMIT = 1500

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
) -> dict[str, Any]:
    """What goes up with a moment's file, for whoever makes its storyboard:
    see the module's docstring. Times are seconds into the file."""
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


def state_words(state: str | None, error: str | None = None) -> str:
    """What the review page says of where a moment's reel stands."""
    return {
        None: "",
        SENDING: "Sending the clip to GitHub.",
        STORYBOARDING: "The storyboard is being made. That takes a few minutes.",
        CHOOSING: "Choose a variant and a hook.",
        RENDERING: "The reel is being rendered. That takes a minute or two.",
        DONE: "The reel is ready.",
        FAILED: f"It did not work: {error}" if error else "It did not work.",
    }.get(state, "")
