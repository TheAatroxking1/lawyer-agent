from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lawyer_agent.application.tenancy import (
    AuthorizationCacheInvalidationStatus,
    AuthorizationCacheInvalidationTask,
    NewAuthorizationCacheInvalidation,
)
from lawyer_agent.domain.common import require_uuid7
from lawyer_agent.infrastructure.persistence.models import (
    AuthorizationCacheInvalidationOutboxModel,
)


class AuthorizationCacheInvalidationOutboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, task: NewAuthorizationCacheInvalidation) -> None:
        if not isinstance(task, NewAuthorizationCacheInvalidation):
            raise ValueError("cache invalidation outbox task must be strongly typed")
        self._session.add(
            AuthorizationCacheInvalidationOutboxModel(
                id=task.id,
                tenant_id=task.tenant_id,
                membership_id=task.membership_id,
                authz_version=task.authz_version,
                idempotency_record_id=task.idempotency_record_id,
                status=AuthorizationCacheInvalidationStatus.PENDING.value,
                attempt_count=0,
                available_at=_naive(task.available_at),
                processing_started_at=None,
                completed_at=None,
                last_error_code=None,
            )
        )

    async def claim_batch(
        self,
        *,
        now: datetime,
        processing_expired_before: datetime,
        limit: int,
    ) -> tuple[AuthorizationCacheInvalidationTask, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("cache invalidation claim limit is invalid")
        effective_now = _naive(now)
        expired_before = _naive(processing_expired_before)
        rows = (
            await self._session.scalars(
                select(AuthorizationCacheInvalidationOutboxModel)
                .where(
                    or_(
                        and_(
                            AuthorizationCacheInvalidationOutboxModel.status
                            == AuthorizationCacheInvalidationStatus.PENDING.value,
                            AuthorizationCacheInvalidationOutboxModel.available_at
                            <= effective_now,
                        ),
                        and_(
                            AuthorizationCacheInvalidationOutboxModel.status
                            == AuthorizationCacheInvalidationStatus.PROCESSING.value,
                            AuthorizationCacheInvalidationOutboxModel.processing_started_at
                            <= expired_before,
                        ),
                    )
                )
                .order_by(
                    AuthorizationCacheInvalidationOutboxModel.available_at,
                    AuthorizationCacheInvalidationOutboxModel.id,
                )
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for row in rows:
            row.status = AuthorizationCacheInvalidationStatus.PROCESSING.value
            row.attempt_count += 1
            row.processing_started_at = effective_now
            row.last_error_code = None
            row.version += 1
            row.updated_at = effective_now
        await self._session.flush()
        return tuple(_task(row) for row in rows)

    async def mark_completed(self, *, task_id: UUID, now: datetime) -> None:
        require_uuid7(task_id, field="outbox task_id")
        row = await self._session.scalar(
            select(AuthorizationCacheInvalidationOutboxModel)
            .where(AuthorizationCacheInvalidationOutboxModel.id == task_id)
            .with_for_update()
        )
        if row is None:
            raise RuntimeError("cache invalidation outbox task disappeared")
        if row.status == AuthorizationCacheInvalidationStatus.COMPLETED.value:
            return
        effective_now = _naive(now)
        row.status = AuthorizationCacheInvalidationStatus.COMPLETED.value
        row.completed_at = effective_now
        row.processing_started_at = None
        row.last_error_code = None
        row.version += 1
        row.updated_at = effective_now

    async def release_failed(
        self,
        *,
        task_id: UUID,
        available_at: datetime,
        error_code: str,
        now: datetime,
    ) -> None:
        require_uuid7(task_id, field="outbox task_id")
        if error_code != "cache_unavailable":
            raise ValueError("cache invalidation outbox error code is invalid")
        row = await self._session.scalar(
            select(AuthorizationCacheInvalidationOutboxModel)
            .where(AuthorizationCacheInvalidationOutboxModel.id == task_id)
            .with_for_update()
        )
        if row is None or row.status != AuthorizationCacheInvalidationStatus.PROCESSING.value:
            raise RuntimeError("cache invalidation outbox task is not processing")
        row.status = AuthorizationCacheInvalidationStatus.PENDING.value
        row.available_at = _naive(available_at)
        row.processing_started_at = None
        row.last_error_code = error_code
        row.version += 1
        row.updated_at = _naive(now)


def _task(model: AuthorizationCacheInvalidationOutboxModel) -> AuthorizationCacheInvalidationTask:
    return AuthorizationCacheInvalidationTask(
        id=model.id,
        tenant_id=model.tenant_id,
        membership_id=model.membership_id,
        authz_version=model.authz_version,
        idempotency_record_id=model.idempotency_record_id,
        status=AuthorizationCacheInvalidationStatus(model.status),
        attempt_count=model.attempt_count,
        available_at=_aware(model.available_at),
        processing_started_at=_aware_optional(model.processing_started_at),
    )


def _naive(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("outbox timestamp must be UTC-aware")
    return value.astimezone(UTC).replace(tzinfo=None)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _aware_optional(value: datetime | None) -> datetime | None:
    return None if value is None else _aware(value)
