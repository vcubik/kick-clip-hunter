"""The platforms channels are watched on, and how a channel's name says which
one it is on.

A channel is known in the service by one name: its chat and its moments are
stored under it, its recorder and its folders are called by it, the
dashboard shows it. A Kick channel's name is its slug, as it was before
there were other platforms. A channel anywhere else is called by its name
there and the platform's, `login@twitch` - so the same name on two platforms
is two channels, and the name alone says where to look for the channel.

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

KICK = "kick"
TWITCH = "twitch"
PLATFORMS = (KICK, TWITCH)
# How each is written for people.
TITLES = {KICK: "Kick", TWITCH: "Twitch"}

# What stands between a channel's name on its platform and the platform's.
# Neither Kick's names nor Twitch's can have one.
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


def parse(text: str) -> tuple[str, str]:
    """Which channel someone means by what they typed: a link to its page,
    its name with its platform's (`login@twitch`), or a bare name - which is
    taken for a Kick channel's, as it always was. Returns the platform and
    the channel's name there."""
    text = text.strip()
    link = _CHANNEL_LINK.match(text)
    if link:
        return _LINK_HOSTS[link.group(1).lower()], link.group(2)
    return split(text)


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

    live: str
    # Master playlist of the recording of the current broadcast, or None if
    # the channel doesn't keep one (or the lookup failed).
    vod: str | None = None
