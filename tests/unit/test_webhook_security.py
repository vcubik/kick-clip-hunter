"""Verification of Kick's webhook signatures.

Kick signs `{message id}.{timestamp}.{raw body}` with RSA-SHA256 (PKCS#1
v1.5). The tests sign with a throwaway key pair exactly that way and check
that everything else is rejected.
"""

from __future__ import annotations

import base64

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from kick_clip_hunter import webhook_security
from kick_clip_hunter.webhook_security import verify_signature
from tests.support.webhook import WebhookSigner

MESSAGE_ID = "01JD0000000000000000000000"
TIMESTAMP = "2026-03-01T20:00:00Z"
BODY = b'{"message_id":"abc","content":"xDDD"}'


@pytest.fixture
def signature(signer) -> str:
    return signer.sign(MESSAGE_ID, TIMESTAMP, BODY)


class TestVerifySignature:
    def test_accepts_what_kick_signed(self, signer, signature):
        assert verify_signature(signer.public_key, MESSAGE_ID, TIMESTAMP, BODY, signature) is True

    def test_rejects_a_changed_body(self, signer, signature):
        assert verify_signature(signer.public_key, MESSAGE_ID, TIMESTAMP, BODY + b" ", signature) is False

    def test_rejects_a_changed_message_id(self, signer, signature):
        assert verify_signature(signer.public_key, "another-id", TIMESTAMP, BODY, signature) is False

    def test_rejects_a_changed_timestamp(self, signer, signature):
        assert verify_signature(signer.public_key, MESSAGE_ID, "2026-03-01T20:00:01Z", BODY, signature) is False

    def test_rejects_a_signature_made_with_another_key(self, signer):
        forged = WebhookSigner().sign(MESSAGE_ID, TIMESTAMP, BODY)

        assert verify_signature(signer.public_key, MESSAGE_ID, TIMESTAMP, BODY, forged) is False

    def test_rejects_a_signature_over_the_body_alone(self, signer):
        # The id and timestamp are part of what is signed; leaving them out
        # must not verify.
        body_only = base64.b64encode(signer._private_key.sign(BODY, padding.PKCS1v15(), hashes.SHA256())).decode()

        assert verify_signature(signer.public_key, MESSAGE_ID, TIMESTAMP, BODY, body_only) is False

    def test_rejects_the_right_data_signed_with_a_different_scheme(self, signer):
        pss = padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH)
        signed = signer._private_key.sign(f"{MESSAGE_ID}.{TIMESTAMP}.".encode() + BODY, pss, hashes.SHA256())

        assert (
            verify_signature(signer.public_key, MESSAGE_ID, TIMESTAMP, BODY, base64.b64encode(signed).decode()) is False
        )

    def test_rejects_a_well_formed_signature_of_the_wrong_length(self, signer):
        too_short = base64.b64encode(b"\x00" * 64).decode()

        assert verify_signature(signer.public_key, MESSAGE_ID, TIMESTAMP, BODY, too_short) is False

    def test_signs_the_raw_bytes_not_a_reencoding_of_them(self, signer):
        body = '{"content":"příliš žluťoučký kůň 🐴"}'.encode()
        signature = signer.sign(MESSAGE_ID, TIMESTAMP, body)

        assert verify_signature(signer.public_key, MESSAGE_ID, TIMESTAMP, body, signature) is True
        assert (
            verify_signature(signer.public_key, MESSAGE_ID, TIMESTAMP, body.decode().encode("utf-16"), signature)
            is False
        )

    def test_an_empty_body_can_be_signed_too(self, signer):
        signature = signer.sign(MESSAGE_ID, TIMESTAMP, b"")

        assert verify_signature(signer.public_key, MESSAGE_ID, TIMESTAMP, b"", signature) is True


class TestKicksPublicKey:
    pytestmark = pytest.mark.anyio

    async def test_is_fetched_from_kick_and_verifies_kicks_signatures(self, kick_api, signer, signature):
        public_key = await webhook_security.get_kick_public_key()

        assert verify_signature(public_key, MESSAGE_ID, TIMESTAMP, BODY, signature) is True
        assert kick_api.calls("GET", "/public/v1/public-key") == [{}]

    async def test_is_fetched_only_once(self, kick_api):
        first = await webhook_security.get_kick_public_key()
        second = await webhook_security.get_kick_public_key()

        assert second is first
        assert len(kick_api.calls("GET", "/public-key")) == 1

    async def test_a_failed_fetch_is_an_error_and_is_retried_next_time(self, kick_api):
        kick_api.failures["/public-key"] = 503
        with pytest.raises(httpx.HTTPStatusError):
            await webhook_security.get_kick_public_key()

        kick_api.failures.clear()

        assert await webhook_security.get_kick_public_key() is not None
        assert len(kick_api.calls("GET", "/public-key")) == 2
