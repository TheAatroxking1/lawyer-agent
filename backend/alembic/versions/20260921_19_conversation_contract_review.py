"""Bind a durable conversation to one owned contract review."""

import sqlalchemy as sa

from alembic import op

revision = "20260921_19"
down_revision = "20260921_18"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_conversations",
        sa.Column("contract_review_id", sa.BINARY(16), nullable=True),
    )
    op.create_foreign_key(
        "fk_conversation_contract_review",
        "tenant_conversations",
        "tenant_contract_reviews",
        ["tenant_id", "contract_review_id"],
        ["tenant_id", "id"],
    )
    op.create_index(
        "ix_conversation_contract_review",
        "tenant_conversations",
        ["tenant_id", "contract_review_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_conversation_contract_review", table_name="tenant_conversations")
    op.drop_constraint(
        "fk_conversation_contract_review", "tenant_conversations", type_="foreignkey"
    )
    op.drop_column("tenant_conversations", "contract_review_id")
