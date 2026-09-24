from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BINARY,
    JSON,
    BigInteger,
    CheckConstraint,
    Computed,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from lawyer_agent.infrastructure.persistence.base import Base
from lawyer_agent.infrastructure.persistence.models._mixins import TimestampMixin, VersionMixin
from lawyer_agent.infrastructure.persistence.types import UTC_DATETIME, UuidBinary

AI_JOB_STATUSES = (
    "queued",
    "running",
    "retry_scheduled",
    "cancel_requested",
    "succeeded",
    "failed",
    "cancelled",
)
ATTEMPT_STATUSES = (
    "claimed",
    "running",
    "succeeded",
    "retryable_failed",
    "permanent_failed",
    "cancelled",
    "lease_expired",
)
OUTBOX_STATUSES = ("pending", "publishing", "published", "blocked", "superseded")
INBOX_STATUSES = ("accepted", "processing", "completed", "rejected")
EFFECT_STATUSES = ("started", "applied", "failed")
FEATURE_PHASES = ("catalog_only", "activating", "activated", "deactivating")
TENANT_FEATURE_PHASES = ("catalog_only", "activating", "activated", "deactivating", "failed")


def _status_check(column: str, statuses: tuple[str, ...], name: str) -> CheckConstraint:
    quoted = ", ".join(f"'{value}'" for value in statuses)
    return CheckConstraint(f"{column} IN ({quoted})", name=name)


class PermissionFeatureRolloutModel(VersionMixin, TimestampMixin, Base):
    __tablename__ = "permission_feature_rollouts"
    __table_args__ = (
        _status_check("phase", FEATURE_PHASES, "permission_feature_phase"),
        CheckConstraint(
            "rollout_generation BETWEEN 0 AND 9223372036854775807",
            name="permission_feature_generation",
        ),
        CheckConstraint(
            "old_instances_drained_at IS NULL OR old_instances_drained_by IS NOT NULL",
            name="permission_feature_drain_evidence",
        ),
    )

    feature_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    phase: Mapped[str] = mapped_column(String(32), nullable=False, server_default="catalog_only")
    manifest_version: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_digest: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    rollout_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    old_instances_drained_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    old_instances_drained_by: Mapped[str | None] = mapped_column(String(128))
    drain_evidence_ref: Mapped[str | None] = mapped_column(String(255))
    activated_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)


