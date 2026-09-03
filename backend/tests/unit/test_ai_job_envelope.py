from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from lawyer_agent.infrastructure.messaging.envelope import AIJobEnvelope
from lawyer_agent.infrastructure.messaging.signing import (
    EnvelopeSignatureError,
    EnvelopeSigner,
    EnvelopeVerifier,
    decode_ring,
)

_MESSAGE_ID = UUID("0192e1a0-0000-7000-8000-000000000001")
_JOB_ID = UUID("0192e1a0-0000-7000-8000-000000000002")
_TENANT_ID = UUID("0192e1a0-0000-7000-8000-000000000003")
_CORRELATION_ID = UUID("0192e1a0-0000-7000-8000-000000000004")
_OTHER_JOB = UUID("0192e1a0-0000-7000-8000-000000000009")
_ISSUED_AT = datetime(2026, 9, 3, 1, 2, 3, 123456, tzinfo=UTC)
_PLACEHOLDER = "ed25519.dev." + "A" * 86


def _private_key() -> tuple[bytes, bytes]:
    private_key = Ed25519PrivateKey.generate()
    seed = private_key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return seed, public


def _unsigned_envelope(**overrides: object) -> AIJobEnvelope:
    fields: dict[str, object] = {
        "schema_version": 1,
        "message_id": _MESSAGE_ID,
        "job_id": _JOB_ID,
        "tenant_id": _TENANT_ID,
        "correlation_id": _CORRELATION_ID,
        "issued_at": _ISSUED_AT,
        "nonce": "A" * 22,
        "signature": _PLACEHOLDER,
    }
    fields.update(overrides)
    return AIJobEnvelope(**fields)  # type: ignore[arg-type]


def test_envelope_forbids_payload_and_extra_fields() -> None:
    with pytest.raises(ValidationError):
        AIJobEnvelope.model_validate(
            {
                **_unsigned_envelope().model_dump(mode="json"),
                "prompt": "secret",
            }
        )


def test_signature_binds_tenant_job_and_message() -> None:
    seed, public = _private_key()
    signer = EnvelopeSigner({"k2026": seed}, active_kid="k2026")
    verifier = EnvelopeVerifier({"k2026": public})
    signed = signer.sign(_unsigned_envelope())
    assert signed.signature != _PLACEHOLDER
    verifier.verify(signed)
    with pytest.raises(EnvelopeSignatureError):
        verifier.verify(signed.model_copy(update={"job_id": _OTHER_JOB}))


def test_unknown_kid_is_rejected() -> None:
    seed, public = _private_key()
    signer = EnvelopeSigner({"k2026": seed}, active_kid="k2026")
    verifier = EnvelopeVerifier({"k-other": public})
    with pytest.raises(EnvelopeSignatureError, match="unknown kid"):
        verifier.verify(signer.sign(_unsigned_envelope()))


def test_previous_public_key_verifies_after_rotation() -> None:
    old_seed, old_public = _private_key()
    new_seed, new_public = _private_key()
    old_signer = EnvelopeSigner({"k-old": old_seed}, active_kid="k-old")
    signed = old_signer.sign(_unsigned_envelope())
    verifier = EnvelopeVerifier({"k-old": old_public, "k-new": new_public})
    assert verifier.verify(signed) is signed


def test_canonical_bytes_are_deterministic_and_field_ordered() -> None:
    envelope = _unsigned_envelope()
    assert envelope.canonical_bytes() == envelope.canonical_bytes()
    canonical = envelope.canonical_bytes()
    assert b"schema_version" in canonical
    assert b"job_id" in canonical
    assert b"signature" not in canonical
    # First field is schema_version with a 4-byte length prefix.
    assert canonical[:4] == (14).to_bytes(4, "big")


def test_noncanonical_datetime_is_normalized() -> None:
    shifted = _ISSUED_AT + timedelta(seconds=0)
    first = _unsigned_envelope().canonical_bytes()
    second = _unsigned_envelope(issued_at=shifted).canonical_bytes()
    assert first == second


def test_invalid_nonce_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _unsigned_envelope(nonce="not-a-valid-nonce")


def test_invalid_signature_format_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _unsigned_envelope(signature="invalid-signature")


def test_decode_ring_rejects_duplicate_material() -> None:
    material = "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE="
    with pytest.raises(ValueError, match="repeat"):
        decode_ring({"a": material, "b": material}, field_name="ring")
