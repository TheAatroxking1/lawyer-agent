from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
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


class PermissionModel(TimestampMixin, Base):
    __tablename__ = "permission_catalog"
    __table_args__ = (
        CheckConstraint(
            "risk_level IN ('low','medium','high','critical')", name="permission_risk_level"
        ),
        CheckConstraint("status IN ('active','disabled')", name="permission_status"),
        UniqueConstraint("code"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    code: Mapped[str] = mapped_column(String(128), nullable=False)
    resource: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")


class RoleTemplateModel(VersionMixin, TimestampMixin, Base):
    __tablename__ = "role_templates"
    __table_args__ = (
        CheckConstraint("status IN ('active','disabled')", name="role_template_status"),
        UniqueConstraint("code"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")


class RoleTemplatePermissionModel(Base):
    __tablename__ = "role_template_permissions"

    role_template_id: Mapped[UUID] = mapped_column(
        UuidBinary(), ForeignKey("role_templates.id"), primary_key=True
    )
    permission_id: Mapped[UUID] = mapped_column(
        UuidBinary(), ForeignKey("permission_catalog.id"), primary_key=True
    )


class TenantRoleModel(VersionMixin, TimestampMixin, Base):
    __tablename__ = "tenant_roles"
    __table_args__ = (
        CheckConstraint("status IN ('active','disabled')", name="tenant_role_status"),
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "code", name="uq_tenant_roles_tenant_id_code"),
        Index("ix_tenant_roles_template_id", "role_template_id"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(UuidBinary(), ForeignKey("tenants.id"), nullable=False)
    role_template_id: Mapped[UUID | None] = mapped_column(
        UuidBinary(), ForeignKey("role_templates.id")
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    is_custom: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("0"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")


class TenantRolePermissionModel(Base):
    __tablename__ = "tenant_role_permissions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "tenant_role_id"], ["tenant_roles.tenant_id", "tenant_roles.id"]
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(
        UuidBinary(), ForeignKey("tenants.id"), primary_key=True
    )
    tenant_role_id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    permission_id: Mapped[UUID] = mapped_column(
        UuidBinary(), ForeignKey("permission_catalog.id"), primary_key=True
    )


class MembershipRoleAssignmentModel(Base):
    __tablename__ = "membership_role_assignments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "membership_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "tenant_role_id"], ["tenant_roles.tenant_id", "tenant_roles.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "assigned_by_membership_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id"],
            name="fk_membership_role_assignments_tenant_id_assigned_by",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(
        UuidBinary(), ForeignKey("tenants.id"), primary_key=True
    )
    membership_id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    tenant_role_id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    assigned_by_membership_id: Mapped[UUID | None] = mapped_column(UuidBinary())
    created_at: Mapped[datetime] = mapped_column(
        UTC_DATETIME, nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )


class PlatformRoleModel(VersionMixin, TimestampMixin, Base):
    __tablename__ = "platform_roles"
    __table_args__ = (
        CheckConstraint("status IN ('active','disabled')", name="platform_role_status"),
        UniqueConstraint("code"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")


class PlatformRolePermissionModel(Base):
    __tablename__ = "platform_role_permissions"

    platform_role_id: Mapped[UUID] = mapped_column(
        UuidBinary(), ForeignKey("platform_roles.id"), primary_key=True
    )
    permission_id: Mapped[UUID] = mapped_column(
        UuidBinary(), ForeignKey("permission_catalog.id"), primary_key=True
    )


class PlatformRoleAssignmentModel(VersionMixin, TimestampMixin, Base):
    __tablename__ = "platform_role_assignments"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','revoked','expired')", name="platform_role_assignment_status"
        ),
        UniqueConstraint("user_id", "platform_role_id"),
        Index("ix_platform_role_assignments_user_id", "user_id"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(UuidBinary(), ForeignKey("users.id"), nullable=False)
    platform_role_id: Mapped[UUID] = mapped_column(
        UuidBinary(), ForeignKey("platform_roles.id"), nullable=False
    )
    assigned_by_user_id: Mapped[UUID | None] = mapped_column(UuidBinary(), ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    expires_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    revoked_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    revocation_reason: Mapped[str | None] = mapped_column(String(128))


class TenantInvitationRoleAssignmentModel(Base):
    __tablename__ = "tenant_invitation_role_assignments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "invitation_id"],
            ["tenant_invitations.tenant_id", "tenant_invitations.id"],
            name="fk_tenant_invitation_role_assignments_tenant_id_invitations",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "tenant_role_id"],
            ["tenant_roles.tenant_id", "tenant_roles.id"],
            name="fk_tenant_invitation_role_assignments_tenant_id_roles",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(
        UuidBinary(), ForeignKey("tenants.id"), primary_key=True
    )
    invitation_id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    tenant_role_id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
