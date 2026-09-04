"""Add the phase 2 tenant matter and document schema.

Revision ID: 20260905_08
Revises: 20260904_07
Create Date: 2026-09-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "20260905_08"
down_revision: str | Sequence[str] | None = "20260904_07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE_OPTIONS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}
_DATETIME = mysql.DATETIME(fsp=6)
_ID = sa.BINARY(16)


def _id(name: str) -> sa.Column:
    return sa.Column(name, _ID, nullable=False)


def upgrade() -> None:
    op.create_table(
        "tenant_matters",
        _id("id"),
        _id("tenant_id"),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), server_default="open", nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        _id("created_by_user_id"),
        _id("created_by_membership_id"),
        sa.Column("owner_membership_id", _ID, nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "created_at", _DATETIME, server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", _DATETIME, server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tenant_matters"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_tenant_matters_tenant_id"),
        sa.CheckConstraint(
            "kind IN ('contract_review','litigation','legal_advice','compliance','other')",
            name="ck_tenant_matters_kind",
        ),
        sa.CheckConstraint(
            "status IN ('open','active','closed','archived')",
            name="ck_tenant_matters_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_tenant_matters_tenant_id_tenants"
        ),
        **_TABLE_OPTIONS,
    )
    op.create_index(
        "ix_tenant_matters_tenant_status", "tenant_matters", ["tenant_id", "status"]
    )

    op.create_table(
        "tenant_matter_parties",
        _id("id"),
        _id("tenant_id"),
        _id("matter_id"),
        sa.Column("display_name", sa.String(512), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "created_at", _DATETIME, server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", _DATETIME, server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tenant_matter_parties"),
        sa.UniqueConstraint(
            "tenant_id", "matter_id", "id", name="uq_matter_parties_tenant_id"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "matter_id"],
            ["tenant_matters.tenant_id", "tenant_matters.id"],
            name="fk_matter_parties_matter",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_matter_parties_tenant_id_tenants"
        ),
        **_TABLE_OPTIONS,
    )
    op.create_index(
        "ix_matter_parties_matter_id", "tenant_matter_parties", ["tenant_id", "matter_id"]
    )

    op.create_table(
        "tenant_documents",
        _id("id"),
        _id("tenant_id"),
        _id("matter_id"),
        sa.Column("display_name", sa.String(512), nullable=False),
        sa.Column(
            "current_version_no", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "created_at", _DATETIME, server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", _DATETIME, server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tenant_documents"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_tenant_documents_tenant_id"),
        sa.UniqueConstraint(
            "tenant_id", "matter_id", "id", name="uq_tenant_documents_matter_id"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "matter_id"],
            ["tenant_matters.tenant_id", "tenant_matters.id"],
            name="fk_tenant_documents_matter",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_tenant_documents_tenant_id_tenants"
        ),
        **_TABLE_OPTIONS,
    )
    op.create_index(
        "ix_tenant_documents_matter_id", "tenant_documents", ["tenant_id", "matter_id"]
    )

    op.create_table(
        "tenant_document_versions",
        _id("id"),
        _id("tenant_id"),
        _id("document_id"),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("object_key", sa.String(512), nullable=False),
        sa.Column("sha256", sa.BINARY(32), nullable=False),
        sa.Column(
            "upload_status", sa.String(16), server_default="uploaded", nullable=False
        ),
        sa.Column("review_status", sa.String(24), nullable=True),
        sa.Column("file_name", sa.String(256), nullable=True),
        sa.Column("mime_type", sa.String(128), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("parser_version", sa.String(64), nullable=True),
        sa.Column("parse_error", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", _ID, nullable=True),
        sa.Column("created_by_membership_id", _ID, nullable=True),
        sa.Column("uploaded_at", _DATETIME, nullable=True),
        sa.Column(
            "created_at", _DATETIME, server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", _DATETIME, server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tenant_document_versions"),
        sa.UniqueConstraint(
            "tenant_id", "document_id", "version_no",
            name="uq_document_versions_doc_no",
        ),
        sa.CheckConstraint(
            "kind IN ('original','derived')",
            name="ck_document_versions_kind",
        ),
        sa.CheckConstraint(
            "upload_status IN ('uploaded','validating','accepted','needs_review',"
            "'parsing','ready','failed')",
            name="ck_document_versions_upload_status",
        ),
        sa.CheckConstraint(
            "review_status IS NULL OR review_status IN "
            "('draft','pending_review','changes_requested','approved','rejected')",
            name="ck_document_versions_review_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["tenant_documents.tenant_id", "tenant_documents.id"],
            name="fk_document_versions_document",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_document_versions_tenant_id_tenants"
        ),
        **_TABLE_OPTIONS,
    )
    op.create_index(
        "ix_document_versions_document_id",
        "tenant_document_versions",
        ["tenant_id", "document_id"],
    )


def downgrade() -> None:
    op.drop_table("tenant_document_versions")
    op.drop_table("tenant_documents")
    op.drop_table("tenant_matter_parties")
    op.drop_table("tenant_matters")
