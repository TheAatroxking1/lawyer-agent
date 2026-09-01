from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from lawyer_agent.application.idempotency import (
    AcquiredIdempotencyRecord,
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
        request_fingerprint: bytes,
        expires_at: datetime,
    ) -> None:
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(IdempotencyRecordModel)
                .where(IdempotencyRecordModel.id == record_id)
                .values(
                    request_fingerprint=request_fingerprint,
                    status=IdempotencyStatus.RESERVED.value,
                    result_type=None,
                    result_id=None,
                    expires_at=_naive(expires_at),
                    version=IdempotencyRecordModel.version + 1,
                )
            ),
        )
        if result.rowcount != 1:
            raise IdempotencyStateError("idempotency record could not be restarted")

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
                    result_type=result.result_type,
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
        result = IdempotencyResultReference(model.result_type, model.result_id)
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
