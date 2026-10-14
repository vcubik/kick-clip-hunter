"""The Twitch account the service reads chat as.

Twitch lets an application read a channel's chat over EventSub only on
behalf of an account that reads it (scope user:read:chat). Any account can
read any channel's chat that way, up to a hundred channels at once - more
than a watchlist holds - and the streamers are asked nothing. The account
signs in once, through Twitch's device flow (scripts/twitch_login.py): a
code is shown, whoever owns the account confirms it on twitch.tv/activate,
and the tokens Twitch then hands out are kept in TOKEN_FILE.

An access token runs out after a few hours and the refresh token renews it -
once: each renewal hands out a new refresh token, which is kept in place of
the old one. A refresh token unused for thirty days lapses, and then the
account has to sign in again; while the service runs it renews the token as
it runs out (`TokenKeeper`), so that does not happen to one that is in use.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from .twitch_client import ID_BASE

SCOPES = ("user:read:chat",)
TOKEN_FILE = Path("data/twitch_token.json")
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
# What Twitch answers while the code shown has not been confirmed yet, and
# when it is asked too often.
PENDING = "authorization_pending"
SLOW_DOWN = "slow_down"
# A token with less than this left is renewed before it is used.
RENEW_BEFORE_SECONDS = 600


class SignInNeeded(RuntimeError):
    """There is no Twitch account to read chat as: none ever signed in, or
    its sign-in lapsed or was taken back. scripts/twitch_login.py signs one
    in."""


@dataclass(frozen=True)
class AccountToken:
    """What is kept of the signed-in account: its tokens, when the access
    token runs out, and who it is."""

    access_token: str
    refresh_token: str
    expires_at: datetime
    user_id: str
    login: str

    def encoded(self) -> str:
        return json.dumps({**asdict(self), "expires_at": self.expires_at.isoformat()}, indent=2)

    @classmethod
    def decoded(cls, text: str) -> AccountToken:
        fields = json.loads(text)
        return cls(**{**fields, "expires_at": datetime.fromisoformat(fields["expires_at"])})

    def runs_out_within(self, seconds: float, now: datetime) -> bool:
        return self.expires_at - now <= timedelta(seconds=seconds)


def load(path: Path = TOKEN_FILE) -> AccountToken | None:
    """The account that signed in, or None if none did (or what is kept of
    it cannot be read)."""
    try:
        return AccountToken.decoded(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save(token: AccountToken, path: Path = TOKEN_FILE) -> None:
    """Keeps the account's tokens. Written whole or not at all: a refresh
    token works once, so a half-written file would sign the account out."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    partial.write_text(token.encoded(), encoding="utf-8")
    partial.replace(path)


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
    return AccountToken(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token") or token.refresh_token,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=int(payload["expires_in"])),
        user_id=token.user_id,
        login=token.login,
    )


class TokenKeeper:
    """The signed-in account's token for the running service: read from
    TOKEN_FILE, renewed before it runs out and kept again. One renewal at a
    time - a refresh token used twice signs the account out."""

    def __init__(self, client_id: str, client_secret: str, path: Path = TOKEN_FILE) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._path = path
        self._token: AccountToken | None = None
        self._lock = asyncio.Lock()
        # Why there is no token to be had, the last time there was none.
        self.trouble: str | None = None

    @property
    def account(self) -> AccountToken | None:
        """The account as it was last known, without asking Twitch."""
        return self._token or load(self._path)

    async def token(self, *, renew_now: bool = False) -> AccountToken:
        """A token that does not run out for a while yet. `renew_now` renews
        it whatever is left of it - for one Twitch has just turned down."""
        async with self._lock:
            token = self._token or load(self._path)
            if token is None:
                self.trouble = "No Twitch account has signed in to read chat as."
                raise SignInNeeded(self.trouble)
            if renew_now or token.runs_out_within(RENEW_BEFORE_SECONDS, datetime.now(timezone.utc)):
                try:
                    token = await renew(token, self._client_id, self._client_secret)
                except SignInNeeded as refused:
                    self._token, self.trouble = None, str(refused)
                    raise
                save(token, self._path)
            self._token, self.trouble = token, None
            return token
