"""Create the identity and authorization baseline.

Revision ID: 20260901_01
Revises:
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "20260901_01"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE_OPTIONS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}
_DATETIME = mysql.DATETIME(fsp=6)


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
    op.create_table(
        "users",
        _id(),
        sa.Column("status", sa.String(32), server_default="active", nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("auth_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("deleted_at", _DATETIME, nullable=True),
        *_timestamps(versioned=True),
        sa.CheckConstraint(
            "status IN ('active','locked','disabled','pending_deletion')",
            name="user_status",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        **_TABLE_OPTIONS,
    )
    op.create_table(
        "permission_catalog",
        _id(),
        sa.Column("code", sa.String(128), nullable=False),
        sa.Column("resource", sa.String(64), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("risk_level", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "risk_level IN ('low','medium','high','critical')",
            name="permission_risk_level",
        ),
        sa.CheckConstraint("status IN ('active','disabled')", name="permission_status"),
        sa.PrimaryKeyConstraint("id", name="pk_permission_catalog"),
        sa.UniqueConstraint("code", name="uq_permission_catalog_code"),
        **_TABLE_OPTIONS,
    )
    op.create_table(
        "role_templates",
        _id(),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.String(512), nullable=False),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        *_timestamps(versioned=True),
        sa.CheckConstraint("status IN ('active','disabled')", name="role_template_status"),
        sa.PrimaryKeyConstraint("id", name="pk_role_templates"),
        sa.UniqueConstraint("code", name="uq_role_templates_code"),
        **_TABLE_OPTIONS,
    )
    op.create_table(
        "platform_roles",
        _id(),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.String(512), nullable=False),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        *_timestamps(versioned=True),
        sa.CheckConstraint("status IN ('active','disabled')", name="platform_role_status"),
        sa.PrimaryKeyConstraint("id", name="pk_platform_roles"),
        sa.UniqueConstraint("code", name="uq_platform_roles_code"),
        **_TABLE_OPTIONS,
    )
    op.create_table(
        "tenants",
        _id(),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("normalized_name", sa.String(255), nullable=False),
        sa.Column("tenant_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), server_default="pending_verification", nullable=False),
        _id("created_by_user_id"),
        sa.Column("review_status", sa.String(32), server_default="pending", nullable=False),
        sa.Column("reviewed_at", _DATETIME, nullable=True),
        _id("reviewed_by_user_id", nullable=True),
        sa.Column("review_reason_code", sa.String(64), nullable=True),
        *_timestamps(versioned=True),
        sa.CheckConstraint(
            "tenant_type IN ('law_firm','enterprise','university')",
            name="tenant_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending_verification','active','suspended','closed')",
            name="tenant_status",
        ),
        sa.CheckConstraint(
            "review_status IN ('pending','approved','rejected')",
            name="tenant_review_status",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], name="fk_tenants_created_by_user_id_users"
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_user_id"], ["users.id"], name="fk_tenants_reviewed_by_user_id_users"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tenants"),
        **_TABLE_OPTIONS,
    )
    op.create_index("ix_tenants_normalized_name", "tenants", ["normalized_name"])
    op.create_table(
        "auth_identities",
        _id(),
        _id("user_id"),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("issuer", sa.String(255), nullable=False),
        sa.Column("display_value", sa.String(255), nullable=True),
        sa.Column("subject_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("subject_blind_index", sa.BINARY(32), nullable=False),
        sa.Column("key_version", sa.SmallInteger(), nullable=False),
        sa.Column("verified_at", _DATETIME, nullable=False),
        sa.Column("status", sa.String(32), server_default="active", nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "kind IN ('username','phone','email','wechat_unionid','wechat_openid')",
            name="auth_identity_kind",
        ),
        sa.CheckConstraint("status IN ('active','revoked')", name="auth_identity_status"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_auth_identities_user_id_users"),
        sa.PrimaryKeyConstraint("id", name="pk_auth_identities"),
        sa.UniqueConstraint(
            "kind",
            "issuer",
            "subject_blind_index",
            name="uq_auth_identities_kind",
        ),
        **_TABLE_OPTIONS,
    )
    op.create_index("ix_auth_identities_user_id", "auth_identities", ["user_id"])
    op.create_table(
        "identity_verification_challenges",
        _id(),
        _id("user_id", nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("issuer", sa.String(255), nullable=False),
        sa.Column("target_blind_index", sa.BINARY(32), nullable=False),
        sa.Column("challenge_hash", sa.BINARY(32), nullable=False),
        sa.Column("rate_limit_context_hash", sa.BINARY(32), nullable=False),
        sa.Column("attempt_count", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.SmallInteger(), nullable=False),
        sa.Column("expires_at", _DATETIME, nullable=False),
        sa.Column("consumed_at", _DATETIME, nullable=True),
        sa.Column("status", sa.String(32), server_default="pending", nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "kind IN ('phone','email','wechat_unionid','wechat_openid')",
            name="identity_challenge_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending','consumed','expired','locked')",
            name="identity_challenge_status",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_identity_verification_challenges_user_id_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_identity_verification_challenges"),
        **_TABLE_OPTIONS,
    )
    op.create_index(
        "ix_identity_verification_challenges_target",
        "identity_verification_challenges",
        ["kind", "target_blind_index"],
    )
    op.create_table(
        "password_credentials",
        _id("user_id"),
        sa.Column("password_hash", sa.String(512), nullable=False),
        sa.Column("algorithm", sa.String(32), server_default="argon2id", nullable=False),
        sa.Column("parameters_json", sa.JSON(), nullable=False),
        sa.Column("password_changed_at", _DATETIME, nullable=False),
        sa.Column(
            "failed_attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("locked_until", _DATETIME, nullable=True),
        sa.Column("status", sa.String(32), server_default="active", nullable=False),
        *_timestamps(versioned=True),
        sa.CheckConstraint(
            "status IN ('active','locked','revoked')",
            name="password_credential_status",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_password_credentials_user_id_users"
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_password_credentials"),
        **_TABLE_OPTIONS,
    )
    op.create_table(
        "departments",
        _id(),
        _id("tenant_id"),
        _id("parent_id", nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), server_default="active", nullable=False),
        *_timestamps(versioned=True),
        sa.CheckConstraint("status IN ('active','disabled')", name="department_status"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_departments_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "parent_id"],
            ["departments.tenant_id", "departments.id"],
            name="fk_departments_tenant_id_departments",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_departments"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_departments_tenant_id"),
        sa.UniqueConstraint(
            "tenant_id", "parent_id", "name", name="uq_departments_tenant_id_parent"
        ),
        **_TABLE_OPTIONS,
    )
    op.create_table(
        "tenant_memberships",
        _id(),
        _id("tenant_id"),
        _id("user_id"),
        _id("department_id", nullable=True),
        sa.Column("member_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), server_default="invited", nullable=False),
        sa.Column("valid_from", _DATETIME, nullable=False),
        sa.Column("valid_until", _DATETIME, nullable=True),
        sa.Column("authz_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        *_timestamps(versioned=True),
        sa.CheckConstraint(
            "member_type IN ('owner','internal','student','external_client')",
            name="tenant_membership_type",
        ),
        sa.CheckConstraint(
            "status IN ('invited','active','suspended','revoked')",
            name="tenant_membership_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_tenant_memberships_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_tenant_memberships_user_id_users"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "department_id"],
            ["departments.tenant_id", "departments.id"],
            name="fk_tenant_memberships_tenant_id_departments",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tenant_memberships"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_tenant_memberships_tenant_id"),
        sa.UniqueConstraint("tenant_id", "user_id", name="uq_tenant_memberships_tenant_id_user"),
        sa.UniqueConstraint(
            "tenant_id",
            "user_id",
            "id",
            name="uq_tenant_memberships_tenant_id_user_id",
        ),
        **_TABLE_OPTIONS,
    )
    op.create_index("ix_tenant_memberships_user_id", "tenant_memberships", ["user_id"])
    op.create_table(
        "role_template_permissions",
        _id("role_template_id"),
        _id("permission_id"),
        sa.ForeignKeyConstraint(
            ["role_template_id"],
            ["role_templates.id"],
            name="fk_role_template_permissions_role_template_id_role_templates",
        ),
        sa.ForeignKeyConstraint(
            ["permission_id"],
            ["permission_catalog.id"],
            name="fk_role_template_permissions_permission_id_permission_catalog",
        ),
        sa.PrimaryKeyConstraint(
            "role_template_id", "permission_id", name="pk_role_template_permissions"
        ),
        **_TABLE_OPTIONS,
    )
    op.create_table(
        "tenant_roles",
        _id(),
        _id("tenant_id"),
        _id("role_template_id", nullable=True),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("is_custom", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        *_timestamps(versioned=True),
        sa.CheckConstraint("status IN ('active','disabled')", name="tenant_role_status"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_tenant_roles_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["role_template_id"],
            ["role_templates.id"],
            name="fk_tenant_roles_role_template_id_role_templates",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tenant_roles"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_tenant_roles_tenant_id"),
        sa.UniqueConstraint("tenant_id", "code", name="uq_tenant_roles_tenant_id_code"),
        **_TABLE_OPTIONS,
    )
    op.create_index("ix_tenant_roles_template_id", "tenant_roles", ["role_template_id"])
    op.create_table(
        "tenant_role_permissions",
        _id("tenant_id"),
        _id("tenant_role_id"),
        _id("permission_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_tenant_role_permissions_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "tenant_role_id"],
            ["tenant_roles.tenant_id", "tenant_roles.id"],
            name="fk_tenant_role_permissions_tenant_id_tenant_roles",
        ),
        sa.ForeignKeyConstraint(
            ["permission_id"],
            ["permission_catalog.id"],
            name="fk_tenant_role_permissions_permission_id_permission_catalog",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "tenant_role_id", "permission_id", name="pk_tenant_role_permissions"
        ),
        **_TABLE_OPTIONS,
    )
    op.create_table(
        "membership_role_assignments",
        _id("tenant_id"),
        _id("membership_id"),
        _id("tenant_role_id"),
        _id("assigned_by_membership_id", nullable=True),
        sa.Column(
            "created_at",
            _DATETIME,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_membership_role_assignments_tenant_id_tenants",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "membership_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id"],
            name="fk_membership_role_assignments_tenant_id_tenant_memberships",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "tenant_role_id"],
            ["tenant_roles.tenant_id", "tenant_roles.id"],
            name="fk_membership_role_assignments_tenant_id_tenant_roles",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "assigned_by_membership_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id"],
            name="fk_membership_role_assignments_tenant_id_assigned_by",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "membership_id", "tenant_role_id", name="pk_membership_role_assignments"
        ),
        **_TABLE_OPTIONS,
    )
    op.create_table(
        "platform_role_permissions",
        _id("platform_role_id"),
        _id("permission_id"),
        sa.ForeignKeyConstraint(
            ["platform_role_id"],
            ["platform_roles.id"],
            name="fk_platform_role_permissions_platform_role_id_platform_roles",
        ),
        sa.ForeignKeyConstraint(
            ["permission_id"],
            ["permission_catalog.id"],
            name="fk_platform_role_permissions_permission_id_permission_catalog",
        ),
        sa.PrimaryKeyConstraint(
            "platform_role_id", "permission_id", name="pk_platform_role_permissions"
        ),
        **_TABLE_OPTIONS,
    )
    op.create_table(
        "platform_role_assignments",
        _id(),
        _id("user_id"),
        _id("platform_role_id"),
        _id("assigned_by_user_id", nullable=True),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column("expires_at", _DATETIME, nullable=True),
        sa.Column("revoked_at", _DATETIME, nullable=True),
        sa.Column("revocation_reason", sa.String(128), nullable=True),
        *_timestamps(versioned=True),
        sa.CheckConstraint(
            "status IN ('active','revoked','expired')",
            name="platform_role_assignment_status",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_platform_role_assignments_user_id_users"
        ),
        sa.ForeignKeyConstraint(
            ["platform_role_id"],
            ["platform_roles.id"],
            name="fk_platform_role_assignments_platform_role_id_platform_roles",
        ),
        sa.ForeignKeyConstraint(
            ["assigned_by_user_id"],
            ["users.id"],
            name="fk_platform_role_assignments_assigned_by_user_id_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_platform_role_assignments"),
        sa.UniqueConstraint(
            "user_id", "platform_role_id", name="uq_platform_role_assignments_user_id"
        ),
        **_TABLE_OPTIONS,
    )
    op.create_index(
        "ix_platform_role_assignments_user_id", "platform_role_assignments", ["user_id"]
    )
    op.create_table(
        "tenant_invitations",
        _id(),
        _id("tenant_id"),
        sa.Column("target_kind", sa.String(16), nullable=False),
        sa.Column("target_blind_index", sa.BINARY(32), nullable=False),
        sa.Column("token_hash", sa.BINARY(32), nullable=False),
        _id("invited_by_membership_id"),
        sa.Column("expires_at", _DATETIME, nullable=False),
        sa.Column("accepted_at", _DATETIME, nullable=True),
        sa.Column("revoked_at", _DATETIME, nullable=True),
        sa.Column("status", sa.String(32), server_default="pending", nullable=False),
        *_timestamps(versioned=True),
        sa.CheckConstraint(
            "target_kind IN ('phone','email')",
            name="tenant_invitation_target_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending','accepted','revoked','expired')",
            name="tenant_invitation_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_tenant_invitations_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "invited_by_membership_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id"],
            name="fk_tenant_invitations_tenant_id_tenant_memberships",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tenant_invitations"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_tenant_invitations_tenant_id"),
        sa.UniqueConstraint("token_hash", name="uq_tenant_invitations_token_hash"),
        **_TABLE_OPTIONS,
    )
    op.create_index(
        "ix_tenant_invitations_target",
        "tenant_invitations",
        ["tenant_id", "target_blind_index"],
    )
    op.create_table(
        "tenant_invitation_role_assignments",
        _id("tenant_id"),
        _id("invitation_id"),
        _id("tenant_role_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_tenant_invitation_role_assignments_tenant_id_tenants",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "invitation_id"],
            ["tenant_invitations.tenant_id", "tenant_invitations.id"],
            name="fk_tenant_invitation_role_assignments_tenant_id_invitations",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "tenant_role_id"],
            ["tenant_roles.tenant_id", "tenant_roles.id"],
            name="fk_tenant_invitation_role_assignments_tenant_id_roles",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "invitation_id",
            "tenant_role_id",
            name="pk_tenant_invitation_role_assignments",
        ),
        **_TABLE_OPTIONS,
    )
    op.create_table(
        "auth_sessions",
        _id(),
        _id("user_id"),
        _id("tenant_id", nullable=True),
        _id("membership_id", nullable=True),
        _id("current_family_id"),
        sa.Column("auth_version_at_issue", sa.Integer(), nullable=False),
        sa.Column("authz_version_at_issue", sa.Integer(), nullable=True),
        sa.Column("revoked_at", _DATETIME, nullable=True),
        sa.Column("revocation_reason", sa.String(128), nullable=True),
        sa.Column("last_seen_at", _DATETIME, nullable=False),
        sa.Column("expires_at", _DATETIME, nullable=False),
        *_timestamps(versioned=True),
        sa.CheckConstraint(
            "(tenant_id IS NULL AND membership_id IS NULL "
            "AND authz_version_at_issue IS NULL) OR "
            "(tenant_id IS NOT NULL AND membership_id IS NOT NULL "
            "AND authz_version_at_issue IS NOT NULL)",
            name="auth_session_tenant_context",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_auth_sessions_user_id_users"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_auth_sessions_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "membership_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id"],
            name="fk_auth_sessions_tenant_id_tenant_memberships",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id", "membership_id"],
            [
                "tenant_memberships.tenant_id",
                "tenant_memberships.user_id",
                "tenant_memberships.id",
            ],
            name="fk_auth_sessions_tenant_id_user_membership",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_auth_sessions"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_auth_sessions_tenant_id"),
        sa.UniqueConstraint("user_id", "id", name="uq_auth_sessions_user_id"),
        **_TABLE_OPTIONS,
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_index("ix_auth_sessions_current_family_id", "auth_sessions", ["current_family_id"])
    op.create_table(
        "refresh_token_records",
        _id(),
        sa.Column("token_hash", sa.BINARY(32), nullable=False),
        _id("family_id"),
        _id("session_id"),
        _id("user_id"),
        _id("tenant_id", nullable=True),
        _id("membership_id", nullable=True),
        sa.Column(
            "issued_at",
            _DATETIME,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("expires_at", _DATETIME, nullable=False),
        sa.Column("idle_expires_at", _DATETIME, nullable=False),
        sa.Column("used_at", _DATETIME, nullable=True),
        sa.Column("revoked_at", _DATETIME, nullable=True),
        _id("replaced_by_id", nullable=True),
        sa.Column("revocation_reason", sa.String(128), nullable=True),
        sa.Column("device_label", sa.String(128), nullable=True),
        sa.Column("user_agent_hash", sa.BINARY(32), nullable=True),
        sa.Column("ip_hash", sa.BINARY(32), nullable=True),
        *_timestamps(versioned=True),
        sa.CheckConstraint(
            "(tenant_id IS NULL AND membership_id IS NULL) OR "
            "(tenant_id IS NOT NULL AND membership_id IS NOT NULL)",
            name="refresh_token_tenant_context",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "session_id"],
            ["auth_sessions.tenant_id", "auth_sessions.id"],
            name="fk_refresh_token_records_tenant_id_auth_sessions",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "session_id"],
            ["auth_sessions.user_id", "auth_sessions.id"],
            name="fk_refresh_token_records_user_id_auth_sessions",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_refresh_token_records_user_id_users"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_refresh_token_records_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "membership_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id"],
            name="fk_refresh_token_records_tenant_id_tenant_memberships",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id", "membership_id"],
            [
                "tenant_memberships.tenant_id",
                "tenant_memberships.user_id",
                "tenant_memberships.id",
            ],
            name="fk_refresh_token_records_tenant_id_user_membership",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "replaced_by_id"],
            ["refresh_token_records.tenant_id", "refresh_token_records.id"],
            name="fk_refresh_token_records_tenant_id_replacement",
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "replaced_by_id"],
            ["refresh_token_records.session_id", "refresh_token_records.id"],
            name="fk_refresh_token_records_session_id_replacement",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_refresh_token_records"),
        sa.UniqueConstraint("token_hash", name="uq_refresh_token_records_token_hash"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_refresh_token_records_tenant_id"),
        sa.UniqueConstraint("session_id", "id", name="uq_refresh_token_records_session_id"),
        **_TABLE_OPTIONS,
    )
    op.create_index("ix_refresh_token_records_family_id", "refresh_token_records", ["family_id"])
    op.create_index(
        "ix_refresh_token_records_replaced_by_id", "refresh_token_records", ["replaced_by_id"]
    )
    op.create_index("ix_refresh_token_records_session_id", "refresh_token_records", ["session_id"])
    op.create_table(
        "audit_events",
        _id(),
        _id("actor_user_id", nullable=True),
        _id("tenant_id", nullable=True),
        _id("actor_membership_id", nullable=True),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("result", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(64), nullable=True),
        _id("target_id", nullable=True),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("client_ip_hash", sa.BINARY(32), nullable=True),
        sa.Column("user_agent_hash", sa.BINARY(32), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("occurred_at", _DATETIME, nullable=False),
        sa.Column(
            "created_at",
            _DATETIME,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint("result IN ('success','failure','denied')", name="audit_event_result"),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"], name="fk_audit_events_actor_user_id_users"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_audit_events_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "actor_membership_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id"],
            name="fk_audit_events_tenant_id_tenant_memberships",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
        **_TABLE_OPTIONS,
    )
    op.create_index("ix_audit_events_tenant_occurred", "audit_events", ["tenant_id", "occurred_at"])
    op.create_index("ix_audit_events_actor_user_id", "audit_events", ["actor_user_id"])
    op.create_index("ix_audit_events_trace_id", "audit_events", ["trace_id"])
    op.create_table(
        "idempotency_records",
        _id(),
        _id("tenant_id", nullable=True),
        sa.Column("scope_type", sa.String(16), nullable=False),
        _id("scope_id"),
        sa.Column("operation", sa.String(128), nullable=False),
        sa.Column("key_hash", sa.BINARY(32), nullable=False),
        sa.Column("request_fingerprint", sa.BINARY(32), nullable=False),
        sa.Column("status", sa.String(16), server_default="reserved", nullable=False),
        sa.Column("result_type", sa.String(64), nullable=True),
        _id("result_id", nullable=True),
        sa.Column("expires_at", _DATETIME, nullable=False),
        *_timestamps(versioned=True),
        sa.CheckConstraint(
            "scope_type IN ('user','membership','platform')",
            name="idempotency_scope_type",
        ),
        sa.CheckConstraint(
            "status IN ('reserved','completed','failed')",
            name="idempotency_status",
        ),
        sa.CheckConstraint(
            "(scope_type = 'membership' AND tenant_id IS NOT NULL) OR "
            "(scope_type IN ('user','platform') AND tenant_id IS NULL)",
            name="idempotency_tenant_scope",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_idempotency_records_tenant_id_tenants",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "scope_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id"],
            name="fk_idempotency_records_tenant_id_membership",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_idempotency_records"),
        sa.UniqueConstraint(
            "scope_type",
            "scope_id",
            "operation",
            "key_hash",
            name="uq_idempotency_records_scope_type",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "scope_type",
            "scope_id",
            "operation",
            "key_hash",
            name="uq_idempotency_records_tenant_scope",
        ),
        **_TABLE_OPTIONS,
    )
    op.create_index("ix_idempotency_records_expires_at", "idempotency_records", ["expires_at"])
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_auth_sessions_context_immutable
            BEFORE UPDATE ON auth_sessions
            FOR EACH ROW
            BEGIN
                IF NOT (NEW.user_id <=> OLD.user_id)
                   OR NOT (NEW.tenant_id <=> OLD.tenant_id)
                   OR NOT (NEW.membership_id <=> OLD.membership_id) THEN
                    SIGNAL SQLSTATE '45000'
                        SET MESSAGE_TEXT = 'auth session context is immutable';
                END IF;
            END
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_refresh_context_before_insert
            BEFORE INSERT ON refresh_token_records
            FOR EACH ROW
            BEGIN
                DECLARE session_tenant_id BINARY(16) DEFAULT NULL;
                SELECT tenant_id INTO session_tenant_id
                FROM auth_sessions
                WHERE id = NEW.session_id AND user_id = NEW.user_id
                LIMIT 1;
                IF NOT (NEW.tenant_id <=> session_tenant_id) THEN
                    SIGNAL SQLSTATE '45000'
                        SET MESSAGE_TEXT = 'refresh token session context mismatch';
                END IF;
            END
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_refresh_context_before_update
            BEFORE UPDATE ON refresh_token_records
            FOR EACH ROW
            BEGIN
                DECLARE session_tenant_id BINARY(16) DEFAULT NULL;
                SELECT tenant_id INTO session_tenant_id
                FROM auth_sessions
                WHERE id = NEW.session_id AND user_id = NEW.user_id
                LIMIT 1;
                IF NOT (NEW.tenant_id <=> session_tenant_id) THEN
                    SIGNAL SQLSTATE '45000'
                        SET MESSAGE_TEXT = 'refresh token session context mismatch';
                END IF;
            END
            """
        )
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_refresh_context_before_update")
    op.execute("DROP TRIGGER IF EXISTS trg_refresh_context_before_insert")
    op.execute("DROP TRIGGER IF EXISTS trg_auth_sessions_context_immutable")
    op.drop_table("idempotency_records")
    op.drop_table("audit_events")
    op.drop_table("refresh_token_records")
    op.drop_table("auth_sessions")
    op.drop_table("tenant_invitation_role_assignments")
    op.drop_table("tenant_invitations")
    op.drop_table("platform_role_assignments")
    op.drop_table("platform_role_permissions")
    op.drop_table("membership_role_assignments")
    op.drop_table("tenant_role_permissions")
    op.drop_table("tenant_roles")
    op.drop_table("role_template_permissions")
    op.drop_table("tenant_memberships")
    op.drop_table("departments")
    op.drop_table("password_credentials")
    op.drop_table("identity_verification_challenges")
    op.drop_table("auth_identities")
    op.drop_table("tenants")
    op.drop_table("platform_roles")
    op.drop_table("role_templates")
    op.drop_table("permission_catalog")
    op.drop_table("users")
