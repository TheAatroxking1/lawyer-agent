from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from lawyer_agent.application.tenancy import (
    ActorSessionState,
    MemberCursor,
    MemberPageItem,
    RoleTemplateUnavailable,
    TenantAuthorizationSnapshot,
)
from lawyer_agent.domain.authorization import AuthorizationScope, Principal
from lawyer_agent.domain.common import new_uuid7, require_uuid7
from lawyer_agent.domain.tenancy import (
    Membership,
    MembershipStatus,
    MemberType,
    Tenant,
    TenantContext,
    TenantStatus,
)
from lawyer_agent.infrastructure.persistence.models import (
    AuthSessionModel,
    DepartmentModel,
    MembershipRoleAssignmentModel,
    PermissionModel,
    RefreshTokenRecordModel,
    RoleTemplateModel,
    RoleTemplatePermissionModel,
    TenantMembershipModel,
    TenantModel,
    TenantRoleModel,
    TenantRolePermissionModel,
    UserModel,
)
from lawyer_agent.infrastructure.persistence.seed_authz import TENANT_ROLE_TEMPLATES


class TenantApplicationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_active_user(self, user_id: UUID) -> bool:
        require_uuid7(user_id, field="actor_user_id")
        return (
            await self._session.scalar(
                select(UserModel.id)
                .where(UserModel.id == user_id, UserModel.status == "active")
                .with_for_update()
            )
            is not None
        )

    async def add_application(self, tenant: Tenant) -> None:
        if not isinstance(tenant, Tenant):
            raise ValueError("tenant must be strongly typed")
        self._session.add(
            TenantModel(
                id=tenant.id,
                name=tenant.name,
                normalized_name=tenant.normalized_name,
                tenant_type=tenant.tenant_type,
                status=tenant.status.value,
                created_by_user_id=tenant.created_by_user_id,
                review_status=tenant.review_status,
                version=tenant.version,
            )
        )

    async def get_application_for_creator(
        self,
        *,
        user_id: UUID,
        tenant_id: UUID,
    ) -> Tenant | None:
        require_uuid7(user_id, field="actor_user_id")
        require_uuid7(tenant_id, field="tenant_id")
        model = await self._session.scalar(
            select(TenantModel).where(
                TenantModel.id == tenant_id,
                TenantModel.created_by_user_id == user_id,
            )
        )
        return None if model is None else _tenant(model)

    async def flush(self) -> None:
        await self._session.flush()

    async def update_name(
        self,
        *,
        context: TenantContext,
        name: str,
        normalized_name: str,
        expected_version: int,
        now: datetime,
    ) -> Tenant | None:
        _require_context(context)
        changed = cast(
            CursorResult[Any],
            await self._session.execute(
                update(TenantModel)
                .where(
                    TenantModel.id == context.tenant_id,
                    TenantModel.version == expected_version,
                )
                .values(
                    name=name,
                    normalized_name=normalized_name,
                    version=TenantModel.version + 1,
                    updated_at=_naive(now),
                )
            ),
        )
        if changed.rowcount != 1:
            return None
        model = await self._session.get(TenantModel, context.tenant_id)
        return None if model is None else _tenant(model)


class MembershipWorkflowRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_owner_for_application(self, membership: Membership) -> None:
        if (
            not isinstance(membership, Membership)
            or membership.member_type is not MemberType.OWNER
            or membership.status is not MembershipStatus.ACTIVE
        ):
            raise ValueError("tenant application owner membership is invalid")
        self._session.add(
            TenantMembershipModel(
                id=membership.id,
                tenant_id=membership.tenant_id,
                user_id=membership.user_id,
                department_id=membership.department_id,
                member_type=membership.member_type.value,
                status=membership.status.value,
                valid_from=_naive(membership.valid_from),
                valid_until=None,
                authz_version=membership.authz_version,
                version=membership.version,
            )
        )

    async def get_owner_for_application(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
    ) -> Membership | None:
        require_uuid7(tenant_id, field="tenant_id")
        require_uuid7(user_id, field="actor_user_id")
        model = await self._session.scalar(
            select(TenantMembershipModel)
            .join(
                TenantModel,
                and_(
                    TenantModel.id == TenantMembershipModel.tenant_id,
                    TenantModel.created_by_user_id == user_id,
                ),
            )
            .where(
                TenantMembershipModel.tenant_id == tenant_id,
                TenantMembershipModel.user_id == user_id,
                TenantMembershipModel.member_type == MemberType.OWNER.value,
            )
        )
        return None if model is None else _membership(model)

    async def get(
        self,
        *,
        context: TenantContext,
        membership_id: UUID,
    ) -> Membership | None:
        _require_context(context)
        require_uuid7(membership_id, field="membership_id")
        model = await self._session.scalar(
            select(TenantMembershipModel).where(
                TenantMembershipModel.tenant_id == context.tenant_id,
                TenantMembershipModel.id == membership_id,
            )
        )
        return None if model is None else _membership(model)

    async def update_locked(
        self,
        *,
        context: TenantContext,
        current: Membership,
        status: MembershipStatus,
        department_id: UUID | None,
        expected_version: int,
        now: datetime,
    ) -> Membership | None:
        _require_context(context)
        if current.tenant_id != context.tenant_id or not isinstance(status, MembershipStatus):
            raise ValueError("membership update does not match tenant context")
        changed = cast(
            CursorResult[Any],
            await self._session.execute(
                update(TenantMembershipModel)
                .where(
                    TenantMembershipModel.tenant_id == context.tenant_id,
                    TenantMembershipModel.id == current.id,
                    TenantMembershipModel.version == expected_version,
                )
                .values(
                    status=status.value,
                    department_id=department_id,
                    authz_version=TenantMembershipModel.authz_version + 1,
                    version=TenantMembershipModel.version + 1,
                    updated_at=_naive(now),
                )
            ),
        )
        if changed.rowcount != 1:
            return None
        model = await self._session.scalar(
            select(TenantMembershipModel).where(
                TenantMembershipModel.tenant_id == context.tenant_id,
                TenantMembershipModel.id == current.id,
            )
        )
        return None if model is None else _membership(model)

    async def department_exists(
        self,
        *,
        context: TenantContext,
        department_id: UUID,
    ) -> bool:
        _require_context(context)
        require_uuid7(department_id, field="department_id")
        return (
            await self._session.scalar(
                select(DepartmentModel.id).where(
                    DepartmentModel.tenant_id == context.tenant_id,
                    DepartmentModel.id == department_id,
                    DepartmentModel.status == "active",
                )
            )
            is not None
        )

    async def list_page(
        self,
        *,
        context: TenantContext,
        after: MemberCursor | None,
        limit: int,
    ) -> tuple[MemberPageItem, ...]:
        _require_context(context)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 101:
            raise ValueError("member repository page limit is invalid")
        statement = select(TenantMembershipModel).where(
            TenantMembershipModel.tenant_id == context.tenant_id
        )
        if after is not None:
            statement = statement.where(
                or_(
                    TenantMembershipModel.created_at > _naive(after.created_at),
                    and_(
                        TenantMembershipModel.created_at == _naive(after.created_at),
                        TenantMembershipModel.id > after.membership_id,
                    ),
                )
            )
        models = (
            await self._session.scalars(
                statement.order_by(
                    TenantMembershipModel.created_at,
                    TenantMembershipModel.id,
                ).limit(limit)
            )
        ).all()
        if not models:
            return ()
        role_rows = (
            await self._session.execute(
                select(
                    MembershipRoleAssignmentModel.membership_id,
                    MembershipRoleAssignmentModel.tenant_role_id,
                ).where(
                    MembershipRoleAssignmentModel.tenant_id == context.tenant_id,
                    MembershipRoleAssignmentModel.membership_id.in_(
                        [model.id for model in models]
                    ),
                )
            )
        ).all()
        roles_by_membership: dict[UUID, list[UUID]] = {}
        for membership_id, role_id in role_rows:
            roles_by_membership.setdefault(membership_id, []).append(role_id)
        return tuple(
            MemberPageItem(
                membership=_membership(model),
                role_ids=tuple(sorted(roles_by_membership.get(model.id, []), key=str)),
                created_at=_aware(model.created_at),
            )
            for model in models
        )


class TenantRoleWorkflowRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def clone_templates_for_tenant(self, tenant_id: UUID) -> dict[str, UUID]:
        require_uuid7(tenant_id, field="tenant_id")
        templates = (
            await self._session.scalars(
                select(RoleTemplateModel)
                .where(RoleTemplateModel.status == "active")
                .order_by(RoleTemplateModel.code)
            )
        ).all()
        expected_codes = {seed.code for seed in TENANT_ROLE_TEMPLATES}
        if {template.code for template in templates} != expected_codes:
            raise RoleTemplateUnavailable
        permission_rows = (
            await self._session.execute(
                select(
                    RoleTemplatePermissionModel.role_template_id,
                    RoleTemplatePermissionModel.permission_id,
                    PermissionModel.code,
                )
                .join(
                    PermissionModel,
                    PermissionModel.id == RoleTemplatePermissionModel.permission_id,
                )
                .where(
                    RoleTemplatePermissionModel.role_template_id.in_(
                        [template.id for template in templates]
                    ),
                    PermissionModel.status == "active",
                )
            )
        ).all()
        permissions_by_template: dict[UUID, list[tuple[UUID, str]]] = {}
        for template_id, permission_id, permission_code in permission_rows:
            permissions_by_template.setdefault(template_id, []).append(
                (permission_id, permission_code)
            )
        expected_by_code = {seed.code: seed for seed in TENANT_ROLE_TEMPLATES}
        for template in templates:
            expected = expected_by_code[template.code]
            if (
                template.name != expected.name
                or template.description != expected.description
                or frozenset(
                    code for _, code in permissions_by_template.get(template.id, [])
                )
                != expected.permission_codes
            ):
                raise RoleTemplateUnavailable
        role_ids: dict[str, UUID] = {}
        for template in templates:
            role_id = new_uuid7()
            role_ids[template.code] = role_id
            self._session.add(
                TenantRoleModel(
                    id=role_id,
                    tenant_id=tenant_id,
                    role_template_id=template.id,
                    code=template.code,
                    name=template.name,
                    is_custom=False,
                    status="active",
                )
            )
        await self._session.flush()
        for template in templates:
            for permission_id, _ in permissions_by_template.get(template.id, []):
                self._session.add(
                    TenantRolePermissionModel(
                        tenant_id=tenant_id,
                        tenant_role_id=role_ids[template.code],
                        permission_id=permission_id,
                    )
                )
        await self._session.flush()
        return role_ids

    async def assign_owner(
        self,
        *,
        tenant_id: UUID,
        membership_id: UUID,
        owner_role_id: UUID,
        now: datetime,
    ) -> None:
        require_uuid7(tenant_id, field="tenant_id")
        require_uuid7(membership_id, field="membership_id")
        require_uuid7(owner_role_id, field="owner_role_id")
        self._session.add(
            MembershipRoleAssignmentModel(
                tenant_id=tenant_id,
                membership_id=membership_id,
                tenant_role_id=owner_role_id,
                assigned_by_membership_id=membership_id,
                created_at=_naive(now),
            )
        )

    async def role_ids(
        self,
        *,
        context: TenantContext,
        membership_id: UUID,
    ) -> tuple[UUID, ...]:
        _require_context(context)
        require_uuid7(membership_id, field="membership_id")
        values = (
            await self._session.scalars(
                select(MembershipRoleAssignmentModel.tenant_role_id).where(
                    MembershipRoleAssignmentModel.tenant_id == context.tenant_id,
                    MembershipRoleAssignmentModel.membership_id == membership_id,
                )
            )
        ).all()
        return tuple(sorted(values, key=str))

    async def roles_exist(
        self,
        *,
        context: TenantContext,
        role_ids: tuple[UUID, ...],
    ) -> bool:
        _require_context(context)
        if not role_ids:
            return True
        for role_id in role_ids:
            require_uuid7(role_id, field="role_id")
        count = len(
            (
                await self._session.scalars(
                    select(TenantRoleModel.id).where(
                        TenantRoleModel.tenant_id == context.tenant_id,
                        TenantRoleModel.id.in_(role_ids),
                        TenantRoleModel.status == "active",
                    )
                )
            ).all()
        )
        return count == len(role_ids)

    async def role_codes_for_ids(
        self,
        *,
        context: TenantContext,
        role_ids: tuple[UUID, ...],
    ) -> dict[UUID, str] | None:
        _require_context(context)
        for role_id in role_ids:
            require_uuid7(role_id, field="role_id")
        if not role_ids:
            return {}
        rows = (
            await self._session.execute(
                select(TenantRoleModel.id, TenantRoleModel.code).where(
                    TenantRoleModel.tenant_id == context.tenant_id,
                    TenantRoleModel.id.in_(role_ids),
                    TenantRoleModel.status == "active",
                )
            )
        ).all()
        if len(rows) != len(role_ids):
            return None
        return {role_id: code for role_id, code in rows}

    async def count_active_owners_locked(self, *, context: TenantContext) -> int:
        _require_context(context)
        owner_ids = (
            await self._session.scalars(
                select(TenantMembershipModel.id)
                .join(
                    MembershipRoleAssignmentModel,
                    and_(
                        MembershipRoleAssignmentModel.tenant_id
                        == TenantMembershipModel.tenant_id,
                        MembershipRoleAssignmentModel.membership_id
                        == TenantMembershipModel.id,
                    ),
                )
                .join(
                    TenantRoleModel,
                    and_(
                        TenantRoleModel.tenant_id
                        == MembershipRoleAssignmentModel.tenant_id,
                        TenantRoleModel.id
                        == MembershipRoleAssignmentModel.tenant_role_id,
                    ),
                )
                .where(
                    TenantMembershipModel.tenant_id == context.tenant_id,
                    TenantMembershipModel.status == MembershipStatus.ACTIVE.value,
                    TenantRoleModel.status == "active",
                    TenantRoleModel.code == "tenant_owner",
                )
                .order_by(TenantMembershipModel.id)
                .with_for_update()
            )
        ).all()
        return len(owner_ids)

    async def replace_roles(
        self,
        *,
        context: TenantContext,
        membership_id: UUID,
        role_ids: tuple[UUID, ...],
        assigned_by_membership_id: UUID,
        now: datetime,
    ) -> None:
        _require_context(context)
        require_uuid7(membership_id, field="membership_id")
        require_uuid7(assigned_by_membership_id, field="assigned_by_membership_id")
        await self._session.execute(
            delete(MembershipRoleAssignmentModel).where(
                MembershipRoleAssignmentModel.tenant_id == context.tenant_id,
                MembershipRoleAssignmentModel.membership_id == membership_id,
            )
        )
        for role_id in role_ids:
            self._session.add(
                MembershipRoleAssignmentModel(
                    tenant_id=context.tenant_id,
                    membership_id=membership_id,
                    tenant_role_id=role_id,
                    assigned_by_membership_id=assigned_by_membership_id,
                    created_at=_naive(now),
                )
            )


class TenantAuthorizationWorkflowRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_snapshot(
        self,
        *,
        principal: Principal,
        context: TenantContext,
        target_membership_ids: tuple[UUID, ...] = (),
        for_update: bool = False,
    ) -> TenantAuthorizationSnapshot | None:
        _require_context(context)
        if context.membership_id is None or context.membership_user_id is None:
            return None
        for membership_id in target_membership_ids:
            require_uuid7(membership_id, field="target membership_id")
        user_statement = select(UserModel).where(UserModel.id == principal.user_id)
        if for_update:
            user_statement = user_statement.with_for_update()
        user = await self._session.scalar(user_statement)
        if user is None:
            return None

        session_statement = select(AuthSessionModel).where(
            AuthSessionModel.id == principal.session_id
        )
        if for_update:
            session_statement = session_statement.with_for_update()
        actor_session_model = await self._session.scalar(session_statement)

        tenant_statement = select(TenantModel).where(TenantModel.id == context.tenant_id)
        if for_update:
            tenant_statement = tenant_statement.with_for_update()
        tenant_model = await self._session.scalar(tenant_statement)
        if tenant_model is None:
            return None

        requested_ids = tuple(
            sorted({context.membership_id, *target_membership_ids}, key=str)
        )
        membership_statement = (
            select(TenantMembershipModel)
            .where(
                TenantMembershipModel.tenant_id == context.tenant_id,
                TenantMembershipModel.id.in_(requested_ids),
            )
            .order_by(TenantMembershipModel.id)
        )
        if for_update:
            membership_statement = membership_statement.with_for_update()
        membership_models = (await self._session.scalars(membership_statement)).all()
        memberships = {model.id: _membership(model) for model in membership_models}
        actor = memberships.get(context.membership_id)
        if actor is None or actor.user_id != context.membership_user_id:
            return None
        role_rows = (
            await self._session.execute(
                select(TenantRoleModel.code, PermissionModel.code)
                .join(
                    MembershipRoleAssignmentModel,
                    and_(
                        MembershipRoleAssignmentModel.tenant_id == TenantRoleModel.tenant_id,
                        MembershipRoleAssignmentModel.tenant_role_id == TenantRoleModel.id,
                    ),
                )
                .outerjoin(
                    TenantRolePermissionModel,
                    and_(
                        TenantRolePermissionModel.tenant_id == TenantRoleModel.tenant_id,
                        TenantRolePermissionModel.tenant_role_id == TenantRoleModel.id,
                    ),
                )
                .outerjoin(
                    PermissionModel,
                    and_(
                        PermissionModel.id == TenantRolePermissionModel.permission_id,
                        PermissionModel.status == "active",
                    ),
                )
                .where(
                    TenantRoleModel.tenant_id == context.tenant_id,
                    TenantRoleModel.status == "active",
                    MembershipRoleAssignmentModel.membership_id == context.membership_id,
                )
            )
        ).all()
        role_codes = frozenset(row[0] for row in role_rows)
        permissions = frozenset(row[1] for row in role_rows if row[1] is not None)
        authoritative_context = TenantContext(
            tenant_id=tenant_model.id,
            membership_id=actor.id,
            membership_user_id=actor.user_id,
            department_id=actor.department_id,
            tenant_status=TenantStatus(tenant_model.status),
            membership_status=actor.status,
            valid_from=actor.valid_from,
            valid_until=actor.valid_until,
            authz_version=actor.authz_version,
            session_authz_version=context.session_authz_version,
            scope=context.scope,
        )
        return TenantAuthorizationSnapshot(
            tenant=_tenant(tenant_model),
            actor_membership=actor,
            context=authoritative_context,
            user_status=user.status,
            user_auth_version=user.auth_version,
            permissions=permissions,
            role_codes=role_codes,
            target_memberships={
                membership_id: membership
                for membership_id, membership in memberships.items()
                if membership_id in target_membership_ids
            },
            actor_session=(
                None
                if actor_session_model is None
                else ActorSessionState(
                    id=actor_session_model.id,
                    user_id=actor_session_model.user_id,
                    tenant_id=actor_session_model.tenant_id,
                    membership_id=actor_session_model.membership_id,
                    auth_version_at_issue=actor_session_model.auth_version_at_issue,
                    authz_version_at_issue=actor_session_model.authz_version_at_issue,
                    revoked_at=_aware_optional(actor_session_model.revoked_at),
                    expires_at=_aware(actor_session_model.expires_at),
                )
            ),
        )


class TenantSessionRevocationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_for_membership(
        self,
        *,
        tenant_id: UUID,
        membership_id: UUID,
    ) -> None:
        require_uuid7(tenant_id, field="tenant_id")
        require_uuid7(membership_id, field="membership_id")
        refresh_rows = (
            await self._session.execute(
                select(
                    RefreshTokenRecordModel.id,
                    RefreshTokenRecordModel.family_id,
                    RefreshTokenRecordModel.session_id,
                )
                .where(
                    RefreshTokenRecordModel.tenant_id == tenant_id,
                    RefreshTokenRecordModel.membership_id == membership_id,
                )
                .order_by(
                    RefreshTokenRecordModel.family_id,
                    RefreshTokenRecordModel.id,
                )
                .with_for_update()
            )
        ).all()
        session_ids = tuple(sorted({row.session_id for row in refresh_rows}, key=str))
        if session_ids:
            await self._session.execute(
                select(AuthSessionModel.id)
                .where(
                    AuthSessionModel.tenant_id == tenant_id,
                    AuthSessionModel.membership_id == membership_id,
                    AuthSessionModel.id.in_(session_ids),
                )
                .order_by(AuthSessionModel.id)
                .with_for_update()
            )

    async def revoke_for_membership(
        self,
        *,
        tenant_id: UUID,
        membership_id: UUID,
        reason: str,
        now: datetime,
    ) -> None:
        require_uuid7(tenant_id, field="tenant_id")
        require_uuid7(membership_id, field="membership_id")
        if reason not in {"authorization_changed", "membership_revoked"}:
            raise ValueError("unsupported tenant session revocation reason")
        await self._session.execute(
            update(RefreshTokenRecordModel)
            .where(
                RefreshTokenRecordModel.tenant_id == tenant_id,
                RefreshTokenRecordModel.membership_id == membership_id,
                RefreshTokenRecordModel.revoked_at.is_(None),
            )
            .values(
                revoked_at=_naive(now),
                revocation_reason=reason,
                version=RefreshTokenRecordModel.version + 1,
                updated_at=_naive(now),
            )
        )
        await self._session.execute(
            update(AuthSessionModel)
            .where(
                AuthSessionModel.tenant_id == tenant_id,
                AuthSessionModel.membership_id == membership_id,
                AuthSessionModel.revoked_at.is_(None),
            )
            .values(
                revoked_at=_naive(now),
                revocation_reason=reason,
                version=AuthSessionModel.version + 1,
                updated_at=_naive(now),
            )
        )


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


def _naive(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("database timestamp must be UTC-aware")
    return value.astimezone(UTC).replace(tzinfo=None)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _aware_optional(value: datetime | None) -> datetime | None:
    return None if value is None else _aware(value)


def _require_context(context: TenantContext) -> None:
    if not isinstance(context, TenantContext) or not isinstance(
        context.scope, AuthorizationScope
    ):
        raise ValueError("invalid tenant context")
    require_uuid7(context.tenant_id, field="tenant context tenant_id")
    require_uuid7(context.membership_id, field="tenant context membership_id")
    require_uuid7(context.membership_user_id, field="tenant context membership_user_id")
