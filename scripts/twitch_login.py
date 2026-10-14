"""CLI that signs in the Twitch account the service reads chat as.

Twitch lets an application read a channel's chat only on behalf of an
account (see twitch_auth.py). Run this once, and again whenever the Health
page says the sign-in has lapsed: it shows a code to confirm on twitch.tv
and keeps the account's tokens in data/twitch_token.json, where the running
service picks them up. Any Twitch account will do - one kept for this is
best, since it is listed among the viewers of every chat it reads.

Usage: python scripts/twitch_login.py
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kick_clip_hunter import twitch_auth, twitch_client
from kick_clip_hunter.config import load_settings


async def main() -> None:
    settings = load_settings()
    if not settings.twitch:
        raise SystemExit(twitch_client.NOT_SET_UP)
    started = await twitch_auth.start_sign_in(settings.twitch_client_id)
    print(f"Open {started['verification_uri']} and confirm the code {started['user_code']}.")
    print("Waiting for the code to be confirmed...")
    try:
        token = await twitch_auth.finish_sign_in(settings.twitch_client_id, settings.twitch_client_secret, started)
    except twitch_auth.SignInNeeded as refused:
        raise SystemExit(str(refused)) from None
    twitch_auth.save(token)
    print(f"Signed in as {token.login}. The service reads Twitch chat as this account.")


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()
    asyncio.run(main())
