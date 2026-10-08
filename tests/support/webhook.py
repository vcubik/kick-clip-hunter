"""Builds Kick webhook deliveries the way Kick signs them.

Kick signs `{message id}.{timestamp}.{raw body}` with RSA-SHA256 (PKCS#1
v1.5) and sends the base64 signature in a header; the service verifies it
against Kick's public key. `WebhookSigner` holds a throwaway key pair, so
tests can produce deliveries the service accepts - and ones it must reject.
"""

from __future__ import annotations

import base64
import itertools
import json
from datetime import datetime, timezone

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

_message_ids = itertools.count(1)


class WebhookSigner:
    def __init__(self) -> None:
        self._private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public_key = self._private_key.public_key()

    @property
    def public_key_pem(self) -> str:
        return self.public_key.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode()

    def sign(self, message_id: str, timestamp: str, body: bytes) -> str:
        signature = self._private_key.sign(
            f"{message_id}.{timestamp}.".encode() + body, padding.PKCS1v15(), hashes.SHA256()
        )
        return base64.b64encode(signature).decode()

    def delivery(self, payload: dict, event_type: str = "chat.message.sent") -> tuple[bytes, dict[str, str]]:
        """The raw body and headers of one correctly signed delivery."""
        body = json.dumps(payload).encode()
        message_id = f"delivery-{next(_message_ids)}"
        timestamp = datetime.now(timezone.utc).isoformat()
        return body, {
            "Content-Type": "application/json",
            "Kick-Event-Message-Id": message_id,
            "Kick-Event-Message-Timestamp": timestamp,
            "Kick-Event-Signature": self.sign(message_id, timestamp, body),
            "Kick-Event-Type": event_type,
        }


def chat_payload(
    channel_slug: str,
    broadcaster_user_id: int,
    sender: str,
    content: str,
    emote_positions: int = 0,
) -> dict:
    """A `chat.message.sent` payload with the fields the service reads."""
    message_id = f"chat-{next(_message_ids)}"
    emotes = [{"emote_id": "1", "positions": [{"s": i, "e": i + 1} for i in range(emote_positions)]}]
    return {
        "message_id": message_id,
        "broadcaster": {"user_id": broadcaster_user_id, "channel_slug": channel_slug},
        "sender": {"user_id": abs(hash(sender)) % 10_000_000, "username": sender},
        "content": content,
        "emotes": emotes if emote_positions else [],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
