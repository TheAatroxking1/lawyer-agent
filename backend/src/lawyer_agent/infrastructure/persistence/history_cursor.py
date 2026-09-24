"""Bounded keyset cursor bound to an explicit tenant and creator."""

from __future__ import annotations

import base64
import binascii
import json
from datetime import UTC, datetime
from uuid import UUID

from lawyer_agent.domain.common import is_uuid7


def encode_cursor(tenant_id: UUID, user_id: UUID, at: datetime, ident: UUID) -> str:
    value = [str(tenant_id), str(user_id), at.isoformat(), str(ident)]
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def decode_cursor(
    value: str | None, tenant_id: UUID, user_id: UUID
) -> tuple[datetime, UUID] | None:
    if value is None:
        return None
    try:
        if not isinstance(value, str) or len(value) > 512:
            raise ValueError
        parts = json.loads(
            base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        )
        if (
            not isinstance(parts, list)
            or len(parts) != 4
            or parts[:2] != [str(tenant_id), str(user_id)]
            or not isinstance(parts[2], str)
            or not isinstance(parts[3], str)
        ):
            raise ValueError
        at = datetime.fromisoformat(parts[2])
        if at.tzinfo is not None:
            at = at.astimezone(UTC).replace(tzinfo=None)
        ident = UUID(parts[3])
        if not is_uuid7(ident):
            raise ValueError
        return at, ident
    except (ValueError, TypeError, OverflowError, binascii.Error):
        raise ValueError("invalid_history_cursor") from None
