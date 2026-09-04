"""Add nullable review_reason to tenant document versions.

Revision ID: 20260905_10
Revises: 20260905_09
Create Date: 2026-09-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "20260905_10"
down_revision: str | Sequence[str] | None = "20260905_09"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE_OPTIONS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


def upgrade() -> None:
    op.add_column(
        "tenant_document_versions",
        sa.Column("review_reason", mysql.TEXT(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenant_document_versions", "review_reason")
