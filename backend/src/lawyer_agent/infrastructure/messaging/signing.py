from __future__ import annotations

import base64
from binascii import Error as BinasciiError

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from lawyer_agent.infrastructure.messaging.envelope import AIJobEnvelope

_KID_PATTERN = "[A-Za-z0-9._-]{1,64}"


class EnvelopeSignatureError(ValueError):
    code = "envelope_signature_error"


def decode_ring(mapping: dict[str, str], *, field_name: str) -> dict[str, bytes]:
    if not mapping:
        raise ValueError(f"{field_name} must not be empty")
    decoded: dict[str, bytes] = {}
    for kid, material in mapping.items():
        if not isinstance(kid, str) or len(kid) < 1 or len(kid) > 64:
            raise ValueError(f"{field_name} contains an invalid kid")
        if not material.strip():
            raise ValueError(f"{field_name} contains a blank key for {kid}")
        try:
            raw = base64.b64decode(material, validate=True)
        except BinasciiError as exc:
            raise ValueError(f"{field_name} key {kid} must be valid Base64") from exc
        if len(raw) != 32:
            raise ValueError(f"{field_name} key {kid} must decode to exactly 32 bytes")
        decoded[kid] = raw
    if len(set(decoded.values())) != len(decoded):
        raise ValueError(f"{field_name} must not repeat decoded key material")
    return decoded


class EnvelopeSigner:
    """Publisher-only signer holding the private seed ring."""

    def __init__(self, seed_ring: dict[str, bytes], *, active_kid: str) -> None:
        if not isinstance(seed_ring, dict) or not seed_ring:
            raise ValueError("seed ring must not be empty")
        if any(not isinstance(value, bytes) or len(value) != 32 for value in seed_ring.values()):
            raise ValueError("seed ring must contain 32-byte seeds")
        if active_kid not in seed_ring:
            raise ValueError("active kid must identify a configured seed")
        self._seed_ring = seed_ring
        self._active_kid = active_kid

    @property
    def active_kid(self) -> str:
        return self._active_kid

    def sign(self, envelope: AIJobEnvelope) -> AIJobEnvelope:
        seed = self._seed_ring[self._active_kid]
        private_key = Ed25519PrivateKey.from_private_bytes(seed)
        signature_bytes = private_key.sign(envelope.canonical_bytes())
        encoded = base64.urlsafe_b64encode(signature_bytes).decode("ascii").rstrip("=")
        return envelope.model_copy(
            update={"signature": f"ed25519.{self._active_kid}.{encoded}"}
        )


class EnvelopeVerifier:
    """Worker-only verifier holding the public verify ring."""

    def __init__(self, verify_ring: dict[str, bytes]) -> None:
        if not isinstance(verify_ring, dict) or not verify_ring:
            raise ValueError("verify ring must not be empty")
        if any(not isinstance(value, bytes) or len(value) != 32 for value in verify_ring.values()):
            raise ValueError("verify ring must contain 32-byte public keys")
        self._verify_ring = verify_ring

    def verify(self, envelope: AIJobEnvelope) -> AIJobEnvelope:
        public_key_bytes = self._verify_ring.get(envelope.kid)
        if public_key_bytes is None:
            raise EnvelopeSignatureError("unknown kid")
        try:
            public_key = Ed25519PublicKey.from_public_bytes(public_key_bytes)
        except Exception as exc:
            raise EnvelopeSignatureError("invalid public key") from exc
        signature_bytes = _signature_bytes(envelope.signature)
        try:
            public_key.verify(signature_bytes, envelope.canonical_bytes())
        except InvalidSignature as exc:
            raise EnvelopeSignatureError("signature mismatch") from exc
        return envelope


def _signature_bytes(signature: str) -> bytes:
    _, _, encoded = signature.partition(".")
    encoded = encoded.partition(".")[2] if "." in encoded else encoded
    try:
        return base64.urlsafe_b64decode(encoded + "==")
    except BinasciiError as exc:
        raise EnvelopeSignatureError("invalid signature encoding") from exc
