from __future__ import annotations

from datetime import UTC, datetime
from types import TracebackType
from typing import Self
from uuid import UUID

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lawyer_agent.application.sessions import (
    LockedRefreshToken,
    NewRefreshToken,
    NewSession,
    SessionAuditEvent,
    SessionValidationState,
    TenantSessionState,
    UserSessionState,
)
from lawyer_agent.domain.sessions import RevocationReason
from lawyer_agent.infrastructure.persistence.models import (
    AuditEventModel,
    AuthSessionModel,
    RefreshTokenRecordModel,
    TenantMembershipModel,
    TenantModel,
    UserModel,
)


class SessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_user(self, user_id: UUID) -> UserSessionState | None:
        user = await self._session.get(UserModel, user_id)
        if user is None:
            return None
        return UserSessionState(user.id, user.status, user.auth_version)

    async def get_tenant_context(
        self,
        *,
        user_id: UUID,
        tenant_id: UUID,
        membership_id: UUID,
    ) -> TenantSessionState | None:
        statement = (
            select(TenantModel, TenantMembershipModel)
            .join(
                TenantMembershipModel,
                and_(
                    TenantMembershipModel.tenant_id == TenantModel.id,
                    TenantMembershipModel.id == membership_id,
                    TenantMembershipModel.user_id == user_id,
                ),
            )
            .where(TenantModel.id == tenant_id)
        )
        row = (await self._session.execute(statement)).one_or_none()
        if row is None:
            return None
        tenant, membership = row
        return TenantSessionState(
            tenant.id,
            tenant.status,
            membership.id,
            membership.user_id,
            membership.status,
            _aware(membership.valid_from),
            _aware_optional(membership.valid_until),
            membership.authz_version,
        )

    async def get_validation_state(
        self, session_id: UUID
    ) -> SessionValidationState | None:
        statement = (
            select(AuthSessionModel, UserModel, TenantModel, TenantMembershipModel)
            .join(UserModel, UserModel.id == AuthSessionModel.user_id)
            .outerjoin(TenantModel, TenantModel.id == AuthSessionModel.tenant_id)
            .outerjoin(
                TenantMembershipModel,
                and_(
                    TenantMembershipModel.tenant_id == AuthSessionModel.tenant_id,
                    TenantMembershipModel.id == AuthSessionModel.membership_id,
                ),
            )
            .where(AuthSessionModel.id == session_id)
        )
        row = (await self._session.execute(statement)).one_or_none()
        if row is None:
            return None
        auth_session, user, tenant, membership = row
        return _validation_state(auth_session, user, tenant, membership)

    async def lock_session(self, session_id: UUID) -> SessionValidationState | None:
        locked = await self._session.scalar(
            select(AuthSessionModel)
            .where(AuthSessionModel.id == session_id)
            .with_for_update()
        )
        if locked is None:
            return None
        return await self.get_validation_state(locked.id)

    async def lock_refresh(self, token_hash: bytes) -> LockedRefreshToken | None:
        record = await self._session.scalar(
            select(RefreshTokenRecordModel)
            .where(RefreshTokenRecordModel.token_hash == token_hash)
            .with_for_update()
        )
        if record is None:
            return None
        return LockedRefreshToken(
            record.id,
            record.token_hash,
            record.family_id,
            record.session_id,
            record.user_id,
            record.tenant_id,
            record.membership_id,
            _aware(record.expires_at),
            _aware(record.idle_expires_at),
            _aware_optional(record.used_at),
            _aware_optional(record.revoked_at),
        )

    async def add_session(self, session: NewSession) -> None:
        self._session.add(
            AuthSessionModel(
                id=session.id,
                user_id=session.user_id,
                tenant_id=session.tenant_id,
                membership_id=session.membership_id,
                current_family_id=session.family_id,
                auth_version_at_issue=session.auth_version,
                authz_version_at_issue=session.authz_version,
                revoked_at=None,
                revocation_reason=None,
                last_seen_at=_naive(session.now),
                expires_at=_naive(session.expires_at),
            )
        )

    async def add_refresh(self, token: NewRefreshToken) -> None:
        self._session.add(
            RefreshTokenRecordModel(
                id=token.id,
                token_hash=token.token_hash,
                family_id=token.family_id,
                session_id=token.session_id,
                user_id=token.user_id,
                tenant_id=token.tenant_id,
                membership_id=token.membership_id,
                issued_at=_naive(token.issued_at),
                expires_at=_naive(token.expires_at),
                idle_expires_at=_naive(token.idle_expires_at),
                used_at=None,
                revoked_at=None,
                replaced_by_id=None,
                revocation_reason=None,
                device_label=None,
                user_agent_hash=None,
                ip_hash=None,
            )
        )

    async def mark_refresh_used(
        self,
        *,
        token_id: UUID,
        now: datetime,
    ) -> None:
        record = await self._session.get(RefreshTokenRecordModel, token_id)
        if record is None:
            raise RuntimeError("locked refresh token disappeared")
        record.used_at = _naive(now)
        record.version += 1
        record.updated_at = _naive(now)

    async def link_refresh_replacement(
        self,
        *,
        token_id: UUID,
        replacement_id: UUID,
        now: datetime,
    ) -> None:
        record = await self._session.get(RefreshTokenRecordModel, token_id)
        if record is None:
            raise RuntimeError("used refresh token disappeared")
        record.replaced_by_id = replacement_id
        record.updated_at = _naive(now)

    async def touch_session(self, session_id: UUID, now: datetime) -> None:
        auth_session = await self._session.get(AuthSessionModel, session_id)
        if auth_session is None:
            raise RuntimeError("auth session disappeared during refresh")
        auth_session.last_seen_at = _naive(now)
        auth_session.version += 1
        auth_session.updated_at = _naive(now)

    async def revoke_family(
        self,
        family_id: UUID,
        *,
        reason: RevocationReason,
        now: datetime,
    ) -> None:
        await self._session.execute(
            update(RefreshTokenRecordModel)
            .where(
                RefreshTokenRecordModel.family_id == family_id,
                RefreshTokenRecordModel.revoked_at.is_(None),
            )
            .values(
                revoked_at=_naive(now),
                revocation_reason=reason.value,
                version=RefreshTokenRecordModel.version + 1,
                updated_at=_naive(now),
            )
        )

    async def revoke_session(
        self,
        session_id: UUID,
        *,
        reason: RevocationReason,
        now: datetime,
    ) -> None:
        await self._session.execute(
            update(AuthSessionModel)
            .where(
                AuthSessionModel.id == session_id,
                AuthSessionModel.revoked_at.is_(None),
            )
            .values(
                revoked_at=_naive(now),
                revocation_reason=reason.value,
                version=AuthSessionModel.version + 1,
                updated_at=_naive(now),
            )
        )

    async def flush(self) -> None:
        await self._session.flush()


class SessionAuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, event: SessionAuditEvent) -> None:
        self._session.add(
            AuditEventModel(
                id=event.id,
                actor_user_id=event.actor_user_id,
                tenant_id=event.tenant_id,
                actor_membership_id=event.actor_membership_id,
                action=event.action,
                result=event.result,
                reason_code=event.reason_code,
                target_type=event.target_type,
                target_id=event.target_id,
                trace_id=event.trace_id,
                client_ip_hash=event.client_ip_hash,
                user_agent_hash=event.user_agent_hash,
                metadata_json=None,
                occurred_at=_naive(event.occurred_at),
            )
        )


class SqlAlchemySessionUnitOfWork:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None
        self.sessions: SessionRepository
        self.audit: SessionAuditRepository

    async def __aenter__(self) -> Self:
        if self._session is not None:
            raise RuntimeError("unit of work is already active")
        self._session = self._session_factory()
        self.sessions = SessionRepository(self._session)
        self.audit = SessionAuditRepository(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_value, traceback
        if self._session is None:
            raise RuntimeError("unit of work has not been entered")
        try:
            if exc_type is None:
                await self._session.commit()
            else:
                await self._session.rollback()
        except BaseException:
            await self._session.rollback()
            raise
        finally:
            await self._session.close()
            self._session = None


def _validation_state(
    auth_session: AuthSessionModel,
    user: UserModel,
    tenant: TenantModel | None,
    membership: TenantMembershipModel | None,
) -> SessionValidationState:
    return SessionValidationState(
        session_id=auth_session.id,
        user_id=auth_session.user_id,
        session_tenant_id=auth_session.tenant_id,
        session_membership_id=auth_session.membership_id,
        current_family_id=auth_session.current_family_id,
        auth_version_at_issue=auth_session.auth_version_at_issue,
        authz_version_at_issue=auth_session.authz_version_at_issue,
        revoked_at=_aware_optional(auth_session.revoked_at),
        expires_at=_aware(auth_session.expires_at),
        user_status=user.status,
        user_auth_version=user.auth_version,
        tenant_status=None if tenant is None else tenant.status,
        membership_user_id=None if membership is None else membership.user_id,
        membership_status=None if membership is None else membership.status,
        membership_valid_from=(
            None if membership is None else _aware(membership.valid_from)
        ),
        membership_valid_until=(
            None if membership is None else _aware_optional(membership.valid_until)
        ),
        membership_authz_version=(
            None if membership is None else membership.authz_version
        ),
    )


def _naive(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("database timestamp must be UTC-aware")
    return value.astimezone(UTC).replace(tzinfo=None)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _aware_optional(value: datetime | None) -> datetime | None:
    return None if value is None else _aware(value)
