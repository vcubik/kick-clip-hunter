"""Verification of Kick's webhook payload signatures.

Kick signs `{message_id}.{timestamp}.{raw_body}` with RSA-SHA256 (PKCS#1 v1.5)
and sends the base64 signature in the `Kick-Event-Signature` header. We fetch
their public key once and cache it.
"""

import base64

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

API_BASE = "https://api.kick.com/public/v1"

_public_key_cache = None


async def get_kick_public_key():
    global _public_key_cache
    if _public_key_cache is not None:
        return _public_key_cache

    async with httpx.AsyncClient() as client:
        response = await client.get(f"{API_BASE}/public-key")
        response.raise_for_status()
        pem = response.json()["data"]["public_key"]

    _public_key_cache = serialization.load_pem_public_key(pem.encode())
    return _public_key_cache


def verify_signature(
    public_key, message_id: str, timestamp: str, body: bytes, signature_b64: str
) -> bool:
    signed_payload = f"{message_id}.{timestamp}.".encode() + body
    signature = base64.b64decode(signature_b64)
    try:
        public_key.verify(signature, signed_payload, padding.PKCS1v15(), hashes.SHA256())
        return True
    except InvalidSignature:
        return False
