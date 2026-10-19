"""Verification of Kick's webhook payload signatures.

Kick signs `{message_id}.{timestamp}.{raw_body}` with RSA-SHA256 (PKCS#1 v1.5)
and sends the base64 signature in the `Kick-Event-Signature` header. We fetch
their public key once a run and cache it - and keep a copy in the database,
so that a webhook arriving while Kick's API cannot be reached (the network
gone for a moment just after a restart) is still verified, by the key that
was fetched before, instead of failing and taking its chat message with it.
"""

import base64
import binascii
import logging
import time

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .db import get_connection, get_kick_public_key_pem, set_kick_public_key_pem

logger = logging.getLogger("kick_clip_hunter")

API_BASE = "https://api.kick.com/public/v1"
# While the kept copy stands in for a key that could not be fetched, how long
# until fetching it is tried again - not on every webhook, each of which
# would wait for the network to fail.
FETCH_AGAIN_SECONDS = 600

_public_key_cache = None
# Until when (monotonic) the cached key is the kept copy rather than one
# fetched in this run; None once one was fetched.
_kept_copy_until: float | None = None


async def _fetch_pem() -> str:
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{API_BASE}/public-key")
        response.raise_for_status()
        return response.json()["data"]["public_key"]


def _kept_pem() -> str | None:
    conn = get_connection()
    try:
        return get_kick_public_key_pem(conn)
    finally:
        conn.close()


def _keep_pem(pem: str) -> None:
    conn = get_connection()
    try:
        set_kick_public_key_pem(conn, pem)
    finally:
        conn.close()


async def get_kick_public_key():
    """Kick's public key: fetched from Kick, or - when that fails - the copy
    kept from the last time it was fetched. With neither, the fetch's error."""
    global _public_key_cache, _kept_copy_until
    if _public_key_cache is not None and (_kept_copy_until is None or time.monotonic() < _kept_copy_until):
        return _public_key_cache

    try:
        pem = await _fetch_pem()
    except httpx.HTTPError as error:
        pem = _kept_pem()
        if pem is None:
            raise
        if _kept_copy_until is None:
            logger.warning("Kick's public key could not be fetched (%r) - the one kept from before is used", error)
        _kept_copy_until = time.monotonic() + FETCH_AGAIN_SECONDS
    else:
        if pem != _kept_pem():
            _keep_pem(pem)
        _kept_copy_until = None

    _public_key_cache = serialization.load_pem_public_key(pem.encode())
    return _public_key_cache


def verify_signature(
    public_key, message_id: str, timestamp: str, body: bytes, signature_b64: str
) -> bool:
    signed_payload = f"{message_id}.{timestamp}.".encode() + body
    try:
        signature = base64.b64decode(signature_b64)
    except (binascii.Error, ValueError):
        # Not even base64, so certainly not from Kick. Anyone can POST to the
        # webhook URL; garbage in this header must be a rejection like any
        # other bad signature, not an unhandled exception and a traceback.
        return False
    try:
        public_key.verify(signature, signed_payload, padding.PKCS1v15(), hashes.SHA256())
        return True
    except InvalidSignature:
        return False
