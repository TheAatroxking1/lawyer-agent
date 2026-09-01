from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lawyer_agent.domain.authorization import AuthorizationScope
from lawyer_agent.domain.common import require_uuid7
from lawyer_agent.domain.tenancy import (
    Department,
    DepartmentStatus,
    Membership,
    MembershipStatus,
    MemberType,
    Tenant,
    TenantContext,
    TenantStatus,
    normalize_tenant_name,
)
from lawyer_agent.infrastructure.persistence.models import (
    DepartmentModel,
    TenantMembershipModel,
    TenantModel,
)


class TenantRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, context: TenantContext, tenant_id: UUID) -> Tenant | None:
        _require_context(context)
        require_uuid7(tenant_id, field="tenant_id")
        model = await self._session.scalar(
            select(TenantModel).where(
                TenantModel.id == context.tenant_id,
                TenantModel.id == tenant_id,
            )
        )
        return None if model is None else _tenant(model)

    async def exists(self, context: TenantContext, tenant_id: UUID) -> bool:
        return await self.get(context, tenant_id) is not None

    async def update_name(
        self,
        context: TenantContext,
        tenant_id: UUID,
        name: str,
        expected_version: int,
    ) -> bool:
        _require_context(context)
        require_uuid7(tenant_id, field="tenant_id")
        normalized_name = normalize_tenant_name(name)
        _require_version(expected_version)
        try:
            result = cast(CursorResult[Any], await self._session.execute(
                update(TenantModel)
                .where(
                    TenantModel.id == context.tenant_id,
                    TenantModel.id == tenant_id,
                    TenantModel.version == expected_version,
                )
                .values(
                    name=normalized_name.display_value,
                    normalized_name=normalized_name.normalized_value,
                    version=TenantModel.version + 1,
                )
            ))
        except IntegrityError:
            raise TenantNameConflictError("tenant name conflicts with an existing tenant") from None
        return result.rowcount == 1

    async def delete(
        self,
        context: TenantContext,
        tenant_id: UUID,
        expected_version: int,
    ) -> bool:
        _require_context(context)
        require_uuid7(tenant_id, field="tenant_id")
        _require_version(expected_version)
        result = cast(CursorResult[Any], await self._session.execute(
            update(TenantModel)
            .where(
                TenantModel.id == context.tenant_id,
                TenantModel.id == tenant_id,
                TenantModel.version == expected_version,
            )
            .values(status=TenantStatus.CLOSED.value, version=TenantModel.version + 1)
        ))
        return result.rowcount == 1


class DepartmentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, context: TenantContext, department_id: UUID) -> Department | None:
        _require_context(context)
        require_uuid7(department_id, field="department_id")
        model = await self._session.scalar(
            select(DepartmentModel).where(
                DepartmentModel.tenant_id == context.tenant_id,
                DepartmentModel.id == department_id,
            )
        )
        return None if model is None else _department(model)

    async def list(self, context: TenantContext) -> tuple[Department, ...]:
        _require_context(context)
        models = (
            await self._session.scalars(
                select(DepartmentModel)
                .where(DepartmentModel.tenant_id == context.tenant_id)
                .order_by(DepartmentModel.created_at, DepartmentModel.id)
            )
        ).all()
        return tuple(_department(model) for model in models)

    async def exists(self, context: TenantContext, department_id: UUID) -> bool:
        return await self.get(context, department_id) is not None

    async def add(self, context: TenantContext, department: Department) -> None:
        _require_context(context)
        if department.tenant_id != context.tenant_id:
            raise ValueError("department does not match tenant context")
        self._session.add(
            DepartmentModel(
                id=department.id,
                tenant_id=context.tenant_id,
                parent_id=department.parent_id,
                name=department.name,
                status=department.status.value,
                version=department.version,
            )
        )

    async def update_name(
        self,
        context: TenantContext,
        department_id: UUID,
        name: str,
        expected_version: int,
    ) -> bool:
        _require_context(context)
        require_uuid7(department_id, field="department_id")
        _require_name(name)
        _require_version(expected_version)
        result = cast(CursorResult[Any], await self._session.execute(
            update(DepartmentModel)
            .where(
                DepartmentModel.tenant_id == context.tenant_id,
                DepartmentModel.id == department_id,
                DepartmentModel.version == expected_version,
            )
            .values(name=name.strip(), version=DepartmentModel.version + 1)
        ))
        return result.rowcount == 1

    async def delete(
        self,
        context: TenantContext,
        department_id: UUID,
        expected_version: int,
    ) -> bool:
        _require_context(context)
        require_uuid7(department_id, field="department_id")
        _require_version(expected_version)
        result = cast(CursorResult[Any], await self._session.execute(
            update(DepartmentModel)
            .where(
                DepartmentModel.tenant_id == context.tenant_id,
                DepartmentModel.id == department_id,
                DepartmentModel.version == expected_version,
            )
            .values(
                status=DepartmentStatus.DISABLED.value,
                version=DepartmentModel.version + 1,
            )
        ))
        return result.rowcount == 1


class MembershipRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, context: TenantContext, membership_id: UUID) -> Membership | None:
        _require_context(context)
        require_uuid7(membership_id, field="membership_id")
        model = await self._session.scalar(
            select(TenantMembershipModel).where(
                TenantMembershipModel.tenant_id == context.tenant_id,
                TenantMembershipModel.id == membership_id,
            )
        )
        return None if model is None else _membership(model)

    async def list(self, context: TenantContext) -> tuple[Membership, ...]:
        _require_context(context)
        models = (
            await self._session.scalars(
                select(TenantMembershipModel)
                .where(TenantMembershipModel.tenant_id == context.tenant_id)
                .order_by(TenantMembershipModel.created_at, TenantMembershipModel.id)
            )
        ).all()
        return tuple(_membership(model) for model in models)

    async def exists(self, context: TenantContext, membership_id: UUID) -> bool:
        return await self.get(context, membership_id) is not None

    async def add(self, context: TenantContext, membership: Membership) -> None:
        _require_context(context)
        if membership.tenant_id != context.tenant_id:
            raise ValueError("membership does not match tenant context")
        self._session.add(
            TenantMembershipModel(
                id=membership.id,
                tenant_id=context.tenant_id,
                user_id=membership.user_id,
                department_id=membership.department_id,
                member_type=membership.member_type.value,
                status=membership.status.value,
                valid_from=_naive(membership.valid_from),
                valid_until=_naive_optional(membership.valid_until),
                authz_version=membership.authz_version,
                version=membership.version,
            )
        )

    async def update_status(
        self,
        context: TenantContext,
        membership_id: UUID,
        status: MembershipStatus,
        expected_version: int,
    ) -> bool:
        _require_context(context)
        require_uuid7(membership_id, field="membership_id")
        if not isinstance(status, MembershipStatus):
            raise ValueError("membership status must be strongly typed")
        _require_version(expected_version)
        result = cast(CursorResult[Any], await self._session.execute(
            update(TenantMembershipModel)
            .where(
                TenantMembershipModel.tenant_id == context.tenant_id,
                TenantMembershipModel.id == membership_id,
                TenantMembershipModel.version == expected_version,
            )
            .values(
                status=status.value,
                authz_version=TenantMembershipModel.authz_version + 1,
                version=TenantMembershipModel.version + 1,
            )
        ))
        return result.rowcount == 1

    async def delete(
        self,
        context: TenantContext,
        membership_id: UUID,
        expected_version: int,
    ) -> bool:
        return await self.update_status(
            context,
            membership_id,
            MembershipStatus.REVOKED,
            expected_version,
        )


class TenantNameConflictError(ValueError):
    """Stable, non-sensitive tenant-name conflict at the repository boundary."""


def _tenant(model: TenantModel) -> Tenant:
    return Tenant(
        id=model.id,
        name=model.name,
        normalized_name=model.normalized_name,
        tenant_type=model.tenant_type,
        status=TenantStatus(model.status),
        version=model.version,
        created_by_user_id=model.created_by_user_id,
        review_status=model.review_status,
    )


def _department(model: DepartmentModel) -> Department:
    return Department(
        id=model.id,
        tenant_id=model.tenant_id,
        parent_id=model.parent_id,
        name=model.name,
        status=DepartmentStatus(model.status),
        version=model.version,
    )


def _membership(model: TenantMembershipModel) -> Membership:
    return Membership(
        id=model.id,
        tenant_id=model.tenant_id,
        user_id=model.user_id,
        department_id=model.department_id,
        member_type=MemberType(model.member_type),
        status=MembershipStatus(model.status),
        valid_from=_aware(model.valid_from),
        valid_until=_aware_optional(model.valid_until),
        authz_version=model.authz_version,
        version=model.version,
    )


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


def _require_name(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 255:
        raise ValueError("name is invalid")


def _require_version(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("version must be a positive integer")


def _naive(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("database timestamp must be UTC-aware")
    return value.astimezone(UTC).replace(tzinfo=None)


def _naive_optional(value: datetime | None) -> datetime | None:
    return None if value is None else _naive(value)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _aware_optional(value: datetime | None) -> datetime | None:
    return None if value is None else _aware(value)
