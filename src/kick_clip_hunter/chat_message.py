"""One chat message as the service takes it in, whichever platform it came
from.

Each platform's chat arrives its own way and in its own shape - Kick posts a
webhook per message, other platforms are read from a connection the service
holds open. What the receiver of each makes of what it was sent is a
`ChatMessage`, and from there on every message goes the same way: stored,
counted by the detector, turned into a moment (see main.py's `_take_chat`).

What the detector and the dashboard read in a message is the same for every
platform, so a platform's receiver puts it in the form Kick sends it in: its
own emotes stand in the text as `[emote:<id>:<name>]` (see
detector.NATIVE_EMOTE_TOKEN_PATTERN), and `emotes` lists where they are.

Pure: what was received comes in, what is stored comes out.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .chat_identity import Identity


@dataclass(frozen=True)
class ChatMessage:
    # The channel's id in the database (streamers.broadcaster_user_id), which
    # everything stored of its chat is kept by.
    broadcaster_user_id: int
    # The name the channel's chat is kept under, and the detector knows it by.
    channel: str
    # The platform's own id of the message: one that is delivered twice is
    # stored, and counted, once.
    message_id: str
    sender: str
    content: str
    # When the platform says it was sent, as it said it - kept, never gone
    # by: what the service goes by is when the message arrived.
    created_at: str = ""
    # The platform's own emotes in the message, as Kick lists them: their id
    # and where they are, [{"emote_id": ..., "positions": [{"s": ..., "e": ...}]}].
    emotes: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    # The sender's name colour and badges as they are stored (see
    # chat_identity.py), or None if the message came without them.
    identity: Identity | None = None
    # Whether the sender is the channel's streamer or one of its moderators -
    # by what the platform sent, which a chatter has no say over.
    runs_channel: bool = False
    # When the service got it, if that was a while before it is taken in;
    # None for "just now".
    received_at: datetime | None = None

    @property
    def emote_count(self) -> int:
        """How many of the platform's own emotes are in the message."""
        return sum(len(emote.get("positions") or ()) for emote in self.emotes)

    def emotes_json(self) -> str:
        """`emotes` as they are stored with the message."""
        return json.dumps(list(self.emotes))
