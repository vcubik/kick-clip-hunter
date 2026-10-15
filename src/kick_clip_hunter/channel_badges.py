"""A channel's own subscriber badges: the pictures the channel gives its
subscribers, one for each length of subscription it marks - and on Twitch
one for each tier as well.

A chat message only says that its sender is a subscriber, and for how many
months (see chat_identity.py); which picture that is in the channel is the
channel's to say. Kick lists them with the channel (`subscriber_badges` of
kick.com's own channel endpoint, which a plain HTTP client can read), Twitch
as the channel's `subscriber` badge set (Helix, `chat/badges`). They are
fetched with the channel's emotes and kept per channel (db.channel_badges);
a channel without any of its own, or one whose badges could not be fetched,
keeps the placeholder picture.

Pure: what a platform sent comes in, the pictures to keep and the one to
draw for a badge come out. An address ends up in a picture's `src`, so only
https on the platforms' own picture hosts is kept.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

# Where a platform keeps badge pictures.
IMAGE_HOSTS = frozenset({"files.kick.com", "static-cdn.jtvnw.net"})
MAX_IMAGE_URL = 300
# Twitch's subscriber badges come in versions numbered by tier and months:
# "0", "3", "12" are tier 1, "2000", "2006" tier 2, "3012" tier 3.
_TWITCH_VERSION = re.compile(r"\d{1,6}")
TIER_STEP = 1000


@dataclass(frozen=True)
class Picture:
    """A subscriber badge of a channel: for subscribers of this tier (1 on
    Kick, which has one) from this many months on."""

    tier: int
    months: int
    image: str


def _image(url: Any) -> str | None:
    if not isinstance(url, str) or len(url) > MAX_IMAGE_URL:
        return None
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in IMAGE_HOSTS or '"' in url or "'" in url:
        return None
    return url


def _months(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def from_kick(channel: Mapping[str, Any]) -> list[Picture]:
    """The subscriber badges in Kick's channel payload (kick.com's
    /api/v2/channels/<slug>)."""
    listed = channel.get("subscriber_badges")
    pictures = {}
    for entry in listed if isinstance(listed, list) else []:
        if not isinstance(entry, Mapping):
            continue
        months = _months(entry.get("months"))
        image = entry.get("badge_image")
        image = _image(image.get("src")) if isinstance(image, Mapping) else None
        if months is not None and image:
            pictures[months] = Picture(1, months, image)
    return [pictures[months] for months in sorted(pictures)]


def twitch_tier(version: str) -> tuple[int, int]:
    """The tier and months a Twitch subscriber badge's version stands for."""
    number = int(version)
    if number < TIER_STEP:
        return 1, number
    return number // TIER_STEP, number % TIER_STEP


def from_twitch(sets: Iterable[Any]) -> list[Picture]:
    """The subscriber badges in a channel's badge sets, as Helix lists them
    (`chat/badges`): the `subscriber` set's versions."""
    pictures = {}
    for badge_set in sets:
        if not isinstance(badge_set, Mapping) or badge_set.get("set_id") != "subscriber":
            continue
        versions = badge_set.get("versions")
        for version in versions if isinstance(versions, list) else []:
            if not isinstance(version, Mapping):
                continue
            number = version.get("id")
            image = _image(version.get("image_url_4x")) or _image(version.get("image_url_2x"))
            if isinstance(number, str) and _TWITCH_VERSION.fullmatch(number) and image:
                tier, months = twitch_tier(number)
                pictures[tier, months] = Picture(tier, months, image)
    return [pictures[key] for key in sorted(pictures)]


def pick(pictures: Iterable[Picture], tier: int = 1, months: int | None = None) -> str | None:
    """The picture of a subscriber of that tier and for that many months, as
    the channel's chat shows it: the badge of the longest subscription they
    have reached, of their tier - or of tier 1 if the channel has none of
    theirs. Someone who has reached none of them (or whose months are not
    known) gets the first; a channel without badges of its own, None."""
    pictures = list(pictures)
    of_tier = [picture for picture in pictures if picture.tier == tier] or [
        picture for picture in pictures if picture.tier == 1
    ]
    if not of_tier:
        return None
    reached = [picture for picture in of_tier if months is not None and picture.months <= months]
    if reached:
        return max(reached, key=lambda picture: picture.months).image
    return min(of_tier, key=lambda picture: picture.months).image
