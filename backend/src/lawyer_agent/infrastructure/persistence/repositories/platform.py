from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lawyer_agent.application.platform import (
    BootstrapState,
    PlatformApplicationProjection,
    PlatformAuthorizationSnapshot,
    ReviewDecision,
)
from lawyer_agent.domain.authorization import Principal
from lawyer_agent.domain.common import require_uuid7
from lawyer_agent.infrastructure.persistence.models import (
    AuditEventModel,
    AuthSessionModel,
    PermissionModel,
    PlatformRoleAssignmentModel,
    PlatformRoleModel,
    PlatformRolePermissionModel,
    TenantModel,
    UserModel,
)


class PlatformRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_actor(
        self, *, principal: Principal, for_update: bool
    ) -> PlatformAuthorizationSnapshot | None:
        session_statement = select(AuthSessionModel).where(
            AuthSessionModel.id == principal.session_id,
            AuthSessionModel.user_id == principal.user_id,
            AuthSessionModel.tenant_id.is_(None),
            AuthSessionModel.membership_id.is_(None),
        )
        if for_update:
            session_statement = session_statement.with_for_update()
        auth_session = await self._session.scalar(session_statement)
        if auth_session is None:
            return None
        user_statement = select(UserModel).where(UserModel.id == principal.user_id)
        if for_update:
            user_statement = user_statement.with_for_update()
        user = await self._session.scalar(user_statement)
        if user is None:
            return None
        role_statement = (
            select(PlatformRoleModel.code, PermissionModel.code)
            .join(
                PlatformRoleAssignmentModel,
                PlatformRoleAssignmentModel.platform_role_id == PlatformRoleModel.id,
            )
            .outerjoin(
                PlatformRolePermissionModel,
                PlatformRolePermissionModel.platform_role_id == PlatformRoleModel.id,
            )
            .outerjoin(
                PermissionModel,
                and_(
                    PermissionModel.id == PlatformRolePermissionModel.permission_id,
                    PermissionModel.status == "active",
                ),
            )
            .where(
                PlatformRoleAssignmentModel.user_id == principal.user_id,
                PlatformRoleAssignmentModel.status == "active",
                PlatformRoleAssignmentModel.revoked_at.is_(None),
                or_(
                    PlatformRoleAssignmentModel.expires_at.is_(None),
                    PlatformRoleAssignmentModel.expires_at > func.utc_timestamp(6),
                ),
                PlatformRoleModel.status == "active",
            )
            .order_by(PlatformRoleModel.id)
        )
        if for_update:
            role_statement = role_statement.with_for_update()
        rows = (await self._session.execute(role_statement)).all()
        return PlatformAuthorizationSnapshot(
            user_id=user.id,
            user_status=user.status,
            user_auth_version=user.auth_version,
            session_id=auth_session.id,
            session_user_id=auth_session.user_id,
            session_auth_version=auth_session.auth_version_at_issue,
            session_revoked_at=(
                None if auth_session.revoked_at is None else _aware(auth_session.revoked_at)
            ),
            session_expires_at=_aware(auth_session.expires_at),
            permissions=frozenset(row[1] for row in rows if row[1] is not None),
            role_codes=frozenset(row[0] for row in rows),
        )

    async def list_applications(
        self, *, limit: int
    ) -> tuple[PlatformApplicationProjection, ...]:
        models = (
            await self._session.scalars(
                select(TenantModel)
                .where(TenantModel.review_status == "pending")
                .order_by(TenantModel.created_at, TenantModel.id)
                .limit(limit)
            )
        ).all()
        return tuple(_projection(model) for model in models)

    async def get_application(
        self, *, tenant_id: UUID, for_update: bool
    ) -> PlatformApplicationProjection | None:
        require_uuid7(tenant_id, field="platform review tenant_id")
        statement = select(TenantModel).where(TenantModel.id == tenant_id)
        if for_update:
            statement = statement.with_for_update()
        model = await self._session.scalar(statement)
        return None if model is None else _projection(model)

    async def review_application(
        self,
        *,
        application: PlatformApplicationProjection,
        reviewer_user_id: UUID,
        decision: ReviewDecision,
        reason_code: str,
        now: datetime,
    ) -> PlatformApplicationProjection:
        if not isinstance(application, PlatformApplicationProjection):
            raise ValueError("platform application must be strongly typed")
        require_uuid7(application.tenant_id, field="platform review tenant_id")
        require_uuid7(reviewer_user_id, field="platform reviewer_user_id")
        _naive(now)
        model = await self._session.scalar(
            select(TenantModel)
            .where(
                TenantModel.id == application.tenant_id,
                TenantModel.version == application.version,
                TenantModel.status == "pending_verification",
                TenantModel.review_status == "pending",
            )
            .with_for_update()
        )
        if model is None:
            raise RuntimeError("tenant application changed during review")
        model.status = "active" if decision is ReviewDecision.APPROVE else "closed"
        model.review_status = "approved" if decision is ReviewDecision.APPROVE else "rejected"
        model.reviewed_at = _naive(now)
        model.reviewed_by_user_id = reviewer_user_id
        model.review_reason_code = reason_code
        model.version += 1
        model.updated_at = _naive(now)
        return _projection(model)

    async def load_bootstrap_state(
        self, *, user_id: UUID, now: datetime
    ) -> BootstrapState:
        require_uuid7(user_id, field="bootstrap user_id")
        _naive(now)
        user = await self._session.scalar(
            select(UserModel).where(UserModel.id == user_id).with_for_update()
        )
        role = await self._session.scalar(
            select(PlatformRoleModel)
            .where(
                PlatformRoleModel.code == "super_admin",
                PlatformRoleModel.status == "active",
            )
            .with_for_update()
        )
        if role is None:
            role_id = None
            current = False
            user_assignment = False
        else:
            role_id = role.id
            assignments = (
                await self._session.scalars(
                    select(PlatformRoleAssignmentModel)
                    .where(PlatformRoleAssignmentModel.platform_role_id == role.id)
                    .order_by(PlatformRoleAssignmentModel.id)
                    .with_for_update()
                )
            ).all()
            database_now = _naive(now)
            current = any(
                assignment.status == "active"
                and assignment.revoked_at is None
                and (
                    assignment.expires_at is None or assignment.expires_at > database_now
                )
                for assignment in assignments
            )
            user_assignment = any(
                assignment.user_id == user_id for assignment in assignments
            )
        was_bootstrapped = bool(
            await self._session.scalar(
                select(
                    exists().where(
                        AuditEventModel.action == "platform_admin.bootstrap",
                        AuditEventModel.result == "success",
                    )
                )
            )
        )
        return BootstrapState(
            user_is_active=user is not None and user.status == "active",
            super_admin_role_id=role_id,
            has_current_super_admin=current,
            was_bootstrapped=was_bootstrapped,
            user_has_super_admin_assignment=user_assignment,
        )

    async def add_super_admin_assignment(
        self,
        *,
        assignment_id: UUID,
        user_id: UUID,
        role_id: UUID,
        now: datetime,
    ) -> None:
        require_uuid7(assignment_id, field="platform assignment_id")
        require_uuid7(user_id, field="platform assignment user_id")
        require_uuid7(role_id, field="platform assignment role_id")
        _naive(now)
        self._session.add(
            PlatformRoleAssignmentModel(
                id=assignment_id,
                user_id=user_id,
                platform_role_id=role_id,
                assigned_by_user_id=None,
                status="active",
                expires_at=None,
                revoked_at=None,
                revocation_reason=None,
            )
        )

    async def flush(self) -> None:
        await self._session.flush()


def _projection(model: TenantModel) -> PlatformApplicationProjection:
    return PlatformApplicationProjection(
        tenant_id=model.id,
        name=model.name,
        tenant_type=model.tenant_type,
        status=model.status,
        review_status=model.review_status,
        created_at=_aware(model.created_at),
        version=model.version,
    )


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _naive(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("database timestamp must be UTC-aware")
    return value.astimezone(UTC).replace(tzinfo=None)
