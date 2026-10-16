"""The accounts the bot writes in chat as, signed in for a test the way the
service would find them kept: on Kick as the dashboard's sign-in leaves it
(see kick_auth.py), on Twitch as scripts/twitch_login.py does - with tokens
the fake platform takes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kick_clip_hunter import account_tokens, kick_auth, twitch_auth
from kick_clip_hunter.account_tokens import AccountToken
from tests.support.kick_api import BOT_SCOPES, FakeKickAccount, FakeKickApi
from tests.support.twitch_api import SCOPES, FakeAccount, FakeTwitchApi


def kick_bot_signed_in(
    kick_api: FakeKickApi,
    account: FakeKickAccount | None = None,
    *,
    scopes: tuple[str, ...] = BOT_SCOPES,
    runs_out_in: float = 7200,
) -> AccountToken:
    """The account the bot writes in Kick's chat as, signed in."""
    account = account or FakeKickAccount()
    tokens = kick_api.user_tokens_for(account, scopes)
    token = AccountToken(
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=runs_out_in),
        user_id=str(account.user_id),
        login=account.name,
        scopes=tuple(scopes),
    )
    account_tokens.save(token, kick_auth.TOKEN_FILE)
    return token


def twitch_signed_in(
    twitch_api: FakeTwitchApi,
    account: FakeAccount | None = None,
    *,
    scopes: tuple[str, ...] = SCOPES,
    known: bool = True,
    runs_out_in: float = 14400,
) -> AccountToken:
    """The account Twitch's chat is read - and written - as, signed in.
    Not `known`, what it let the service do was not kept with it, as for
    one signed in before the bot could write."""
    account = account or FakeAccount()
    tokens = twitch_api.tokens_for(account, scopes)
    token = AccountToken(
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=runs_out_in),
        user_id=account.user_id,
        login=account.login,
        scopes=tuple(scopes) if known else None,
    )
    twitch_auth.save(token)
    return token
