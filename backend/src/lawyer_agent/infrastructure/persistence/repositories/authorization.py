from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from lawyer_agent.domain.authorization import AuthorizationScope
from lawyer_agent.domain.common import require_uuid7
from lawyer_agent.domain.tenancy import TenantContext
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
        require_uuid7(role_id, field="role_id")
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
        require_uuid7(membership_id, field="membership_id")
        require_uuid7(role_id, field="role_id")
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
        now: datetime,
    ) -> None:
        _require_context(context)
        require_uuid7(membership_id, field="membership_id")
        require_uuid7(role_id, field="role_id")
        require_uuid7(assigned_by_membership_id, field="assigned_by_membership_id")
        effective_at = _naive_utc(now)
        membership_ids = sorted({membership_id, assigned_by_membership_id}, key=str)
        memberships = (
            await self._session.scalars(
                select(TenantMembershipModel)
                .where(
                    TenantMembershipModel.tenant_id == context.tenant_id,
                    TenantMembershipModel.id.in_(membership_ids),
                )
                .order_by(TenantMembershipModel.id)
                .with_for_update()
            )
        ).all()
        memberships_by_id = {model.id: model for model in memberships}
        if not _active_membership(memberships_by_id.get(membership_id), effective_at):
            raise ValueError("membership unavailable for role assignment")
        if not _active_membership(
            memberships_by_id.get(assigned_by_membership_id), effective_at
        ):
            raise ValueError("assigner unavailable for role assignment")
        role = await self._session.scalar(
            select(TenantRoleModel)
            .where(
                TenantRoleModel.tenant_id == context.tenant_id,
                TenantRoleModel.id == role_id,
            )
            .with_for_update()
        )
        if role is None or role.status != "active":
            raise ValueError("role unavailable for role assignment")
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
        require_uuid7(membership_id, field="membership_id")
        require_uuid7(role_id, field="role_id")
        result = cast(CursorResult[Any], await self._session.execute(
            delete(MembershipRoleAssignmentModel).where(
                MembershipRoleAssignmentModel.tenant_id == context.tenant_id,
                MembershipRoleAssignmentModel.membership_id == membership_id,
                MembershipRoleAssignmentModel.tenant_role_id == role_id,
            )
        ))
        return result.rowcount == 1

def _require_context(context: TenantContext) -> None:
    if not isinstance(context, TenantContext) or not isinstance(
        context.scope, AuthorizationScope
    ):
        raise ValueError("invalid tenant context")
    require_uuid7(context.tenant_id, field="tenant context tenant_id")
    require_uuid7(context.membership_id, field="tenant context membership_id")
    require_uuid7(
        context.membership_user_id,
        field="tenant context membership_user_id",
    )
    if context.department_id is not None:
        require_uuid7(context.department_id, field="tenant context department_id")
    for department_id in context.scope.department_ids:
        require_uuid7(department_id, field="authorization scope department_id")


def _naive_utc(value: object) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != UTC.utcoffset(value)
    ):
        raise ValueError("role assignment time must be UTC-aware")
    return value.astimezone(UTC).replace(tzinfo=None)


def _active_membership(model: TenantMembershipModel | None, now: datetime) -> bool:
    return (
        model is not None
        and model.status == "active"
        and model.valid_from <= now
        and (model.valid_until is None or model.valid_until > now)
    )
