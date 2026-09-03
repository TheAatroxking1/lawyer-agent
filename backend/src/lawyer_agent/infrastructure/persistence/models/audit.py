from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BINARY,
    JSON,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from lawyer_agent.infrastructure.persistence.base import Base
from lawyer_agent.infrastructure.persistence.models._mixins import TimestampMixin, VersionMixin
from lawyer_agent.infrastructure.persistence.types import UTC_DATETIME, UuidBinary

_STRUCTURED_AUDIT_CHECK = (
    "(actor_kind IS NULL "
    "AND on_behalf_of_user_id IS NULL AND on_behalf_of_membership_id IS NULL "
    "AND target_job_id IS NULL AND attempt_id IS NULL AND message_id IS NULL "
    "AND rollout_feature_code IS NULL AND rollout_generation IS NULL) "
    "OR (actor_kind IS NOT NULL "
    "AND (actor_membership_id IS NULL OR (tenant_id IS NOT NULL AND actor_user_id IS NOT NULL)) "
    "AND ((on_behalf_of_user_id IS NULL AND on_behalf_of_membership_id IS NULL) "
    "OR (on_behalf_of_user_id IS NOT NULL AND on_behalf_of_membership_id IS NOT NULL)) "
    "AND (on_behalf_of_user_id IS NULL OR tenant_id IS NOT NULL) "
    "AND (attempt_id IS NULL OR (target_job_id IS NOT NULL AND tenant_id IS NOT NULL)) "
    "AND (message_id IS NULL OR (target_job_id IS NOT NULL AND tenant_id IS NOT NULL)) "
    "AND (target_job_id IS NULL OR tenant_id IS NOT NULL) "
    "AND ((rollout_feature_code IS NULL AND rollout_generation IS NULL) "
    "OR (rollout_feature_code IS NOT NULL AND rollout_generation IS NOT NULL)) "
    "AND ("
    "(actor_kind = 'tenant_user' AND tenant_id IS NOT NULL AND actor_user_id IS NOT NULL "
    "AND actor_membership_id IS NOT NULL AND on_behalf_of_user_id IS NULL "
    "AND on_behalf_of_membership_id IS NULL AND rollout_feature_code IS NULL "
    "AND rollout_generation IS NULL) "
    "OR (actor_kind = 'system_worker' AND tenant_id IS NOT NULL "
    "AND on_behalf_of_user_id IS NOT NULL AND on_behalf_of_membership_id IS NOT NULL "
    "AND target_job_id IS NOT NULL AND actor_user_id IS NULL AND actor_membership_id IS NULL "
    "AND rollout_feature_code IS NULL AND rollout_generation IS NULL) "
    "OR (actor_kind = 'system_publisher' AND tenant_id IS NOT NULL "
    "AND target_job_id IS NOT NULL AND message_id IS NOT NULL AND actor_user_id IS NULL "
    "AND actor_membership_id IS NULL AND rollout_feature_code IS NULL "
    "AND rollout_generation IS NULL) "
    "OR (actor_kind = 'system_job_maintenance' AND tenant_id IS NOT NULL "
    "AND target_job_id IS NOT NULL AND actor_user_id IS NULL AND actor_membership_id IS NULL "
    "AND rollout_feature_code IS NULL AND rollout_generation IS NULL) "
    "OR (actor_kind = 'system_feature_rollout' AND tenant_id IS NOT NULL "
    "AND rollout_feature_code IS NOT NULL AND rollout_generation IS NOT NULL "
    "AND actor_user_id IS NULL AND actor_membership_id IS NULL "
    "AND on_behalf_of_user_id IS NULL AND on_behalf_of_membership_id IS NULL "
    "AND target_job_id IS NULL AND attempt_id IS NULL AND message_id IS NULL) "
    "OR (actor_kind = 'system_global_feature_rollout' AND tenant_id IS NULL "
    "AND actor_user_id IS NULL AND actor_membership_id IS NULL "
    "AND on_behalf_of_user_id IS NULL AND on_behalf_of_membership_id IS NULL "
    "AND target_job_id IS NULL AND attempt_id IS NULL AND message_id IS NULL "
    "AND rollout_feature_code IS NOT NULL AND rollout_generation IS NOT NULL) "
    "OR (actor_kind = 'global_user' AND tenant_id IS NULL AND actor_user_id IS NOT NULL "
    "AND actor_membership_id IS NULL AND on_behalf_of_user_id IS NULL "
    "AND on_behalf_of_membership_id IS NULL AND target_job_id IS NULL "
    "AND attempt_id IS NULL AND message_id IS NULL AND rollout_feature_code IS NULL "
    "AND rollout_generation IS NULL) "
    "OR (actor_kind = 'anonymous' AND tenant_id IS NULL AND actor_user_id IS NULL "
    "AND actor_membership_id IS NULL AND on_behalf_of_user_id IS NULL "
    "AND on_behalf_of_membership_id IS NULL AND target_job_id IS NULL "
    "AND attempt_id IS NULL AND message_id IS NULL AND rollout_feature_code IS NULL "
    "AND rollout_generation IS NULL) "
    "OR (actor_kind = 'system_identity_bootstrap' AND tenant_id IS NULL "
    "AND actor_user_id IS NULL AND actor_membership_id IS NULL "
    "AND on_behalf_of_user_id IS NULL AND on_behalf_of_membership_id IS NULL "
    "AND target_job_id IS NULL AND attempt_id IS NULL AND message_id IS NULL "
    "AND rollout_feature_code IS NULL AND rollout_generation IS NULL) "
    "OR (actor_kind = 'system_global_maintenance' AND tenant_id IS NULL "
    "AND actor_user_id IS NULL AND actor_membership_id IS NULL "
    "AND on_behalf_of_user_id IS NULL AND on_behalf_of_membership_id IS NULL "
    "AND target_job_id IS NULL AND attempt_id IS NULL AND message_id IS NULL "
    "AND rollout_feature_code IS NULL AND rollout_generation IS NULL) "
    "OR (actor_kind = 'platform_operator' AND actor_user_id IS NOT NULL "
    "AND actor_membership_id IS NULL AND on_behalf_of_user_id IS NULL "
    "AND on_behalf_of_membership_id IS NULL AND rollout_feature_code IS NULL "
    "AND rollout_generation IS NULL) "
    "))"
)


