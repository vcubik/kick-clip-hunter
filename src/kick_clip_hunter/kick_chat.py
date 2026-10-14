"""What Kick's `chat.message.sent` webhook carries, as the service takes a
chat message in (see chat_message.py).

Kick puts its own emotes in the text as `[emote:<id>:<name>]` and lists them
beside it - the form every platform's chat is stored in - so the text and
the list are taken as they come. Who sent the message (name colour, badges)
is in `sender.identity`, see chat_identity.py.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import chat_identity
from .chat_message import ChatMessage


def chat_message(payload: Mapping[str, Any]) -> ChatMessage:
    """The message a delivery holds. Raises KeyError for one that does not
    say which channel it is from - Kick always does."""
    broadcaster = payload.get("broadcaster") or {}
    sender = payload.get("sender") or {}
    broadcaster_user_id = broadcaster["user_id"]
    return ChatMessage(
        broadcaster_user_id=broadcaster_user_id,
        channel=broadcaster.get("channel_slug", "?"),
        message_id=payload["message_id"],
        sender=sender.get("username", ""),
        content=payload.get("content", ""),
        created_at=payload.get("created_at", ""),
        emotes=payload.get("emotes") or [],
        identity=chat_identity.from_sender(sender),
        runs_channel=chat_identity.runs_channel(sender, broadcaster_user_id),
    )
