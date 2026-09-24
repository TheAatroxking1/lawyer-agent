"""Preserve durable dataset publication decisions and unresolved alias ownership.

Revision ID: 20260906_14
Revises: 20260906_13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "20260906_14"
down_revision: str | Sequence[str] | None = "20260906_13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "legal_dataset_publications",
        sa.Column("id", sa.BINARY(16), nullable=False),
        sa.Column("active_alias", sa.String(64)),
        sa.Column("alias", sa.String(64), nullable=False),
        sa.Column("index_name", sa.String(255), nullable=False),
        sa.Column("previous_target", sa.String(255)),
        sa.Column("candidate_json", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column("completed_at", mysql.DATETIME(fsp=6)),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("active_alias"),
        sa.UniqueConstraint("index_name"),
        sa.CheckConstraint(
            "CAST(state AS BINARY) IN ('ready','switching','acknowledged','completed')",
            name="state",
        ),
        sa.CheckConstraint(
            "(CAST(state AS BINARY) = 'completed' AND active_alias IS NULL "
            "AND completed_at IS NOT NULL) OR "
            "(CAST(state AS BINARY) <> 'completed' AND active_alias IS NOT NULL "
            "AND CAST(active_alias AS BINARY) = CAST(alias AS BINARY) "
            "AND completed_at IS NULL)",
            name="active",
        ),
        sa.CheckConstraint("JSON_TYPE(candidate_json) = 'OBJECT'", name="candidate"),
    )


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT 1 FROM legal_dataset_publications LIMIT 1")).first():
        raise RuntimeError("cannot downgrade: publication history requires archival review")
    op.drop_table("legal_dataset_publications")
