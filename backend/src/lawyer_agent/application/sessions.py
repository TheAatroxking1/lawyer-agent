from __future__ import annotations

import hmac
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from lawyer_agent.application.identity import AuditContext
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.sessions import AccessTokenClaims, Audience, InvalidToken

_ACCESS_TTL = timedelta(minutes=10)
_REFRESH_ABSOLUTE_TTL = timedelta(days=30)
_REFRESH_IDLE_TTL = timedelta(days=7)
_REFRESH_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}", re.ASCII)


class InvalidSession(Exception):
    code = "authentication_failed"

    def __init__(self) -> None:
        super().__init__("invalid session")


class InvalidRefreshToken(Exception):
    code = "authentication_failed"

    def __init__(self) -> None:
        super().__init__("invalid refresh token")


class RefreshReplayDetected(InvalidRefreshToken):
    code = "refresh_replay_detected"

    def __init__(self) -> None:
        super().__init__()


@dataclass(frozen=True, slots=True)
class UserSessionState:
    user_id: UUID
    status: str
    auth_version: int


@dataclass(frozen=True, slots=True)
class TenantSessionState:
    tenant_id: UUID
    tenant_status: str
    membership_id: UUID
    membership_user_id: UUID
    membership_status: str
    valid_from: datetime
    valid_until: datetime | None
    authz_version: int


@dataclass(frozen=True, slots=True)
class SessionValidationState:
    session_id: UUID
    user_id: UUID
    session_tenant_id: UUID | None
    session_membership_id: UUID | None
    current_family_id: UUID
    auth_version_at_issue: int
    authz_version_at_issue: int | None
    revoked_at: datetime | None
    expires_at: datetime
    user_status: str
    user_auth_version: int
    tenant_status: str | None
    membership_user_id: UUID | None
    membership_status: str | None
    membership_valid_from: datetime | None
    membership_valid_until: datetime | None
    membership_authz_version: int | None


@dataclass(frozen=True, slots=True)
class LockedRefreshToken:
    id: UUID
    token_hash: bytes
    family_id: UUID
    session_id: UUID
    user_id: UUID
    tenant_id: UUID | None
    membership_id: UUID | None
    expires_at: datetime
    idle_expires_at: datetime
    used_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True)
class NewSession:
    id: UUID
    user_id: UUID
    tenant_id: UUID | None
    membership_id: UUID | None
    family_id: UUID
    auth_version: int
    authz_version: int | None
    now: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class NewRefreshToken:
    id: UUID
    token_hash: bytes
    family_id: UUID
    session_id: UUID
    user_id: UUID
    tenant_id: UUID | None
    membership_id: UUID | None
    issued_at: datetime
    expires_at: datetime
    idle_expires_at: datetime


@dataclass(frozen=True, slots=True)
class SessionAuditEvent:
    id: UUID
    actor_user_id: UUID | None
    tenant_id: UUID | None
    actor_membership_id: UUID | None
    action: str
    result: str
    reason_code: str
    target_type: str | None
    target_id: UUID | None
    trace_id: str
    client_ip_hash: bytes | None
    user_agent_hash: bytes | None
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class SessionResult:
    session_id: UUID
    family_id: UUID
    access_token: str
    refresh_token: str


@dataclass(frozen=True, slots=True)
class RefreshResult:
    session_id: UUID
    family_id: UUID
    access_token: str
    refresh_token: str


@dataclass(frozen=True, slots=True)
class ValidatedSession:
    user_id: UUID
    session_id: UUID
    tenant_id: UUID | None
    membership_id: UUID | None


@dataclass(frozen=True, slots=True)
class SwitchTenantCommand:
    session_id: UUID
    tenant_id: UUID
    membership_id: UUID


