"""The platforms channels are watched on, and how a channel's name says which
one it is on.

A channel is known in the service by one name: its chat and its moments are
stored under it, its recorder and its folders are called by it, the
dashboard shows it. A Kick channel's name is its slug, as it was before
there were other platforms. A channel anywhere else is called by its name
there and the platform's, `login@twitch`, `handle@youtube`, `handle@tiktok` -
so the same name on two platforms is two channels, and the name alone says
where to look for the channel. A YouTube or TikTok channel's name is its
handle, without the "@" the platform writes it with.

A TikTok channel is watched to be recorded and for nothing else: its whole
stream is kept, with its chat, but no clips are looked for in it
(RECORDED_ONLY).

In the database a channel is kept by an id (streamers.broadcaster_user_id):
a Kick channel's is its Kick user id, any other channel's a negative number
given out when it was added, which no Kick id can be (see
db.add_platform_streamer). Its id on its own platform is kept beside it.

Also here, the shapes in which what is asked of a channel's platform comes
back (see streams.py, which asks it). Pure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qs, unquote

KICK = "kick"
TWITCH = "twitch"
YOUTUBE = "youtube"
TIKTOK = "tiktok"
PLATFORMS = (KICK, TWITCH, YOUTUBE, TIKTOK)
# How each is written for people.
TITLES = {KICK: "Kick", TWITCH: "Twitch", YOUTUBE: "YouTube", TIKTOK: "TikTok"}
# The platforms whose channels are only ever recorded whole: never tracked
# for clips, their chat kept with a recording and looked for nothing in.
RECORDED_ONLY = frozenset({TIKTOK})
# The platforms that keep a recording of a broadcast as it goes along (its
# VOD), which stays there once the broadcast is over: what an ad covered can
# be had from it, and footage a moment's file does not hold, days later too.
# YouTube keeps a stream that is on to be wound back (see Vod.numbered), but
# nothing of it in the stream's own form once it is over.
KEEP_VODS = frozenset({KICK, TWITCH})

# What stands between a channel's name on its platform and the platform's.
# Neither Kick's names nor Twitch's can have one, nor YouTube's or TikTok's
# handles.
SEPARATOR = "@"


def channel_name(platform: str, login: str) -> str:
    """The name a channel is known by in the service."""
    return login if platform == KICK else f"{login}{SEPARATOR}{platform}"


def split(channel: str) -> tuple[str, str]:
    """Which platform a channel the service knows by this name is on, and
    the channel's name there."""
    login, separator, platform = channel.rpartition(SEPARATOR)
    if separator and login and platform in PLATFORMS and platform != KICK:
        return platform, login
    return KICK, channel


def platform_of(channel: str) -> str:
    return split(channel)[0]


# A link to a channel's page, with or without its scheme and its "www.".
_CHANNEL_LINK = re.compile(r"(?:https?://)?(?:www\.|m\.)?(kick\.com|twitch\.tv)/([^/?#\s]+)", re.IGNORECASE)
_LINK_HOSTS = {"kick.com": KICK, "twitch.tv": TWITCH}
# A link to YouTube - to a channel's page by its handle (youtube.com/@name),
# or to anything else there that leads to a channel: its page by its id or
# by an older kind of name, one of its videos, a live stream among them.
_YOUTUBE_LINK = re.compile(
    r"(?:https?://)?(?:(?:www\.|m\.)?youtube\.com|(youtu\.be))(?=[/?#]|$)(/[^#\s]*)?", re.IGNORECASE
)
_YOUTUBE_HANDLE = re.compile(r"/@([^/?#]+)")
YOUTUBE_SITE = "https://www.youtube.com"
# A link to a TikTok channel's page, or to its live stream: tiktok.com/@name
# or tiktok.com/@name/live.
_TIKTOK_LINK = re.compile(r"(?:https?://)?(?:www\.|m\.)?tiktok\.com/@([^/?#\s]+)", re.IGNORECASE)
TIKTOK_SITE = "https://www.tiktok.com"


