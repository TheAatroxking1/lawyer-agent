"""Add the durable authorization-cache invalidation outbox.

Revision ID: 20260902_03
Revises: 20260901_02
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "20260902_03"
down_revision: str | Sequence[str] | None = "20260901_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DATETIME = mysql.DATETIME(fsp=6)


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_idempotency_records_tenant_id_id",
        "idempotency_records",
        ["tenant_id", "id"],
    )
    op.create_table(
        "authz_cache_invalidation_outbox",
        sa.Column("id", sa.BINARY(16), nullable=False),
        sa.Column("tenant_id", sa.BINARY(16), nullable=False),
        sa.Column("membership_id", sa.BINARY(16), nullable=False),
        sa.Column("authz_version", sa.Integer(), nullable=False),
        sa.Column("idempotency_record_id", sa.BINARY(16), nullable=False),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("available_at", _DATETIME, nullable=False),
        sa.Column("processing_started_at", _DATETIME, nullable=True),
        sa.Column("claim_token", sa.BINARY(16), nullable=True),
        sa.Column("completed_at", _DATETIME, nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
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
        sa.CheckConstraint(
            "status IN ('pending','processing','completed')",
            name="outbox_status",
        ),
        sa.CheckConstraint(
            "authz_version BETWEEN 1 AND 2147483647",
            name="outbox_authz_version",
        ),
        sa.CheckConstraint(
            "attempt_count BETWEEN 0 AND 2147483647",
            name="outbox_attempt_count",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "membership_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id"],
            name="fk_authz_cache_outbox_tenant_membership",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "idempotency_record_id"],
            ["idempotency_records.tenant_id", "idempotency_records.id"],
            name="fk_authz_cache_outbox_tenant_idempotency",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_authz_cache_invalidation_outbox"),
        sa.UniqueConstraint(
            "tenant_id",
            "membership_id",
            "authz_version",
            name="uq_authz_cache_outbox_membership_version",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_record_id",
            name="uq_authz_cache_outbox_tenant_idempotency",
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_authz_cache_outbox_dispatch",
        "authz_cache_invalidation_outbox",
        ["status", "available_at", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_authz_cache_outbox_dispatch",
        table_name="authz_cache_invalidation_outbox",
    )
    op.drop_table("authz_cache_invalidation_outbox")
    op.drop_constraint(
        "uq_idempotency_records_tenant_id_id",
        "idempotency_records",
        type_="unique",
    )
