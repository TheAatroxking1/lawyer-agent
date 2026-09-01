from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from lawyer_agent.domain.tenancy import TenantContext, is_uuid7
from lawyer_agent.infrastructure.persistence.models import (
    MembershipRoleAssignmentModel,
    PermissionModel,
    TenantMembershipModel,
    TenantRoleModel,
    TenantRolePermissionModel,
)


class AuthorizationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def permission_codes(self, context: TenantContext) -> frozenset[str]:
        _require_context(context)
        statement = (
            select(PermissionModel.code)
            .join(
                TenantRolePermissionModel,
                TenantRolePermissionModel.permission_id == PermissionModel.id,
            )
            .join(
                TenantRoleModel,
                and_(
                    TenantRoleModel.tenant_id == TenantRolePermissionModel.tenant_id,
                    TenantRoleModel.id == TenantRolePermissionModel.tenant_role_id,
                ),
            )
            .join(
                MembershipRoleAssignmentModel,
                and_(
                    MembershipRoleAssignmentModel.tenant_id == TenantRoleModel.tenant_id,
                    MembershipRoleAssignmentModel.tenant_role_id == TenantRoleModel.id,
                ),
            )
            .join(
                TenantMembershipModel,
                and_(
                    TenantMembershipModel.tenant_id
                    == MembershipRoleAssignmentModel.tenant_id,
                    TenantMembershipModel.id
                    == MembershipRoleAssignmentModel.membership_id,
                ),
            )
            .where(
                MembershipRoleAssignmentModel.tenant_id == context.tenant_id,
                MembershipRoleAssignmentModel.membership_id == context.membership_id,
                TenantRolePermissionModel.tenant_id == context.tenant_id,
                TenantRoleModel.tenant_id == context.tenant_id,
                TenantRoleModel.status == "active",
                TenantMembershipModel.tenant_id == context.tenant_id,
                TenantMembershipModel.status == "active",
                PermissionModel.status == "active",
            )
            .distinct()
        )
        return frozenset((await self._session.scalars(statement)).all())

    async def has_permission(self, context: TenantContext, permission_code: str) -> bool:
        if not isinstance(permission_code, str) or not permission_code:
            raise ValueError("permission code is invalid")
        return permission_code in await self.permission_codes(context)

    async def role_exists(self, context: TenantContext, role_id: UUID) -> bool:
        _require_context(context)
        return (
            await self._session.scalar(
                select(TenantRoleModel.id).where(
                    TenantRoleModel.tenant_id == context.tenant_id,
                    TenantRoleModel.id == role_id,
                )
            )
            is not None
        )

    async def assignment_exists(
        self,
        context: TenantContext,
        membership_id: UUID,
        role_id: UUID,
    ) -> bool:
        _require_context(context)
        return (
            await self._session.scalar(
                select(MembershipRoleAssignmentModel.membership_id).where(
                    MembershipRoleAssignmentModel.tenant_id == context.tenant_id,
                    MembershipRoleAssignmentModel.membership_id == membership_id,
                    MembershipRoleAssignmentModel.tenant_role_id == role_id,
                )
            )
            is not None
        )

    async def assign_role(
        self,
        context: TenantContext,
        membership_id: UUID,
        role_id: UUID,
        *,
        assigned_by_membership_id: UUID,
    ) -> None:
        _require_context(context)
        for value in (membership_id, role_id, assigned_by_membership_id):
            if not is_uuid7(value):
                raise ValueError("role assignment identifier must be UUIDv7")
        if not await self._membership_exists(context, membership_id):
            raise ValueError("membership does not match tenant context")
        if not await self._membership_exists(context, assigned_by_membership_id):
            raise ValueError("assigner does not match tenant context")
        if not await self.role_exists(context, role_id):
            raise ValueError("role does not match tenant context")
        self._session.add(
            MembershipRoleAssignmentModel(
                tenant_id=context.tenant_id,
                membership_id=membership_id,
                tenant_role_id=role_id,
                assigned_by_membership_id=assigned_by_membership_id,
            )
        )

    async def unassign_role(
        self,
        context: TenantContext,
        membership_id: UUID,
        role_id: UUID,
    ) -> bool:
        _require_context(context)
        result = cast(CursorResult[Any], await self._session.execute(
            delete(MembershipRoleAssignmentModel).where(
                MembershipRoleAssignmentModel.tenant_id == context.tenant_id,
                MembershipRoleAssignmentModel.membership_id == membership_id,
                MembershipRoleAssignmentModel.tenant_role_id == role_id,
            )
        ))
        return result.rowcount == 1

    async def _membership_exists(
        self,
        context: TenantContext,
        membership_id: UUID,
    ) -> bool:
        return (
            await self._session.scalar(
                select(TenantMembershipModel.id).where(
                    TenantMembershipModel.tenant_id == context.tenant_id,
                    TenantMembershipModel.id == membership_id,
                )
            )
            is not None
        )


def _require_context(context: TenantContext) -> None:
    if (
        not isinstance(context, TenantContext)
        or not is_uuid7(context.tenant_id)
        or not is_uuid7(context.membership_id)
    ):
        raise ValueError("invalid tenant context")
