"""Add the tenant durable AI job runtime schema.

Revision ID: 20260903_06
Revises: 20260902_05
Create Date: 2026-09-03
"""

from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op
from lawyer_agent.domain.ai_jobs import (
    AI_JOB_BASE_MANIFEST_VERSION,
    ai_job_manifest_digest,
)
from lawyer_agent.domain.common import new_uuid7

revision: str = "20260903_06"
down_revision: str | Sequence[str] | None = "20260902_05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE_OPTIONS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}
_DATETIME = mysql.DATETIME(fsp=6)

_AI_JOB_CREATE = UUID("01a05a44-fc00-7000-8000-0000000000c1")
_AI_JOB_READ = UUID("01a05a44-fc01-7000-8000-0000000000c2")
_AI_JOB_CANCEL = UUID("01a05a44-fc02-7000-8000-0000000000c3")

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


def _id(name: str = "id", *, nullable: bool = False) -> sa.Column[bytes]:
    return sa.Column(name, sa.BINARY(16), nullable=nullable)


def _timestamps(*, versioned: bool = False) -> list[sa.Column[object]]:
    columns: list[sa.Column[object]] = []
    if versioned:
        columns.append(
            sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False)
        )
    columns.extend(
        [
            sa.Column(
                "created_at",
                _DATETIME,
                server_default=sa.text("CURRENT_TIMESTAMP(6)"),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                _DATETIME,
                server_default=sa.text("CURRENT_TIMESTAMP(6)"),
                nullable=False,
            ),
        ]
    )
    return columns


def upgrade() -> None:
    _add_candidate_keys()
    _create_permission_feature_tables()
    _create_ai_job_tables()
    _expand_audit_events()
    _seed_ai_job_permissions_and_feature()


def downgrade() -> None:
    op.execute(
        "DELETE FROM permission_feature_tenant_states WHERE feature_code = 'ai_job_runtime_v1'"
    )
    op.execute("DELETE FROM permission_feature_rollouts WHERE feature_code = 'ai_job_runtime_v1'")
    op.execute(
        "DELETE FROM permission_catalog "
        "WHERE code IN ('ai_job.create','ai_job.read','ai_job.cancel')"
    )
    op.execute(
        sa.text("ALTER TABLE audit_events DROP CHECK ck_audit_events_audit_structured_actor")
    )
    op.drop_constraint("fk_audit_events_tenant_target_message", "audit_events", type_="foreignkey")
    op.drop_constraint("fk_audit_events_tenant_target_attempt", "audit_events", type_="foreignkey")
    op.drop_constraint("fk_audit_events_tenant_target_job", "audit_events", type_="foreignkey")
    op.drop_constraint(
        "fk_audit_events_tenant_on_behalf_membership", "audit_events", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_audit_events_tenant_actor_membership", "audit_events", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_audit_events_rollout_feature_code_permission_feature_rollouts",
        "audit_events",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_audit_events_on_behalf_of_user_id_users", "audit_events", type_="foreignkey"
    )
    op.drop_column("audit_events", "rollout_generation")
    op.drop_column("audit_events", "rollout_feature_code")
    op.drop_column("audit_events", "message_id")
    op.drop_column("audit_events", "attempt_id")
    op.drop_column("audit_events", "target_job_id")
    op.drop_column("audit_events", "on_behalf_of_membership_id")
    op.drop_column("audit_events", "on_behalf_of_user_id")
    op.drop_column("audit_events", "actor_kind")
    op.drop_table("message_security_rejections")
    op.drop_table("ai_job_step_effects")
    op.drop_table("ai_job_inbox")
    op.drop_table("ai_job_outbox")
    op.drop_table("ai_job_attempts")
    op.drop_table("ai_job_access_grants")
    op.drop_table("ai_job_execution_grants")
    op.drop_table("ai_jobs")
    op.drop_table("permission_feature_tenant_states")
    op.drop_table("permission_feature_rollouts")
    op.drop_constraint(
        "uq_auth_sessions_tenant_id_id_user_membership", "auth_sessions", type_="unique"
    )
    op.drop_constraint(
        "uq_tenant_memberships_tenant_id_id_user", "tenant_memberships", type_="unique"
    )


