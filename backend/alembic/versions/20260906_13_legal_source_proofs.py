"""Persist original/input proof per legal version without backfilling old evidence.

Revision ID: 20260906_13
Revises: 20260905_12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "20260906_13"
down_revision: str | Sequence[str] | None = "20260905_12"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "legal_version_source_proofs",
        sa.Column("version_id", sa.BINARY(16), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=False),
        sa.Column("input_ref", sa.Text(), nullable=False),
        sa.Column("source_sha256", sa.BINARY(32), nullable=False),
        sa.Column("input_sha256", sa.BINARY(32), nullable=False),
        sa.Column("structure_sha256", sa.BINARY(32), nullable=False),
        sa.Column("loader_version", sa.String(64), nullable=False),
        sa.Column("parser_version", sa.String(64), nullable=False),
        sa.Column("converter_version", sa.String(128)),
        sa.Column("converter_fingerprint", sa.String(64)),
        sa.Column("recovery_reason", sa.String(64)),
        sa.Column("quality_flags", sa.JSON(), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.PrimaryKeyConstraint("version_id"),
        sa.ForeignKeyConstraint(
            ["version_id"], ["legal_versions.id"], name="fk_legal_source_proofs_version",
        ),
        sa.CheckConstraint(
            "(converter_version IS NULL AND converter_fingerprint IS NULL) OR "
            "(converter_version IS NOT NULL AND converter_fingerprint IS NOT NULL)",
            name="converter_pair",
        ),
        sa.CheckConstraint(
            "recovery_reason IS NULL OR (converter_version IS NOT NULL AND "
            "CAST(converter_version AS BINARY) = CAST('binary-word-static-text-v1' AS BINARY) AND "
            "CAST(recovery_reason AS BINARY) = CAST('office_validation_failed' AS BINARY))",
            name="recovery",
        ),
        sa.CheckConstraint(
            "JSON_TYPE(quality_flags) = 'ARRAY' AND JSON_LENGTH(quality_flags) <= 64",
            name="flags",
        ),
    )


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT 1 FROM legal_version_source_proofs LIMIT 1")).first():
        raise RuntimeError("cannot downgrade: source proofs require explicit archival review")
    op.drop_table("legal_version_source_proofs")
