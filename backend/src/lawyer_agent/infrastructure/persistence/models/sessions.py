from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BINARY,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from lawyer_agent.infrastructure.persistence.base import Base
from lawyer_agent.infrastructure.persistence.models._mixins import TimestampMixin, VersionMixin
from lawyer_agent.infrastructure.persistence.types import UTC_DATETIME, UuidBinary


class AuthSessionModel(VersionMixin, TimestampMixin, Base):
    __tablename__ = "auth_sessions"
    __table_args__ = (
        CheckConstraint(
            "(tenant_id IS NULL AND membership_id IS NULL "
            "AND authz_version_at_issue IS NULL) OR "
            "(tenant_id IS NOT NULL AND membership_id IS NOT NULL "
            "AND authz_version_at_issue IS NOT NULL)",
            name="auth_session_tenant_context",
        ),
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("user_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "membership_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "user_id", "membership_id"],
            [
                "tenant_memberships.tenant_id",
                "tenant_memberships.user_id",
                "tenant_memberships.id",
            ],
            name="fk_auth_sessions_tenant_id_user_membership",
        ),
        Index("ix_auth_sessions_user_id", "user_id"),
        Index("ix_auth_sessions_current_family_id", "current_family_id"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(UuidBinary(), ForeignKey("users.id"), nullable=False)
    tenant_id: Mapped[UUID | None] = mapped_column(UuidBinary(), ForeignKey("tenants.id"))
    membership_id: Mapped[UUID | None] = mapped_column(UuidBinary())
    current_family_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    auth_version_at_issue: Mapped[int] = mapped_column(nullable=False)
    authz_version_at_issue: Mapped[int | None] = mapped_column()
    revoked_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    revocation_reason: Mapped[str | None] = mapped_column(String(128))
    last_seen_at: Mapped[datetime] = mapped_column(UTC_DATETIME, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTC_DATETIME, nullable=False)


class RefreshTokenRecordModel(VersionMixin, TimestampMixin, Base):
    __tablename__ = "refresh_token_records"
    __table_args__ = (
        CheckConstraint(
            "(tenant_id IS NULL AND membership_id IS NULL) OR "
            "(tenant_id IS NOT NULL AND membership_id IS NOT NULL)",
            name="refresh_token_tenant_context",
        ),
        UniqueConstraint("token_hash"),
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("session_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "membership_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "user_id", "membership_id"],
            [
                "tenant_memberships.tenant_id",
                "tenant_memberships.user_id",
                "tenant_memberships.id",
            ],
            name="fk_refresh_token_records_tenant_id_user_membership",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "session_id"],
            ["auth_sessions.tenant_id", "auth_sessions.id"],
        ),
        ForeignKeyConstraint(
            ["user_id", "session_id"],
            ["auth_sessions.user_id", "auth_sessions.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "replaced_by_id"],
            ["refresh_token_records.tenant_id", "refresh_token_records.id"],
            name="fk_refresh_token_records_tenant_id_replacement",
        ),
        ForeignKeyConstraint(
            ["session_id", "replaced_by_id"],
            ["refresh_token_records.session_id", "refresh_token_records.id"],
            name="fk_refresh_token_records_session_id_replacement",
        ),
        Index("ix_refresh_token_records_family_id", "family_id"),
        Index("ix_refresh_token_records_replaced_by_id", "replaced_by_id"),
        Index("ix_refresh_token_records_session_id", "session_id"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    token_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    family_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    session_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    user_id: Mapped[UUID] = mapped_column(UuidBinary(), ForeignKey("users.id"), nullable=False)
    tenant_id: Mapped[UUID | None] = mapped_column(UuidBinary(), ForeignKey("tenants.id"))
    membership_id: Mapped[UUID | None] = mapped_column(UuidBinary())
    issued_at: Mapped[datetime] = mapped_column(
        UTC_DATETIME, nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    expires_at: Mapped[datetime] = mapped_column(UTC_DATETIME, nullable=False)
    idle_expires_at: Mapped[datetime] = mapped_column(UTC_DATETIME, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    revoked_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    replaced_by_id: Mapped[UUID | None] = mapped_column(UuidBinary())
    revocation_reason: Mapped[str | None] = mapped_column(String(128))
    device_label: Mapped[str | None] = mapped_column(String(128))
    user_agent_hash: Mapped[bytes | None] = mapped_column(BINARY(32))
    ip_hash: Mapped[bytes | None] = mapped_column(BINARY(32))