class AuditEventModel(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint("result IN ('success','failure','denied')", name="audit_event_result"),
        CheckConstraint(_STRUCTURED_AUDIT_CHECK, name="audit_structured_actor"),
        ForeignKeyConstraint(
            ["tenant_id", "actor_membership_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "actor_user_id", "actor_membership_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.user_id", "tenant_memberships.id"],
            name="fk_audit_events_tenant_actor_membership",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "on_behalf_of_user_id", "on_behalf_of_membership_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.user_id", "tenant_memberships.id"],
            name="fk_audit_events_tenant_on_behalf_membership",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "target_job_id"],
            ["ai_jobs.tenant_id", "ai_jobs.id"],
            name="fk_audit_events_tenant_target_job",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "target_job_id", "attempt_id"],
            ["ai_job_attempts.tenant_id", "ai_job_attempts.job_id", "ai_job_attempts.id"],
            name="fk_audit_events_tenant_target_attempt",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "target_job_id", "message_id"],
            ["ai_job_outbox.tenant_id", "ai_job_outbox.job_id", "ai_job_outbox.message_id"],
            name="fk_audit_events_tenant_target_message",
        ),
        Index("ix_audit_events_tenant_occurred", "tenant_id", "occurred_at"),
        Index("ix_audit_events_actor_user_id", "actor_user_id"),
        Index("ix_audit_events_trace_id", "trace_id"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    actor_user_id: Mapped[UUID | None] = mapped_column(UuidBinary(), ForeignKey("users.id"))
    tenant_id: Mapped[UUID | None] = mapped_column(UuidBinary(), ForeignKey("tenants.id"))
    actor_membership_id: Mapped[UUID | None] = mapped_column(UuidBinary())
    actor_kind: Mapped[str | None] = mapped_column(String(64))
    on_behalf_of_user_id: Mapped[UUID | None] = mapped_column(UuidBinary(), ForeignKey("users.id"))
    on_behalf_of_membership_id: Mapped[UUID | None] = mapped_column(UuidBinary())
    target_job_id: Mapped[UUID | None] = mapped_column(UuidBinary())
    attempt_id: Mapped[UUID | None] = mapped_column(UuidBinary())
    message_id: Mapped[UUID | None] = mapped_column(UuidBinary())
    rollout_feature_code: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("permission_feature_rollouts.feature_code")
    )
    rollout_generation: Mapped[int | None] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(64))
    target_id: Mapped[UUID | None] = mapped_column(UuidBinary())
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    client_ip_hash: Mapped[bytes | None] = mapped_column(BINARY(32))
    user_agent_hash: Mapped[bytes | None] = mapped_column(BINARY(32))
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    occurred_at: Mapped[datetime] = mapped_column(UTC_DATETIME, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTC_DATETIME, nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )


class IdempotencyRecordModel(VersionMixin, TimestampMixin, Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        CheckConstraint(
            "scope_type IN ('user','membership','platform')", name="idempotency_scope_type"
        ),
        CheckConstraint("status IN ('reserved','completed','failed')", name="idempotency_status"),
        CheckConstraint(
            "(scope_type = 'membership' AND tenant_id IS NOT NULL) OR "
            "(scope_type IN ('user','platform') AND tenant_id IS NULL)",
            name="idempotency_tenant_scope",
        ),
        UniqueConstraint("scope_type", "scope_id", "operation", "key_hash"),
        UniqueConstraint(
            "tenant_id",
            "scope_type",
            "scope_id",
            "operation",
            "key_hash",
            name="uq_idempotency_records_tenant_scope",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_idempotency_records_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "scope_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id"],
            name="fk_idempotency_records_tenant_id_membership",
        ),
        Index("ix_idempotency_records_expires_at", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    tenant_id: Mapped[UUID | None] = mapped_column(UuidBinary(), ForeignKey("tenants.id"))
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    key_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    request_fingerprint: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="reserved")
    result_type: Mapped[str | None] = mapped_column(String(64))
    result_id: Mapped[UUID | None] = mapped_column(UuidBinary())
    expires_at: Mapped[datetime] = mapped_column(UTC_DATETIME, nullable=False)
