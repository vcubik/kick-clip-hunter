"""Thin client for the parts of the Kick Public API this project needs.

Uses an App Access Token (OAuth2 client credentials grant) rather than a
user-authorized token. This is deliberate: app tokens can subscribe to
webhook events for *any* broadcaster_user_id without that streamer having
to authorize our app, which is exactly what a watchlist bot needs.
"""

import time

import httpx

ID_BASE = "https://id.kick.com/oauth"
API_BASE = "https://api.kick.com/public/v1"

_token_cache: dict[str, float | str] = {}


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
        raise ValueError(f"No Kick channel found for slug {slug!r}")
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
