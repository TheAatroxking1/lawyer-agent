"""Add stable category to legal instruments.

Revision ID: 20260905_11
Revises: 20260905_10
Create Date: 2026-09-05
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260905_11"
down_revision: str | Sequence[str] | None = "20260905_10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CATEGORY_CHECK = (
    "CAST(category AS BINARY) IN ("
    "CAST('constitution' AS BINARY),CAST('law' AS BINARY),"
    "CAST('administrative_regulation' AS BINARY),"
    "CAST('judicial_interpretation' AS BINARY),"
    "CAST('local_regulation' AS BINARY),"
    "CAST('supervisory_regulation' AS BINARY),CAST('unknown' AS BINARY))"
)


def upgrade() -> None:
    op.add_column(
        "legal_instruments",
        sa.Column(
            "category",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'unknown'"),
        ),
    )
    op.create_check_constraint(
        "ck_legal_instruments_category", "legal_instruments", _CATEGORY_CHECK
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_legal_instruments_category", "legal_instruments", type_="check"
    )
    op.drop_column("legal_instruments", "category")
