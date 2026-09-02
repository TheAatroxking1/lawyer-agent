from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from lawyer_agent.application.invitations import (
    InvitationLocator,
    InvitationRecord,
    InvitationTargetKind,
    VerifiedIdentityRecord,
)
from lawyer_agent.domain.common import require_uuid7
from lawyer_agent.domain.tenancy import Membership, MembershipStatus, MemberType
from lawyer_agent.infrastructure.persistence.models import (
    AuthIdentityModel,
    MembershipRoleAssignmentModel,
    TenantInvitationModel,
    TenantInvitationRoleAssignmentModel,
    TenantMembershipModel,
    TenantModel,
    TenantRoleModel,
    UserModel,
)


class InvitationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def locate_by_token_hash(self, token_hash: bytes) -> InvitationLocator | None:
        _require_digest(token_hash)
        row = (
            await self._session.execute(
                select(TenantInvitationModel.id, TenantInvitationModel.tenant_id).where(
                    TenantInvitationModel.token_hash == token_hash
                )
            )
        ).one_or_none()
        return None if row is None else InvitationLocator(row.id, row.tenant_id)

    async def lock_by_token_hash(
        self, *, tenant_id: UUID, token_hash: bytes
    ) -> InvitationRecord | None:
        require_uuid7(tenant_id, field="invitation tenant_id")
        _require_digest(token_hash)
        model = await self._session.scalar(
            select(TenantInvitationModel)
            .where(
                TenantInvitationModel.tenant_id == tenant_id,
                TenantInvitationModel.token_hash == token_hash,
            )
            .with_for_update()
        )
        return None if model is None else _invitation(model)

    async def tenant_status_locked(self, *, tenant_id: UUID) -> str | None:
        require_uuid7(tenant_id, field="invitation tenant_id")
        value = await self._session.scalar(
            select(TenantModel.status)
            .where(TenantModel.id == tenant_id)
            .with_for_update()
        )
        return value if isinstance(value, str) else None

    async def add(self, invitation: InvitationRecord) -> None:
        _require_invitation_record(invitation)
        self._session.add(
            TenantInvitationModel(
                id=invitation.id,
                tenant_id=invitation.tenant_id,
                target_kind=invitation.target_kind.value,
                target_blind_index=invitation.target_blind_index,
                token_hash=invitation.token_hash,
                invited_by_membership_id=invitation.invited_by_membership_id,
                expires_at=_naive(invitation.expires_at),
                accepted_at=None,
                revoked_at=None,
                status=invitation.status,
                version=invitation.version,
            )
        )

    async def add_roles(
        self, *, tenant_id: UUID, invitation_id: UUID, role_ids: tuple[UUID, ...]
    ) -> None:
        _require_scoped_ids(tenant_id, invitation_id, *role_ids)
        for role_id in role_ids:
            self._session.add(
                TenantInvitationRoleAssignmentModel(
                    tenant_id=tenant_id,
                    invitation_id=invitation_id,
                    tenant_role_id=role_id,
                )
            )

    async def get_for_actor(
        self, *, tenant_id: UUID, invitation_id: UUID
    ) -> InvitationRecord | None:
        _require_scoped_ids(tenant_id, invitation_id)
        model = await self._session.scalar(
            select(TenantInvitationModel).where(
                TenantInvitationModel.tenant_id == tenant_id,
                TenantInvitationModel.id == invitation_id,
            )
        )
        return None if model is None else _invitation(model)

    async def role_codes_locked(
        self, *, tenant_id: UUID, invitation_id: UUID
    ) -> dict[UUID, str] | None:
        _require_scoped_ids(tenant_id, invitation_id)
        assignment_ids = tuple(
            (
                await self._session.scalars(
                    select(TenantInvitationRoleAssignmentModel.tenant_role_id)
                    .where(
                        TenantInvitationRoleAssignmentModel.tenant_id == tenant_id,
                        TenantInvitationRoleAssignmentModel.invitation_id == invitation_id,
                    )
                    .order_by(TenantInvitationRoleAssignmentModel.tenant_role_id)
                    .with_for_update()
                )
            ).all()
        )
        if not assignment_ids:
            return None
        return await self.role_codes_for_ids_locked(
            tenant_id=tenant_id,
            role_ids=assignment_ids,
        )

    async def role_codes_for_ids_locked(
        self, *, tenant_id: UUID, role_ids: tuple[UUID, ...]
    ) -> dict[UUID, str] | None:
        _require_scoped_ids(tenant_id, *role_ids)
        if not role_ids:
            return None
        models = (
            await self._session.scalars(
                select(TenantRoleModel)
                .where(
                    TenantRoleModel.tenant_id == tenant_id,
                    TenantRoleModel.id.in_(role_ids),
                    TenantRoleModel.status == "active",
                )
                .order_by(TenantRoleModel.id)
                .with_for_update()
            )
        ).all()
        if len(models) != len(role_ids):
            return None
        return {model.id: model.code for model in models}

    async def lock_active_user_identities(
        self, *, user_id: UUID, kinds: tuple[InvitationTargetKind, ...]
    ) -> tuple[VerifiedIdentityRecord, ...] | None:
        require_uuid7(user_id, field="invitation user_id")
        user = await self._session.scalar(
            select(UserModel)
            .where(UserModel.id == user_id)
            .with_for_update()
        )
        if user is None or user.status != "active":
            return None
        models = (
            await self._session.scalars(
                select(AuthIdentityModel)
                .where(
                    AuthIdentityModel.user_id == user_id,
                    AuthIdentityModel.kind.in_(tuple(kind.value for kind in kinds)),
                    AuthIdentityModel.status == "active",
                    AuthIdentityModel.verified_at.is_not(None),
                )
                .order_by(AuthIdentityModel.id)
                .with_for_update()
            )
        ).all()
        return tuple(
            VerifiedIdentityRecord(
                identity_id=model.id,
                kind=InvitationTargetKind(model.kind),
                subject_ciphertext=model.subject_ciphertext,
                verified_at=_aware(model.verified_at),
            )
            for model in models
        )

    async def lock_membership_by_user(
        self, *, tenant_id: UUID, user_id: UUID
    ) -> Membership | None:
        _require_scoped_ids(tenant_id, user_id)
        model = await self._session.scalar(
            select(TenantMembershipModel)
            .where(
                TenantMembershipModel.tenant_id == tenant_id,
                TenantMembershipModel.user_id == user_id,
            )
            .with_for_update()
        )
        return None if model is None else _membership(model)

    async def get_membership(
        self, *, tenant_id: UUID, membership_id: UUID
    ) -> Membership | None:
        _require_scoped_ids(tenant_id, membership_id)
        model = await self._session.scalar(
            select(TenantMembershipModel).where(
                TenantMembershipModel.tenant_id == tenant_id,
                TenantMembershipModel.id == membership_id,
            )
        )
        return None if model is None else _membership(model)

    async def add_membership(self, membership: Membership) -> None:
        if not isinstance(membership, Membership):
            raise ValueError("membership must be strongly typed")
        self._session.add(
            TenantMembershipModel(
                id=membership.id,
                tenant_id=membership.tenant_id,
                user_id=membership.user_id,
                department_id=membership.department_id,
                member_type=membership.member_type.value,
                status=membership.status.value,
                valid_from=_naive(membership.valid_from),
                valid_until=(
                    None if membership.valid_until is None else _naive(membership.valid_until)
                ),
                authz_version=membership.authz_version,
                version=membership.version,
            )
        )

    async def activate_membership(
        self, *, membership: Membership, now: datetime
    ) -> Membership:
        if not isinstance(membership, Membership):
            raise ValueError("membership must be strongly typed")
        model = await self._session.scalar(
            select(TenantMembershipModel)
            .where(
                TenantMembershipModel.tenant_id == membership.tenant_id,
                TenantMembershipModel.id == membership.id,
                TenantMembershipModel.status == "invited",
            )
            .with_for_update()
        )
        if model is None:
            raise RuntimeError("invited membership disappeared")
        model.status = "active"
        model.valid_from = _naive(now)
        model.authz_version += 1
        model.version += 1
        model.updated_at = _naive(now)
        return _membership(model)

    async def assign_roles(
        self,
        *,
        tenant_id: UUID,
        membership_id: UUID,
        role_ids: tuple[UUID, ...],
        assigned_by_membership_id: UUID,
        now: datetime,
    ) -> None:
        _require_scoped_ids(
            tenant_id,
            membership_id,
            assigned_by_membership_id,
            *role_ids,
        )
        _naive(now)
        await self._session.execute(
            delete(MembershipRoleAssignmentModel).where(
                MembershipRoleAssignmentModel.tenant_id == tenant_id,
                MembershipRoleAssignmentModel.membership_id == membership_id,
            )
        )
        for role_id in role_ids:
            self._session.add(
                MembershipRoleAssignmentModel(
                    tenant_id=tenant_id,
                    membership_id=membership_id,
                    tenant_role_id=role_id,
                    assigned_by_membership_id=assigned_by_membership_id,
                )
            )

    async def consume(
        self, *, invitation: InvitationRecord, now: datetime
    ) -> InvitationRecord:
        _require_invitation_record(invitation)
        _naive(now)
        model = await self._session.scalar(
            select(TenantInvitationModel)
            .where(
                TenantInvitationModel.tenant_id == invitation.tenant_id,
                TenantInvitationModel.id == invitation.id,
                TenantInvitationModel.version == invitation.version,
                TenantInvitationModel.status == "pending",
            )
            .with_for_update()
        )
        if model is None:
            raise RuntimeError("invitation disappeared during acceptance")
        model.status = "accepted"
        model.accepted_at = _naive(now)
        model.version += 1
        model.updated_at = _naive(now)
        return _invitation(model)

    async def flush(self) -> None:
        await self._session.flush()


