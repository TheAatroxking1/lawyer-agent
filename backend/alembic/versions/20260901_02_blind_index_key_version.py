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

_INSERT_TRIGGER = "trg_auth_identities_bi_version_insert"
_UPDATE_TRIGGER = "trg_auth_identities_bi_version_update"


def upgrade() -> None:
    # Revision 01 used the same version for ciphertext and blind indexes, but
    # that shared version was not necessarily v1. Keep the new column nullable
    # during the rolling window and let a compatibility trigger derive the
    # value from each row's key_version when an old binary omits it.
    op.add_column(
        "auth_identities",
        sa.Column(
            "blind_index_key_version",
            sa.SmallInteger(),
            nullable=True,
        ),
    )
    op.execute(
        """
        CREATE TRIGGER trg_auth_identities_bi_version_insert
        BEFORE INSERT ON auth_identities
        FOR EACH ROW
        SET NEW.blind_index_key_version =
            COALESCE(NEW.blind_index_key_version, NEW.key_version)
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_auth_identities_bi_version_update
        BEFORE UPDATE ON auth_identities
        FOR EACH ROW
        BEGIN
            IF NEW.blind_index_key_version IS NULL THEN
                SIGNAL SQLSTATE '45000'
                    SET MESSAGE_TEXT =
                        'blind_index_key_version must be explicit on update';
            END IF;
            IF (
                NEW.blind_index_key_version <> OLD.blind_index_key_version
                AND NEW.subject_blind_index = OLD.subject_blind_index
            ) OR (
                NEW.blind_index_key_version = OLD.blind_index_key_version
                AND NEW.subject_blind_index <> OLD.subject_blind_index
            ) THEN
                SIGNAL SQLSTATE '45000'
                    SET MESSAGE_TEXT =
                        'blind-index digest and key version must change together';
            END IF;
        END
        """
    )
    op.execute(
        "UPDATE auth_identities SET blind_index_key_version = key_version "
        "WHERE blind_index_key_version IS NULL"
    )
    op.create_check_constraint(
        "auth_identity_cipher_key_version",
        "auth_identities",
        "key_version BETWEEN 1 AND 32767",
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
    # Contract gate for a future forward migration: all revision 01 writers
    # must be drained, no NULL version rows may remain, and rotation telemetry
    # must confirm every persisted version is readable before these triggers
    # are removed and the column is made NOT NULL.


def downgrade() -> None:
    decoupled_rows = op.get_bind().scalar(
        sa.text(
            "SELECT COUNT(*) FROM auth_identities "
            "WHERE blind_index_key_version IS NULL "
            "OR blind_index_key_version <> key_version"
        )
    )
    if decoupled_rows:
        raise RuntimeError(
            "refusing unsafe downgrade while decoupled blind-index rows remain"
        )

    op.execute(f"DROP TRIGGER IF EXISTS {_UPDATE_TRIGGER}")
    op.execute(f"DROP TRIGGER IF EXISTS {_INSERT_TRIGGER}")
    op.drop_index("ix_auth_identities_blind_key_version", table_name="auth_identities")
    op.drop_constraint("uq_auth_identities_subject", "auth_identities", type_="unique")
    op.drop_constraint(
        "auth_identity_blind_key_version",
        "auth_identities",
        type_="check",
    )
    op.drop_constraint(
        "auth_identity_cipher_key_version",
        "auth_identities",
        type_="check",
    )
    op.create_unique_constraint(
        "uq_auth_identities_kind",
        "auth_identities",
        ["kind", "issuer", "subject_blind_index"],
    )
    op.drop_column("auth_identities", "blind_index_key_version")
