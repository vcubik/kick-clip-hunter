"""The tokens of an account the service acts as on a platform: kept in a
file, renewed before they run out.

The service acts as an account in two places. Twitch lets an application
read a channel's chat only on behalf of an account that reads it, and the
bot writes in chat as an account - on Twitch that same one, on Kick one
signed in for it (twitch_auth.py, kick_auth.py). Both platforms hand out
OAuth tokens: an access token that runs out after a few hours, and a
refresh token that renews it - once: each renewal hands out a new refresh
token, which is kept in place of the old one. A refresh token left unused
for long lapses, and then the account has to sign in again; while the
service runs it renews the token as it runs out (`TokenKeeper`), so that
does not happen to one that is in use.

What is kept of an account is one file, written whole or not at all. A new
sign-in can write it while the service is running, so the keeper reads it
again whenever it has changed since it last did: the account that signed in
last is the one it goes on with. Otherwise it would go on with the one it
had in memory - and the next renewal of that would write the old account
back over the new one.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# A token with less than this left is renewed before it is used.
RENEW_BEFORE_SECONDS = 600


class SignInNeeded(RuntimeError):
    """There is no account to act as: none ever signed in, or its sign-in
    lapsed or was taken back."""


@dataclass(frozen=True)
class AccountToken:
    """What is kept of a signed-in account: its tokens, when the access
    token runs out and who it is - and what it let the service do, as the
    platform said when it handed the token out (`scopes`; None where that is
    not known, as for a token kept before it was)."""

    access_token: str
    refresh_token: str
    expires_at: datetime
    user_id: str
    login: str
    scopes: tuple[str, ...] | None = None

    def encoded(self) -> str:
        return json.dumps({**asdict(self), "expires_at": self.expires_at.isoformat()}, indent=2)

    @classmethod
    def decoded(cls, text: str) -> AccountToken:
        fields = json.loads(text)
        scopes = fields.get("scopes")
        return cls(
            **{
                **fields,
                "expires_at": datetime.fromisoformat(fields["expires_at"]),
                "scopes": tuple(str(scope) for scope in scopes) if isinstance(scopes, list) else None,
            }
        )

    def runs_out_within(self, seconds: float, now: datetime) -> bool:
        return self.expires_at - now <= timedelta(seconds=seconds)

    def may(self, scope: str) -> bool | None:
        """Whether the account let the service do what `scope` stands for -
        None where that is not known."""
        return None if self.scopes is None else scope in self.scopes


def scopes_of(given: object) -> tuple[str, ...] | None:
    """The scopes a platform says a token has - a list, or one string with
    spaces between them - or None if it said nothing that can be read."""
    if isinstance(given, str):
        return tuple(given.split())
    if isinstance(given, list) and all(isinstance(scope, str) for scope in given):
        return tuple(given)
    return None


def load(path: Path) -> AccountToken | None:
    """The account that signed in, or None if none did (or what is kept of
    it cannot be read)."""
    try:
        return AccountToken.decoded(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save(token: AccountToken, path: Path) -> None:
    """Keeps the account's tokens. Written whole or not at all: a refresh
    token works once, so a half-written file would sign the account out."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    partial.write_text(token.encoded(), encoding="utf-8")
    partial.replace(path)


class TokenKeeper:
    """An account's token for the running service: read from its file,
    renewed before it runs out and kept again. One renewal at a time - a
    refresh token used twice signs the account out. `renew` asks the
    platform for new tokens; `nobody` is why there is no token while no
    account has signed in."""

    def __init__(self, path: Path, renew: Callable[[AccountToken], Awaitable[AccountToken]], nobody: str) -> None:
        self._path = path
        self._renew = renew
        self._nobody = nobody
        self._token: AccountToken | None = None
        # The file as it was when it was last read or written, so that one
        # written since - a new sign-in - is told apart.
        self._read: tuple[int, int, int] | None = None
        self._lock = asyncio.Lock()
        # Why there is no token to be had, the last time there was none.
        self.trouble: str | None = None

    def _stamp(self) -> tuple[int, int, int] | None:
        """What tells the file apart from the one before: `save` replaces it
        with a new file, which has an inode of its own - even where two
        writes fall within one tick of the clock and are as long."""
        try:
            stat = self._path.stat()
        except OSError:
            return None
        return stat.st_ino, stat.st_mtime_ns, stat.st_size

    def _current(self) -> AccountToken | None:
        """The token in memory - or the file's, if it was written since it
        was last read."""
        stamp = self._stamp()
        if stamp is not None and stamp != self._read:
            token = load(self._path)
            if token is not None:
                self._token, self._read = token, stamp
        return self._token

    @property
    def account(self) -> AccountToken | None:
        """The account as it was last known, without asking the platform."""
        return self._current()

    def keep(self, token: AccountToken) -> None:
        """Keeps tokens the service was just handed - a renewal, or a sign-in
        it saw to itself - and goes on with them."""
        save(token, self._path)
        self._token, self._read, self.trouble = token, self._stamp(), None

    async def token(self, *, renew_now: bool = False) -> AccountToken:
        """A token that does not run out for a while yet. `renew_now` renews
        it whatever is left of it - for one the platform has just turned
        down."""
        async with self._lock:
            token = self._current()
            if token is None:
                # A sign-in that lapsed is still why, until another is made.
                self.trouble = self.trouble or self._nobody
                raise SignInNeeded(self.trouble)
            if renew_now or token.runs_out_within(RENEW_BEFORE_SECONDS, datetime.now(timezone.utc)):
                try:
                    renewed = await self._renew(token)
                except SignInNeeded as refused:
                    self._token, self.trouble = None, str(refused)
                    raise
                # Read whatever the stamp says: it is what was renewed that is
                # not to be written over the file, and that is its content.
                signed_in = load(self._path)
                if signed_in is not None and signed_in != token:
                    # An account signed in while this one's token was being
                    # renewed: that is the account now, not to be written over.
                    self._token, self._read = signed_in, self._stamp()
                    renewed = signed_in
                else:
                    self.keep(renewed)
                token = renewed
            self._token, self.trouble = token, None
            return token
