from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BINARY,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from lawyer_agent.infrastructure.persistence.base import Base
from lawyer_agent.infrastructure.persistence.models._mixins import TimestampMixin, VersionMixin
from lawyer_agent.infrastructure.persistence.types import UTC_DATETIME, UuidBinary


class TenantModel(VersionMixin, TimestampMixin, Base):
    __tablename__ = "tenants"
    __table_args__ = (
        CheckConstraint(
            "tenant_type IN ('law_firm','enterprise','university')", name="tenant_type"
        ),
        CheckConstraint(
            "status IN ('pending_verification','active','suspended','closed')",
            name="tenant_status",
        ),
        CheckConstraint(
            "review_status IN ('pending','approved','rejected')", name="tenant_review_status"
        ),
        Index("ix_tenants_normalized_name", "normalized_name"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(255), nullable=False)
    tenant_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="pending_verification"
    )
    created_by_user_id: Mapped[UUID] = mapped_column(
        UuidBinary(), ForeignKey("users.id"), nullable=False
    )
    review_status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="pending")
    reviewed_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    reviewed_by_user_id: Mapped[UUID | None] = mapped_column(UuidBinary(), ForeignKey("users.id"))
    review_reason_code: Mapped[str | None] = mapped_column(String(64))


class DepartmentModel(VersionMixin, TimestampMixin, Base):
    __tablename__ = "departments"
    __table_args__ = (
        CheckConstraint("status IN ('active','disabled')", name="department_status"),
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "parent_id", "name", name="uq_departments_tenant_id_parent"),
        ForeignKeyConstraint(
            ["tenant_id", "parent_id"], ["departments.tenant_id", "departments.id"]
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(UuidBinary(), ForeignKey("tenants.id"), nullable=False)
    parent_id: Mapped[UUID | None] = mapped_column(UuidBinary())
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="active")


class TenantMembershipModel(VersionMixin, TimestampMixin, Base):
    __tablename__ = "tenant_memberships"
    __table_args__ = (
        CheckConstraint(
            "member_type IN ('owner','internal','student','external_client')",
            name="tenant_membership_type",
        ),
        CheckConstraint(
            "status IN ('invited','active','suspended','revoked')",
            name="tenant_membership_status",
        ),
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "user_id", name="uq_tenant_memberships_tenant_id_user"),
        UniqueConstraint(
            "tenant_id", "user_id", "id", name="uq_tenant_memberships_tenant_id_user_id"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "department_id"], ["departments.tenant_id", "departments.id"]
        ),
        Index("ix_tenant_memberships_user_id", "user_id"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(UuidBinary(), ForeignKey("tenants.id"), nullable=False)
    user_id: Mapped[UUID] = mapped_column(UuidBinary(), ForeignKey("users.id"), nullable=False)
    department_id: Mapped[UUID | None] = mapped_column(UuidBinary())
    member_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="invited")
    valid_from: Mapped[datetime] = mapped_column(UTC_DATETIME, nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    authz_version: Mapped[int] = mapped_column(nullable=False, server_default="1")


class TenantInvitationModel(VersionMixin, TimestampMixin, Base):
    __tablename__ = "tenant_invitations"
    __table_args__ = (
        CheckConstraint("target_kind IN ('phone','email')", name="tenant_invitation_target_kind"),
        CheckConstraint(
            "status IN ('pending','accepted','revoked','expired')",
            name="tenant_invitation_status",
        ),
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("token_hash"),
        ForeignKeyConstraint(
            ["tenant_id", "invited_by_membership_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id"],
        ),
        Index("ix_tenant_invitations_target", "tenant_id", "target_blind_index"),
        Index(
            "ix_tenant_invitations_blind_key_version",
            "target_blind_index_key_version",
        ),
        CheckConstraint(
            "target_blind_index_key_version BETWEEN 1 AND 32767",
            name="tenant_invitation_blind_key_version",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(UuidBinary(), ForeignKey("tenants.id"), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    target_blind_index: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    target_blind_index_key_version: Mapped[int | None] = mapped_column(
        SmallInteger,
        nullable=True,
    )
    token_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    invited_by_membership_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTC_DATETIME, nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    revoked_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="pending")
