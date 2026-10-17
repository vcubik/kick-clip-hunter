"""CLI that lists the Telegram chats notifications can be sent to.

A Telegram bot writes only in a chat that has written to it first - a
private chat with it, or a group or channel it was added to - and the
service needs that chat's id (see notify.py). Make a bot with @BotFather,
put its token in .env as TELEGRAM_BOT_TOKEN, write anything to the bot (or
in the group it is in), then run this: it lists the chats that wrote to it
lately, with their ids. Put the one notifications are to go to in .env as
TELEGRAM_CHAT_ID and restart the service.

Telegram keeps what was written to a bot for a day, and only while no
other program is fetching it - so run this while nothing else uses the
bot's token that way. The service itself never does.

Usage: python scripts/telegram_chats.py
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kick_clip_hunter import notify
from kick_clip_hunter.config import load_settings


async def main() -> None:
    settings = load_settings()
    token = settings.telegram_bot_token
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN in .env is empty: put the token @BotFather gave the bot there first.")
    try:
        bot = await notify.telegram_bot(token, settings.telegram_api_url)
        chats = await notify.telegram_chats(token, settings.telegram_api_url)
    except notify.CannotSend as refused:
        raise SystemExit(refused.words) from None
    if not chats:
        print(f"Nobody has written to @{bot} lately. Write anything to it in Telegram, then run this again.")
        return
    print(f"Chats that wrote to @{bot} lately - TELEGRAM_CHAT_ID in .env is one of these ids:")
    for chat in chats:
        print(f"  {chat.id:>16}  {chat.kind:<10}  {chat.name}")


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()
    asyncio.run(main())
