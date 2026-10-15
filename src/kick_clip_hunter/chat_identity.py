"""Who a chatter is in a channel: the colour of their name and the badges
in front of it.

Kick sends both with every chat message (`sender.identity`), and so does
Twitch (`color`, `badges`), whose badges are put in the form Kick's are
kept in - as are YouTube's (`authorBadges`), whose names have no colour of
their own. They belong to the person in that channel, not to the message, so
they are kept once per chatter (db.chat_identities) and only written when
they change - the dashboard looks them up by name when it draws chat. What
is shown is therefore the latest known state, also beside an older moment.

Pure: what a platform sent comes in, what is stored or drawn comes out.
Everything a chatter controls is checked here before it is stored, because
the colour ends up in a style attribute and a badge's type in a file name.
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


# Twitch's badges by the type Kick's same badge has, where the two differ;
# any other is kept under its own name, with "_" for its "-".
TWITCH_BADGE_TYPES = {"sub-gifter": "sub_gifter", "partner": "verified"}
# The Twitch badges whose count says how much: months subscribed (`info`),
# subscriptions gifted and bits given (the badge's own `id`, its tier).
_TWITCH_BADGE_COUNTS = {"subscriber": "info", "founder": "info", "sub-gifter": "id", "bits": "id"}


def _twitch_badge(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, Mapping) or not isinstance(entry.get("set_id"), str):
        return None
    set_id = entry["set_id"]
    count_field = _TWITCH_BADGE_COUNTS.get(set_id)
    count = entry.get(count_field) if count_field else None
    return _badge(
        {
            "type": TWITCH_BADGE_TYPES.get(set_id, set_id.replace("-", "_")),
            "text": set_id.replace("-", " ").capitalize(),
            "count": int(count) if isinstance(count, str) and count.isdigit() else None,
        }
    )


def from_twitch(colour: Any, badges: Any) -> Identity:
    """A Twitch chatter's identity as it is stored: the colour of their name
    (Twitch sends none for someone who never chose one) and their badges, as
    a chat message of theirs lists them (`color`, `badges`)."""
    if not isinstance(colour, str) or not _COLOUR.fullmatch(colour):
        colour = None
    kept = [badge for badge in map(_twitch_badge, badges if isinstance(badges, list) else []) if badge]
    return (colour.lower() if colour else None, json.dumps(kept[:MAX_BADGES], separators=(",", ":"), sort_keys=True))


# YouTube's badges by the type Kick's same badge has, by the icon YouTube
# draws them with; a member's badge is a picture of the channel's own.
YOUTUBE_BADGE_TYPES = {"OWNER": "broadcaster", "MODERATOR": "moderator", "VERIFIED": "verified"}
YOUTUBE_MEMBER_BADGE = "subscriber"
# How long a member has been one, as the badge's tooltip says it in English:
# "Member (2 years, 3 months)", "Member (6 months)".
_YOUTUBE_YEARS = re.compile(r"(\d+)\s+year")
_YOUTUBE_MONTHS = re.compile(r"(\d+)\s+month")


def _youtube_months(tooltip: str) -> int | None:
    years, months = _YOUTUBE_YEARS.search(tooltip), _YOUTUBE_MONTHS.search(tooltip)
    if years is None and months is None:
        return None
    return (int(years.group(1)) * 12 if years else 0) + (int(months.group(1)) if months else 0)


def _youtube_badge(entry: Any) -> dict[str, Any] | None:
    badge = entry.get("liveChatAuthorBadgeRenderer") if isinstance(entry, Mapping) else None
    if not isinstance(badge, Mapping):
        return None
    tooltip = badge.get("tooltip") if isinstance(badge.get("tooltip"), str) else ""
    icon = badge.get("icon")
    icon_type = icon.get("iconType") if isinstance(icon, Mapping) else None
    if isinstance(icon_type, str):
        return _badge({"type": YOUTUBE_BADGE_TYPES.get(icon_type, icon_type.lower()), "text": tooltip})
    if "customThumbnail" in badge:
        return _badge({"type": YOUTUBE_MEMBER_BADGE, "text": tooltip, "count": _youtube_months(tooltip)})
    return None


def from_youtube(badges: Any) -> Identity:
    """A YouTube chatter's identity as it is stored: YouTube gives names no
    colour of their own, and the badges as a chat message of theirs lists
    them (`authorBadges`)."""
    kept = [badge for badge in map(_youtube_badge, badges if isinstance(badges, list) else []) if badge]
    return (None, json.dumps(kept[:MAX_BADGES], separators=(",", ":"), sort_keys=True))


# The badges of those who run a channel: its streamer and its moderators.
RUNNING_BADGES = frozenset({"broadcaster", "moderator"})


def runs_channel(sender: Mapping[str, Any], broadcaster_user_id: int) -> bool:
    """Whether a chat message's `sender` is the channel's streamer or one of
    its moderators - by the badges Kick sent with the message, which a
    chatter has no say over, or by being the broadcaster's own account."""
    if sender.get("user_id") == broadcaster_user_id:
        return True
    identity = sender.get("identity")
    listed = identity.get("badges") if isinstance(identity, Mapping) else None
    badges = map(_badge, listed if isinstance(listed, list) else [])
    return any(badge is not None and badge["type"] in RUNNING_BADGES for badge in badges)


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
