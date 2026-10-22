"""Thin client for the parts of Twitch's API (Helix) this project needs.

As on Kick, most of it is asked with an app access token (OAuth2 client
credentials): looking a channel up by its name, asking whether it is live,
finding the recording of its stream. Reading a channel's chat is different:
Twitch lets an application read any channel's chat only on behalf of an
account that reads it, so subscribing to it (`subscribe_chat`) takes that
account's token (see twitch_auth.py) - and so does writing in it
(`send_chat_message`), as that account.
"""

from __future__ import annotations

import re
import time

import httpx

from .config import Settings

ID_BASE = "https://id.twitch.tv/oauth2"
API_BASE = "https://api.twitch.tv/helix"

# What a Twitch channel can be called: what its login can be made of.
LOGIN = re.compile(r"[a-z0-9_]{1,25}")

_token_cache: dict[str, float | str] = {}


class NotSetUp(RuntimeError):
    """The service has no Twitch application to ask Twitch with."""


NOT_SET_UP = "Twitch is not set up: TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET are not in .env."


async def app_token(settings: Settings) -> str:
    """The app access token of the Twitch application the service is set
    up with - or NotSetUp, if it is set up with none."""
    if not settings.twitch:
        raise NotSetUp(NOT_SET_UP)
    return await get_app_access_token(settings.twitch_client_id, settings.twitch_client_secret)


async def get_app_access_token(client_id: str, client_secret: str) -> str:
    cached = _token_cache.get("token")
    expires_at = _token_cache.get("expires_at", 0.0)
    if cached and time.monotonic() < expires_at:
        return cached  # type: ignore[return-value]

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{ID_BASE}/token",
            data={"client_id": client_id, "client_secret": client_secret, "grant_type": "client_credentials"},
        )
        response.raise_for_status()
        payload = response.json()

    token = payload["access_token"]
    # Refresh a little early to avoid using a token that expires mid-request.
    _token_cache["token"] = token
    _token_cache["expires_at"] = time.monotonic() + payload["expires_in"] - 30
    return token


def _headers(client_id: str, token: str) -> dict[str, str]:
    return {"Client-Id": client_id, "Authorization": f"Bearer {token}"}


async def _get(path: str, params: dict | list, client_id: str, token: str) -> list[dict]:
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{API_BASE}/{path}", params=params, headers=_headers(client_id, token))
        response.raise_for_status()
        return response.json().get("data") or []


async def get_user(login: str, client_id: str, token: str) -> dict:
    """A channel's account by its name: its id, its login and the name it
    is shown under. Raises ValueError if there is no such channel."""
    login = login.strip().lower()
    if not LOGIN.fullmatch(login):
        raise ValueError(f"{login!r} is not a name a Twitch channel can have")
    users = await _get("users", {"login": login}, client_id, token)
    if not users:
        raise ValueError(f"No Twitch channel found for {login!r}")
    return users[0]


async def get_stream(login: str, client_id: str, token: str) -> dict | None:
    """The channel's stream if it is live - when it began (`started_at`),
    its id - or None if it is not."""
    streams = await _get("streams", {"user_login": login, "type": "live"}, client_id, token)
    return streams[0] if streams else None


async def get_archives(user_id: str, client_id: str, token: str, count: int = 5) -> list[dict]:
    """The channel's newest `count` recordings of its own streams (its past
    broadcasts, at most 100), newest first: each with when its stream began
    (`created_at`), how long it is (`duration`, as "3h2m1s") and who may
    watch it (`viewable`). The one of a stream that is still on is among
    them, with that stream's id (`stream_id`), where the channel keeps them
    at all."""
    return await _get("videos", {"user_id": user_id, "type": "archive", "first": count}, client_id, token)


async def get_chat_badges(user_id: str, client_id: str, token: str) -> list[dict]:
    """The badge sets a channel has of its own - its subscriber badges, its
    custom bits badges - each with its versions' pictures."""
    return await _get("chat/badges", {"broadcaster_id": user_id}, client_id, token)


async def subscribe_chat(broadcaster_user_id: str, user_id: str, session_id: str, client_id: str, user_token: str):
    """Subscribes the chat of a channel to an EventSub WebSocket session,
    read as the account `user_id`, whose token it is."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{API_BASE}/eventsub/subscriptions",
            headers=_headers(client_id, user_token),
            json={
                "type": "channel.chat.message",
                "version": "1",
                "condition": {"broadcaster_user_id": broadcaster_user_id, "user_id": user_id},
                "transport": {"method": "websocket", "session_id": session_id},
            },
        )
        response.raise_for_status()
        return response.json()


async def send_chat_message(broadcaster_id: str, sender_id: str, message: str, client_id: str, user_token: str):
    """Writes a message in a channel's chat as the account `sender_id`,
    whose token it is (scope user:write:chat). Returns what Twitch answered
    of it: whether it was sent (`is_sent`), and why not (`drop_reason`)."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{API_BASE}/chat/messages",
            headers=_headers(client_id, user_token),
            json={"broadcaster_id": broadcaster_id, "sender_id": sender_id, "message": message},
        )
        response.raise_for_status()
        sent = response.json().get("data") or [{}]
        return sent[0]
