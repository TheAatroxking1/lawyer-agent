"""Persist creator-private conversations and index contract history."""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision = "20260921_17"
down_revision = "20260916_16"
branch_labels = None
depends_on = None


def timestamps():
    return [
        sa.Column(
            name,
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        )
        for name in ("created_at", "updated_at")
    ]


def upgrade() -> None:
    op.create_table(
        "tenant_conversations",
        sa.Column("id", sa.BINARY(16), nullable=False),
        sa.Column("tenant_id", sa.BINARY(16), nullable=False),
        sa.Column("creator_user_id", sa.BINARY(16), nullable=False),
        sa.Column("creator_membership_id", sa.BINARY(16), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("active_request_id", sa.BINARY(16)),
        sa.Column("active_token_hash", sa.BINARY(32)),
        sa.Column("active_expires_at", mysql.DATETIME(fsp=6)),
        *timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["creator_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(
            ["tenant_id", "creator_membership_id", "creator_user_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id", "tenant_memberships.user_id"],
            name="fk_conversation_owner_membership",
        ),
        sa.UniqueConstraint("tenant_id", "id", "creator_user_id", name="uq_conversation_owner"),
        sa.CheckConstraint(
            "(active_request_id IS NULL AND active_token_hash IS NULL "
            "AND active_expires_at IS NULL) OR (active_request_id IS NOT NULL "
            "AND active_token_hash IS NOT NULL AND active_expires_at IS NOT NULL)",
            name="ck_tenant_conversations_conversation_lease",
        ),
    )
    op.create_index(
        "ix_conversation_owner_updated",
        "tenant_conversations",
        ["tenant_id", "creator_user_id", "updated_at", "id"],
    )
    op.create_table(
        "tenant_conversation_messages",
        sa.Column("id", sa.BINARY(16), nullable=False),
        sa.Column("tenant_id", sa.BINARY(16), nullable=False),
        sa.Column("conversation_id", sa.BINARY(16), nullable=False),
        sa.Column("creator_user_id", sa.BINARY(16), nullable=False),
        sa.Column("request_id", sa.BINARY(16), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", mysql.MEDIUMTEXT(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        *timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "conversation_id", "creator_user_id"],
            [
                "tenant_conversations.tenant_id",
                "tenant_conversations.id",
                "tenant_conversations.creator_user_id",
            ],
            name="fk_conversation_message_owner",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "conversation_id",
            "request_id",
            "role",
            name="uq_conversation_request_role",
        ),
        sa.UniqueConstraint(
            "tenant_id", "conversation_id", "sequence", name="uq_conversation_message_sequence"
        ),
        sa.CheckConstraint(
            "role IN ('user','assistant')",
            name="ck_tenant_conversation_messages_conversation_message_role",
        ),
        sa.CheckConstraint(
            "status IN ('streaming','completed','incomplete')",
            name="ck_tenant_conversation_messages_conversation_message_status",
        ),
    )
    op.create_index(
        "ix_conversation_message_order",
        "tenant_conversation_messages",
        ["tenant_id", "conversation_id", "created_at", "id"],
    )
    op.create_index(
        "ix_contract_review_owner_updated",
        "tenant_contract_reviews",
        ["tenant_id", "created_by_user_id", "updated_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_contract_review_owner_updated", table_name="tenant_contract_reviews")
    op.drop_table("tenant_conversation_messages")
    op.drop_table("tenant_conversations")
