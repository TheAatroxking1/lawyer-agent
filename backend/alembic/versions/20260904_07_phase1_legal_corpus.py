"""Add the phase 1 legal corpus schema.

Revision ID: 20260904_07
Revises: 20260903_06
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "20260904_07"
down_revision: str | Sequence[str] | None = "20260903_06"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE_OPTIONS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}
_DATETIME = mysql.DATETIME(fsp=6)
_ID = sa.BINARY(16)


def _uuid_column(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(name, _ID, nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "legal_instruments",
        _uuid_column("id", nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("issuing_authority", sa.String(256), nullable=False),
        sa.Column("jurisdiction", sa.String(64), nullable=False),
        sa.Column("region_code", sa.String(16), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "created_at", _DATETIME, server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", _DATETIME, server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_legal_instruments"),
        sa.UniqueConstraint("title", "jurisdiction", name="uq_legal_instruments_title_jur"),
        **_TABLE_OPTIONS,
    )

    op.create_table(
        "legal_versions",
        _uuid_column("id", nullable=False),
        _uuid_column("instrument_id", nullable=False),
        sa.Column("version_label", sa.String(128), nullable=False),
        sa.Column("law_number", sa.String(256), nullable=True),
        sa.Column(
            "status",
            sa.String(32),
            server_default="status_unknown",
            nullable=False,
        ),
        sa.Column("published_on", sa.Date(), nullable=True),
        sa.Column("effective_on", sa.Date(), nullable=True),
        sa.Column("repealed_on", sa.Date(), nullable=True),
        sa.Column("content_hash", sa.BINARY(32), nullable=True),
        sa.Column("source_ref", sa.String(512), nullable=True),
        sa.Column("dataset_version", sa.String(64), nullable=True),
        sa.Column("parser_version", sa.String(64), nullable=True),
        sa.Column(
            "created_at", _DATETIME, server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", _DATETIME, server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_legal_versions"),
        sa.UniqueConstraint(
            "instrument_id", "version_label", name="uq_legal_versions_inst_label"
        ),
        sa.CheckConstraint(
            "status IN ('current','repealed','status_unknown','historical','draft')",
            name="ck_legal_versions_status",
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["legal_instruments.id"],
            name="fk_legal_versions_instrument_id_legal_instruments",
        ),
        **_TABLE_OPTIONS,
    )
    op.create_index(
        "ix_legal_versions_instrument_id", "legal_versions", ["instrument_id"]
    )

    op.create_table(
        "legal_provisions",
        _uuid_column("id", nullable=False),
        _uuid_column("version_id", nullable=False),
        sa.Column("provision_no", sa.String(64), nullable=False),
        sa.Column("level", sa.String(16), nullable=False),
        sa.Column("structure_path_json", sa.JSON(), nullable=True),
        sa.Column("title", sa.String(512), nullable=True),
        sa.Column("full_text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.BINARY(32), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", _DATETIME, server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", _DATETIME, server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_legal_provisions"),
        sa.UniqueConstraint("version_id", "provision_no", name="uq_legal_provisions_ver_no"),
        sa.CheckConstraint(
            "level IN ('part','chapter','section','article','paragraph','item','sub_item')",
            name="ck_legal_provisions_level",
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["legal_versions.id"],
            name="fk_legal_provisions_version_id_legal_versions",
        ),
        **_TABLE_OPTIONS,
    )
    op.create_index(
        "ix_legal_provisions_version_id", "legal_provisions", ["version_id"]
    )

    op.create_table(
        "legal_chunks",
        _uuid_column("id", nullable=False),
        _uuid_column("version_id", nullable=False),
        _uuid_column("provision_id", nullable=False),
        _uuid_column("parent_chunk_id", nullable=True),
        sa.Column("chunk_type", sa.String(16), server_default="provision", nullable=False),
        sa.Column("quality", sa.String(16), server_default="ok", nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.BINARY(32), nullable=False),
        sa.Column("parser_version", sa.String(64), nullable=True),
        sa.Column(
            "created_at", _DATETIME, server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", _DATETIME, server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_legal_chunks"),
        sa.UniqueConstraint(
            "version_id", "provision_id", "chunk_type", name="uq_legal_chunks_prov_type"
        ),
        sa.CheckConstraint(
            "quality IN ('ok','degraded','failed')", name="ck_legal_chunks_quality"
        ),
        sa.ForeignKeyConstraint(
            ["version_id"], ["legal_versions.id"], name="fk_legal_chunks_version_id_legal_versions"
        ),
        sa.ForeignKeyConstraint(
            ["provision_id"],
            ["legal_provisions.id"],
            name="fk_legal_chunks_provision_id_legal_provisions",
        ),
        sa.ForeignKeyConstraint(
            ["parent_chunk_id"], ["legal_chunks.id"], name="fk_legal_chunks_parent_id_legal_chunks"
        ),
        **_TABLE_OPTIONS,
    )
    op.create_index("ix_legal_chunks_provision_id", "legal_chunks", ["provision_id"])

    op.create_table(
        "legal_dataset_snapshots",
        _uuid_column("id", nullable=False),
        sa.Column("dataset_name", sa.String(64), nullable=False),
        sa.Column("parser_version", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), server_default="pending", nullable=False),
        sa.Column("manifest_json", sa.JSON(), nullable=False),
        sa.Column("quality_metrics_json", sa.JSON(), nullable=False),
        sa.Column("released_at", _DATETIME, nullable=True),
        sa.Column(
            "created_at", _DATETIME, server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", _DATETIME, server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_legal_dataset_snapshots"),
        sa.UniqueConstraint("dataset_name", name="uq_legal_dataset_snapshots_name"),
        sa.CheckConstraint(
            "state IN ('pending','published','superseded','rejected')",
            name="ck_legal_dataset_snapshots_state",
        ),
        **_TABLE_OPTIONS,
    )

    op.create_table(
        "legal_load_batches",
        _uuid_column("id", nullable=False),
        sa.Column("batch_no", sa.String(64), nullable=False),
        sa.Column("source_ref", sa.String(512), nullable=False),
        sa.Column("file_sha256", sa.BINARY(32), nullable=False),
        sa.Column("parser_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), server_default="inventoried", nullable=False),
        sa.Column("item_counts_json", sa.JSON(), nullable=False),
        sa.Column("started_at", _DATETIME, nullable=True),
        sa.Column("completed_at", _DATETIME, nullable=True),
        sa.Column("error_message", sa.String(2048), nullable=True),
        sa.Column(
            "created_at", _DATETIME, server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", _DATETIME, server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_legal_load_batches"),
        sa.UniqueConstraint("file_sha256", name="uq_legal_load_batches_file_sha256"),
        sa.UniqueConstraint("batch_no", name="uq_legal_load_batches_batch_no"),
        sa.CheckConstraint(
            "status IN ('inventoried','parsing','completed','failed')",
            name="ck_legal_load_batches_status",
        ),
        sa.CheckConstraint(
            "item_counts_json IS NOT NULL", name="ck_legal_load_batches_counts"
        ),
        **_TABLE_OPTIONS,
    )

    op.create_table(
        "legal_quality_issues",
        _uuid_column("id", nullable=False),
        _uuid_column("batch_id", nullable=False),
        sa.Column("file_sha256", sa.BINARY(32), nullable=False),
        sa.Column("issue_type", sa.String(64), nullable=False),
        sa.Column("message", sa.String(2048), nullable=False),
        sa.Column(
            "created_at", _DATETIME, server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", _DATETIME, server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_legal_quality_issues"),
        sa.ForeignKeyConstraint(
            ["batch_id"], ["legal_load_batches.id"], name="fk_legal_quality_issues_batch_id"
        ),
        **_TABLE_OPTIONS,
    )
    op.create_index("ix_legal_quality_issues_batch_id", "legal_quality_issues", ["batch_id"])


def downgrade() -> None:
    op.drop_table("legal_quality_issues")
    op.drop_table("legal_load_batches")
    op.drop_table("legal_dataset_snapshots")
    op.drop_table("legal_chunks")
    op.drop_table("legal_provisions")
    op.drop_table("legal_versions")
    op.drop_table("legal_instruments")
