from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lawyer_agent.application.audit import StructuredAuditEvent
from lawyer_agent.application.tenancy import TenantAuditEvent
from lawyer_agent.infrastructure.persistence.models import AuditEventModel

_MAX_QUERY_LIMIT = 100


@dataclass(frozen=True, slots=True)
class TenantAuditRow:
    """Read model exposing a safe allowlist of audit fields (no IP/UA hash)."""

    id: UUID
    actor_kind: str | None
    action: str
    result: str
    reason_code: str
    target_type: str | None
    target_id: UUID | None
    trace_id: str
    occurred_at: datetime


class AuditRepository:
    """Append-only audit adapter; mutation and deletion are deliberately absent."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_tenant(
        self,
        tenant_id: UUID,
        *,
        action_prefix: str | None = None,
        target_type: str | None = None,
        trace_id: str | None = None,
        limit: int = 20,
        before_id: UUID | None = None,
    ) -> tuple[TenantAuditRow, ...]:
        """List the tenant's audit rows newest first with stable cursor pagination."""
        statement = select(AuditEventModel).where(
            AuditEventModel.tenant_id == tenant_id
        )
        if action_prefix:
            statement = statement.where(
                AuditEventModel.action.startswith(action_prefix)
            )
        if target_type is not None:
            statement = statement.where(AuditEventModel.target_type == target_type)
        if trace_id is not None:
            statement = statement.where(AuditEventModel.trace_id == trace_id)
        if before_id is not None:
            cursor_occurred = await self._session.scalar(
                select(AuditEventModel.occurred_at).where(
                    AuditEventModel.tenant_id == tenant_id,
                    AuditEventModel.id == before_id,
                )
            )
            if cursor_occurred is None:
                raise ValueError("audit cursor is not in this tenant")
            statement = statement.where(
                or_(
                    AuditEventModel.occurred_at < cursor_occurred,
                    (AuditEventModel.occurred_at == cursor_occurred)
                    & (AuditEventModel.id < before_id),
                )
            )
        bounded = min(int(limit), _MAX_QUERY_LIMIT)
        rows = await self._session.scalars(
            statement.order_by(
                AuditEventModel.occurred_at.desc(), AuditEventModel.id.desc()
            ).limit(bounded)
        )
        return tuple(_row(model) for model in rows)

    async def append(self, event: TenantAuditEvent) -> None:
        if not isinstance(event, TenantAuditEvent):
            raise ValueError("audit event must be strongly typed")
        self._session.add(
            AuditEventModel(
                id=event.id,
                actor_user_id=event.actor_user_id,
                tenant_id=event.tenant_id,
                actor_membership_id=event.actor_membership_id,
                action=event.action,
                result=event.result,
                reason_code=event.reason_code,
                target_type=event.target_type,
                target_id=event.target_id,
                trace_id=event.trace_id,
                client_ip_hash=event.client_ip_hash,
                user_agent_hash=event.user_agent_hash,
                metadata_json=None,
                occurred_at=_naive(event.occurred_at),
            )
        )

    async def append_structured(self, event: StructuredAuditEvent) -> None:
        if not isinstance(event, StructuredAuditEvent):
            raise ValueError("structured audit event must be strongly typed")
        self._session.add(
            AuditEventModel(
                id=event.id,
                actor_user_id=event.actor_user_id,
                tenant_id=event.tenant_id,
                actor_membership_id=event.actor_membership_id,
                actor_kind=event.actor_kind.value,
                on_behalf_of_user_id=event.on_behalf_of_user_id,
                on_behalf_of_membership_id=event.on_behalf_of_membership_id,
                target_job_id=event.target_job_id,
                attempt_id=event.attempt_id,
                message_id=event.message_id,
                rollout_feature_code=event.rollout_feature_code,
                rollout_generation=event.rollout_generation,
                action=event.action,
                result=event.result,
                reason_code=event.reason_code,
                target_type=event.target_type,
                target_id=event.target_id,
                trace_id=event.trace_id,
                client_ip_hash=event.client_ip_hash,
                user_agent_hash=event.user_agent_hash,
                metadata_json=None,
                occurred_at=_naive(event.occurred_at),
            )
        )


def _row(model: AuditEventModel) -> TenantAuditRow:
    return TenantAuditRow(
        id=model.id,
        actor_kind=model.actor_kind,
        action=model.action,
        result=model.result,
        reason_code=model.reason_code,
        target_type=model.target_type,
        target_id=model.target_id,
        trace_id=model.trace_id,
        occurred_at=_aware(model.occurred_at),
    )


def _naive(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("database timestamp must be UTC-aware")
    return value.astimezone(UTC).replace(tzinfo=None)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(UTC)
    return value.replace(tzinfo=UTC)
