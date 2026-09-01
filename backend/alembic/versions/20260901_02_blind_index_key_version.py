"""Separate blind-index and ciphertext key versions.

Revision ID: 20260901_02
Revises: 20260901_01
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260901_02"
down_revision: str | Sequence[str] | None = "20260901_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Revision 01 writers only knew the original blind-index key, which is v1.
    # Keep this default during the rolling window so an old binary can omit the
    # new column; new binaries always persist their explicit BI key version.
    op.add_column(
        "auth_identities",
        sa.Column(
            "blind_index_key_version",
            sa.SmallInteger(),
            server_default=sa.text("1"),
            nullable=True,
        ),
    )
    op.execute(
        "UPDATE auth_identities SET blind_index_key_version = 1 "
        "WHERE blind_index_key_version IS NULL"
    )
    op.alter_column(
        "auth_identities",
        "blind_index_key_version",
        existing_type=sa.SmallInteger(),
        server_default=sa.text("1"),
        nullable=False,
    )
    op.create_check_constraint(
        "auth_identity_blind_key_version",
        "auth_identities",
        "blind_index_key_version BETWEEN 1 AND 32767",
    )
    op.drop_constraint("uq_auth_identities_kind", "auth_identities", type_="unique")
    op.create_unique_constraint(
        "uq_auth_identities_subject",
        "auth_identities",
        ["kind", "issuer", "blind_index_key_version", "subject_blind_index"],
    )
    op.create_index(
        "ix_auth_identities_blind_key_version",
        "auth_identities",
        ["blind_index_key_version"],
    )


def downgrade() -> None:
    non_v1_rows = op.get_bind().scalar(
        sa.text(
            "SELECT COUNT(*) FROM auth_identities "
            "WHERE blind_index_key_version <> 1"
        )
    )
    if non_v1_rows:
        raise RuntimeError(
            "refusing unsafe downgrade while non-v1 blind-index rows remain"
        )

    op.drop_index("ix_auth_identities_blind_key_version", table_name="auth_identities")
    op.drop_constraint("uq_auth_identities_subject", "auth_identities", type_="unique")
    op.drop_constraint(
        "auth_identity_blind_key_version",
        "auth_identities",
        type_="check",
    )
    op.create_unique_constraint(
        "uq_auth_identities_kind",
        "auth_identities",
        ["kind", "issuer", "subject_blind_index"],
    )
    op.drop_column("auth_identities", "blind_index_key_version")