class SessionRepositoryPort(Protocol):
    async def get_user(self, user_id: UUID) -> UserSessionState | None: ...

    async def get_tenant_context(
        self,
        *,
        user_id: UUID,
        tenant_id: UUID,
        membership_id: UUID,
    ) -> TenantSessionState | None: ...

    async def get_validation_state(
        self, session_id: UUID
    ) -> SessionValidationState | None: ...

    async def lock_session(self, session_id: UUID) -> SessionValidationState | None: ...

    async def lock_refresh(self, token_hash: bytes) -> LockedRefreshToken | None: ...

    async def add_session(self, session: NewSession) -> None: ...

    async def add_refresh(self, token: NewRefreshToken) -> None: ...

    async def mark_refresh_used(
        self,
        *,
        token_id: UUID,
        now: datetime,
    ) -> None: ...

    async def link_refresh_replacement(
        self,
        *,
        token_id: UUID,
        replacement_id: UUID,
        now: datetime,
    ) -> None: ...

    async def touch_session(self, session_id: UUID, now: datetime) -> None: ...

    async def revoke_family(self, family_id: UUID, *, reason: str, now: datetime) -> None: ...

    async def revoke_session(self, session_id: UUID, *, reason: str, now: datetime) -> None: ...

    async def flush(self) -> None: ...


class SessionAuditRepositoryPort(Protocol):
    async def append(self, event: SessionAuditEvent) -> None: ...


class SessionUnitOfWork(Protocol):
    sessions: SessionRepositoryPort
    audit: SessionAuditRepositoryPort

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


class SessionValidationCachePort(Protocol):
    async def get(self, session_id: UUID) -> SessionValidationState | None: ...

    async def set(
        self,
        session_id: UUID,
        state: SessionValidationState,
        *,
        ttl_seconds: int,
    ) -> None: ...

    async def invalidate(self, session_id: UUID) -> None: ...


class TokenServicePort(Protocol):
    def issue_account(self, claims: AccessTokenClaims) -> str: ...

    def issue_tenant(self, claims: AccessTokenClaims) -> str: ...

    def verify(
        self,
        encoded: str,
        *,
        audience: Audience,
        now: datetime | None = None,
    ) -> AccessTokenClaims: ...


