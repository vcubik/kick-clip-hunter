"""The Twitch account the service reads chat as - and the bot writes in
Twitch's chat as (see chat_bot.py).

Twitch lets an application read a channel's chat over EventSub only on
behalf of an account that reads it (scope user:read:chat), and write in it
only as an account that let it (user:write:chat). Any account can read any
channel's chat that way, up to a hundred channels at once - more than a
watchlist holds - and the streamers are asked nothing; what it writes, it
writes as itself, wherever the channel lets it chat. The account signs in
once, through Twitch's device flow (scripts/twitch_login.py): a code is
shown, whoever owns the account confirms it on twitch.tv/activate, and the
tokens Twitch then hands out are kept in TOKEN_FILE. One that signed in
before the bot could write was asked for the first scope alone: it reads,
and has to sign in again to write.

The tokens are kept as account_tokens.py keeps any account's: an access
token runs out after a few hours and the refresh token renews it - once,
each renewal handing out a new refresh token in place of the old one. A
refresh token unused for thirty days lapses, and then the account has to
sign in again; while the service runs it renews the token as it runs out
(`TokenKeeper`), so that does not happen to one that is in use.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from . import account_tokens
from .account_tokens import AccountToken, SignInNeeded, scopes_of
from .twitch_client import ID_BASE

# What the account is asked to let the service do: read chat, and write in
# it - the second is what the bot needs (WRITE_SCOPE).
SCOPES = ("user:read:chat", "user:write:chat")
WRITE_SCOPE = "user:write:chat"
TOKEN_FILE = Path("data/twitch_token.json")
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
# What Twitch answers while the code shown has not been confirmed yet, and
# when it is asked too often.
PENDING = "authorization_pending"
SLOW_DOWN = "slow_down"


def load(path: Path = TOKEN_FILE) -> AccountToken | None:
    """The account that signed in, or None if none did (or what is kept of
    it cannot be read)."""
    return account_tokens.load(path)


def save(token: AccountToken, path: Path = TOKEN_FILE) -> None:
    """Keeps the account's tokens (see account_tokens.save)."""
    account_tokens.save(token, path)


def _message(response: httpx.Response) -> str:
    try:
        return str(response.json().get("message", ""))
    except ValueError:
        return ""


async def validate(access_token: str) -> dict:
    """Who an access token is of, and what it may do: Twitch's own account
    of it (user_id, login, scopes, expires_in). Raises SignInNeeded for one
    Twitch no longer takes."""
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{ID_BASE}/validate", headers={"Authorization": f"OAuth {access_token}"})
    if response.status_code == 401:
        raise SignInNeeded("Twitch no longer takes the token of the account chat is read as.")
    response.raise_for_status()
    return response.json()


async def _account(payload: dict) -> AccountToken:
    """The tokens Twitch handed out, with who they are of."""
    who = await validate(payload["access_token"])
    return AccountToken(
        access_token=payload["access_token"],
        refresh_token=payload["refresh_token"],
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=int(payload["expires_in"])),
        user_id=str(who["user_id"]),
        login=who["login"],
        scopes=scopes_of(who.get("scopes")),
    )


async def start_sign_in(client_id: str) -> dict:
    """Starts the device flow: what to show whoever signs in (`user_code`,
    `verification_uri`) and what to wait with (`device_code`, `interval`,
    `expires_in`)."""
    async with httpx.AsyncClient() as client:
        response = await client.post(f"{ID_BASE}/device", data={"client_id": client_id, "scopes": " ".join(SCOPES)})
    response.raise_for_status()
    return response.json()


async def finish_sign_in(
    client_id: str,
    client_secret: str,
    started: dict,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> AccountToken:
    """Waits for the code shown to be confirmed and returns the account's
    tokens. Raises SignInNeeded if it is turned down, or not confirmed
    before it runs out."""
    interval = float(started.get("interval", 5))
    deadline = clock() + float(started.get("expires_in", 1800))
    request = {
        "client_id": client_id,
        "client_secret": client_secret,
        "scopes": " ".join(SCOPES),
        "device_code": started["device_code"],
        "grant_type": DEVICE_GRANT,
    }
    async with httpx.AsyncClient() as client:
        while True:
            response = await client.post(f"{ID_BASE}/token", data=request)
            if response.is_success:
                return await _account(response.json())
            said = _message(response)
            if response.status_code != 400 or said not in (PENDING, SLOW_DOWN):
                raise SignInNeeded(f"Twitch did not sign the account in: {said or response.status_code}.")
            if said == SLOW_DOWN:
                interval += 5
            if clock() + interval > deadline:
                raise SignInNeeded("The code was not confirmed in time. Sign in again.")
            await sleep(interval)


async def renew(token: AccountToken, client_id: str, client_secret: str) -> AccountToken:
    """New tokens for the account, for its refresh token - which they
    replace: that one works only once. Raises SignInNeeded if Twitch no
    longer takes it."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{ID_BASE}/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": token.refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
    if response.status_code in (400, 401):
        raise SignInNeeded(f"The account's sign-in has lapsed ({_message(response) or response.status_code}).")
    response.raise_for_status()
    payload = response.json()
    scopes = scopes_of(payload.get("scope"))
    return AccountToken(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token") or token.refresh_token,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=int(payload["expires_in"])),
        user_id=token.user_id,
        login=token.login,
        scopes=scopes if scopes is not None else token.scopes,
    )


class TokenKeeper(account_tokens.TokenKeeper):
    """The signed-in account's token for the running service: read from
    TOKEN_FILE, renewed before it runs out and kept again (see
    account_tokens.TokenKeeper). The chat reader and the bot share one."""

    def __init__(self, client_id: str, client_secret: str, path: Path = TOKEN_FILE) -> None:
        async def renewed(token: AccountToken) -> AccountToken:
            return await renew(token, client_id, client_secret)

        super().__init__(path, renewed, "No Twitch account has signed in to read chat as.")
