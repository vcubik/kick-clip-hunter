"""The emotes a Twitch channel's chat uses besides Twitch's own.

Twitch's chat is drawn by three browser extensions as much as by Twitch
itself, each with emotes a channel picks for itself: 7TV (seventv_client.py,
which knows Twitch channels as it knows Kick's), BetterTTV and
FrankerFaceZ. Like 7TV's, their emotes are typed as plain words, which the
extension draws as a picture in a channel that has one of that name. So a
Twitch channel's emotes are all three sets together: what the detector
listens for, and what the dashboard draws.

An emote of BetterTTV or FrankerFaceZ is kept with its service before its
id ("bttv.<id>", "ffz.<id>"), so that the dashboard knows where its picture
is (dashboard_view.py); a 7TV emote's id stands alone, as it always did.
Where two of them have an emote of the same name, 7TV's wins, then
BetterTTV's - the order the extensions are usually layered in.
"""

from __future__ import annotations

import re

import httpx

from .seventv_client import Emote, get_channel_emotes

BTTV_API = "https://api.betterttv.net/3"
FFZ_API = "https://api.frankerfacez.com/v1"
BTTV = "bttv"
FFZ = "ffz"

# What an id may be to go into the address of a picture.
_EMOTE_ID = re.compile(r"[0-9A-Za-z]{1,64}")


def _size(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _emote(source: str, entry: object, name_field: str) -> Emote | None:
    if not isinstance(entry, dict):
        return None
    name, emote_id = entry.get(name_field), str(entry.get("id", ""))
    if not isinstance(name, str) or not name or not _EMOTE_ID.fullmatch(emote_id):
        return None
    width, height = _size(entry.get("width")), _size(entry.get("height"))
    if not (width and height):
        width = height = 0
    return Emote(name, f"{source}.{emote_id}", width, height)


async def get_bttv_emotes(twitch_id: str) -> list[Emote]:
    """The emotes a channel has on BetterTTV: its own and those it shares
    from others. Nothing for a channel without a BetterTTV account."""
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{BTTV_API}/cached/users/twitch/{twitch_id}")
    if response.status_code == 404:
        return []
    response.raise_for_status()
    data = response.json()
    entries = [*(data.get("channelEmotes") or []), *(data.get("sharedEmotes") or [])]
    return [emote for emote in (_emote(BTTV, entry, "code") for entry in entries) if emote]


async def get_ffz_emotes(twitch_id: str) -> list[Emote]:
    """The emotes of a channel's FrankerFaceZ room. Nothing for a channel
    without one."""
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{FFZ_API}/room/id/{twitch_id}")
    if response.status_code == 404:
        return []
    response.raise_for_status()
    data = response.json()
    emotes = []
    for emote_set in (data.get("sets") or {}).values():
        for entry in (emote_set or {}).get("emoticons") or []:
            emote = _emote(FFZ, entry, "name")
            if emote:
                emotes.append(emote)
    return emotes


async def get_channel_emotes_on_twitch(twitch_id: str) -> list[Emote]:
    """All of a Twitch channel's emotes beyond Twitch's own, the one that
    wins a name last (see the module's docstring)."""
    return [
        *await get_ffz_emotes(twitch_id),
        *await get_bttv_emotes(twitch_id),
        *await get_channel_emotes(twitch_id, "twitch"),
    ]