def _add_candidate_keys() -> None:
    op.create_unique_constraint(
        "uq_tenant_memberships_tenant_id_id_user",
        "tenant_memberships",
        ["tenant_id", "id", "user_id"],
    )
    op.create_unique_constraint(
        "uq_auth_sessions_tenant_id_id_user_membership",
        "auth_sessions",
        ["tenant_id", "id", "user_id", "membership_id"],
    )


def _create_permission_feature_tables() -> None:
    op.create_table(
        "permission_feature_rollouts",
        sa.Column("feature_code", sa.String(64), nullable=False),
        sa.Column("phase", sa.String(32), server_default="catalog_only", nullable=False),
        sa.Column("manifest_version", sa.String(64), nullable=False),
        sa.Column("manifest_digest", sa.BINARY(32), nullable=False),
        sa.Column(
            "rollout_generation", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("old_instances_drained_at", _DATETIME, nullable=True),
        sa.Column("old_instances_drained_by", sa.String(128), nullable=True),
        sa.Column("drain_evidence_ref", sa.String(255), nullable=True),
        sa.Column("activated_at", _DATETIME, nullable=True),
        *_timestamps(versioned=True),
        sa.CheckConstraint(
            "phase IN ('catalog_only','activating','activated','deactivating')",
            name="permission_feature_phase",
        ),
        sa.CheckConstraint(
            "rollout_generation BETWEEN 0 AND 9223372036854775807",
            name="permission_feature_generation",
        ),
        sa.CheckConstraint(
            "old_instances_drained_at IS NULL OR old_instances_drained_by IS NOT NULL",
            name="permission_feature_drain_evidence",
        ),
        sa.PrimaryKeyConstraint("feature_code", name="pk_permission_feature_rollouts"),
        **_TABLE_OPTIONS,
    )
    op.create_table(
        "permission_feature_tenant_states",
        _id(),
        sa.Column("feature_code", sa.String(64), nullable=False),
        _id("tenant_id"),
        sa.Column("phase", sa.String(32), server_default="catalog_only", nullable=False),
        sa.Column("target_manifest_version", sa.String(64), nullable=False),
        sa.Column("applied_manifest_version", sa.String(64), nullable=False),
        sa.Column(
            "target_rollout_generation",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "applied_rollout_generation",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("claim_owner", sa.String(128), nullable=True),
        _id("claim_token", nullable=True),
        sa.Column("claim_fence", sa.Integer(), nullable=True),
        sa.Column("claim_expires_at", _DATETIME, nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("activated_at", _DATETIME, nullable=True),
        *_timestamps(versioned=True),
        sa.CheckConstraint(
            "phase IN ('catalog_only','activating','activated','deactivating','failed')",
            name="tenant_feature_phase",
        ),
        sa.CheckConstraint(
            "target_rollout_generation >= applied_rollout_generation",
            name="tenant_feature_gen_order",
        ),
        sa.CheckConstraint(
            "target_rollout_generation BETWEEN 0 AND 9223372036854775807",
            name="tenant_feature_target_gen",
        ),
        sa.CheckConstraint(
            "applied_rollout_generation BETWEEN 0 AND 9223372036854775807",
            name="tenant_feature_applied_gen",
        ),
        sa.CheckConstraint(
            "(claim_owner IS NULL AND claim_token IS NULL "
            "AND claim_fence IS NULL AND claim_expires_at IS NULL) OR "
            "(claim_owner IS NOT NULL AND claim_token IS NOT NULL "
            "AND claim_fence IS NOT NULL AND claim_expires_at IS NOT NULL)",
            name="tenant_feature_claim_ok",
        ),
        sa.ForeignKeyConstraint(
            ["feature_code"],
            ["permission_feature_rollouts.feature_code"],
            name="fk_tenant_feature_states_feature_code",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_tenant_feature_states_tenant_id_tenants"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_permission_feature_tenant_states"),
        sa.UniqueConstraint(
            "feature_code", "tenant_id", name="uq_permission_feature_tenant_states_feature_code"
        ),
        **_TABLE_OPTIONS,
    )
    op.create_index(
        "ix_tenant_feature_states_phase",
        "permission_feature_tenant_states",
        ["feature_code", "phase"],
    )


def _create_ai_job_tables() -> None:
    op.create_table(
        "ai_jobs",
        _id(),
        _id("tenant_id"),
        sa.Column("handler_code", sa.String(64), nullable=False),
        sa.Column("handler_version", sa.String(64), nullable=False),
        sa.Column("input_schema_version", sa.String(64), nullable=False),
        sa.Column("input_json", sa.JSON(), nullable=False),
        sa.Column("input_fingerprint", sa.BINARY(32), nullable=False),
        _id("created_by_user_id"),
        _id("created_by_membership_id"),
        _id("created_by_session_id"),
        sa.Column("actor_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("policy_version_at_submit", sa.String(64), nullable=False),
        sa.Column("visibility", sa.String(32), server_default="owner_only", nullable=False),
        sa.Column("status", sa.String(32), server_default="queued", nullable=False),
        sa.Column("current_attempt_no", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("4"), nullable=False),
        sa.Column("next_attempt_at", _DATETIME, nullable=True),
        sa.Column("lease_owner", sa.String(128), nullable=True),
        _id("lease_token", nullable=True),
        sa.Column("lease_fence", sa.Integer(), nullable=True),
        sa.Column("lease_expires_at", _DATETIME, nullable=True),
        sa.Column("cancel_requested_at", _DATETIME, nullable=True),
        _id("cancel_requested_by_user_id", nullable=True),
        sa.Column("cancel_reason_code", sa.String(64), nullable=True),
        sa.Column("risk_class", sa.String(32), server_default="synthetic", nullable=False),
        sa.Column("release_state", sa.String(32), server_default="non_publishable", nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("failure_class", sa.String(64), nullable=True),
        sa.Column("failure_code", sa.String(64), nullable=True),
        _id("idempotency_record_id"),
        _id("correlation_id"),
        sa.Column("expires_at", _DATETIME, nullable=False),
        sa.Column("submitted_at", _DATETIME, nullable=False),
        sa.Column("started_at", _DATETIME, nullable=True),
        sa.Column("completed_at", _DATETIME, nullable=True),
        *_timestamps(versioned=True),
        sa.CheckConstraint(
            "status IN ('queued','running','retry_scheduled','cancel_requested',"
            "'succeeded','failed','cancelled')",
            name="ai_job_status",
        ),
        sa.CheckConstraint("visibility = 'owner_only'", name="ai_job_visibility"),
        sa.CheckConstraint("max_attempts BETWEEN 1 AND 10", name="ai_job_max_attempts"),
        sa.CheckConstraint(
            "current_attempt_no BETWEEN 0 AND max_attempts", name="ai_job_current_attempt_no"
        ),
        sa.CheckConstraint(
            "risk_class = 'synthetic' AND release_state = 'non_publishable'",
            name="ai_job_risk_release",
        ),
        sa.CheckConstraint(
            "(lease_owner IS NULL AND lease_token IS NULL "
            "AND lease_fence IS NULL AND lease_expires_at IS NULL "
            "AND status NOT IN ('running','cancel_requested')) OR "
            "(lease_owner IS NOT NULL AND lease_token IS NOT NULL "
            "AND lease_fence IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND status IN ('running','cancel_requested'))",
            name="ai_job_lease_consistency",
        ),
        sa.CheckConstraint(
            "(status IN ('succeeded','failed','cancelled') "
            "AND completed_at IS NOT NULL AND next_attempt_at IS NULL) OR "
            "(status NOT IN ('succeeded','failed','cancelled') "
            "AND completed_at IS NULL)",
            name="ai_job_terminal_consistency",
        ),
        sa.CheckConstraint(
            "submitted_at < expires_at AND expires_at <= DATE_ADD(submitted_at, INTERVAL 24 HOUR)",
            name="ai_job_lifetime",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_ai_jobs_tenant_id_tenants"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "created_by_membership_id", "created_by_user_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id", "tenant_memberships.user_id"],
            name="fk_ai_jobs_tenant_membership_user",
        ),
        sa.ForeignKeyConstraint(
            [
                "tenant_id",
                "created_by_session_id",
                "created_by_user_id",
                "created_by_membership_id",
            ],
            [
                "auth_sessions.tenant_id",
                "auth_sessions.id",
                "auth_sessions.user_id",
                "auth_sessions.membership_id",
            ],
            name="fk_ai_jobs_tenant_session_actor",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "idempotency_record_id"],
            ["idempotency_records.tenant_id", "idempotency_records.id"],
            name="fk_ai_jobs_tenant_idempotency",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ai_jobs"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_ai_jobs_tenant_id"),
        **_TABLE_OPTIONS,
    )
    op.create_index("ix_ai_jobs_tenant_status", "ai_jobs", ["tenant_id", "status"])
    op.create_index(
        "ix_ai_jobs_tenant_lease_expiry", "ai_jobs", ["tenant_id", "status", "lease_expires_at"]
    )
    op.create_index("ix_ai_jobs_expires_at", "ai_jobs", ["expires_at"])

    op.create_table(
        "ai_job_execution_grants",
        _id(),
        _id("tenant_id"),
        _id("job_id"),
        _id("user_id"),
        _id("membership_id"),
        sa.Column("permission_code", sa.String(128), nullable=False),
        sa.Column("auth_version_at_submit", sa.Integer(), nullable=False),
        sa.Column("authz_version_at_submit", sa.Integer(), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("job_scope_manifest_version", sa.String(64), nullable=False),
        sa.Column("job_scope_code", sa.String(32), nullable=False),
        sa.Column("issued_at", _DATETIME, nullable=False),
        sa.Column("revoked_at", _DATETIME, nullable=True),
        sa.Column("revocation_reason_code", sa.String(64), nullable=True),
        *_timestamps(versioned=True),
        sa.CheckConstraint("permission_code = 'ai_job.create'", name="ai_job_grant_permission"),
        sa.CheckConstraint(
            "(revoked_at IS NULL AND revocation_reason_code IS NULL) OR "
            "(revoked_at IS NOT NULL AND revocation_reason_code IS NOT NULL)",
            name="ai_job_grant_revocation_consistency",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_ai_job_execution_grants_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_ai_job_execution_grants_user_id_users"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["ai_jobs.tenant_id", "ai_jobs.id"],
            name="fk_ai_job_execution_grants_tenant_id_ai_jobs",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "membership_id", "user_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id", "tenant_memberships.user_id"],
            name="fk_ai_job_grants_tenant_membership_user",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ai_job_execution_grants"),
        sa.UniqueConstraint("tenant_id", "job_id", name="uq_ai_job_execution_grants_tenant_id"),
        **_TABLE_OPTIONS,
    )

    op.create_table(
        "ai_job_access_grants",
        _id(),
        _id("tenant_id"),
        _id("job_id"),
        _id("membership_id"),
        sa.Column("access_level", sa.String(16), server_default="read", nullable=False),
        sa.Column("generation", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("supersedes_generation", sa.Integer(), nullable=True),
        _id("supersedes_grant_id", nullable=True),
        _id("granted_by_user_id"),
        _id("granted_by_membership_id"),
        sa.Column("granted_at", _DATETIME, nullable=False),
        sa.Column("revoked_at", _DATETIME, nullable=True),
        sa.Column(
            "active_marker",
            sa.Integer(),
            sa.Computed("CASE WHEN revoked_at IS NULL THEN 1 ELSE NULL END"),
            nullable=True,
        ),
        *_timestamps(versioned=True),
        sa.CheckConstraint("access_level = 'read'", name="ai_job_access_level"),
        sa.CheckConstraint(
            "(supersedes_generation IS NULL AND supersedes_grant_id IS NULL) OR "
            "(supersedes_generation IS NOT NULL AND supersedes_grant_id IS NOT NULL)",
            name="ai_job_access_supersedes_consistency",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_ai_job_access_grants_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["granted_by_user_id"],
            ["users.id"],
            name="fk_ai_job_access_grants_granted_by_user_id_users",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["ai_jobs.tenant_id", "ai_jobs.id"],
            name="fk_ai_job_access_grants_tenant_id_ai_jobs",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "membership_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id"],
            name="fk_ai_job_access_grants_tenant_membership",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "granted_by_membership_id", "granted_by_user_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id", "tenant_memberships.user_id"],
            name="fk_ai_job_access_grants_granted_by",
        ),
        sa.ForeignKeyConstraint(
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
        sa.PrimaryKeyConstraint("id", name="pk_ai_job_access_grants"),
        sa.UniqueConstraint(
            "tenant_id",
            "job_id",
            "membership_id",
            "generation",
            name="uq_ai_job_access_grants_tenant_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "job_id",
            "membership_id",
            "generation",
            "id",
            name="uq_ai_job_access_grants_tenant_id_gen_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "job_id",
            "membership_id",
            "active_marker",
            name="uq_ai_job_access_grants_active",
        ),
        **_TABLE_OPTIONS,
    )

    op.create_table(
        "ai_job_attempts",
        _id(),
        _id("tenant_id"),
        _id("job_id"),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), server_default="claimed", nullable=False),
        _id("trigger_message_id"),
        sa.Column("worker_instance_ref", sa.String(128), nullable=True),
        _id("lease_token"),
        sa.Column("lease_fence", sa.Integer(), nullable=False),
        sa.Column("handler_code", sa.String(64), nullable=False),
        sa.Column("handler_version", sa.String(64), nullable=False),
        sa.Column("started_at", _DATETIME, nullable=False),
        sa.Column("last_heartbeat_at", _DATETIME, nullable=True),
        sa.Column("finished_at", _DATETIME, nullable=True),
        sa.Column("failure_class", sa.String(64), nullable=True),
        sa.Column("failure_code", sa.String(64), nullable=True),
        sa.Column("retry_delay_seconds", sa.Integer(), nullable=True),
        sa.Column("cancellation_seen_at", _DATETIME, nullable=True),
        *_timestamps(versioned=True),
        sa.CheckConstraint(
            "status IN ('claimed','running','succeeded','retryable_failed',"
            "'permanent_failed','cancelled','lease_expired')",
            name="ai_job_attempt_status",
        ),
        sa.CheckConstraint("attempt_no BETWEEN 1 AND 10", name="ai_job_attempt_no"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_ai_job_attempts_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["ai_jobs.tenant_id", "ai_jobs.id"],
            name="fk_ai_job_attempts_tenant_id_ai_jobs",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ai_job_attempts"),
        sa.UniqueConstraint("tenant_id", "job_id", "id", name="uq_ai_job_attempts_tenant_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "job_id",
            "id",
            "lease_fence",
            name="uq_ai_job_attempts_tenant_id_fence",
        ),
        sa.UniqueConstraint(
            "tenant_id", "job_id", "attempt_no", name="uq_ai_job_attempts_tenant_id_attempt"
        ),
        sa.UniqueConstraint(
            "tenant_id", "trigger_message_id", name="uq_ai_job_attempts_tenant_id_trigger"
        ),
        **_TABLE_OPTIONS,
    )
    op.create_index("ix_ai_job_attempts_tenant_job", "ai_job_attempts", ["tenant_id", "job_id"])

    op.create_table(
        "ai_job_outbox",
        _id(),
        _id("tenant_id"),
        _id("job_id"),
        sa.Column("dispatch_generation", sa.Integer(), nullable=False),
        sa.Column("envelope_generation", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "max_envelope_generations", sa.Integer(), server_default=sa.text("4"), nullable=False
        ),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("routing_key", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        _id("message_id", nullable=True),
        _id("correlation_id", nullable=True),
        sa.Column("issued_at", _DATETIME, nullable=True),
        sa.Column("nonce", sa.String(64), nullable=True),
        sa.Column("signature", sa.String(256), nullable=True),
        sa.Column("envelope_digest", sa.BINARY(32), nullable=True),
        sa.Column("recovery_source_fence", sa.Integer(), nullable=True),
        _id("source_attempt_id", nullable=True),
        sa.Column("status", sa.String(32), server_default="pending", nullable=False),
        sa.Column("available_at", _DATETIME, nullable=False),
        sa.Column(
            "publish_attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "max_publish_attempts", sa.Integer(), server_default=sa.text("8"), nullable=False
        ),
        sa.Column("claim_owner", sa.String(128), nullable=True),
        _id("claim_token", nullable=True),
        sa.Column("claim_fence", sa.Integer(), nullable=True),
        sa.Column("claim_expires_at", _DATETIME, nullable=True),
        sa.Column("published_at", _DATETIME, nullable=True),
        sa.Column("confirmed_at", _DATETIME, nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        *_timestamps(versioned=True),
        sa.CheckConstraint(
            "status IN ('pending','publishing','published','blocked','superseded')",
            name="ai_job_outbox_status",
        ),
        sa.CheckConstraint(
            "envelope_generation BETWEEN 1 AND max_envelope_generations "
            "AND max_envelope_generations BETWEEN 1 AND 4",
            name="ai_job_outbox_envelope_generation",
        ),
        sa.CheckConstraint(
            "(source_attempt_id IS NULL AND recovery_source_fence IS NULL) OR "
            "(source_attempt_id IS NOT NULL AND recovery_source_fence IS NOT NULL)",
            name="ai_job_outbox_recovery_consistency",
        ),
        sa.CheckConstraint(
            "publish_attempt_count BETWEEN 0 AND 2147483647",
            name="ai_job_outbox_publish_attempts",
        ),
        sa.CheckConstraint(
            "(claim_owner IS NULL AND claim_token IS NULL "
            "AND claim_fence IS NULL AND claim_expires_at IS NULL) OR "
            "(claim_owner IS NOT NULL AND claim_token IS NOT NULL "
            "AND claim_fence IS NOT NULL AND claim_expires_at IS NOT NULL)",
            name="ai_job_outbox_claim_consistency",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_ai_job_outbox_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["ai_jobs.tenant_id", "ai_jobs.id"],
            name="fk_ai_job_outbox_tenant_id_ai_jobs",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "job_id", "source_attempt_id", "recovery_source_fence"],
            [
                "ai_job_attempts.tenant_id",
                "ai_job_attempts.job_id",
                "ai_job_attempts.id",
                "ai_job_attempts.lease_fence",
            ],
            name="fk_ai_job_outbox_recovery_attempt",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ai_job_outbox"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_ai_job_outbox_tenant_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "job_id",
            "dispatch_generation",
            "envelope_generation",
            name="uq_ai_job_outbox_tenant_id_dispatch",
        ),
        sa.UniqueConstraint("tenant_id", "message_id", name="uq_ai_job_outbox_tenant_id_message"),
        sa.UniqueConstraint(
            "tenant_id", "job_id", "message_id", name="uq_ai_job_outbox_tenant_id_job_message"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "job_id",
            "recovery_source_fence",
            "envelope_generation",
            name="uq_ai_job_outbox_tenant_id_recovery",
        ),
        **_TABLE_OPTIONS,
    )
    op.create_index("ix_ai_job_outbox_dispatch", "ai_job_outbox", ["status", "available_at", "id"])

    op.create_table(
        "ai_job_inbox",
        _id(),
        _id("tenant_id"),
        _id("message_id"),
        _id("job_id"),
        sa.Column("schema_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("envelope_digest", sa.BINARY(32), nullable=False),
        sa.Column("nonce_digest", sa.BINARY(32), nullable=False),
        sa.Column("status", sa.String(32), server_default="accepted", nullable=False),
        sa.Column("first_received_at", _DATETIME, nullable=False),
        sa.Column("last_received_at", _DATETIME, nullable=False),
        sa.Column("delivery_count", sa.Integer(), server_default=sa.text("1"), nullable=False),
        _id("handling_attempt_id", nullable=True),
        sa.Column("handling_fence", sa.Integer(), nullable=True),
        sa.Column("completed_at", _DATETIME, nullable=True),
        sa.Column("rejection_code", sa.String(64), nullable=True),
        *_timestamps(versioned=True),
        sa.CheckConstraint(
            "status IN ('accepted','processing','completed','rejected')", name="ai_job_inbox_status"
        ),
        sa.CheckConstraint(
            "(handling_attempt_id IS NULL AND handling_fence IS NULL) OR "
            "(handling_attempt_id IS NOT NULL AND handling_fence IS NOT NULL)",
            name="ai_job_inbox_handling_consistency",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_ai_job_inbox_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["ai_jobs.tenant_id", "ai_jobs.id"],
            name="fk_ai_job_inbox_tenant_id_ai_jobs",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "job_id", "handling_attempt_id", "handling_fence"],
            [
                "ai_job_attempts.tenant_id",
                "ai_job_attempts.job_id",
                "ai_job_attempts.id",
                "ai_job_attempts.lease_fence",
            ],
            name="fk_ai_job_inbox_handling_attempt",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ai_job_inbox"),
        sa.UniqueConstraint("tenant_id", "message_id", name="uq_ai_job_inbox_tenant_id_message"),
        sa.UniqueConstraint("tenant_id", "nonce_digest", name="uq_ai_job_inbox_tenant_id_nonce"),
        **_TABLE_OPTIONS,
    )
    op.create_index("ix_ai_job_inbox_tenant_status", "ai_job_inbox", ["tenant_id", "status"])

    op.create_table(
        "ai_job_step_effects",
        _id(),
        _id("tenant_id"),
        _id("job_id"),
        _id("attempt_id"),
        sa.Column("step_code", sa.String(64), nullable=False),
        sa.Column("effect_key", sa.String(128), nullable=False),
        sa.Column("input_digest", sa.BINARY(32), nullable=False),
        sa.Column("status", sa.String(16), server_default="started", nullable=False),
        sa.Column("lease_fence", sa.Integer(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("failure_code", sa.String(64), nullable=True),
        sa.Column("started_at", _DATETIME, nullable=False),
        sa.Column("applied_at", _DATETIME, nullable=True),
        *_timestamps(versioned=True),
        sa.CheckConstraint("status IN ('started','applied','failed')", name="ai_job_effect_status"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_ai_job_step_effects_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["ai_jobs.tenant_id", "ai_jobs.id"],
            name="fk_ai_job_step_effects_tenant_id_ai_jobs",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "job_id", "attempt_id", "lease_fence"],
            [
                "ai_job_attempts.tenant_id",
                "ai_job_attempts.job_id",
                "ai_job_attempts.id",
                "ai_job_attempts.lease_fence",
            ],
            name="fk_ai_job_effects_attempt_fence",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ai_job_step_effects"),
        sa.UniqueConstraint(
            "tenant_id",
            "job_id",
            "step_code",
            "effect_key",
            name="uq_ai_job_step_effects_tenant_id",
        ),
        **_TABLE_OPTIONS,
    )

    op.create_table(
        "message_security_rejections",
        _id(),
        sa.Column("observability_ref", sa.BINARY(32), nullable=False),
        sa.Column("rejection_code", sa.String(64), nullable=False),
        sa.Column("schema_version_hint", sa.Integer(), nullable=True),
        sa.Column("kid_hint", sa.String(64), nullable=True),
        sa.Column("source_channel", sa.String(32), nullable=False),
        sa.Column("received_at", _DATETIME, nullable=False),
        sa.Column("retention_bucket", sa.String(16), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_message_security_rejections"),
        sa.UniqueConstraint(
            "observability_ref",
            "rejection_code",
            "retention_bucket",
            name="uq_message_security_rejections_observability_ref",
        ),
        **_TABLE_OPTIONS,
    )
    op.create_index(
        "ix_message_security_rejections_retention",
        "message_security_rejections",
        ["retention_bucket", "received_at"],
    )


def _expand_audit_events() -> None:
    op.add_column("audit_events", sa.Column("actor_kind", sa.String(64), nullable=True))
    op.add_column("audit_events", sa.Column("on_behalf_of_user_id", sa.BINARY(16), nullable=True))
    op.add_column(
        "audit_events", sa.Column("on_behalf_of_membership_id", sa.BINARY(16), nullable=True)
    )
    op.add_column("audit_events", sa.Column("target_job_id", sa.BINARY(16), nullable=True))
    op.add_column("audit_events", sa.Column("attempt_id", sa.BINARY(16), nullable=True))
    op.add_column("audit_events", sa.Column("message_id", sa.BINARY(16), nullable=True))
    op.add_column("audit_events", sa.Column("rollout_feature_code", sa.String(64), nullable=True))
    op.add_column("audit_events", sa.Column("rollout_generation", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_audit_events_on_behalf_of_user_id_users",
        "audit_events",
        "users",
        ["on_behalf_of_user_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_audit_events_rollout_feature_code_permission_feature_rollouts",
        "audit_events",
        "permission_feature_rollouts",
        ["rollout_feature_code"],
        ["feature_code"],
    )
    op.create_foreign_key(
        "fk_audit_events_tenant_actor_membership",
        "audit_events",
        "tenant_memberships",
        ["tenant_id", "actor_user_id", "actor_membership_id"],
        ["tenant_id", "user_id", "id"],
    )
    op.create_foreign_key(
        "fk_audit_events_tenant_on_behalf_membership",
        "audit_events",
        "tenant_memberships",
        ["tenant_id", "on_behalf_of_user_id", "on_behalf_of_membership_id"],
        ["tenant_id", "user_id", "id"],
    )
    op.create_foreign_key(
        "fk_audit_events_tenant_target_job",
        "audit_events",
        "ai_jobs",
        ["tenant_id", "target_job_id"],
        ["tenant_id", "id"],
    )
    op.create_foreign_key(
        "fk_audit_events_tenant_target_attempt",
        "audit_events",
        "ai_job_attempts",
        ["tenant_id", "target_job_id", "attempt_id"],
        ["tenant_id", "job_id", "id"],
    )
    op.create_foreign_key(
        "fk_audit_events_tenant_target_message",
        "audit_events",
        "ai_job_outbox",
        ["tenant_id", "target_job_id", "message_id"],
        ["tenant_id", "job_id", "message_id"],
    )
    op.execute(
        sa.text(
            "ALTER TABLE audit_events ADD CONSTRAINT "
            "ck_audit_events_audit_structured_actor CHECK (" + _STRUCTURED_AUDIT_CHECK + ")"
        )
    )


def _seed_ai_job_permissions_and_feature() -> None:
    bind = op.get_bind()
    now = bind.scalar(sa.text("SELECT UTC_TIMESTAMP(6)"))
    base_manifest_version = AI_JOB_BASE_MANIFEST_VERSION
    base_digest = ai_job_manifest_digest(base_manifest_version, ())
    for permission_id, code, action, risk_level in (
        (_AI_JOB_CREATE, "ai_job.create", "create", "medium"),
        (_AI_JOB_READ, "ai_job.read", "read", "medium"),
        (_AI_JOB_CANCEL, "ai_job.cancel", "cancel", "high"),
    ):
        bind.execute(
            sa.text(
                "INSERT INTO permission_catalog "
                "(id, code, resource, action, risk_level, status, created_at, updated_at) "
                "VALUES (:id, :code, 'ai_job', :action, :risk, 'active', :now, :now)"
            ),
            {
                "id": permission_id.bytes,
                "code": code,
                "action": action,
                "risk": risk_level,
                "now": now,
            },
        )
    bind.execute(
        sa.text(
            "INSERT INTO permission_feature_rollouts "
            "(feature_code, phase, manifest_version, manifest_digest, rollout_generation, version) "
            "VALUES ('ai_job_runtime_v1', 'catalog_only', :manifest, :digest, 0, 1)"
        ),
        {"manifest": base_manifest_version, "digest": base_digest},
    )
    tenant_ids = list(bind.scalars(sa.text("SELECT id FROM tenants ORDER BY id")).all())
    for tenant_id in tenant_ids:
        bind.execute(
            sa.text(
                "INSERT INTO permission_feature_tenant_states "
                "(id, feature_code, tenant_id, phase, target_manifest_version, "
                "applied_manifest_version, target_rollout_generation, "
                "applied_rollout_generation, version) "
                "VALUES (:id, 'ai_job_runtime_v1', :tenant_id, 'catalog_only', :manifest, "
                ":manifest, 0, 0, 1)"
            ),
            {
                "id": new_uuid7().bytes,
                "tenant_id": tenant_id,
                "manifest": base_manifest_version,
            },
        )
