"""Add tenant-owned contract review runs."""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision = "20260916_16"
down_revision = "20260906_15"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_contract_reviews",
        sa.Column("id", sa.BINARY(16), nullable=False),
        sa.Column("tenant_id", sa.BINARY(16), nullable=False),
        sa.Column("created_by_user_id", sa.BINARY(16), nullable=False),
        sa.Column("created_by_membership_id", sa.BINARY(16), nullable=False),
        sa.Column("idempotency_hash", sa.BINARY(32), nullable=False),
        sa.Column("request_fingerprint", sa.BINARY(32), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("status", sa.String(32), server_default="awaiting_upload", nullable=False),
        sa.Column("pdf_object_key", sa.String(1024), nullable=True),
        sa.Column("pdf_sha256", sa.BINARY(32), nullable=True),
        sa.Column("pdf_size", sa.BigInteger(), nullable=True),
        sa.Column("parsed_document", mysql.JSON(), nullable=True),
        sa.Column("result_json", mysql.JSON(), nullable=True),
        sa.Column("evidence_manifest", mysql.JSON(), nullable=True),
        sa.Column("failure_code", sa.String(64), nullable=True),
        sa.Column("failure_lease_hash", sa.BINARY(32), nullable=True),
        sa.Column("failure_lease_expires_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('awaiting_upload','uploaded','running','draft','no_evidence',"
            "'failed','cancelled')",
            name="ck_tenant_contract_reviews_contract_review_status",
        ),
        sa.CheckConstraint(
            "pdf_size IS NULL OR pdf_size BETWEEN 1 AND 52428800",
            name="ck_tenant_contract_reviews_pdf_size",
        ),
        sa.CheckConstraint(
            "(pdf_object_key IS NULL AND pdf_sha256 IS NULL AND pdf_size IS NULL) OR "
            "(pdf_object_key IS NOT NULL AND pdf_sha256 IS NOT NULL AND pdf_size IS NOT NULL)",
            name="ck_tenant_contract_reviews_pdf_identity",
        ),
        sa.CheckConstraint(
            "(failure_lease_hash IS NULL AND failure_lease_expires_at IS NULL) OR "
            "(failure_lease_hash IS NOT NULL AND failure_lease_expires_at IS NOT NULL)",
            name="ck_tenant_contract_reviews_failure_lease_identity",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(
            ["tenant_id", "created_by_membership_id", "created_by_user_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id", "tenant_memberships.user_id"],
            name="fk_contract_review_owner_membership",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_tenant_contract_reviews_tenant_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "created_by_membership_id",
            "idempotency_hash",
            name="uq_contract_review_tenant_member_idempotency",
        ),
    )
    op.create_index(
        "ix_contract_review_tenant_created",
        "tenant_contract_reviews",
        ["tenant_id", "created_at"],
    )
    op.create_index(
        "ix_contract_review_tenant_status",
        "tenant_contract_reviews",
        ["tenant_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_contract_review_tenant_status", table_name="tenant_contract_reviews")
    op.drop_index("ix_contract_review_tenant_created", table_name="tenant_contract_reviews")
    op.drop_table("tenant_contract_reviews")