class PermissionFeatureTenantStateModel(VersionMixin, TimestampMixin, Base):
    __tablename__ = "permission_feature_tenant_states"
    __table_args__ = (
        _status_check("phase", TENANT_FEATURE_PHASES, "tenant_feature_phase"),
        UniqueConstraint(
            "feature_code", "tenant_id", name="uq_permission_feature_tenant_states_feature_code"
        ),
        CheckConstraint(
            "target_rollout_generation >= applied_rollout_generation",
            name="tenant_feature_gen_order",
        ),
        CheckConstraint(
            "target_rollout_generation BETWEEN 0 AND 9223372036854775807",
            name="tenant_feature_target_gen",
        ),
        CheckConstraint(
            "applied_rollout_generation BETWEEN 0 AND 9223372036854775807",
            name="tenant_feature_applied_gen",
        ),
        CheckConstraint(
            "(claim_owner IS NULL AND claim_token IS NULL "
            "AND claim_fence IS NULL AND claim_expires_at IS NULL) OR "
            "(claim_owner IS NOT NULL AND claim_token IS NOT NULL "
            "AND claim_fence IS NOT NULL AND claim_expires_at IS NOT NULL)",
            name="tenant_feature_claim_ok",
        ),
        ForeignKeyConstraint(
            ["feature_code"],
            ["permission_feature_rollouts.feature_code"],
            name="fk_tenant_feature_states_feature_code",
        ),
        Index("ix_tenant_feature_states_phase", "feature_code", "phase"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    feature_code: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[UUID] = mapped_column(UuidBinary(), ForeignKey("tenants.id"), nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False, server_default="catalog_only")
    target_manifest_version: Mapped[str] = mapped_column(String(64), nullable=False)
    applied_manifest_version: Mapped[str] = mapped_column(String(64), nullable=False)
    target_rollout_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    applied_rollout_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    claim_owner: Mapped[str | None] = mapped_column(String(128))
    claim_token: Mapped[UUID | None] = mapped_column(UuidBinary())
    claim_fence: Mapped[int | None] = mapped_column(Integer)
    claim_expires_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    activated_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)


class AIJobModel(VersionMixin, TimestampMixin, Base):
    __tablename__ = "ai_jobs"
    __table_args__ = (
        _status_check("status", AI_JOB_STATUSES, "ai_job_status"),
        CheckConstraint("visibility = 'owner_only'", name="ai_job_visibility"),
        CheckConstraint("max_attempts BETWEEN 1 AND 10", name="ai_job_max_attempts"),
        CheckConstraint(
            "current_attempt_no BETWEEN 0 AND max_attempts",
            name="ai_job_current_attempt_no",
        ),
        CheckConstraint(
            "risk_class = 'synthetic' AND release_state = 'non_publishable'",
            name="ai_job_risk_release",
        ),
        CheckConstraint(
            "(lease_owner IS NULL AND lease_token IS NULL "
            "AND lease_fence IS NULL AND lease_expires_at IS NULL "
            "AND status NOT IN ('running','cancel_requested')) OR "
            "(lease_owner IS NOT NULL AND lease_token IS NOT NULL "
            "AND lease_fence IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND status IN ('running','cancel_requested'))",
            name="ai_job_lease_consistency",
        ),
        CheckConstraint(
            "(status IN ('succeeded','failed','cancelled') "
            "AND completed_at IS NOT NULL AND next_attempt_at IS NULL) OR "
            "(status NOT IN ('succeeded','failed','cancelled') "
            "AND completed_at IS NULL)",
            name="ai_job_terminal_consistency",
        ),
        CheckConstraint(
            "submitted_at < expires_at AND expires_at <= DATE_ADD(submitted_at, INTERVAL 24 HOUR)",
            name="ai_job_lifetime",
        ),
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "created_by_membership_id", "created_by_user_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id", "tenant_memberships.user_id"],
            name="fk_ai_jobs_tenant_membership_user",
        ),
        ForeignKeyConstraint(
            ["created_by_user_id", "created_by_session_id"],
            ["auth_sessions.user_id", "auth_sessions.id"],
            name="fk_ai_jobs_user_session_actor",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "idempotency_record_id"],
            ["idempotency_records.tenant_id", "idempotency_records.id"],
            name="fk_ai_jobs_tenant_idempotency",
        ),
        Index("ix_ai_jobs_tenant_status", "tenant_id", "status"),
        Index("ix_ai_jobs_tenant_lease_expiry", "tenant_id", "status", "lease_expires_at"),
        Index("ix_ai_jobs_expires_at", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(UuidBinary(), ForeignKey("tenants.id"), nullable=False)
    handler_code: Mapped[str] = mapped_column(String(64), nullable=False)
    handler_version: Mapped[str] = mapped_column(String(64), nullable=False)
    input_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    input_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    input_fingerprint: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    created_by_user_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    created_by_membership_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    created_by_session_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    actor_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    policy_version_at_submit: Mapped[str] = mapped_column(String(64), nullable=False)
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, server_default="owner_only")
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="queued")
    current_attempt_no: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("4"))
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_token: Mapped[UUID | None] = mapped_column(UuidBinary())
    lease_fence: Mapped[int | None] = mapped_column(Integer)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    cancel_requested_by_user_id: Mapped[UUID | None] = mapped_column(UuidBinary())
    cancel_reason_code: Mapped[str | None] = mapped_column(String(64))
    risk_class: Mapped[str] = mapped_column(String(32), nullable=False, server_default="synthetic")
    release_state: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="non_publishable"
    )
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    failure_class: Mapped[str | None] = mapped_column(String(64))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    idempotency_record_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTC_DATETIME, nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(UTC_DATETIME, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    completed_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)


