from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from lawyer_agent.infrastructure.persistence.base import Base
from lawyer_agent.infrastructure.persistence.models._mixins import TimestampMixin, VersionMixin
from lawyer_agent.infrastructure.persistence.types import UTC_DATETIME, UuidBinary


class AuthorizationCacheInvalidationOutboxModel(VersionMixin, TimestampMixin, Base):
    __tablename__ = "authz_cache_invalidation_outbox"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','processing','completed')",
            name="outbox_status",
        ),
        CheckConstraint(
            "authz_version BETWEEN 1 AND 2147483647",
            name="outbox_authz_version",
        ),
        CheckConstraint(
            "attempt_count BETWEEN 0 AND 2147483647",
            name="outbox_attempt_count",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "membership_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id"],
            name="fk_authz_cache_outbox_tenant_membership",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "idempotency_record_id"],
            ["idempotency_records.tenant_id", "idempotency_records.id"],
            name="fk_authz_cache_outbox_tenant_idempotency",
        ),
        UniqueConstraint(
            "tenant_id",
            "membership_id",
            "authz_version",
            name="uq_authz_cache_outbox_membership_version",
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_record_id",
            name="uq_authz_cache_outbox_tenant_idempotency",
        ),
        Index("ix_authz_cache_outbox_dispatch", "status", "available_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    membership_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    authz_version: Mapped[int] = mapped_column(nullable=False)
    idempotency_record_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    attempt_count: Mapped[int] = mapped_column(nullable=False, server_default="0")
    available_at: Mapped[datetime] = mapped_column(UTC_DATETIME, nullable=False)
    processing_started_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    completed_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    last_error_code: Mapped[str | None] = mapped_column(String(64))
