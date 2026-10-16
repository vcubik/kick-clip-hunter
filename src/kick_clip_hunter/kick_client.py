"""Thin client for the parts of the Kick Public API this project needs.

Uses an App Access Token (OAuth2 client credentials grant) rather than a
user-authorized token. This is deliberate: app tokens can subscribe to
webhook events for *any* broadcaster_user_id without that streamer having
to authorize our app, which is exactly what a watchlist bot needs.

The one thing an app token can't do is write in a channel's chat as
someone: that takes the user access token of the account the bot writes as
(see kick_auth.py), which is what `get_user` and `send_chat_message` are
handed.
"""

import time

import httpx

ID_BASE = "https://id.kick.com/oauth"
API_BASE = "https://api.kick.com/public/v1"
# The site's own API, which says more of a channel than the public one does
# - its subscriber badges, among other things - and needs no token.
SITE_API_BASE = "https://kick.com/api/v2"

_token_cache: dict[str, float | str] = {}


class NoSuchChannel(ValueError):
    """Kick knows no channel by that name (or id) - as a rule, because the
    channel has renamed itself, or is gone."""


async def get_app_access_token(client_id: str, client_secret: str) -> str:
    cached = _token_cache.get("token")
    expires_at = _token_cache.get("expires_at", 0.0)
    if cached and time.monotonic() < expires_at:
        return cached  # type: ignore[return-value]

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{ID_BASE}/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
        response.raise_for_status()
        payload = response.json()

    token = payload["access_token"]
    # Refresh a little early to avoid using a token that expires mid-request.
    _token_cache["token"] = token
    _token_cache["expires_at"] = time.monotonic() + payload["expires_in"] - 30
    return token


async def get_channel_by_slug(slug: str, token: str) -> dict:
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{API_BASE}/channels",
            params={"slug": slug},
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        channels = response.json()["data"]

    if not channels:
        raise NoSuchChannel(f"No Kick channel found for slug {slug!r}")
    return channels[0]


async def get_channel_by_id(broadcaster_user_id: int, token: str) -> dict:
    """A channel by its broadcaster's user id, which stays the same when the
    channel renames itself - so its `slug` is the name it goes by now."""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{API_BASE}/channels",
            params={"broadcaster_user_id": broadcaster_user_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        channels = response.json()["data"]

    if not channels:
        raise NoSuchChannel(f"No Kick channel found for broadcaster user id {broadcaster_user_id}")
    return channels[0]


async def get_event_subscriptions(token: str) -> list[dict]:
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{API_BASE}/events/subscriptions",
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        return response.json().get("data", [])


async def subscribe_chat_messages(broadcaster_user_id: int, token: str) -> dict:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{API_BASE}/events/subscriptions",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "broadcaster_user_id": broadcaster_user_id,
                "events": [{"name": "chat.message.sent", "version": 1}],
                "method": "webhook",
            },
        )
        response.raise_for_status()
        return response.json()


async def get_site_channel(slug: str) -> dict:
    """A channel as kick.com's own pages are told about it, by its slug -
    where its subscriber badges are (see channel_badges.from_kick)."""
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{SITE_API_BASE}/channels/{slug.lower()}", headers={"Accept": "application/json"})
        response.raise_for_status()
        return response.json()


async def get_user(token: str) -> dict:
    """The account a user access token is of: its id (`user_id`) and its
    name. Raises ValueError if Kick names none."""
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{API_BASE}/users", headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        users = response.json().get("data") or []
    if not users:
        raise ValueError("Kick did not say whose account the token is")
    return users[0]


async def send_chat_message(broadcaster_user_id: int, content: str, token: str) -> dict:
    """Writes a message in a channel's chat as the account whose user access
    token it is (scope chat:write). Returns what Kick answered of it:
    whether it was sent (`is_sent`) and its id."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{API_BASE}/chat",
            headers={"Authorization": f"Bearer {token}"},
            json={"broadcaster_user_id": broadcaster_user_id, "content": content, "type": "user"},
        )
        response.raise_for_status()
        return response.json().get("data") or {}
