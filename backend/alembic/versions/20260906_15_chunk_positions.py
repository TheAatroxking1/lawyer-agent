"""Persist optional exact chunk positions without changing historical chunks."""

import sqlalchemy as sa

from alembic import op

revision = "20260906_15"
down_revision = "20260906_14"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("legal_chunks", sa.Column(
        "parent_relative_char_start", sa.Integer(), nullable=True,
    ))
    op.add_column("legal_chunks", sa.Column(
        "parent_relative_char_end", sa.Integer(), nullable=True,
    ))
    op.create_check_constraint("span", "legal_chunks",
        "(parent_relative_char_start IS NULL AND parent_relative_char_end IS NULL) OR "
        "(parent_relative_char_start IS NOT NULL AND parent_relative_char_end IS NOT NULL "
        "AND parent_relative_char_start >= 0 "
        "AND parent_relative_char_end > parent_relative_char_start)")


def downgrade() -> None:
    if op.get_bind().execute(sa.text(
        "SELECT 1 FROM legal_chunks WHERE parent_relative_char_start IS NOT NULL LIMIT 1"
    )).first():
        raise RuntimeError("cannot downgrade: exact chunk positions require archival review")
    op.drop_constraint("span", "legal_chunks", type_="check")
    op.drop_column("legal_chunks", "parent_relative_char_end")
    op.drop_column("legal_chunks", "parent_relative_char_start")
