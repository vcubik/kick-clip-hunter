"""Client for the public 7TV API (https://7tv.io/v3).

7TV emotes are rendered client-side by a browser extension - they never
appear in Kick's own `emotes` field on a chat message, only as plain text
in the message content. So detecting them means matching the channel's
actual 7TV emote names against message text, not counting Kick's native
emotes - and showing them as pictures means knowing which emote a name
stands for in that channel: every channel picks its own set and can rename
what is in it, so the same word is a different picture from one channel to
the next.
"""

import re
from dataclasses import dataclass

import httpx

API_BASE = "https://7tv.io/v3"

# An emote's id ends up in the address its picture is loaded from, so
# anything that is not a plain id (7TV uses ULIDs, and hex ids before them)
# is not passed on as one.
_EMOTE_ID = re.compile(r"[0-9A-Za-z]{1,64}")
# The file an emote's proportions are read from. Every size of an emote has
# the same shape; this one is always there.
_SIZE_FILE = "1x.webp"


@dataclass(frozen=True)
class Emote:
    # What is typed in chat: the name the emote has in this channel's set,
    # which is not necessarily the name it was uploaded under.
    name: str
    # None if 7TV gave no usable id - the name still counts as an emote name.
    emote_id: str | None = None
    # The picture's size in pixels at its smallest scale, 0 if unknown. Only
    # the proportion matters: emotes are not all square.
    width: int = 0
    height: int = 0


def _emote(entry: dict) -> Emote:
    emote_id = entry.get("id")
    if not isinstance(emote_id, str) or not _EMOTE_ID.fullmatch(emote_id):
        return Emote(entry["name"])
    files = ((entry.get("data") or {}).get("host") or {}).get("files") or []
    size = next((file for file in files if file.get("name") == _SIZE_FILE), {})
    width, height = size.get("width"), size.get("height")
    if not (isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0):
        width = height = 0
    return Emote(entry["name"], emote_id, width, height)


async def get_channel_emotes(broadcaster_user_id: int) -> list[Emote]:
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{API_BASE}/users/kick/{broadcaster_user_id}")
        if response.status_code == 404:
            return []  # channel has no 7TV emote set connected
        response.raise_for_status()
        data = response.json()

    emote_set = data.get("emote_set") or {}
    return [_emote(entry) for entry in emote_set.get("emotes", [])]


def emote_pictures(emotes: list[Emote]) -> dict[str, tuple[str, int, int]]:
    """Those of a channel's emotes that have a picture, as (id, width,
    height) by name - the form they are stored in (db.replace_channel_emotes)."""
    return {emote.name: (emote.emote_id, emote.width, emote.height) for emote in emotes if emote.emote_id}
