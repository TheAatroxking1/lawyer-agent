from __future__ import annotations

from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lawyer_agent.application.security_locks import (
    PermissionFeatureSnapshot,
    SessionFamilyWriteLockRequest,
    TenantFeatureSnapshot,
    TenantSecurityWriteLockRequest,
)
from lawyer_agent.domain.common import require_uuid7
from lawyer_agent.infrastructure.persistence.models import (
    AuthSessionModel,
    PermissionFeatureRolloutModel,
    PermissionFeatureTenantStateModel,
    RefreshTokenRecordModel,
    TenantMembershipModel,
    TenantModel,
    TenantRoleModel,
    UserModel,
)


class SecurityWriteLockRepository:
    """Acquire security-sensitive MySQL rows in one global order.

    Tenant-scoped writes are serialized by Tenant first, then lock Refresh,
    Session, User, Membership and Role rows in stable UUID order. Standalone
    Session revocation uses the shared Refresh-before-Session suffix.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def acquire_tenant_gate(self, tenant_id: UUID) -> bool:
        require_uuid7(tenant_id, field="tenant security gate tenant_id")
        return (
            await self._session.scalar(
                select(TenantModel.id)
                .where(TenantModel.id == tenant_id)
                .with_for_update()
            )
            is not None
        )

    async def acquire_tenant_write(self, request: TenantSecurityWriteLockRequest) -> bool:
        if not isinstance(request, TenantSecurityWriteLockRequest):
            raise ValueError("tenant security lock request must be strongly typed")
        if not await self.acquire_tenant_gate(request.tenant_id):
            return False
        membership_ids = tuple(
            sorted(
                {request.actor_membership_id, *request.target_membership_ids},
                key=str,
            )
        )
        await self._session.execute(
            select(RefreshTokenRecordModel.id)
            .where(
                or_(
                    and_(RefreshTokenRecordModel.tenant_id == request.tenant_id,
                         RefreshTokenRecordModel.membership_id.in_(membership_ids)),
                    and_(RefreshTokenRecordModel.session_id == request.actor_session_id,
                         RefreshTokenRecordModel.user_id == request.actor_user_id,
                         RefreshTokenRecordModel.tenant_id.is_(None),
                         RefreshTokenRecordModel.membership_id.is_(None)),
                ),
            )
            .order_by(
                RefreshTokenRecordModel.family_id,
                RefreshTokenRecordModel.id,
            )
            .with_for_update()
        )
        await self._session.execute(
            select(AuthSessionModel.id)
            .where(
                or_(
                    and_(
                        AuthSessionModel.tenant_id == request.tenant_id,
                        or_(AuthSessionModel.id == request.actor_session_id,
                            AuthSessionModel.membership_id.in_(membership_ids)),
                    ),
                    and_(AuthSessionModel.id == request.actor_session_id,
                         AuthSessionModel.user_id == request.actor_user_id,
                         AuthSessionModel.tenant_id.is_(None),
                         AuthSessionModel.membership_id.is_(None),
                         AuthSessionModel.authz_version_at_issue.is_(None)),
                ),
            )
            .order_by(AuthSessionModel.id)
            .with_for_update()
        )
        target_user_ids = tuple(
            sorted(
                set(
                    (
                        await self._session.scalars(
                            select(TenantMembershipModel.user_id).where(
                                TenantMembershipModel.tenant_id == request.tenant_id,
                                TenantMembershipModel.id.in_(membership_ids),
                            )
                        )
                    ).all()
                )
                | {request.actor_user_id},
                key=str,
            )
        )
        await self._session.execute(
            select(UserModel.id)
            .where(UserModel.id.in_(target_user_ids))
            .order_by(UserModel.id)
            .with_for_update()
        )
        await self._session.execute(
            select(TenantMembershipModel.id)
            .where(
                TenantMembershipModel.tenant_id == request.tenant_id,
                TenantMembershipModel.id.in_(membership_ids),
            )
            .order_by(TenantMembershipModel.id)
            .with_for_update()
        )
        await self._session.execute(
            select(TenantRoleModel.id)
            .where(TenantRoleModel.tenant_id == request.tenant_id)
            .order_by(TenantRoleModel.id)
            .with_for_update()
        )
        return True

    async def acquire_session_family(
        self, request: SessionFamilyWriteLockRequest
    ) -> bool:
        if not isinstance(request, SessionFamilyWriteLockRequest):
            raise ValueError("session family lock request must be strongly typed")
        for tenant_id in request.tenant_ids:
            if not await self.acquire_tenant_gate(tenant_id):
                return False
        (
            await self._session.scalars(
                select(RefreshTokenRecordModel.id)
                .where(
                    RefreshTokenRecordModel.session_id == request.session_id,
                    RefreshTokenRecordModel.family_id == request.family_id,
                )
                .order_by(RefreshTokenRecordModel.id)
                .with_for_update()
            )
        ).all()
        session_id = await self._session.scalar(
            select(AuthSessionModel.id)
            .where(AuthSessionModel.id == request.session_id)
            .with_for_update()
        )
        return session_id is not None

    async def lock_global_feature_for_tenant_create(self) -> PermissionFeatureSnapshot:
        model = await self._session.scalar(
            select(PermissionFeatureRolloutModel)
            .where(PermissionFeatureRolloutModel.feature_code == "ai_job_runtime_v1")
            .with_for_update(read=True)
        )
        if model is None:
            raise ValueError("global permission feature row is missing")
        return PermissionFeatureSnapshot(
            feature_code=model.feature_code,
            phase=model.phase,
            manifest_version=model.manifest_version,
            manifest_digest=bytes(model.manifest_digest),
            rollout_generation=model.rollout_generation,
            version=model.version,
        )

    async def lock_tenant_feature_shared(self, tenant_id: UUID) -> TenantFeatureSnapshot:
        require_uuid7(tenant_id, field="tenant feature lock tenant_id")
        model = await self._session.scalar(
            select(PermissionFeatureTenantStateModel)
            .where(
                PermissionFeatureTenantStateModel.feature_code == "ai_job_runtime_v1",
                PermissionFeatureTenantStateModel.tenant_id == tenant_id,
            )
            .with_for_update(read=True)
        )
        if model is None:
            raise ValueError("tenant permission feature state is missing")
        return TenantFeatureSnapshot(
            tenant_id=model.tenant_id,
            feature_code=model.feature_code,
            phase=model.phase,
            applied_manifest_version=model.applied_manifest_version,
            applied_rollout_generation=model.applied_rollout_generation,
            target_manifest_version=model.target_manifest_version,
            target_rollout_generation=model.target_rollout_generation,
            version=model.version,
        )
