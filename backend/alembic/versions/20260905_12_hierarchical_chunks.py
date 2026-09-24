"""Allow multiple hierarchical chunks of the same type per provision.

Revision ID: 20260905_12
Revises: 20260905_11
Create Date: 2026-09-05
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260905_12"
down_revision: str | Sequence[str] | None = "20260905_11"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The version foreign key previously relied on the leading column of the
    # unique index. Give it a dedicated supporting index before removing that
    # uniqueness constraint.
    op.create_index(
        "ix_legal_chunks_version_id", "legal_chunks", ["version_id"], unique=False
    )
    op.drop_constraint(
        "uq_legal_chunks_prov_type", "legal_chunks", type_="unique"
    )


def downgrade() -> None:
    connection = op.get_bind()
    duplicate = connection.execute(
        sa.text(
            "SELECT 1 FROM legal_chunks "
            "GROUP BY version_id, provision_id, chunk_type "
            "HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "cannot downgrade: duplicate hierarchical chunks require explicit cleanup"
        )
    op.create_unique_constraint(
        "uq_legal_chunks_prov_type",
        "legal_chunks",
        ["version_id", "provision_id", "chunk_type"],
    )
    op.drop_index("ix_legal_chunks_version_id", table_name="legal_chunks")