class SessionService:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], SessionUnitOfWork],
        token_service: TokenServicePort,
        refresh_hash_key: bytes,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        validation_cache: SessionValidationCachePort | None = None,
        validation_cache_ttl_seconds: int = 60,
    ) -> None:
        if len(refresh_hash_key) != 32:
            raise ValueError("refresh token hash key must contain exactly 32 bytes")
        if (
            isinstance(validation_cache_ttl_seconds, bool)
            or not 0 <= validation_cache_ttl_seconds <= 60
        ):
            raise ValueError("session validation cache TTL must be between 0 and 60 seconds")
        self._uow_factory = uow_factory
        self._tokens = token_service
        self._refresh_hash_key = refresh_hash_key
        self._clock = clock
        self._cache = validation_cache
        self._cache_ttl = validation_cache_ttl_seconds

    async def start(
        self,
        *,
        user_id: UUID,
        audit_context: AuditContext,
    ) -> SessionResult:
        now = self._now()
        async with self._uow_factory() as uow:
            user = await uow.sessions.get_user(user_id)
            if user is None or user.status != "active":
                raise InvalidSession
            material = self._new_session_material(
                user=user,
                tenant=None,
                now=now,
            )
            access_token = self._issue_access(material.session, now)
            await uow.sessions.add_session(material.session)
            await uow.sessions.add_refresh(material.refresh_record)
            await uow.audit.append(
                _audit_event(
                    audit_context,
                    actor_user_id=user_id,
                    tenant_id=None,
                    membership_id=None,
                    action="session.start",
                    result="success",
                    reason="authenticated",
                    session_id=material.session.id,
                    now=now,
                )
            )
            await uow.sessions.flush()
        return SessionResult(
            material.session.id,
            material.session.family_id,
            access_token,
            material.raw_refresh,
        )

    async def refresh(
        self,
        raw_token: str,
        *,
        audit_context: AuditContext,
    ) -> RefreshResult:
        now = self._now()
        token_hash = self._refresh_hash(raw_token)
        outcome: RefreshResult | None = None
        replay = False
        invalid = False
        invalidated_session_id: UUID | None = None
        async with self._uow_factory() as uow:
            current = await uow.sessions.lock_refresh(token_hash)
            if current is None:
                await uow.audit.append(
                    _audit_event(
                        audit_context,
                        actor_user_id=None,
                        tenant_id=None,
                        membership_id=None,
                        action="session.refresh",
                        result="failure",
                        reason="authentication_failed",
                        session_id=None,
                        now=now,
                    )
                )
                invalid = True
            elif current.used_at is not None:
                await self._revoke_for_replay(uow, current, audit_context, now)
                replay = True
                invalidated_session_id = current.session_id
            elif not _refresh_is_current(current, now):
                await uow.sessions.revoke_family(
                    current.family_id,
                    reason="refresh_invalid",
                    now=now,
                )
                await uow.sessions.revoke_session(
                    current.session_id,
                    reason="refresh_invalid",
                    now=now,
                )
                await uow.audit.append(
                    _audit_event(
                        audit_context,
                        actor_user_id=current.user_id,
                        tenant_id=current.tenant_id,
                        membership_id=current.membership_id,
                        action="session.refresh",
                        result="failure",
                        reason="authentication_failed",
                        session_id=current.session_id,
                        now=now,
                    )
                )
                invalid = True
                invalidated_session_id = current.session_id
            else:
                state = await uow.sessions.get_validation_state(current.session_id)
                if (
                    state is None
                    or not _state_is_authoritative(state, None, now)
                    or not _refresh_matches_state(current, state)
                ):
                    await uow.sessions.revoke_family(
                        current.family_id,
                        reason="authoritative_state_changed",
                        now=now,
                    )
                    await uow.sessions.revoke_session(
                        current.session_id,
                        reason="authoritative_state_changed",
                        now=now,
                    )
                    await uow.audit.append(
                        _audit_event(
                            audit_context,
                            actor_user_id=current.user_id,
                            tenant_id=current.tenant_id,
                            membership_id=current.membership_id,
                            action="session.refresh",
                            result="denied",
                            reason="authoritative_state_changed",
                            session_id=current.session_id,
                            now=now,
                        )
                    )
                    await uow.sessions.flush()
                    invalid = True
                    invalidated_session_id = current.session_id
                else:
                    replacement_raw = _new_raw_refresh_token()
                    replacement_id = new_uuid7()
                    replacement = NewRefreshToken(
                        id=replacement_id,
                        token_hash=self._refresh_hash(replacement_raw),
                        family_id=current.family_id,
                        session_id=current.session_id,
                        user_id=current.user_id,
                        tenant_id=current.tenant_id,
                        membership_id=current.membership_id,
                        issued_at=now,
                        expires_at=current.expires_at,
                        idle_expires_at=min(current.expires_at, now + _REFRESH_IDLE_TTL),
                    )
                    await uow.sessions.mark_refresh_used(
                        token_id=current.id,
                        now=now,
                    )
                    await uow.sessions.flush()
                    await uow.sessions.add_refresh(replacement)
                    await uow.sessions.flush()
                    await uow.sessions.link_refresh_replacement(
                        token_id=current.id,
                        replacement_id=replacement_id,
                        now=now,
                    )
                    await uow.sessions.touch_session(current.session_id, now)
                    await uow.audit.append(
                        _audit_event(
                            audit_context,
                            actor_user_id=current.user_id,
                            tenant_id=current.tenant_id,
                            membership_id=current.membership_id,
                            action="session.refresh",
                            result="success",
                            reason="rotated",
                            session_id=current.session_id,
                            now=now,
                        )
                    )
                    await uow.sessions.flush()
                    outcome = RefreshResult(
                        current.session_id,
                        current.family_id,
                        self._issue_access_from_state(state, now),
                        replacement_raw,
                    )
        if invalidated_session_id is not None:
            await self._invalidate_cache(invalidated_session_id)
        if replay:
            raise RefreshReplayDetected
        if invalid or outcome is None:
            raise InvalidRefreshToken
        return outcome

    async def validate_access(
        self,
        encoded: str,
        *,
        audience: Audience,
    ) -> ValidatedSession:
        now = self._now()
        try:
            claims = self._tokens.verify(encoded, audience=audience, now=now)
        except InvalidToken:
            raise InvalidSession from None

        # Redis is only an optimization hint. A positive cache entry never replaces
        # the authoritative MySQL check, so revocation cannot fail open when cache
        # invalidation is delayed or Redis recovers with an older entry.
        cached = await self._read_cached_state(claims.session_id)
        if cached is not None and not _state_is_authoritative(cached, claims, now):
            raise InvalidSession
        state = await self._load_validation_state(claims.session_id)
        if state is None or not _state_is_authoritative(state, claims, now):
            raise InvalidSession
        return ValidatedSession(
            state.user_id,
            state.session_id,
            state.session_tenant_id,
            state.session_membership_id,
        )

    async def revoke(
        self,
        session_id: UUID,
        *,
        reason: str,
        audit_context: AuditContext,
    ) -> None:
        now = self._now()
        async with self._uow_factory() as uow:
            state = await uow.sessions.lock_session(session_id)
            if state is None:
                return
            await uow.sessions.revoke_family(state.current_family_id, reason=reason, now=now)
            await uow.sessions.revoke_session(session_id, reason=reason, now=now)
            await uow.audit.append(
                _audit_event(
                    audit_context,
                    actor_user_id=state.user_id,
                    tenant_id=state.session_tenant_id,
                    membership_id=state.session_membership_id,
                    action="session.revoke",
                    result="success",
                    reason=reason,
                    session_id=session_id,
                    now=now,
                )
            )
            await uow.sessions.flush()
        await self._invalidate_cache(session_id)

    async def switch_tenant(
        self,
        command: SwitchTenantCommand,
        *,
        audit_context: AuditContext,
    ) -> SessionResult:
        now = self._now()
        async with self._uow_factory() as uow:
            current = await uow.sessions.lock_session(command.session_id)
            if current is None or not _state_is_authoritative(current, None, now):
                raise InvalidSession
            tenant = await uow.sessions.get_tenant_context(
                user_id=current.user_id,
                tenant_id=command.tenant_id,
                membership_id=command.membership_id,
            )
            if tenant is None or not _tenant_is_active(tenant, now):
                raise InvalidSession
            await uow.sessions.revoke_family(
                current.current_family_id,
                reason="tenant_switched",
                now=now,
            )
            await uow.sessions.revoke_session(
                current.session_id,
                reason="tenant_switched",
                now=now,
            )
            user = UserSessionState(current.user_id, current.user_status, current.user_auth_version)
            material = self._new_session_material(user=user, tenant=tenant, now=now)
            access_token = self._issue_access(material.session, now)
            await uow.sessions.add_session(material.session)
            await uow.sessions.add_refresh(material.refresh_record)
            await uow.audit.append(
                _audit_event(
                    audit_context,
                    actor_user_id=current.user_id,
                    tenant_id=tenant.tenant_id,
                    membership_id=tenant.membership_id,
                    action="session.switch_tenant",
                    result="success",
                    reason="tenant_context_selected",
                    session_id=material.session.id,
                    now=now,
                )
            )
            await uow.sessions.flush()
        await self._invalidate_cache(command.session_id)
        return SessionResult(
            material.session.id,
            material.session.family_id,
            access_token,
            material.raw_refresh,
        )

    async def _revoke_for_replay(
        self,
        uow: SessionUnitOfWork,
        current: LockedRefreshToken,
        audit_context: AuditContext,
        now: datetime,
    ) -> None:
        await uow.sessions.revoke_family(
            current.family_id,
            reason="refresh_replay",
            now=now,
        )
        await uow.sessions.revoke_session(
            current.session_id,
            reason="refresh_replay",
            now=now,
        )
        await uow.audit.append(
            _audit_event(
                audit_context,
                actor_user_id=current.user_id,
                tenant_id=current.tenant_id,
                membership_id=current.membership_id,
                action="session.refresh_replay",
                result="denied",
                reason="refresh_replay",
                session_id=current.session_id,
                now=now,
            )
        )
        await uow.sessions.flush()

    async def _load_validation_state(
        self, session_id: UUID
    ) -> SessionValidationState | None:
        async with self._uow_factory() as uow:
            state = await uow.sessions.get_validation_state(session_id)
        if state is not None and self._cache is not None and self._cache_ttl > 0:
            try:
                await self._cache.set(
                    session_id,
                    state,
                    ttl_seconds=self._cache_ttl,
                )
            except Exception:
                return state
        return state

    async def _read_cached_state(
        self, session_id: UUID
    ) -> SessionValidationState | None:
        if self._cache is None or self._cache_ttl == 0:
            return None
        try:
            return await self._cache.get(session_id)
        except Exception:
            return None

    async def _invalidate_cache(self, session_id: UUID) -> None:
        if self._cache is None:
            return
        try:
            await self._cache.invalidate(session_id)
        except Exception:
            return

    def _new_session_material(
        self,
        *,
        user: UserSessionState,
        tenant: TenantSessionState | None,
        now: datetime,
    ) -> _SessionMaterial:
        session_id = new_uuid7()
        family_id = new_uuid7()
        raw_refresh = _new_raw_refresh_token()
        expires_at = now + _REFRESH_ABSOLUTE_TTL
        session = NewSession(
            id=session_id,
            user_id=user.user_id,
            tenant_id=None if tenant is None else tenant.tenant_id,
            membership_id=None if tenant is None else tenant.membership_id,
            family_id=family_id,
            auth_version=user.auth_version,
            authz_version=None if tenant is None else tenant.authz_version,
            now=now,
            expires_at=expires_at,
        )
        refresh = NewRefreshToken(
            id=new_uuid7(),
            token_hash=self._refresh_hash(raw_refresh),
            family_id=family_id,
            session_id=session_id,
            user_id=user.user_id,
            tenant_id=session.tenant_id,
            membership_id=session.membership_id,
            issued_at=now,
            expires_at=expires_at,
            idle_expires_at=now + _REFRESH_IDLE_TTL,
        )
        return _SessionMaterial(session, refresh, raw_refresh)

    def _issue_access(self, session: NewSession, now: datetime) -> str:
        claims = AccessTokenClaims(
            user_id=session.user_id,
            session_id=session.id,
            token_id=new_uuid7(),
            audience=(Audience.ACCOUNT if session.tenant_id is None else Audience.TENANT),
            issued_at=now,
            not_before=now,
            expires_at=now + _ACCESS_TTL,
            auth_version=session.auth_version,
            tenant_id=session.tenant_id,
            membership_id=session.membership_id,
            authz_version=session.authz_version,
        )
        if claims.audience is Audience.ACCOUNT:
            return self._tokens.issue_account(claims)
        return self._tokens.issue_tenant(claims)

    def _issue_access_from_state(
        self,
        state: SessionValidationState,
        now: datetime,
    ) -> str:
        session = NewSession(
            state.session_id,
            state.user_id,
            state.session_tenant_id,
            state.session_membership_id,
            state.current_family_id,
            state.user_auth_version,
            state.membership_authz_version,
            now,
            state.expires_at,
        )
        return self._issue_access(session, now)

    def _refresh_hash(self, raw_token: str) -> bytes:
        try:
            encoded = raw_token.encode("ascii", errors="strict")
        except UnicodeEncodeError:
            return hmac.digest(
                self._refresh_hash_key,
                b"refresh-invalid:v1",
                sha256,
            )
        if _REFRESH_TOKEN_PATTERN.fullmatch(raw_token) is None:
            return hmac.digest(
                self._refresh_hash_key,
                b"refresh-invalid:v1",
                sha256,
            )
        return hmac.digest(self._refresh_hash_key, b"refresh:v1:" + encoded, sha256)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("session clock must return a UTC-aware datetime")
        return value.astimezone(UTC).replace(microsecond=0)