def _invitation(model: TenantInvitationModel) -> InvitationRecord:
    return InvitationRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        target_kind=InvitationTargetKind(model.target_kind),
        target_blind_index=model.target_blind_index,
        token_hash=model.token_hash,
        invited_by_membership_id=model.invited_by_membership_id,
        expires_at=_aware(model.expires_at),
        accepted_at=None if model.accepted_at is None else _aware(model.accepted_at),
        revoked_at=None if model.revoked_at is None else _aware(model.revoked_at),
        status=model.status,
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
        valid_until=None if model.valid_until is None else _aware(model.valid_until),
        authz_version=model.authz_version,
        version=model.version,
    )


def _require_digest(value: bytes) -> None:
    if not isinstance(value, bytes) or len(value) != 32:
        raise ValueError("invitation token hash must contain exactly 32 bytes")


def _require_scoped_ids(tenant_id: UUID, *resource_ids: UUID) -> None:
    require_uuid7(tenant_id, field="invitation tenant_id")
    for resource_id in resource_ids:
        require_uuid7(resource_id, field="invitation resource_id")


def _require_invitation_record(invitation: InvitationRecord) -> None:
    if not isinstance(invitation, InvitationRecord):
        raise ValueError("invitation must be strongly typed")
    _require_scoped_ids(
        invitation.tenant_id,
        invitation.id,
        invitation.invited_by_membership_id,
    )
    _require_digest(invitation.token_hash)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _naive(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("database timestamp must be UTC-aware")
    return value.astimezone(UTC).replace(tzinfo=None)
