"""Who a chatter is in a channel: the colour of their name and the badges
in front of it.

Kick sends both with every chat message (`sender.identity`). They belong to
the person in that channel, not to the message, so they are kept once per
chatter (db.chat_identities) and only written when they change - the
dashboard looks them up by name when it draws chat. What is shown is
therefore the latest known state, also beside an older moment.

Pure: what Kick sent comes in, what is stored or drawn comes out. Everything
a chatter controls is checked here before it is stored, because the colour
ends up in a style attribute and a badge's type in a file name.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from .dashboard_view import count_words

# A stored identity: the name's colour as "#rrggbb" (None if Kick sent none
# that can be used) and the badges as JSON - a list of {"type", "text"} with
# a "count" where the badge has one (months subscribed, subscriptions
# gifted), in the order Kick lists them.
Identity = tuple[str | None, str]
NO_BADGES = "[]"

_COLOUR = re.compile(r"#[0-9a-fA-F]{6}")
_BADGE_TYPE = re.compile(r"[a-z0-9_]{1,32}")
MAX_BADGES = 8
MAX_BADGE_TEXT = 40

# The badge types there is a picture for (static/badges/<type>.svg). Any
# other type is drawn as OTHER_BADGE and still named in its tooltip.
BADGE_ICONS = frozenset(
    {"broadcaster", "moderator", "vip", "og", "verified", "founder", "subscriber", "sub_gifter", "staff", "bot"}
)
OTHER_BADGE = "other"

# What chat is laid out on (--field in the stylesheet) and how far a name
# has to stand out from it. Names are bold, so the contrast asked of large
# text is enough.
CHAT_BACKGROUND = "#585858"
MIN_NAME_CONTRAST = 3.0
_LIGHTEN_STEPS = 20


def _badge(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, Mapping):
        return None
    badge_type = entry.get("type")
    if not isinstance(badge_type, str) or not _BADGE_TYPE.fullmatch(badge_type):
        return None
    text = entry.get("text")
    badge: dict[str, Any] = {"type": badge_type, "text": text[:MAX_BADGE_TEXT] if isinstance(text, str) else ""}
    count = entry.get("count")
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        badge["count"] = count
    return badge


def from_sender(sender: Mapping[str, Any]) -> Identity | None:
    """The identity in a chat message's `sender`, in the form it is stored.
    None when the message came without one - which says nothing about the
    chatter, so whatever is known about them stays."""
    identity = sender.get("identity")
    if not isinstance(identity, Mapping):
        return None
    colour = identity.get("username_color")
    if not isinstance(colour, str) or not _COLOUR.fullmatch(colour):
        colour = None
    listed = identity.get("badges")
    badges = [badge for badge in map(_badge, listed if isinstance(listed, list) else []) if badge]
    return (colour.lower() if colour else None, json.dumps(badges[:MAX_BADGES], separators=(",", ":"), sort_keys=True))


# -- drawing ----------------------------------------------------------------


def _channels(colour: str) -> tuple[int, int, int]:
    return int(colour[1:3], 16), int(colour[3:5], 16), int(colour[5:7], 16)


def _luminance(colour: str) -> float:
    def linear(channel: int) -> float:
        value = channel / 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = (linear(channel) for channel in _channels(colour))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(colour: str, background: str = CHAT_BACKGROUND) -> float:
    """The contrast ratio between two colours, 1 (none) to 21."""
    lighter, darker = sorted((_luminance(colour), _luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def readable(colour: str) -> str:
    """A chatter's colour as it is drawn: the colour itself where it can be
    read on the chat's grey, otherwise lightened just as far as it takes.
    Kick shows chat on near-black; a dark blue that works there disappears
    here."""
    red, green, blue = _channels(colour)
    mixed = colour
    for step in range(_LIGHTEN_STEPS + 1):
        share = step / _LIGHTEN_STEPS
        mixed = "#{:02x}{:02x}{:02x}".format(
            *(round(channel + (255 - channel) * share) for channel in (red, green, blue))
        )
        if contrast(mixed) >= MIN_NAME_CONTRAST:
            break
    return mixed  # white at the latest, which can be read


def badge_words(badge: Mapping[str, Any]) -> str:
    """What a badge says when pointed at: "Moderator", "Subscriber, 3
    months", "Sub gifter, 5 gifted"."""
    name = badge.get("text") or badge["type"].replace("_", " ").capitalize()
    count = badge.get("count")
    if not count:
        return name
    if badge["type"] == "subscriber":
        return f"{name}, {count_words(count, 'month')}"
    if badge["type"] == "sub_gifter":
        return f"{name}, {count} gifted"
    return f"{name}, {count}"


def badge_parts(badges: str) -> list[dict[str, str]]:
    """Stored badges as they are drawn: which picture, and its words."""
    return [
        {"icon": badge["type"] if badge["type"] in BADGE_ICONS else OTHER_BADGE, "words": badge_words(badge)}
        for badge in json.loads(badges or NO_BADGES)
    ]
