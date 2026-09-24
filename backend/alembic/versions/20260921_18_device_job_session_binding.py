"""Permit device account sessions while preserving job owner and tenant bindings."""

import sqlalchemy as sa

from alembic import op

revision = "20260921_18"
down_revision = "20260921_17"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Keep the tenant/member/user FK intact. The independent session FK now
    # proves the session belongs to that same creator, including account sessions.
    # Add before dropping so a failed DDL never leaves jobs without an owner FK.
    op.create_foreign_key(
        "fk_ai_jobs_user_session_actor", "ai_jobs", "auth_sessions",
        ["created_by_user_id", "created_by_session_id"], ["user_id", "id"],
    )
    op.drop_constraint("fk_ai_jobs_tenant_session_actor", "ai_jobs", type_="foreignkey")


def downgrade() -> None:
    incompatible = op.get_bind().execute(sa.text(
        "SELECT 1 FROM ai_jobs j JOIN auth_sessions s ON s.id=j.created_by_session_id "
        "WHERE s.tenant_id IS NULL OR s.membership_id IS NULL "
        "OR s.tenant_id<>j.tenant_id OR s.membership_id<>j.created_by_membership_id LIMIT 1"
    )).first()
    if incompatible:
        raise RuntimeError(
            "cannot downgrade: jobs bound to a device session require archival review",
        )
    op.create_foreign_key(
        "fk_ai_jobs_tenant_session_actor", "ai_jobs", "auth_sessions",
        ["tenant_id", "created_by_session_id", "created_by_user_id", "created_by_membership_id"],
        ["tenant_id", "id", "user_id", "membership_id"],
    )
    op.drop_constraint("fk_ai_jobs_user_session_actor", "ai_jobs", type_="foreignkey")
