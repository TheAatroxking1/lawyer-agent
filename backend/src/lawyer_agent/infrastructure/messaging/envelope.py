from __future__ import annotations

import base64
import re
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

_NONCE_PATTERN = re.compile(r"[A-Za-z0-9_-]{22}\Z", re.ASCII)
_SIGNATURE_PATTERN = re.compile(
    r"ed25519\.([A-Za-z0-9._-]{1,64})\.([A-Za-z0-9_-]{86})\Z", re.ASCII
)


class AIJobEnvelope(BaseModel):
    """The only allowed RabbitMQ body: a signed reference, never a payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    message_id: UUID
    job_id: UUID
    tenant_id: UUID
    correlation_id: UUID
    issued_at: datetime
    nonce: str
    signature: str

    @field_validator("nonce")
    @classmethod
    def validate_nonce(cls, value: str) -> str:
        if _NONCE_PATTERN.fullmatch(value) is None:
            raise ValueError("nonce must be an unpadded Base64URL 128-bit value")
        try:
            decoded = base64.urlsafe_b64decode(value + "==")
        except Exception as exc:
            raise ValueError("nonce must be an unpadded Base64URL 128-bit value") from exc
        if len(decoded) != 16:
            raise ValueError("nonce must decode to exactly 16 bytes")
        return value

    @field_validator("signature")
    @classmethod
    def validate_signature(cls, value: str) -> str:
        if _SIGNATURE_PATTERN.fullmatch(value) is None:
            raise ValueError("signature must use the ed25519.<kid>.<base64url> form")
        match = _SIGNATURE_PATTERN.fullmatch(value)
        assert match is not None
        try:
            decoded = base64.urlsafe_b64decode(match.group(2) + "==")
        except Exception as exc:
            raise ValueError("signature is not valid Base64URL") from exc
        if len(decoded) != 64:
            raise ValueError("signature must decode to exactly 64 bytes")
        return value

    @property
    def kid(self) -> str:
        match = _SIGNATURE_PATTERN.fullmatch(self.signature)
        assert match is not None
        return match.group(1)

    def canonical_bytes(self) -> bytes:
        """Deterministic signing input over the first seven fields."""
        parts = (
            ("schema_version", "1"),
            ("message_id", str(self.message_id)),
            ("job_id", str(self.job_id)),
            ("tenant_id", str(self.tenant_id)),
            ("correlation_id", str(self.correlation_id)),
            ("issued_at", _canonical_datetime(self.issued_at)),
            ("nonce", self.nonce),
        )
        chunks: list[bytes] = []
        for name, content in parts:
            name_bytes = name.encode("ascii")
            content_bytes = content.encode("ascii")
            chunks.append(len(name_bytes).to_bytes(4, "big"))
            chunks.append(name_bytes)
            chunks.append(len(content_bytes).to_bytes(4, "big"))
            chunks.append(content_bytes)
        return b"".join(chunks)


def _canonical_datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("envelope issued_at must be UTC-aware")
    utc_value = value.astimezone(UTC)
    if utc_value.microsecond == 0:
        return f"{utc_value:%Y-%m-%dT%H:%M:%S}Z"
    return f"{utc_value:%Y-%m-%dT%H:%M:%S.%f}Z"


def envelope_to_json(envelope: AIJobEnvelope) -> dict[str, object]:
    """The fixed eight-field wire representation of an envelope."""
    return {
        "schema_version": envelope.schema_version,
        "message_id": str(envelope.message_id),
        "job_id": str(envelope.job_id),
        "tenant_id": str(envelope.tenant_id),
        "correlation_id": str(envelope.correlation_id),
        "issued_at": _canonical_datetime(envelope.issued_at),
        "nonce": envelope.nonce,
        "signature": envelope.signature,
    }
