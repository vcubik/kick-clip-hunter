"""Notifications: what the service tells whoever runs it, wherever they are.

The dashboard says everything, but only to someone looking at it, on the
machine the service runs on or at most on the tailnet. A moment fires at
three in the morning, a recording ends because the disk is full, Kick drops
its chat subscriptions: nobody knows until a page is opened. So the service
can send a short note of these to Telegram, to a Discord channel, or to
both (`Telegram`, `Discord` - each a `Destination`):

- a clip was saved: whose, how long, how far into the stream, what chat
  said most - and, where that is switched on and it fits, the clip itself,
  without the footage around it (`clip_note`);
- a recording of a whole stream started, was saved - why it ended, how long
  it runs, how big it is - could not be saved, or ended before it had any
  footage (`recording_*_note`);
- a part of the Health page needs a look, and when it looks fine again
  (`HealthWatch`, which keeps a part that only flickers from being told of);
- once a day, what the last day held: clips by channel, the ones still to
  be rated, recordings saved, chat taken in, how the Health page and the
  disk stand (`daily_note`, sent when `summary_due` says).

Nothing is sent until a destination is set up - on the dashboard's Settings
page, or in .env (see config.py) for whatever the page never set (`Setup`,
`in_force`): a Telegram bot, made with @BotFather, and the chat it writes in
- which the page, or scripts/telegram_chats.py, finds among the chats that
wrote to the bot - or a Discord channel's webhook. Each kind of note can be
switched off on the same page. A note links to the dashboard where the setup
says where that is reached from (from a phone: the machine's name on the
tailnet).

Telegram is asked through its Bot API, as the bot (sendMessage, and
sendVideo for a clip - a bot sends files of up to 50 MB, or of up to 2000
MB through a Bot API server of one's own that TELEGRAM_API_URL names);
Discord through
the channel's webhook, as an embed (with a file of up to 20 MB on a server
without boosts - 10 MB until August 2026). A clip too big for a destination
goes there as words alone, which say so. Both answer a note sent too soon
after the last with 429 and how long to wait, which is waited once, up to
RETRY_AFTER_MAX_SECONDS; a destination that could not be reached at all is
tried once more after RETRY_SECONDS.

Sending holds nothing up and never fails loudly, like the bot in chat
(chat_bot.py): a note goes out in the background, the notes for one
destination in the order they were made, and one that can't be sent is
logged and kept for the Health page (`Notifier.sent`) until the next one
goes out. A Telegram bot's token is in every address it is asked at, so no
address is ever put into words, and the chatters' words in a note are
escaped for each destination - and mention nobody on Discord.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

import httpx

from . import stream_recording
from .chat_bot import length_words
from .dashboard_view import count_words, list_words, sentence_case, size_words, stream_time_words

logger = logging.getLogger("kick_clip_hunter")

TELEGRAM_API = "https://api.telegram.org"
# What Telegram takes: a message of 4096 characters, a caption under a
# video of 1024, a video a bot uploads of 50 MB - or of 2000 MB through a
# Bot API server of one's own (telegram-bot-api in its --local mode), which
# TELEGRAM_API_URL in .env points the service at instead of Telegram's.
TELEGRAM_TEXT_MAX = 4096
TELEGRAM_CAPTION_MAX = 1024
TELEGRAM_VIDEO_MAX_BYTES = 50 * 1024**2
TELEGRAM_LOCAL_VIDEO_MAX_BYTES = 2000 * 1024**2
# What a Discord webhook takes: an embed's title of 256 characters, its
# description of 4096, and a file of 20 MB on a server without boosts.
DISCORD_TITLE_MAX = 256
DISCORD_DESCRIPTION_MAX = 4096
DISCORD_VIDEO_MAX_BYTES = 20 * 1024**2
# The addresses a Discord webhook is at.
DISCORD_WEBHOOK = re.compile(r"https://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/api/(?:v\d+/)?webhooks/\d+/[\w-]+")
# The name a note is sent under on Discord.
DISCORD_NAME = "kick-clip-hunter"

# A line of a note is cut short past this - every one is far shorter, but
# what chat said most is chat's own.
LINE_MAX = 300
# How long a note may take to go out, and one with a clip.
SEND_TIMEOUT_SECONDS = 30
VIDEO_TIMEOUT_SECONDS = 300
# The longest wait for a destination that said a note came too soon, and
# how long one that could not be reached is given before it is tried again.
RETRY_AFTER_MAX_SECONDS = 60
RETRY_SECONDS = 30

# What a note is about.
CLIP = "clip"
RECORDING = "recording"
HEALTH = "health"
DAILY = "daily"
TEST = "test"

# The colour of a Discord embed, by what it tells of: the dashboard's
# marker for a moment, its lamp for a recording, the red pen for trouble,
# the grid's green for trouble that is over, its paper for the day's summary.
COLOURS = {CLIP: 0xFFEB57, RECORDING: 0xFF4A3D, HEALTH: 0xA6CCB7, DAILY: 0xF4F1E8, TEST: 0xEDEDED}
TROUBLE_COLOUR = 0xCF2E2E


@dataclass(frozen=True)
class Note:
    """One notification: what it is about (`kind`), its title and the few
    lines under it, the channel it is of, where on the dashboard it leads -
    and whether what it tells of is trouble."""

    kind: str
    title: str
    lines: tuple[str, ...] = ()
    channel: str | None = None
    link: str | None = None
    trouble: bool = False

    def plus(self, line: str) -> Note:
        """The same note with one line more."""
        return replace(self, lines=(*self.lines, line))


# -- what it says -------------------------------------------------------------


def dashboard_link(dashboard_url: str, path: str) -> str | None:
    """Where `path` on the dashboard is reached from, by DASHBOARD_URL: None
    without one."""
    base = dashboard_url.strip().rstrip("/")
    return f"{base}{path}" if base else None


def clip_note(
    channel: str,
    seconds: float,
    stream_seconds: int | None,
    *,
    said: str | None = None,
    reasons: Sequence[str] = (),
    commanded: bool = False,
    link: str | None = None,
) -> Note:
    """A clip's file was saved: how long the clip is, how far into the
    stream chat reacted, and what it reacted with - what chat said most
    (`said`, as chat_trace.reaction_words has it), or what set the detector off (`reasons`, in
    words), or that it was asked for in chat."""
    into = stream_time_words(stream_seconds)
    lines = [f"{sentence_case(length_words(seconds))}{f', {into} into the stream' if into else ''}."]
    if commanded:
        lines.append("Asked for in chat.")
    elif said:
        lines.append(f"Chat: {said}")
    elif reasons:
        lines.append(f"Set off by {list_words(list(reasons))}.")
    return Note(CLIP, "Clip saved", tuple(lines), channel=channel, link=link)


# Who started a recording.
DASHBOARD = "dashboard"
CHAT = "chat"
ALWAYS = "always"


def recording_started_note(
    channel: str, started_by: str, *, live: bool | None = None, asked_by: str | None = None, link: str | None = None
) -> Note:
    """A recording of a channel's stream was started: from the dashboard,
    asked for in its chat (by `asked_by`), or by itself, the channel being
    recorded always. `live` is whether it was last seen live; one that was
    not is recorded from when its stream begins."""
    lines = ["Recorded from when the stream starts." if live is False else "Recording the stream from now on."]
    if started_by == CHAT:
        lines.append(f"Asked for in chat by {asked_by}." if asked_by else "Asked for in chat.")
    elif started_by == ALWAYS:
        lines.append("Started by itself: the channel is recorded always.")
    else:
        lines.append("Started from the dashboard.")
    return Note(RECORDING, "Recording started", tuple(lines), channel=channel, link=link)


# Why a recording ended, as a note says it (the keys are stream_recording.py's).
ENDED_WORDS = {
    stream_recording.STOPPED: "It was stopped.",
    stream_recording.STREAM_ENDED: "The stream is over.",
    stream_recording.DISK_FULL: "It was stopped: the disk is nearly full.",
    stream_recording.WRITE_FAILED: "It was stopped: its footage could not be written.",
    stream_recording.SHUTDOWN: "It was stopped: the service was shutting down.",
    stream_recording.INTERRUPTED: "It was cut short when the service stopped.",
}
# The reasons nobody asked for, which a note tells of as trouble.
UNASKED = (stream_recording.DISK_FULL, stream_recording.WRITE_FAILED)


def _ended(ended_by: str | None) -> str:
    return ENDED_WORDS.get(ended_by or "", ENDED_WORDS[stream_recording.STOPPED])


def recording_saved_note(
    channel: str, ended_by: str | None, seconds: float, size: int, parts: int = 1, *, link: str | None = None
) -> Note:
    """A recording that is over was made into its file: why it ended, how
    much of the stream it holds and how big it is - and how often the
    stream broke off in it, where it did."""
    held = f"{sentence_case(length_words(seconds))} of the stream, {size_words(size)}"
    if parts > 1:
        held += f", with {count_words(parts - 1, 'break')} in it"
    return Note(
        RECORDING,
        "Recording saved",
        (_ended(ended_by), f"{held}."),
        channel=channel,
        link=link,
        trouble=ended_by in UNASKED,
    )


def recording_failed_note(channel: str, *, link: str | None = None) -> Note:
    """A recording that is over could not be made into its file."""
    return Note(
        RECORDING,
        "Recording not saved",
        ("Its file could not be made. What it kept is still there, and the Recordings page can try again.",),
        channel=channel,
        link=link,
        trouble=True,
    )


def recording_empty_note(channel: str, ended_by: str) -> Note:
    """A recording ended before it had any footage: there is nothing of it."""
    return Note(
        RECORDING,
        "Recording ended",
        (f"{_ended(ended_by)} Nothing had been recorded yet.",),
        channel=channel,
        trouble=ended_by in UNASKED,
    )


def health_note(words: str, plural: bool, said: str, trouble: bool, *, link: str | None = None) -> Note:
    """A part of the Health page needs a look (`trouble`), or looks fine
    again: what it is called in a sentence (`words`, "the tunnel"), whether
    that is more than one thing, and what the page says of it now."""
    if trouble:
        title = f"{sentence_case(words)} {'need' if plural else 'needs'} a look"
    else:
        title = f"{sentence_case(words)} {'look' if plural else 'looks'} fine again"
    return Note(HEALTH, title, (said,) if said else (), link=link, trouble=trouble)


@dataclass(frozen=True)
class Day:
    """What a day held, for its summary: clips made, by channel; how many
    clips wait to be rated, of all there are; the recordings saved - how
    many, how much of the streams, how big; chat messages taken in; what the
    Health page says on top, and whether that is trouble; and the room left
    on the disk recordings are kept on."""

    clips: Mapping[str, int] = field(default_factory=dict)
    unrated: int = 0
    recordings: int = 0
    recorded_seconds: float = 0.0
    recorded_bytes: int = 0
    chat_messages: int = 0
    health: str = ""
    health_trouble: bool = False
    free_bytes: int | None = None


def daily_note(day: Day, *, link: str | None = None) -> Note:
    """The summary of the last day: clips first, busiest channel first."""
    made = sum(day.clips.values())
    if made:
        busiest = sorted(day.clips.items(), key=lambda item: (-item[1], item[0].lower()))
        lines = [f"{sentence_case(count_words(made, 'clip'))}: {', '.join(f'{name} {n}' for name, n in busiest)}."]
    else:
        lines = ["No clips."]
    if day.unrated:
        lines.append(
            f"{sentence_case(count_words(day.unrated, 'clip'))} {'waits' if day.unrated == 1 else 'wait'} to be rated."
        )
    if day.recordings:
        lines.append(
            f"{sentence_case(count_words(day.recordings, 'recording'))} saved: "
            f"{length_words(day.recorded_seconds)} of streams, {size_words(day.recorded_bytes)}."
        )
    lines.append(f"{sentence_case(count_words(day.chat_messages, 'chat message'))} taken in.")
    if day.health:
        lines.append(day.health)
    if day.free_bytes is not None:
        lines.append(f"{size_words(day.free_bytes)} free on the disk recordings are kept on.")
    return Note(DAILY, "The last 24 hours", tuple(lines), link=link, trouble=day.health_trouble)


def summary_due(now: datetime, last_sent: datetime | None, hour: int) -> datetime | None:
    """When the day a summary is due for ended - the last `hour` o'clock
    (local time, as `now` is) - if none was sent for it yet (`last_sent` is
    when the one sent last ended); None while the last one is the latest."""
    end = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if end > now:
        end -= timedelta(days=1)
    if last_sent is not None and last_sent >= end:
        return None
    return end


def test_note(*, link: str | None = None) -> Note:
    """What the Health page's button sends, to see that notes arrive."""
    return Note(TEST, "Test", ("Notifications from kick-clip-hunter arrive here.",), link=link)


