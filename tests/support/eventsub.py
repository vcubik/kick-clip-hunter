"""A stand-in for Twitch's EventSub WebSocket server, on loopback.

`FakeEventSub` greets every connection with a session of its own, as Twitch
does, and lets a test say what Twitch says next on the newest connection: a
chat message, a keepalive, a request to move to another server (itself,
again), the end of a subscription - or close it. The messages have the
shapes Twitch's documentation gives them.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from datetime import datetime, timezone
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve

_ids = itertools.count(1)


def _metadata(message_type: str, **more: str) -> dict[str, str]:
    return {
        "message_id": f"eventsub-{next(_ids)}",
        "message_type": message_type,
        "message_timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        **more,
    }


def chat_event(
    broadcaster_id: str,
    login: str,
    chatter: str,
    text: str = "",
    *,
    fragments: list[dict] | None = None,
    badges: list[dict] | None = None,
    color: str = "",
    message_id: str | None = None,
    chatter_id: str | None = None,
    source_broadcaster_id: str | None = None,
) -> dict[str, Any]:
    """A `channel.chat.message` event of a channel's chat."""
    return {
        "broadcaster_user_id": broadcaster_id,
        "broadcaster_user_login": login,
        "broadcaster_user_name": login,
        "chatter_user_id": chatter_id or f"{abs(hash(chatter)) % 10_000_000}",
        "chatter_user_login": chatter.lower(),
        "chatter_user_name": chatter,
        "message_id": message_id or f"chat-{next(_ids)}",
        "message": {
            "text": text,
            "fragments": fragments if fragments is not None else [{"type": "text", "text": text}],
        },
        "message_type": "text",
        "badges": badges or [],
        "color": color,
        "cheer": None,
        "reply": None,
        "channel_points_custom_reward_id": None,
        "source_broadcaster_user_id": source_broadcaster_id,
        "source_message_id": None,
        "source_badges": None,
    }


class FakeEventSub:
    def __init__(self) -> None:
        self.url = ""
        # The session each connection was greeted with, in order, and the
        # path (with its query) each connection asked for.
        self.sessions: list[str] = []
        self.paths: list[str] = []
        # The keepalive the next greeting promises; whether connections are
        # greeted at all.
        self.keepalive_seconds: float = 10
        self.greets = True
        self._connections: list[ServerConnection] = []
        self._server: Server | None = None

    async def start(self) -> FakeEventSub:
        self._server = await serve(self._handle, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/ws"
        return self

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, connection: ServerConnection) -> None:
        self.paths.append(connection.request.path)
        self._connections.append(connection)
        if self.greets:
            session_id = f"session-{len(self.sessions) + 1}"
            welcome = {
                "metadata": _metadata("session_welcome"),
                "payload": {
                    "session": {
                        "id": session_id,
                        "status": "connected",
                        "connected_at": datetime.now(timezone.utc).isoformat(),
                        "keepalive_timeout_seconds": self.keepalive_seconds,
                        "reconnect_url": None,
                    }
                },
            }
            await connection.send(json.dumps(welcome))
            self.sessions.append(session_id)
        await connection.wait_closed()

    @property
    def open_connections(self) -> int:
        return sum(1 for connection in self._connections if connection.close_code is None)

    def _newest(self) -> ServerConnection:
        return self._connections[-1]

    async def notify(self, event: dict[str, Any], subscription_type: str = "channel.chat.message") -> None:
        """Sends a notification over the newest connection."""
        message = {
            "metadata": _metadata("notification", subscription_type=subscription_type, subscription_version="1"),
            "payload": {
                "subscription": {
                    "type": subscription_type,
                    "condition": {"broadcaster_user_id": event.get("broadcaster_user_id")},
                },
                "event": event,
            },
        }
        await self._newest().send(json.dumps(message))

    async def send_raw(self, text: str) -> None:
        await self._newest().send(text)

    async def keepalive(self) -> None:
        await self._newest().send(json.dumps({"metadata": _metadata("session_keepalive"), "payload": {}}))

    async def ask_to_move(self) -> None:
        """Asks the client to move to another server - this one again, as a
        new connection that keeps the old one's subscriptions."""
        message = {
            "metadata": _metadata("session_reconnect"),
            "payload": {
                "session": {
                    "id": self.sessions[-1],
                    "status": "reconnecting",
                    "keepalive_timeout_seconds": None,
                    "reconnect_url": f"{self.url}?reconnect=1",
                    "connected_at": datetime.now(timezone.utc).isoformat(),
                }
            },
        }
        await self._newest().send(json.dumps(message))

    async def revoke(self, broadcaster_id: str, status: str = "authorization_revoked") -> None:
        message = {
            "metadata": _metadata("revocation", subscription_type="channel.chat.message", subscription_version="1"),
            "payload": {
                "subscription": {
                    "type": "channel.chat.message",
                    "status": status,
                    "condition": {"broadcaster_user_id": broadcaster_id},
                }
            },
        }
        await self._newest().send(json.dumps(message))

    async def drop(self, code: int = 4005) -> None:
        """Closes the newest connection, as Twitch does when it ends one."""
        await self._newest().close(code)
        await asyncio.sleep(0)