@dataclass(frozen=True, slots=True)
class _SessionMaterial:
    session: NewSession
    refresh_record: NewRefreshToken
    raw_refresh: str


def _new_raw_refresh_token() -> str:
    return secrets.token_urlsafe(32)


def _refresh_is_current(token: LockedRefreshToken, now: datetime) -> bool:
    return (
        token.revoked_at is None
        and token.used_at is None
        and token.expires_at > now
        and token.idle_expires_at > now
    )


def _refresh_matches_state(
    token: LockedRefreshToken,
    state: SessionValidationState,
) -> bool:
    return (
        token.session_id == state.session_id
        and token.user_id == state.user_id
        and token.family_id == state.current_family_id
        and token.tenant_id == state.session_tenant_id
        and token.membership_id == state.session_membership_id
    )


def _tenant_is_active(tenant: TenantSessionState, now: datetime) -> bool:
    return (
        tenant.tenant_status == "active"
        and tenant.membership_status == "active"
        and tenant.membership_user_id is not None
        and tenant.valid_from <= now
        and (tenant.valid_until is None or tenant.valid_until > now)
        and tenant.authz_version >= 1
    )


def _state_is_authoritative(
    state: SessionValidationState,
    claims: AccessTokenClaims | None,
    now: datetime,
) -> bool:
    if (
        state.revoked_at is not None
        or state.expires_at <= now
        or state.user_status != "active"
        or state.auth_version_at_issue != state.user_auth_version
    ):
        return False
    if claims is not None and (
        claims.user_id != state.user_id
        or claims.session_id != state.session_id
        or claims.auth_version != state.user_auth_version
    ):
        return False
    if state.session_tenant_id is None:
        return (
            state.session_membership_id is None
            and state.authz_version_at_issue is None
            and (claims is None or claims.audience is Audience.ACCOUNT)
        )
    if (
        state.session_membership_id is None
        or state.authz_version_at_issue is None
        or state.tenant_status != "active"
        or state.membership_user_id != state.user_id
        or state.membership_status != "active"
        or state.membership_valid_from is None
        or state.membership_valid_from > now
        or (
            state.membership_valid_until is not None
            and state.membership_valid_until <= now
        )
        or state.membership_authz_version != state.authz_version_at_issue
    ):
        return False
    return claims is None or (
        claims.audience is Audience.TENANT
        and claims.tenant_id == state.session_tenant_id
        and claims.membership_id == state.session_membership_id
        and claims.authz_version == state.membership_authz_version
    )


def _audit_event(
    context: AuditContext,
    *,
    actor_user_id: UUID | None,
    tenant_id: UUID | None,
    membership_id: UUID | None,
    action: str,
    result: str,
    reason: str,
    session_id: UUID | None,
    now: datetime,
) -> SessionAuditEvent:
    return SessionAuditEvent(
        id=new_uuid7(),
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        actor_membership_id=membership_id,
        action=action,
        result=result,
        reason_code=reason,
        target_type=None if session_id is None else "auth_session",
        target_id=session_id,
        trace_id=context.trace_id,
        client_ip_hash=context.client_ip_hash,
        user_agent_hash=context.user_agent_hash,
        occurred_at=now,
    )