def too_big_words(size: int | None, destination: str, most: int | None = None) -> str:
    """Why a note came without its clip: it is bigger than the most the
    destination takes - or than it took, where it turned the clip down
    under that."""
    if size is None:
        return "The clip could not be read to be sent."
    took = f" ({size_words(most)})" if most is not None else ""
    return f"The clip ({size_words(size)}) is more than {destination} takes{took}."


# -- how it is sent -----------------------------------------------------------


class CannotSend(Exception):
    """Why a note could not be sent, in words for the Health page. `again`
    where the destination could not be reached at all, which may be over
    in a moment."""

    def __init__(self, words: str, *, again: bool = False) -> None:
        super().__init__(words)
        self.words = words
        self.again = again


class TooBig(CannotSend):
    """The destination turned the note down for the size of its file."""


class Destination(Protocol):
    """Where notes go: what it is called, the largest file it takes, and
    in words where in it they go."""

    name: str
    video_max_bytes: int

    def where(self) -> str: ...

    async def send(self, note: Note, video: Path | None = None) -> None:
        """Sends the note - with `video` where one is given - or raises
        CannotSend."""


def _line(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= LINE_MAX else f"{text[: LINE_MAX - 1]}…"


def _answer(response: httpx.Response) -> dict[str, Any]:
    try:
        answer = response.json()
    except ValueError:
        return {}
    return answer if isinstance(answer, dict) else {}


def _seconds(value: object) -> float | None:
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return max(0.0, seconds)


def _unreached(destination: str, error: Exception) -> CannotSend:
    # Never the error's own words: they can hold the address, and a
    # Telegram bot's token is in that.
    return CannotSend(f"{destination} could not be reached ({type(error).__name__}).", again=True)


async def _post(url: str, timeout: float, **request: Any) -> httpx.Response:
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.post(url, **request)


def telegram_api(api_url: str) -> str:
    """Where the Bot API is asked: TELEGRAM_API_URL without a slash at its
    end, or Telegram's own where that is empty."""
    return api_url.strip().rstrip("/") or TELEGRAM_API


def telegram_text(note: Note) -> str:
    """A note as Telegram's HTML: the channel and the title in bold, the
    lines under them, and the link."""
    title = html.escape(note.title, quote=False)
    head = f"<b>{html.escape(note.channel, quote=False)}</b> · {title}" if note.channel else f"<b>{title}</b>"
    body = [html.escape(_line(line), quote=False) for line in note.lines]
    if note.link:
        body.append(f'<a href="{html.escape(note.link)}">Open on the dashboard</a>')
    return "\n".join([head, *body])


class Telegram:
    """Sends notes to a Telegram chat, as a bot."""

    name = "Telegram"

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        api_url: str = TELEGRAM_API,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        self._token = token
        self.chat_id = chat_id
        # Telegram's own Bot API, or a server of one's own, which takes
        # larger files.
        self.api_url = telegram_api(api_url)
        self.video_max_bytes = (
            TELEGRAM_VIDEO_MAX_BYTES if self.api_url == TELEGRAM_API else TELEGRAM_LOCAL_VIDEO_MAX_BYTES
        )
        self._sleep = sleep

    def where(self) -> str:
        through = f" through {self.api_url}" if self.api_url != TELEGRAM_API else ""
        return f"chat {self.chat_id}{through}"

    async def send(self, note: Note, video: Path | None = None) -> None:
        text = telegram_text(note)
        if video is None:
            await self._ask(
                "sendMessage",
                SEND_TIMEOUT_SECONDS,
                json={
                    "chat_id": self.chat_id,
                    "text": text[:TELEGRAM_TEXT_MAX],
                    "parse_mode": "HTML",
                    "link_preview_options": {"is_disabled": True},
                },
            )
            return
        content = await asyncio.to_thread(video.read_bytes)
        await self._ask(
            "sendVideo",
            VIDEO_TIMEOUT_SECONDS,
            data={
                "chat_id": self.chat_id,
                "caption": text[:TELEGRAM_CAPTION_MAX],
                "parse_mode": "HTML",
                "supports_streaming": "true",
            },
            files={"video": (video.name, content, "video/mp4")},
        )

    async def _ask(self, method: str, timeout: float, **request: Any) -> Any:
        """What the Bot API answers `method` with, asked once more after the
        wait it asks for where it says the note came too soon."""
        for first in (True, False):
            try:
                response = await _post(f"{self.api_url}/bot{self._token}/{method}", timeout, **request)
            except (httpx.HTTPError, httpx.InvalidURL) as error:
                raise _unreached(self.name, error) from None
            answer = _answer(response)
            if answer.get("ok") is True:
                return answer.get("result")
            parameters = answer.get("parameters")
            wait = _seconds(parameters.get("retry_after")) if isinstance(parameters, dict) else None
            if response.status_code == 429 and first and wait is not None:
                await self._sleep(min(wait, RETRY_AFTER_MAX_SECONDS))
                continue
            break
        said = answer.get("description") or response.reason_phrase
        words = f"Telegram did not take it ({response.status_code} {said})."
        if response.status_code == 413:
            raise TooBig(words)
        if response.status_code == 401:
            words += " TELEGRAM_BOT_TOKEN in .env is not a bot's token."
        elif response.status_code == 403 or (response.status_code == 400 and "chat" in str(said).lower()):
            words += " TELEGRAM_CHAT_ID in .env has to be a chat that has written to the bot, or a group it is in."
        raise CannotSend(words)


def discord_escape(text: str) -> str:
    """Text as Discord shows it as it is: its markdown escaped."""
    text = re.sub(r"([\\*_~`|\[\]<])", r"\\\1", text)
    return re.sub(r"^([>#-])", r"\\\1", text)


def discord_payload(note: Note) -> dict[str, Any]:
    """A note as a Discord webhook takes it: one embed - its author the
    channel, its title the link - and nobody mentioned, whatever chat
    said."""
    embed: dict[str, Any] = {
        "title": note.title[:DISCORD_TITLE_MAX],
        "description": "\n".join(discord_escape(_line(line)) for line in note.lines)[:DISCORD_DESCRIPTION_MAX],
        "color": TROUBLE_COLOUR if note.trouble else COLOURS.get(note.kind, COLOURS[TEST]),
    }
    if note.channel:
        embed["author"] = {"name": note.channel[:DISCORD_TITLE_MAX]}
    if note.link:
        embed["url"] = note.link
    return {"username": DISCORD_NAME, "embeds": [embed], "allowed_mentions": {"parse": []}}


class Discord:
    """Sends notes to a Discord channel, through its webhook."""

    name = "Discord"
    video_max_bytes = DISCORD_VIDEO_MAX_BYTES

    def __init__(self, webhook_url: str, *, sleep: Callable[[float], Awaitable[object]] = asyncio.sleep) -> None:
        self._url = webhook_url
        self._sleep = sleep

    def where(self) -> str:
        return "the webhook's channel"

    async def send(self, note: Note, video: Path | None = None) -> None:
        payload = discord_payload(note)
        if video is None:
            await self._ask(SEND_TIMEOUT_SECONDS, json=payload)
            return
        content = await asyncio.to_thread(video.read_bytes)
        await self._ask(
            VIDEO_TIMEOUT_SECONDS,
            data={"payload_json": json.dumps(payload)},
            files={"files[0]": (video.name, content, "video/mp4")},
        )

    async def _ask(self, timeout: float, **request: Any) -> None:
        for first in (True, False):
            try:
                response = await _post(self._url, timeout, params={"wait": "true"}, **request)
            except (httpx.HTTPError, httpx.InvalidURL) as error:
                raise _unreached(self.name, error) from None
            if response.is_success:
                return
            answer = _answer(response)
            wait = _seconds(answer.get("retry_after", response.headers.get("retry-after")))
            if response.status_code == 429 and first and wait is not None:
                await self._sleep(min(wait, RETRY_AFTER_MAX_SECONDS))
                continue
            break
        said = answer.get("message") or response.reason_phrase
        words = f"Discord did not take it ({response.status_code} {said})."
        if response.status_code == 413:
            raise TooBig(words)
        if response.status_code in (401, 404):
            words += " The webhook notifications go to has to be one the channel still has."
        raise CannotSend(words)


def destinations(
    telegram_token: str = "",
    telegram_chat_id: str = "",
    discord_webhook_url: str = "",
    *,
    telegram_api_url: str = "",
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> tuple[Destination, ...]:
    """Where notes go, by the setup in force (see `in_force`): Telegram where
    it names a bot and a chat (through the Bot API server it names, if it
    does), Discord where it names a webhook."""
    found: list[Destination] = []
    if telegram_token and telegram_chat_id:
        found.append(Telegram(telegram_token, telegram_chat_id, api_url=telegram_api_url, sleep=sleep))
    if discord_webhook_url:
        found.append(Discord(discord_webhook_url, sleep=sleep))
    return tuple(found)


def setup_trouble(
    telegram_token: str, telegram_chat_id: str, discord_webhook_url: str, telegram_api_url: str = ""
) -> list[str]:
    """What is wrong with how .env sets notifications up, in words for the
    log: half of Telegram's, a Bot API server that is no web address, or an
    address that is not a Discord webhook."""
    trouble = []
    if telegram_api_url.strip() and not re.match(r"https?://[^/\s]+", telegram_api_url.strip()):
        trouble.append("TELEGRAM_API_URL is not an http:// or https:// address")
    if telegram_token and not telegram_chat_id:
        trouble.append("TELEGRAM_BOT_TOKEN is set but TELEGRAM_CHAT_ID is not - scripts/telegram_chats.py finds it")
    if telegram_chat_id and not telegram_token:
        trouble.append("TELEGRAM_CHAT_ID is set but TELEGRAM_BOT_TOKEN is not")
    if discord_webhook_url and not DISCORD_WEBHOOK.fullmatch(discord_webhook_url.split("?")[0]):
        trouble.append("DISCORD_WEBHOOK_URL does not look like a Discord webhook's address")
    return trouble


# -- where notes go, as set on the dashboard ----------------------------------

# What can be set of where notes go, in the order the Settings page has it,
# with what the page calls each. .env has the same, by these names in
# capitals, for whatever the page never set. The bot's token and the
# webhook are secrets - anyone who has either can send in your name - so the
# page never shows them, only whether one is set.
SETUP_LABELS = {
    "telegram_bot_token": "Bot token",
    "telegram_chat_id": "Chat",
    "telegram_api_url": "Bot API server",
    "discord_webhook_url": "Webhook",
    "dashboard_url": "Dashboard address",
}
SECRETS = frozenset({"telegram_bot_token", "discord_webhook_url"})

# What a bot's token looks like (its id, a colon, its secret), a chat a bot
# writes in (its id, or a public channel's @name) and a web address.
TELEGRAM_TOKEN_SHAPE = re.compile(r"\d+:[\w-]+")
TELEGRAM_CHAT_SHAPE = re.compile(r"-?\d+|@\w{5,}")
WEB_ADDRESS = re.compile(r"https?://[^/\s?#]+[^\s]*")


@dataclass(frozen=True)
class Setup:
    """Where notes go: a Telegram bot and the chat it writes in (through a
    Bot API server of one's own where one is named), a Discord channel's
    webhook, and where the dashboard is reached from, for their links. An
    empty value is none."""

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_api_url: str = ""
    discord_webhook_url: str = ""
    dashboard_url: str = ""


def in_force(env: Setup, saved: Mapping[str, str]) -> Setup:
    """Where notes go: what was saved on the dashboard - an empty value as
    much as any other, which says there is none - and for the rest what .env
    says."""
    return replace(env, **{name: value for name, value in saved.items() if name in SETUP_LABELS})


def check_setup(setup: Setup) -> None:
    """Raises a ValueError, in words for the Settings page, where notes
    could not go where the setup says: half of Telegram's, or a value of the
    wrong shape."""

    def wrong(name: str, what: str) -> ValueError:
        return ValueError(f'"{SETUP_LABELS[name]}" {what}.')

    if setup.telegram_bot_token and not TELEGRAM_TOKEN_SHAPE.fullmatch(setup.telegram_bot_token):
        raise wrong("telegram_bot_token", "is not a bot's token: @BotFather gives one like 123456789:AAH...")
    if setup.telegram_chat_id and not TELEGRAM_CHAT_SHAPE.fullmatch(setup.telegram_chat_id):
        raise wrong("telegram_chat_id", "has to be a chat's id, a number, or a public channel's @name")
    if setup.telegram_bot_token and not setup.telegram_chat_id:
        raise wrong(
            "telegram_chat_id", "is needed as well as the bot's token: Find chats lists the ones that wrote to the bot"
        )
    if setup.telegram_chat_id and not setup.telegram_bot_token:
        raise wrong("telegram_bot_token", "is needed as well as the chat")
    if setup.telegram_api_url and not WEB_ADDRESS.fullmatch(setup.telegram_api_url):
        raise wrong("telegram_api_url", "has to be an http:// or https:// address")
    if setup.discord_webhook_url and not DISCORD_WEBHOOK.fullmatch(setup.discord_webhook_url.split("?")[0]):
        raise wrong(
            "discord_webhook_url", "is not a Discord webhook's address: a channel's settings, Integrations, Webhooks"
        )
    if setup.dashboard_url and not WEB_ADDRESS.fullmatch(setup.dashboard_url):
        raise wrong("dashboard_url", "has to be an http:// or https:// address")


# -- the chats a Telegram bot can write in --------------------------------------


@dataclass(frozen=True)
class TelegramChat:
    """A chat that wrote to the bot: its id, what kind it is (private,
    group, supergroup, channel) and what it is called."""

    id: int
    kind: str
    name: str


async def telegram_bot(token: str, api_url: str = TELEGRAM_API) -> str:
    """The bot's username: who to write to, for its chat to be found."""
    response = await _telegram_get(token, "getMe", api_url)
    return str(response.get("username") or "")


async def telegram_chats(token: str, api_url: str = TELEGRAM_API) -> list[TelegramChat]:
    """The chats that have written to the bot lately - what Telegram keeps of
    them, for a day - each once, in the order they last wrote."""
    updates = await _telegram_get(token, "getUpdates", api_url)
    chats: dict[int, TelegramChat] = {}
    for update in updates if isinstance(updates, list) else ():
        if not isinstance(update, dict):
            continue
        for key in ("message", "edited_message", "channel_post", "my_chat_member"):
            about = update.get(key)
            chat = about.get("chat") if isinstance(about, dict) else None
            if isinstance(chat, dict) and isinstance(chat.get("id"), int):
                name = chat.get("title") or " ".join(
                    part for part in (chat.get("first_name"), chat.get("last_name")) if part
                )
                if chat.get("username"):
                    name = f"{name} (@{chat['username']})" if name else f"@{chat['username']}"
                chats.pop(chat["id"], None)
                chats[chat["id"]] = TelegramChat(chat["id"], str(chat.get("type") or ""), name)
    return list(chats.values())


async def _telegram_get(token: str, method: str, api_url: str) -> Any:
    try:
        async with httpx.AsyncClient(timeout=SEND_TIMEOUT_SECONDS) as client:
            response = await client.get(f"{telegram_api(api_url)}/bot{token}/{method}")
    except (httpx.HTTPError, httpx.InvalidURL) as error:
        raise _unreached("Telegram", error) from None
    answer = _answer(response)
    if answer.get("ok") is not True:
        said = answer.get("description") or response.reason_phrase
        raise CannotSend(f"Telegram did not answer ({response.status_code} {said}).")
    return answer.get("result")


# -- sending ------------------------------------------------------------------


@dataclass
class Sent:
    """What became of the notes sent to one destination: when one last went
    out, and why the last one did not, if it did not."""

    sent_at: datetime | None = None
    trouble: str | None = None


def _size(video: Path) -> int | None:
    try:
        return video.stat().st_size
    except OSError:
        return None


class Notifier:
    """Sends notes to every destination (`notify`), and keeps what became of
    them, for the Health page."""

    def __init__(
        self, destinations: Iterable[Destination] = (), *, sleep: Callable[[float], Awaitable[object]] = asyncio.sleep
    ) -> None:
        self.destinations = tuple(destinations)
        self._sleep = sleep
        # By destination: what became of its notes, and its turn - one note
        # at a time, in the order they came.
        self.sent: dict[str, Sent] = {destination.name: Sent() for destination in self.destinations}
        self._turns = {destination.name: asyncio.Lock() for destination in self.destinations}

    async def notify(self, note: Note, video: Path | None = None) -> int:
        """Sends the note everywhere - with `video` where it fits - and
        returns to how many destinations it went. Never raises."""
        sent = await asyncio.gather(*(self._send(destination, note, video) for destination in self.destinations))
        return sum(sent)

    async def _send(self, destination: Destination, note: Note, video: Path | None) -> bool:
        async with self._turns[destination.name]:
            here, size = note, None
            if video is not None:
                size = _size(video)
                if size is None or size > destination.video_max_bytes:
                    here, video = note.plus(too_big_words(size, destination.name, destination.video_max_bytes)), None
            try:
                await self._once_more(destination, here, video)
            except CannotSend as refused:
                why = refused.words
            except Exception:
                # Whatever went wrong, it is not worth more than this note.
                logger.exception("a notification could not be sent to %s", destination.name)
                why = "Something went wrong. The server log says what."
            else:
                self.sent[destination.name] = Sent(datetime.now(timezone.utc), None)
                logger.info("notification sent to %s: %s", destination.name, _logged(note))
                return True
            self.sent[destination.name] = replace(self.sent[destination.name], trouble=why)
            logger.warning("a notification could not be sent to %s: %s (%s)", destination.name, why, _logged(note))
            return False

    async def _once_more(self, destination: Destination, note: Note, video: Path | None) -> None:
        """Sends a note; without its clip if that is too big after all, and
        once more after RETRY_SECONDS if the destination could not be
        reached."""
        for first in (True, False):
            try:
                await destination.send(note, video)
                return
            except TooBig:
                if video is None:
                    raise
                await destination.send(note.plus(too_big_words(_size(video), destination.name)), None)
                return
            except CannotSend as failed:
                if not (failed.again and first):
                    raise
            await self._sleep(RETRY_SECONDS)


def _logged(note: Note) -> str:
    return f"[{note.channel}] {note.title}" if note.channel else note.title


# -- the Health page ------------------------------------------------------------


@dataclass(frozen=True)
class Change:
    """A part of the Health page that is to be told of: what it is called
    in a sentence, whether that is more than one thing, what the page says
    of it, and whether it is trouble or fine again."""

    part: str
    words: str
    plural: bool
    said: str
    trouble: bool


@dataclass
class HealthWatch:
    """Which parts of the Health page have been told of as trouble.

    A part is told of once it has been trouble for `after_seconds` on end:
    the page's own judgement waits for most things to have lasted, but not
    all - a disk that dips under its limit for a moment, the bot's last
    message turned down. It is told of again once it is not trouble, and
    then only, so a part that stays in trouble is told of once. A part that
    is no longer on the page (the last Twitch channel taken off the
    watchlist) is not trouble any more."""

    after_seconds: float
    # Since when each part has been trouble, and the ones that were told of.
    since: dict[str, datetime] = field(default_factory=dict)
    told: set[str] = field(default_factory=set)

    def look(self, report: Mapping[str, Any], parts: Iterable[tuple[str, str, bool]], now: datetime) -> list[Change]:
        """What has changed on the page (`report`, health.report's) since the
        last look, as worth telling: `parts` are its parts as health.PARTS
        names them."""
        changes = []
        for name, words, plural in parts:
            part = report.get(name) or {}
            trouble = bool(part.get("trouble")) and part.get("shown", True)
            said = str(part.get("said") or "")
            if trouble:
                since = self.since.setdefault(name, now)
                if name not in self.told and (now - since).total_seconds() >= self.after_seconds:
                    self.told.add(name)
                    changes.append(Change(name, words, plural, said, True))
                continue
            self.since.pop(name, None)
            if name in self.told:
                self.told.discard(name)
                changes.append(Change(name, words, plural, said, False))
        return changes

    def forget(self) -> None:
        """Forgets what was told: what is trouble from now on is told of as
        it is found."""
        self.since.clear()
        self.told.clear()
