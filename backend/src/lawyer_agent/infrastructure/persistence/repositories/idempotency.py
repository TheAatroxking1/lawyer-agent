from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from lawyer_agent.application.idempotency import (
    AcquiredIdempotencyRecord,
    IdempotencyMutationEffect,
    IdempotencyResultReference,
    IdempotencyScope,
    IdempotencyScopeType,
    IdempotencyStateError,
    IdempotencyStatus,
    NewIdempotencyRecord,
    StoredIdempotencyRecord,
)
from lawyer_agent.infrastructure.persistence.models import IdempotencyRecordModel


class SqlAlchemyIdempotencyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def acquire(
        self,
        candidate: NewIdempotencyRecord,
    ) -> AcquiredIdempotencyRecord:
        values = {
            "id": candidate.id,
            "tenant_id": candidate.scope.tenant_id,
            "scope_type": candidate.scope.scope_type.value,
            "scope_id": candidate.scope.scope_id,
            "operation": candidate.operation,
            "key_hash": candidate.key_hash,
            "request_fingerprint": candidate.request_fingerprint,
            "status": IdempotencyStatus.RESERVED.value,
            "result_type": None,
            "result_id": None,
            "expires_at": _naive(candidate.expires_at),
        }
        statement = mysql_insert(IdempotencyRecordModel).values(**values)
        statement = statement.on_duplicate_key_update(id=IdempotencyRecordModel.id)
        await self._session.execute(statement)

        model = await self._session.scalar(
            select(IdempotencyRecordModel)
            .where(
                IdempotencyRecordModel.scope_type == candidate.scope.scope_type.value,
                IdempotencyRecordModel.scope_id == candidate.scope.scope_id,
                IdempotencyRecordModel.operation == candidate.operation,
                IdempotencyRecordModel.key_hash == candidate.key_hash,
            )
            .with_for_update()
        )
        if model is None:
            raise IdempotencyStateError("idempotency record disappeared during reservation")
        stored = _stored(model)
        if stored.scope != candidate.scope:
            raise IdempotencyStateError("idempotency scope collision")
        return AcquiredIdempotencyRecord(stored, inserted=model.id == candidate.id)

    async def restart(
        self,
        *,
        record_id: UUID,
        old_request_fingerprint: bytes,
        new_request_fingerprint: bytes,
        allowed_statuses: frozenset[IdempotencyStatus],
        expired_before: datetime | None,
        expires_at: datetime,
    ) -> bool:
        if not allowed_statuses or any(
            not isinstance(status, IdempotencyStatus) for status in allowed_statuses
        ):
            raise ValueError("idempotency restart statuses must be strongly typed")
        conditions = [
            IdempotencyRecordModel.id == record_id,
            IdempotencyRecordModel.request_fingerprint == old_request_fingerprint,
            IdempotencyRecordModel.status.in_(status.value for status in allowed_statuses),
        ]
        if expired_before is not None:
            conditions.append(IdempotencyRecordModel.expires_at <= _naive(expired_before))
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(IdempotencyRecordModel)
                .where(*conditions)
                .values(
                    request_fingerprint=new_request_fingerprint,
                    status=IdempotencyStatus.RESERVED.value,
                    result_type=None,
                    result_id=None,
                    expires_at=_naive(expires_at),
                    version=IdempotencyRecordModel.version + 1,
                )
            ),
        )
        return result.rowcount == 1

    async def complete(
        self,
        *,
        record_id: UUID,
        request_fingerprint: bytes,
        result: IdempotencyResultReference,
        now: datetime,
    ) -> bool:
        changed = cast(
            CursorResult[Any],
            await self._session.execute(
                update(IdempotencyRecordModel)
                .where(
                    IdempotencyRecordModel.id == record_id,
                    IdempotencyRecordModel.status == IdempotencyStatus.RESERVED.value,
                    IdempotencyRecordModel.request_fingerprint == request_fingerprint,
                )
                .values(
                    status=IdempotencyStatus.COMPLETED.value,
                    result_type=_serialize_result_type(result),
                    result_id=result.result_id,
                    version=IdempotencyRecordModel.version + 1,
                    updated_at=_naive(now),
                )
            ),
        )
        return changed.rowcount == 1

    async def fail(
        self,
        *,
        record_id: UUID,
        request_fingerprint: bytes,
        now: datetime,
    ) -> bool:
        changed = cast(
            CursorResult[Any],
            await self._session.execute(
                update(IdempotencyRecordModel)
                .where(
                    IdempotencyRecordModel.id == record_id,
                    IdempotencyRecordModel.status == IdempotencyStatus.RESERVED.value,
                    IdempotencyRecordModel.request_fingerprint == request_fingerprint,
                )
                .values(
                    status=IdempotencyStatus.FAILED.value,
                    result_type=None,
                    result_id=None,
                    version=IdempotencyRecordModel.version + 1,
                    updated_at=_naive(now),
                )
            ),
        )
        return changed.rowcount == 1


def _stored(model: IdempotencyRecordModel) -> StoredIdempotencyRecord:
    scope_type = IdempotencyScopeType(model.scope_type)
    result = None
    if model.result_type is not None or model.result_id is not None:
        if model.result_type is None or model.result_id is None:
            raise IdempotencyStateError("idempotency result reference is incomplete")
        result = _deserialize_result_type(model.result_type, model.result_id)
    if len(model.key_hash) != 32 or len(model.request_fingerprint) != 32:
        raise IdempotencyStateError("idempotency digests have invalid lengths")
    return StoredIdempotencyRecord(
        id=model.id,
        scope=IdempotencyScope(scope_type, model.scope_id, tenant_id=model.tenant_id),
        operation=model.operation,
        key_hash=bytes(model.key_hash),
        request_fingerprint=bytes(model.request_fingerprint),
        status=IdempotencyStatus(model.status),
        result=result,
        expires_at=_aware(model.expires_at),
    )


def _naive(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("database timestamp must be UTC-aware")
    return value.astimezone(UTC).replace(tzinfo=None)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _serialize_result_type(result: IdempotencyResultReference) -> str:
    if result.mutation_effect is None and result.cache_authz_version is None:
        return result.result_type
    encoded = json.dumps(
        {
            "a": result.cache_authz_version,
            "e": None if result.mutation_effect is None else result.mutation_effect.value,
            "t": result.result_type,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(encoded) > 64:
        raise IdempotencyStateError("idempotency result metadata is too large")
    return encoded


def _deserialize_result_type(value: str, result_id: UUID) -> IdempotencyResultReference:
    if not value.startswith("{"):
        return IdempotencyResultReference(value, result_id)
    try:
        decoded = json.loads(value)
        if not isinstance(decoded, dict) or set(decoded) != {"a", "e", "t"}:
            raise ValueError
        raw_effect = decoded["e"]
        effect = None if raw_effect is None else IdempotencyMutationEffect(raw_effect)
        return IdempotencyResultReference(
            decoded["t"],
            result_id,
            mutation_effect=effect,
            cache_authz_version=decoded["a"],
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise IdempotencyStateError("idempotency result metadata is invalid") from exc
