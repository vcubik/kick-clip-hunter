"""Client for the public 7TV API (https://7tv.io/v3).

7TV emotes are rendered client-side by a browser extension - they never
appear in Kick's own `emotes` field on a chat message, only as plain text
in the message content. So detecting them means matching the channel's
actual 7TV emote names against message text, not counting Kick's native
emotes.
"""

import httpx

API_BASE = "https://7tv.io/v3"


async def get_channel_emote_names(broadcaster_user_id: int) -> list[str]:
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{API_BASE}/users/kick/{broadcaster_user_id}")
        if response.status_code == 404:
            return []  # channel has no 7TV emote set connected
        response.raise_for_status()
        data = response.json()

    emote_set = data.get("emote_set") or {}
    return [emote["name"] for emote in emote_set.get("emotes", [])]
