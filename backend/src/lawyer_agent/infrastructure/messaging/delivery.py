"""Worker delivery codec: bounded, content-checked, canonical envelope parsing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import orjson

from lawyer_agent.infrastructure.messaging.envelope import AIJobEnvelope, envelope_to_json

_MAX_DELIVERY_BODY_BYTES = 16 * 1024
_ALLOWED_CONTENT_TYPE = "application/json"


class RejectDelivery(ValueError):
    """A structural/canonical problem that must never reach tenant audit."""

    def __init__(self, rejection_code: str) -> None:
        self.rejection_code = rejection_code
        super().__init__(f"delivery rejected: {rejection_code}")


@dataclass(frozen=True, slots=True)
class RawDelivery:
    body: bytes
    content_type: str
    received_at: datetime
    redelivered: bool = False


class EnvelopeDeliveryCodec:
    """Validates size/content-type and parses a canonical signed envelope."""

    def require_size_and_content_type(self, delivery: RawDelivery) -> RawDelivery:
        if not isinstance(delivery, RawDelivery):
            raise RejectDelivery("malformed_delivery")
        if delivery.content_type != _ALLOWED_CONTENT_TYPE:
            raise RejectDelivery("unsupported_content_type")
        if not delivery.body or len(delivery.body) > _MAX_DELIVERY_BODY_BYTES:
            raise RejectDelivery("invalid_delivery_size")
        return delivery

    def parse_and_require_canonical(self, delivery: RawDelivery) -> AIJobEnvelope:
        try:
            payload = orjson.loads(delivery.body)
        except (orjson.JSONDecodeError, TypeError) as exc:
            raise RejectDelivery("invalid_envelope_json") from exc
        if not isinstance(payload, dict):
            raise RejectDelivery("invalid_envelope_shape")
        try:
            envelope = AIJobEnvelope.model_validate(payload)
        except Exception as exc:
            raise RejectDelivery("invalid_envelope_fields") from exc
        if envelope_to_json(envelope) != {
            str(key): value for key, value in payload.items()
        }:
            raise RejectDelivery("non_canonical_envelope")
        return envelope
