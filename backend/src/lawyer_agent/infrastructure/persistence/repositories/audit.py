from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from lawyer_agent.application.audit import StructuredAuditEvent
from lawyer_agent.application.tenancy import TenantAuditEvent
from lawyer_agent.infrastructure.persistence.models import AuditEventModel


class AuditRepository:
    """Append-only audit adapter; mutation and deletion are deliberately absent."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

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


def _naive(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("database timestamp must be UTC-aware")
    return value.astimezone(UTC).replace(tzinfo=None)
