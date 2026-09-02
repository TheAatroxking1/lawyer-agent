"""Track invitation target blind-index key versions.

Revision ID: 20260902_04
Revises: 20260902_03
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260902_04"
down_revision: str | Sequence[str] | None = "20260902_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tenant_invitations",
        sa.Column(
            "target_blind_index_key_version",
            sa.SmallInteger(),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "tenant_invitation_blind_key_version",
        "tenant_invitations",
        "target_blind_index_key_version BETWEEN 1 AND 32767",
    )
    op.create_index(
        "ix_tenant_invitations_blind_key_version",
        "tenant_invitations",
        ["target_blind_index_key_version"],
    )
    # A pre-version digest cannot be attributed to any key safely. Revoke only
    # still-actionable legacy invitations and retain NULL as explicit history.
    op.execute(
        "UPDATE tenant_invitations "
        "SET status = 'revoked', "
        "revoked_at = COALESCE(revoked_at, CURRENT_TIMESTAMP(6)), "
        "version = version + 1, "
        "updated_at = CURRENT_TIMESTAMP(6) "
        "WHERE status = 'pending' "
        "AND target_blind_index_key_version IS NULL"
    )


def downgrade() -> None:
    actionable_rows = op.get_bind().scalar(
        sa.text(
            "SELECT COUNT(*) FROM tenant_invitations "
            "WHERE status = 'pending' "
            "AND target_blind_index_key_version IS NOT NULL"
        )
    )
    if actionable_rows:
        raise RuntimeError(
            "refusing unsafe downgrade while versioned pending invitations remain"
        )
    op.drop_index(
        "ix_tenant_invitations_blind_key_version",
        table_name="tenant_invitations",
    )
    op.drop_constraint(
        "tenant_invitation_blind_key_version",
        "tenant_invitations",
        type_="check",
    )
    op.drop_column("tenant_invitations", "target_blind_index_key_version")
