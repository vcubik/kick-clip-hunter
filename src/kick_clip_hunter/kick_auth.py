"""The Kick account the bot writes in chat as.

Kick's chat is read without any account - its webhooks are subscribed with
an app access token (kick_client.py) - but a message can only be written as
an account: through the public API, with the user access token of one that
let the application write in chat (scope chat:write). Any account will do;
one kept for the bot is best, since every message it writes is shown under
that account's name. It writes wherever the channel lets it chat, and the
streamer is asked nothing.

The account signs in from the dashboard, through Kick's OAuth 2.1 flow with
PKCE. The browser is sent to Kick's consent page with a challenge made of a
secret only the service knows (`start_sign_in`). Kick sends it back to the
Kick app's redirect URL - which has to be this service's page for it,
config.DEFAULT_KICK_REDIRECT_URI unless KICK_REDIRECT_URI says otherwise -
with a code, and the code and the secret are exchanged for the account's
tokens (`finish_sign_in`). Who the account is, its name for the pages, is
asked then too (scope user:read). The tokens are kept in TOKEN_FILE and
renewed as they run out, as account_tokens.py keeps any account's.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

import httpx

from . import account_tokens, kick_client
from .account_tokens import AccountToken, SignInNeeded, scopes_of
from .kick_client import ID_BASE

# What the account is asked to let the service do: write in chat, which is
# what the bot needs (WRITE_SCOPE), and say who it is.
SCOPES = ("chat:write", "user:read")
WRITE_SCOPE = "chat:write"
TOKEN_FILE = Path("data/kick_bot_token.json")
AUTHORIZE_URL = f"{ID_BASE}/authorize"
# How long a sign-in that was started may take to come back.
SIGN_IN_SECONDS = 600
# How long Kick's tokens are taken to last where it does not say.
TOKEN_SECONDS = 3600


@dataclass(frozen=True)
class SignIn:
    """A sign-in that was started and has not come back yet: what it is
    known by when it does (`state`), the secret its challenge was made of
    (`verifier`), and when it was started, on the monotonic clock."""

    state: str
    verifier: str
    started_at: float

    def gone_by(self, now: float) -> bool:
        return now - self.started_at > SIGN_IN_SECONDS


def challenge_of(verifier: str) -> str:
    """PKCE's S256 challenge: the verifier's SHA-256, in base64url without
    padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def start_sign_in(client_id: str, redirect_uri: str, clock: Callable[[], float] = time.monotonic) -> tuple[str, SignIn]:
    """Where to send the browser for the account to sign in, and what to
    know the sign-in by when Kick sends it back."""
    sign_in = SignIn(state=secrets.token_urlsafe(24), verifier=secrets.token_urlsafe(64), started_at=clock())
    asked = [
        ("response_type", "code"),
        ("client_id", client_id),
        ("redirect_uri", redirect_uri),
        ("scope", " ".join(SCOPES)),
        ("code_challenge", challenge_of(sign_in.verifier)),
        ("code_challenge_method", "S256"),
        ("state", sign_in.state),
    ]
    if "127.0.0.1" in redirect_uri:
        # Kick's consent page turns the first 127.0.0.1 in its address into
        # localhost, after which the redirect URL no longer matches the
        # app's. What Kick advises: one before it, to be turned instead.
        asked.insert(0, ("redirect", "127.0.0.1"))
    return f"{AUTHORIZE_URL}?{urlencode(asked, quote_via=quote)}", sign_in


def _said(response: httpx.Response) -> str:
    try:
        answer = response.json()
    except ValueError:
        return ""
    if not isinstance(answer, dict):
        return ""
    return str(answer.get("error_description") or answer.get("error") or answer.get("message") or "")


def _expires_at(payload: dict) -> datetime:
    try:
        seconds = float(payload.get("expires_in") or TOKEN_SECONDS)
    except (TypeError, ValueError):
        seconds = TOKEN_SECONDS
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


async def finish_sign_in(
    client_id: str, client_secret: str, redirect_uri: str, sign_in: SignIn, code: str
) -> AccountToken:
    """The tokens of the account that signed in, for the code Kick sent back
    with the sign-in, and who it is. Raises SignInNeeded if Kick does not
    take the code."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{ID_BASE}/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "code_verifier": sign_in.verifier,
                "code": code,
            },
        )
    if response.status_code in (400, 401):
        raise SignInNeeded(f"Kick did not sign the account in ({_said(response) or response.status_code}).")
    response.raise_for_status()
    payload = response.json()
    try:
        who = await kick_client.get_user(payload["access_token"])
        user_id = str(who["user_id"])
    except (httpx.HTTPStatusError, ValueError, KeyError, TypeError) as error:
        raise SignInNeeded(f"Kick did not say whose account signed in ({error}).") from None
    return AccountToken(
        access_token=payload["access_token"],
        refresh_token=payload["refresh_token"],
        expires_at=_expires_at(payload),
        user_id=user_id,
        login=str(who.get("name") or user_id),
        scopes=scopes_of(payload.get("scope")),
    )


async def renew(token: AccountToken, client_id: str, client_secret: str) -> AccountToken:
    """New tokens for the account, for its refresh token - which they
    replace. Raises SignInNeeded if Kick no longer takes it."""
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
        raise SignInNeeded(f"The bot's sign-in on Kick has lapsed ({_said(response) or response.status_code}).")
    response.raise_for_status()
    payload = response.json()
    scopes = scopes_of(payload.get("scope"))
    return AccountToken(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token") or token.refresh_token,
        expires_at=_expires_at(payload),
        user_id=token.user_id,
        login=token.login,
        scopes=scopes if scopes is not None else token.scopes,
    )


class TokenKeeper(account_tokens.TokenKeeper):
    """The token of the account the bot writes in Kick's chat as: read from
    TOKEN_FILE, renewed before it runs out and kept again (see
    account_tokens.TokenKeeper)."""

    def __init__(self, client_id: str, client_secret: str, path: Path = TOKEN_FILE) -> None:
        async def renewed(token: AccountToken) -> AccountToken:
            return await renew(token, client_id, client_secret)

        super().__init__(path, renewed, "No Kick account has signed in for the bot to write as.")
