from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast

from sqlalchemy import func, select, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from lawyer_agent.infrastructure.persistence.models import MessageSecurityRejectionModel

_KID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}\Z", re.ASCII)
_MAX_SCHEMA_VERSION = 2_147_483_647


def observability_ref(key: bytes, raw_message: bytes) -> bytes:
    """HMAC the untrusted message with the dedicated observability key."""
    if not isinstance(key, bytes) or len(key) != 32:
        raise ValueError("observability reference key must contain exactly 32 bytes")
    if not isinstance(raw_message, bytes):
        raise ValueError("raw message must be bytes")
    return hmac.digest(key, b"ai-message-observability:v1\x00" + raw_message, hashlib.sha256)


@dataclass(frozen=True, slots=True)
class SecurityRejection:
    observability_ref: bytes
    rejection_code: str
    source_channel: str
    received_at: datetime
    schema_version_hint: object = None
    kid_hint: object = None

    def __post_init__(self) -> None:
        if not isinstance(self.observability_ref, bytes) or len(self.observability_ref) != 32:
            raise ValueError("security rejection observability ref must be 32 bytes")
        if not isinstance(self.rejection_code, str) or not self.rejection_code:
            raise ValueError("security rejection code must be non-empty text")
        if not isinstance(self.source_channel, str) or not self.source_channel:
            raise ValueError("security rejection source channel must be non-empty text")
        if (
            not isinstance(self.received_at, datetime)
            or self.received_at.tzinfo is None
        ):
            raise ValueError("security rejection received_at must be UTC-aware")


@dataclass(frozen=True, slots=True)
class RejectionCapacity:
    max_rows: int
    max_rows_per_day: int
    sample_rate: int

    def __post_init__(self) -> None:
        for name in ("max_rows", "max_rows_per_day", "sample_rate"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"rejection capacity {name} must be a positive integer")
        if not 1 <= self.sample_rate <= 100:
            raise ValueError("rejection capacity sample_rate must be between 1 and 100")


class RejectionWriteResult(StrEnum):
    PERSISTED = "persisted"
    DEDUPLICATED = "deduplicated"
    SAMPLED = "sampled"
    DROPPED_CAPACITY = "dropped_capacity"


class MessageSecurityRejectionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        rejection: SecurityRejection,
        *,
        capacity: RejectionCapacity,
    ) -> RejectionWriteResult:
        if not isinstance(rejection, SecurityRejection) or not isinstance(
            capacity, RejectionCapacity
        ):
            raise ValueError("security rejection inputs must be strongly typed")
        schema_hint = _sanitize_schema_hint(rejection.schema_version_hint)
        kid_hint = _sanitize_kid_hint(rejection.kid_hint)
        retention_bucket = _retention_bucket(rejection.received_at)

        total_rows = await self._session.scalar(
            select(func.count()).select_from(MessageSecurityRejectionModel)
        )
        if total_rows is not None and total_rows >= capacity.max_rows:
            return RejectionWriteResult.DROPPED_CAPACITY

        if not _deterministic_sample(rejection.observability_ref, capacity.sample_rate):
            return RejectionWriteResult.SAMPLED

        day_rows = await self._session.scalar(
            select(func.count())
            .select_from(MessageSecurityRejectionModel)
            .where(MessageSecurityRejectionModel.retention_bucket == retention_bucket)
        )
        if day_rows is not None and day_rows >= capacity.max_rows_per_day:
            return RejectionWriteResult.DROPPED_CAPACITY

        existing = await self._session.scalar(
            select(MessageSecurityRejectionModel.id).where(
                MessageSecurityRejectionModel.observability_ref
                == rejection.observability_ref,
                MessageSecurityRejectionModel.rejection_code == rejection.rejection_code,
                MessageSecurityRejectionModel.retention_bucket == retention_bucket,
            )
        )
        if existing is not None:
            return RejectionWriteResult.DEDUPLICATED

        await self._session.execute(
            text(
                "INSERT INTO message_security_rejections "
                "(id, observability_ref, rejection_code, schema_version_hint, kid_hint, "
                "source_channel, received_at, retention_bucket) "
                "VALUES (:id, :ref, :code, :schema, :kid, :channel, :received, :bucket) "
                "ON DUPLICATE KEY UPDATE id = id"
            ).bindparams(
                id=_new_id(),
                ref=rejection.observability_ref,
                code=rejection.rejection_code,
                schema=schema_hint,
                kid=kid_hint,
                channel=rejection.source_channel,
                received=_naive(rejection.received_at),
                bucket=retention_bucket,
            )
        )
        return RejectionWriteResult.PERSISTED

    async def delete_expired(self, *, before: datetime, limit: int) -> int:
        if (
            not isinstance(before, datetime)
            or before.tzinfo is None
            or (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000)
        ):
            raise ValueError("delete_expired inputs are invalid")
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                text(
                    "DELETE FROM message_security_rejections "
                    "WHERE received_at < :before LIMIT :limit"
                ),
                {"before": _naive(before), "limit": limit},
            ),
        )
        return result.rowcount


def _sanitize_schema_hint(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not 1 <= value <= _MAX_SCHEMA_VERSION:
        return None
    return value


def _sanitize_kid_hint(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    if _KID_PATTERN.fullmatch(value) is None:
        return None
    return value


def _retention_bucket(value: datetime) -> str:
    return value.astimezone(UTC).date().isoformat()


def _deterministic_sample(ref: bytes, sample_rate: int) -> bool:
    bucket = int.from_bytes(ref[:4], "big") % 100
    return bucket < sample_rate


def _new_id() -> bytes:
    from lawyer_agent.domain.common import new_uuid7

    return new_uuid7().bytes


def _naive(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None)