class AIJobExecutionGrantModel(VersionMixin, TimestampMixin, Base):
    __tablename__ = "ai_job_execution_grants"
    __table_args__ = (
        CheckConstraint("permission_code = 'ai_job.create'", name="ai_job_grant_permission"),
        UniqueConstraint("tenant_id", "job_id"),
        ForeignKeyConstraint(["tenant_id", "job_id"], ["ai_jobs.tenant_id", "ai_jobs.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "membership_id", "user_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id", "tenant_memberships.user_id"],
            name="fk_ai_job_grants_tenant_membership_user",
        ),
        CheckConstraint(
            "(revoked_at IS NULL AND revocation_reason_code IS NULL) OR "
            "(revoked_at IS NOT NULL AND revocation_reason_code IS NOT NULL)",
            name="ai_job_grant_revocation_consistency",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(UuidBinary(), ForeignKey("tenants.id"), nullable=False)
    job_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    user_id: Mapped[UUID] = mapped_column(UuidBinary(), ForeignKey("users.id"), nullable=False)
    membership_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    permission_code: Mapped[str] = mapped_column(String(128), nullable=False)
    auth_version_at_submit: Mapped[int] = mapped_column(Integer, nullable=False)
    authz_version_at_submit: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    job_scope_manifest_version: Mapped[str] = mapped_column(String(64), nullable=False)
    job_scope_code: Mapped[str] = mapped_column(String(32), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(UTC_DATETIME, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    revocation_reason_code: Mapped[str | None] = mapped_column(String(64))


class AIJobAccessGrantModel(VersionMixin, TimestampMixin, Base):
    __tablename__ = "ai_job_access_grants"
    __table_args__ = (
        CheckConstraint("access_level = 'read'", name="ai_job_access_level"),
        UniqueConstraint(
            "tenant_id",
            "job_id",
            "membership_id",
            "generation",
            name="uq_ai_job_access_grants_tenant_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "job_id",
            "membership_id",
            "generation",
            "id",
            name="uq_ai_job_access_grants_tenant_id_gen_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "job_id",
            "membership_id",
            "active_marker",
            name="uq_ai_job_access_grants_active",
        ),
        CheckConstraint(
            "(supersedes_generation IS NULL AND supersedes_grant_id IS NULL) OR "
            "(supersedes_generation IS NOT NULL AND supersedes_grant_id IS NOT NULL)",
            name="ai_job_access_supersedes_consistency",
        ),
        ForeignKeyConstraint(["tenant_id", "job_id"], ["ai_jobs.tenant_id", "ai_jobs.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "membership_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id"],
            name="fk_ai_job_access_grants_tenant_membership",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "granted_by_membership_id", "granted_by_user_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id", "tenant_memberships.user_id"],
            name="fk_ai_job_access_grants_granted_by",
        ),
        ForeignKeyConstraint(
            [
                "tenant_id",
                "job_id",
                "membership_id",
                "supersedes_generation",
                "supersedes_grant_id",
            ],
            [
                "ai_job_access_grants.tenant_id",
                "ai_job_access_grants.job_id",
                "ai_job_access_grants.membership_id",
                "ai_job_access_grants.generation",
                "ai_job_access_grants.id",
            ],
            name="fk_ai_job_access_grants_supersedes",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(UuidBinary(), ForeignKey("tenants.id"), nullable=False)
    job_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    membership_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    access_level: Mapped[str] = mapped_column(String(16), nullable=False, server_default="read")
    generation: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    supersedes_generation: Mapped[int | None] = mapped_column(Integer)
    supersedes_grant_id: Mapped[UUID | None] = mapped_column(UuidBinary())
    granted_by_user_id: Mapped[UUID] = mapped_column(
        UuidBinary(), ForeignKey("users.id"), nullable=False
    )
    granted_by_membership_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(UTC_DATETIME, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    active_marker: Mapped[int | None] = mapped_column(
        Integer, Computed("CASE WHEN revoked_at IS NULL THEN 1 ELSE NULL END")
    )


class AIJobAttemptModel(VersionMixin, TimestampMixin, Base):
    __tablename__ = "ai_job_attempts"
    __table_args__ = (
        _status_check("status", ATTEMPT_STATUSES, "ai_job_attempt_status"),
        UniqueConstraint("tenant_id", "job_id", "id", name="uq_ai_job_attempts_tenant_id"),
        UniqueConstraint(
            "tenant_id",
            "job_id",
            "id",
            "lease_fence",
            name="uq_ai_job_attempts_tenant_id_fence",
        ),
        UniqueConstraint(
            "tenant_id",
            "job_id",
            "attempt_no",
            name="uq_ai_job_attempts_tenant_id_attempt",
        ),
        UniqueConstraint(
            "tenant_id",
            "trigger_message_id",
            name="uq_ai_job_attempts_tenant_id_trigger",
        ),
        ForeignKeyConstraint(["tenant_id", "job_id"], ["ai_jobs.tenant_id", "ai_jobs.id"]),
        CheckConstraint("attempt_no BETWEEN 1 AND 10", name="ai_job_attempt_no"),
        Index("ix_ai_job_attempts_tenant_job", "tenant_id", "job_id"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(UuidBinary(), ForeignKey("tenants.id"), nullable=False)
    job_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="claimed")
    trigger_message_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    worker_instance_ref: Mapped[str | None] = mapped_column(String(128))
    lease_token: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    lease_fence: Mapped[int] = mapped_column(Integer, nullable=False)
    handler_code: Mapped[str] = mapped_column(String(64), nullable=False)
    handler_version: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTC_DATETIME, nullable=False)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    finished_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    failure_class: Mapped[str | None] = mapped_column(String(64))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    retry_delay_seconds: Mapped[int | None] = mapped_column(Integer)
    cancellation_seen_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)


class AIJobOutboxModel(VersionMixin, TimestampMixin, Base):
    __tablename__ = "ai_job_outbox"
    __table_args__ = (
        _status_check("status", OUTBOX_STATUSES, "ai_job_outbox_status"),
        UniqueConstraint("tenant_id", "id", name="uq_ai_job_outbox_tenant_id"),
        UniqueConstraint(
            "tenant_id",
            "job_id",
            "dispatch_generation",
            "envelope_generation",
            name="uq_ai_job_outbox_tenant_id_dispatch",
        ),
        UniqueConstraint("tenant_id", "message_id", name="uq_ai_job_outbox_tenant_id_message"),
        UniqueConstraint(
            "tenant_id",
            "job_id",
            "message_id",
            name="uq_ai_job_outbox_tenant_id_job_message",
        ),
        UniqueConstraint(
            "tenant_id",
            "job_id",
            "recovery_source_fence",
            "envelope_generation",
            name="uq_ai_job_outbox_tenant_id_recovery",
        ),
        CheckConstraint(
            "envelope_generation BETWEEN 1 AND max_envelope_generations "
            "AND max_envelope_generations BETWEEN 1 AND 4",
            name="ai_job_outbox_envelope_generation",
        ),
        CheckConstraint(
            "(source_attempt_id IS NULL AND recovery_source_fence IS NULL) OR "
            "(source_attempt_id IS NOT NULL AND recovery_source_fence IS NOT NULL)",
            name="ai_job_outbox_recovery_consistency",
        ),
        CheckConstraint(
            "publish_attempt_count BETWEEN 0 AND 2147483647",
            name="ai_job_outbox_publish_attempts",
        ),
        CheckConstraint(
            "(claim_owner IS NULL AND claim_token IS NULL "
            "AND claim_fence IS NULL AND claim_expires_at IS NULL) OR "
            "(claim_owner IS NOT NULL AND claim_token IS NOT NULL "
            "AND claim_fence IS NOT NULL AND claim_expires_at IS NOT NULL)",
            name="ai_job_outbox_claim_consistency",
        ),
        ForeignKeyConstraint(["tenant_id", "job_id"], ["ai_jobs.tenant_id", "ai_jobs.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "job_id", "source_attempt_id", "recovery_source_fence"],
            [
                "ai_job_attempts.tenant_id",
                "ai_job_attempts.job_id",
                "ai_job_attempts.id",
                "ai_job_attempts.lease_fence",
            ],
            name="fk_ai_job_outbox_recovery_attempt",
        ),
        Index("ix_ai_job_outbox_dispatch", "status", "available_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(UuidBinary(), ForeignKey("tenants.id"), nullable=False)
    job_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    dispatch_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    envelope_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    max_envelope_generations: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("4")
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    routing_key: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    message_id: Mapped[UUID | None] = mapped_column(UuidBinary())
    correlation_id: Mapped[UUID | None] = mapped_column(UuidBinary())
    issued_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    nonce: Mapped[str | None] = mapped_column(String(64))
    signature: Mapped[str | None] = mapped_column(String(256))
    envelope_digest: Mapped[bytes | None] = mapped_column(BINARY(32))
    recovery_source_fence: Mapped[int | None] = mapped_column(Integer)
    source_attempt_id: Mapped[UUID | None] = mapped_column(UuidBinary())
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="pending")
    available_at: Mapped[datetime] = mapped_column(UTC_DATETIME, nullable=False)
    publish_attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    max_publish_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("8")
    )
    claim_owner: Mapped[str | None] = mapped_column(String(128))
    claim_token: Mapped[UUID | None] = mapped_column(UuidBinary())
    claim_fence: Mapped[int | None] = mapped_column(Integer)
    claim_expires_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    published_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    confirmed_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    last_error_code: Mapped[str | None] = mapped_column(String(64))


class AIJobInboxModel(VersionMixin, TimestampMixin, Base):
    __tablename__ = "ai_job_inbox"
    __table_args__ = (
        _status_check("status", INBOX_STATUSES, "ai_job_inbox_status"),
        UniqueConstraint("tenant_id", "message_id", name="uq_ai_job_inbox_tenant_id_message"),
        UniqueConstraint("tenant_id", "nonce_digest", name="uq_ai_job_inbox_tenant_id_nonce"),
        CheckConstraint(
            "(handling_attempt_id IS NULL AND handling_fence IS NULL) OR "
            "(handling_attempt_id IS NOT NULL AND handling_fence IS NOT NULL)",
            name="ai_job_inbox_handling_consistency",
        ),
        ForeignKeyConstraint(["tenant_id", "job_id"], ["ai_jobs.tenant_id", "ai_jobs.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "job_id", "handling_attempt_id", "handling_fence"],
            [
                "ai_job_attempts.tenant_id",
                "ai_job_attempts.job_id",
                "ai_job_attempts.id",
                "ai_job_attempts.lease_fence",
            ],
            name="fk_ai_job_inbox_handling_attempt",
        ),
        Index("ix_ai_job_inbox_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(UuidBinary(), ForeignKey("tenants.id"), nullable=False)
    message_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    job_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    envelope_digest: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    nonce_digest: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="accepted")
    first_received_at: Mapped[datetime] = mapped_column(UTC_DATETIME, nullable=False)
    last_received_at: Mapped[datetime] = mapped_column(UTC_DATETIME, nullable=False)
    delivery_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    handling_attempt_id: Mapped[UUID | None] = mapped_column(UuidBinary())
    handling_fence: Mapped[int | None] = mapped_column(Integer)
    completed_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    rejection_code: Mapped[str | None] = mapped_column(String(64))


class AIJobStepEffectModel(VersionMixin, TimestampMixin, Base):
    __tablename__ = "ai_job_step_effects"
    __table_args__ = (
        _status_check("status", EFFECT_STATUSES, "ai_job_effect_status"),
        UniqueConstraint("tenant_id", "job_id", "step_code", "effect_key"),
        ForeignKeyConstraint(["tenant_id", "job_id"], ["ai_jobs.tenant_id", "ai_jobs.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "job_id", "attempt_id", "lease_fence"],
            [
                "ai_job_attempts.tenant_id",
                "ai_job_attempts.job_id",
                "ai_job_attempts.id",
                "ai_job_attempts.lease_fence",
            ],
            name="fk_ai_job_effects_attempt_fence",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(UuidBinary(), ForeignKey("tenants.id"), nullable=False)
    job_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    attempt_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    step_code: Mapped[str] = mapped_column(String(64), nullable=False)
    effect_key: Mapped[str] = mapped_column(String(128), nullable=False)
    input_digest: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="started")
    lease_fence: Mapped[int] = mapped_column(Integer, nullable=False)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    failure_code: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(UTC_DATETIME, nullable=False)
    applied_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)


class MessageSecurityRejectionModel(Base):
    __tablename__ = "message_security_rejections"
    __table_args__ = (
        UniqueConstraint("observability_ref", "rejection_code", "retention_bucket"),
        Index("ix_message_security_rejections_retention", "retention_bucket", "received_at"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    observability_ref: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    rejection_code: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version_hint: Mapped[int | None] = mapped_column(Integer)
    kid_hint: Mapped[str | None] = mapped_column(String(64))
    source_channel: Mapped[str] = mapped_column(String(32), nullable=False)
    received_at: Mapped[datetime] = mapped_column(UTC_DATETIME, nullable=False)
    retention_bucket: Mapped[str] = mapped_column(String(16), nullable=False)