def parse(text: str) -> tuple[str, str]:
    """Which channel someone means by what they typed: a link to its page,
    its name with its platform's (`login@twitch`, `handle@youtube`,
    `handle@tiktok`), or a bare name - which is taken for a Kick channel's,
    as it always was. Returns the platform and the channel's name there - for
    a link to YouTube that names no handle, the link itself, made whole, for
    YouTube to say whose it is."""
    text = text.strip()
    link = _CHANNEL_LINK.match(text)
    if link:
        return _LINK_HOSTS[link.group(1).lower()], link.group(2)
    youtube = _YOUTUBE_LINK.match(text)
    if youtube:
        return YOUTUBE, _youtube_where(youtube.group(2) or "/", short=bool(youtube.group(1)))
    tiktok = _TIKTOK_LINK.match(text)
    if tiktok:
        return TIKTOK, unquote(tiktok.group(1))
    platform, login = split(text)
    # YouTube and TikTok write a handle with an "@" before it.
    return platform, login.removeprefix(SEPARATOR) if platform in (YOUTUBE, TIKTOK) else login


def _youtube_where(path: str, *, short: bool) -> str:
    """A YouTube channel's handle, from a link to its page by it; any other
    link to YouTube as a whole address on youtube.com."""
    if short:
        # youtu.be/<id> is the video of that id.
        video_id = path.lstrip("/").partition("?")[0].partition("/")[0]
        return f"{YOUTUBE_SITE}/watch?v={video_id}"
    handle = _YOUTUBE_HANDLE.match(path)
    if handle:
        return unquote(handle.group(1))
    location, _, query = path.partition("?")
    video_id = parse_qs(query).get("v", [""])[0] if location.rstrip("/").lower() == "/watch" else ""
    # A video's address keeps nothing of what follows but which video it is.
    return f"{YOUTUBE_SITE}/watch?v={video_id}" if video_id else f"{YOUTUBE_SITE}{path}"


def login_of(channel: str) -> str:
    """A channel's name on its own platform."""
    return split(channel)[1]


@dataclass(frozen=True)
class LiveStatus:
    """Whether a channel is live, as its platform says."""

    live: bool
    # When the stream began, where the platform says; None while it is not
    # live.
    started_at: datetime | None = None


class StreamUrlError(RuntimeError):
    """No stream could be found for a channel - as a rule, because it is not
    live (any more)."""


@dataclass
class StreamUrls:
    """Where a live channel's stream is: what a recorder follows."""

    # The stream's master playlist - or, where `flv` says so, the stream
    # itself.
    live: str
    # Master playlist of the recording of the current broadcast, or None if
    # the channel doesn't keep one (or the lookup failed).
    vod: str | None = None
    # When these addresses stop working, where the platform says - the
    # recorder asks for new ones before then. YouTube's do after six hours,
    # TikTok's after weeks; Kick's and Twitch's work for as long as the
    # stream is on.
    expires_at: datetime | None = None
    # Whether `live` is the stream as one FLV download that does not end
    # (TikTok's, see flv.py) rather than an HLS playlist.
    flv: bool = False


class NoVod(RuntimeError):
    """There is no recording of a broadcast to take footage of it from - its
    words say why, as the dashboard says it."""


@dataclass(frozen=True)
class Vod:
    """Where a broadcast's own recording is: its master playlist, whose
    segments are footage of the broadcast on its own clock.

    Kick's playlist gives every segment its program date-time. A Twitch
    broadcast that is over has none in its playlist: `started_at` is when
    the broadcast began, as Twitch says it, which the segments' lengths are
    counted on from - to a few seconds, so the picture says exactly where
    footage is (see vod_footage.py). YouTube keeps no recording, but a stream
    that is on can be wound back: `numbered` says its playlist is the live
    one, which lists only its newest segments, and an older one is had by
    its number in the address of the first listed (`/sq/<number>/`)."""

    master: str
    started_at: datetime | None = None
    numbered: bool = False
