"""Guard invitation blind-index rolling downgrades.

Revision ID: 20260902_05
Revises: 20260902_04
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260902_05"
down_revision: str | Sequence[str] | None = "20260902_04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """The runtime readiness and reconciliation service own rolling safety."""


def downgrade() -> None:
    actionable_rows = op.get_bind().scalar(
        sa.text(
            "SELECT COUNT(*) FROM tenant_invitations "
            "WHERE status = 'pending' "
            "AND expires_at > UTC_TIMESTAMP(6)"
        )
    )
    if actionable_rows:
        raise RuntimeError(
            "refusing unsafe downgrade while pending invitations remain"
        )
