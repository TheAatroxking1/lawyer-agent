from __future__ import annotations

import hmac
from base64 import b64decode
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Annotated, Any, cast
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lawyer_agent.api.errors import ApiProblem
from lawyer_agent.application.idempotency import IdempotencyService
from lawyer_agent.application.identity import (
    AuditContext,
    IdentityService,
    IdentityUnitOfWork,
)
from lawyer_agent.application.invitations import (
    InvitationService,
    InvitationTargetFingerprintHasher,
    InvitationTokenHasher,
)
from lawyer_agent.application.platform import PlatformActor, PlatformReviewService
from lawyer_agent.application.sessions import (
    InvalidSession,
    SessionService,
    SessionUnitOfWork,
)
from lawyer_agent.application.tenancy import (
    TenantActor,
    TenantService,
    TenantWorkflowUnitOfWork,
)
from lawyer_agent.config import Settings
from lawyer_agent.domain.authorization import AuthorizationScope, Principal, PrincipalAudience
from lawyer_agent.domain.sessions import Audience, InvalidToken
from lawyer_agent.domain.tenancy import MembershipStatus, TenantContext, TenantStatus
from lawyer_agent.infrastructure.persistence.engine import create_engine, create_session_factory
from lawyer_agent.infrastructure.persistence.invitations_uow import (
    SqlAlchemyInvitationWorkflowUnitOfWork,
)
from lawyer_agent.infrastructure.persistence.models import (
    TenantMembershipModel,
    TenantModel,
    UserModel,
)
from lawyer_agent.infrastructure.persistence.platform_uow import (
    SqlAlchemyPlatformWorkflowUnitOfWork,
)
from lawyer_agent.infrastructure.persistence.repositories.identity import (
    SqlAlchemyIdentityUnitOfWork,
)
from lawyer_agent.infrastructure.persistence.repositories.sessions import (
    SqlAlchemySessionUnitOfWork,
)
from lawyer_agent.infrastructure.persistence.tenancy_uow import (
    SqlAlchemyTenantWorkflowUnitOfWork,
)
from lawyer_agent.infrastructure.redis.authz_cache import AuthorizationCache
from lawyer_agent.infrastructure.redis.client import RedisAsyncioAdapter
from lawyer_agent.infrastructure.redis.rate_limit import RateLimiter
from lawyer_agent.infrastructure.redis.step_up import StepUpStore
from lawyer_agent.infrastructure.security.blind_index import BlindIndexService
from lawyer_agent.infrastructure.security.cipher import SensitiveValueCipher
from lawyer_agent.infrastructure.security.csrf import CsrfService
from lawyer_agent.infrastructure.security.jwt_tokens import TokenService
from lawyer_agent.infrastructure.security.passwords import Argon2PasswordHasher


@dataclass(slots=True)
class ApplicationServices:
    identity: Any
    sessions: Any
    tenancy: Any
    invitations: Any
    platform: Any
    rate_limiter: Any
    step_up: Any
    csrf: Any
    token_service: Any
    accounts: Any


@dataclass(frozen=True, slots=True)
class AccountProjection:
    id: UUID
    display_name: str
    status: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AccountTenantProjection:
    tenant_id: UUID
    membership_id: UUID
    name: str
    tenant_type: str
    tenant_status: str
    membership_status: str


class AccountQueries:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, user_id: UUID) -> AccountProjection:
        async with self._session_factory() as session:
            user = await session.get(UserModel, user_id)
        if user is None:
            raise InvalidSession
        created_at = user.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        return AccountProjection(user.id, user.display_name, user.status, created_at)

    async def list_tenants(self, user_id: UUID) -> tuple[AccountTenantProjection, ...]:
        statement = (
            select(TenantMembershipModel, TenantModel)
            .join(TenantModel, TenantModel.id == TenantMembershipModel.tenant_id)
            .where(
                TenantMembershipModel.user_id == user_id,
                TenantMembershipModel.status.in_(("active", "suspended", "invited")),
            )
            .order_by(TenantMembershipModel.created_at, TenantMembershipModel.id)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
        return tuple(
            AccountTenantProjection(
                tenant_id=tenant.id,
                membership_id=membership.id,
                name=tenant.name,
                tenant_type=tenant.tenant_type,
                tenant_status=tenant.status,
                membership_status=membership.status,
            )
            for membership, tenant in rows
        )


class _UnavailableInvitationDelivery:
    async def deliver(self, *, invitation_id: UUID, token: str) -> None:
        del invitation_id, token
        raise RuntimeError("invitation provider is not configured")


def _derived_key(domain: bytes, key: bytes) -> bytes:
    return hmac.digest(key, b"lawyer-api-composition:v1:" + domain, sha256)


@asynccontextmanager
async def application_services(settings: Settings) -> AsyncIterator[ApplicationServices]:
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    redis = RedisAsyncioAdapter.from_url(
        settings.redis_url,
        key_prefix=settings.redis_key_prefix,
        topology=settings.redis_security_topology,
    )
    data_key = b64decode(settings.data_encryption_key_b64, validate=True)
    blind_key = b64decode(settings.blind_index_key_b64, validate=True)
    refresh_key = b64decode(settings.refresh_token_key_b64, validate=True)
    csrf_key = b64decode(settings.csrf_key_b64, validate=True)
    cipher = SensitiveValueCipher({1: data_key}, active_key_version=1)
    blind_index = BlindIndexService({1: blind_key}, active_key_version=1)
    private_keys = {
        kid: Ed25519PrivateKey.from_private_bytes(sha256(material.encode()).digest())
        for kid, material in settings.jwt_ed25519_key_ring.items()
    }
    token_service = TokenService(
        issuer=settings.jwt_issuer,
        active_kid=settings.jwt_active_kid,
        signing_keys=private_keys,
        verification_keys={kid: key.public_key() for kid, key in private_keys.items()},
    )
    idempotency = IdempotencyService(
        key_hash_secret=_derived_key(b"idempotency", refresh_key)
    )
    authz_cache = AuthorizationCache(redis=redis)
    step_up = StepUpStore(redis=redis, hmac_key=_derived_key(b"step-up", csrf_key))
    identity = IdentityService(
        uow_factory=lambda: cast(
            IdentityUnitOfWork,
            SqlAlchemyIdentityUnitOfWork(session_factory),
        ),
        password_hasher=Argon2PasswordHasher(),
        cipher=cipher,
        blind_index=blind_index,
        rollout_policy=settings.identity_rollout_policy(),
    )
    sessions = SessionService(
        uow_factory=lambda: cast(
            SessionUnitOfWork,
            SqlAlchemySessionUnitOfWork(session_factory),
        ),
        token_service=token_service,
        refresh_hash_key=refresh_key,
    )
    tenancy = TenantService(
        uow_factory=lambda: cast(
            TenantWorkflowUnitOfWork,
            SqlAlchemyTenantWorkflowUnitOfWork(session_factory),
        ),
        idempotency=idempotency,
        cursor_secret=_derived_key(b"member-cursor", csrf_key),
        authorization_cache=authz_cache,
    )
    invitations = InvitationService(
        uow_factory=lambda: SqlAlchemyInvitationWorkflowUnitOfWork(session_factory),
        idempotency=idempotency,
        token_hasher=InvitationTokenHasher(
            hmac_key=_derived_key(b"invitation-token", refresh_key)
        ),
        blind_index=blind_index,
        target_fingerprint_hasher=InvitationTargetFingerprintHasher(
            hmac_key=_derived_key(b"invitation-target", blind_key)
        ),
        cipher=cipher,
        delivery=_UnavailableInvitationDelivery(),
    )
    platform = PlatformReviewService(
        uow_factory=lambda: SqlAlchemyPlatformWorkflowUnitOfWork(session_factory),
        idempotency=idempotency,
        step_up=step_up,
    )
    active = ApplicationServices(
        identity=identity,
        sessions=sessions,
        tenancy=tenancy,
        invitations=invitations,
        platform=platform,
        rate_limiter=RateLimiter(
            redis=redis,
            hmac_key=_derived_key(b"rate-limit", csrf_key),
        ),
        step_up=step_up,
        csrf=CsrfService(key=csrf_key, trusted_origins=settings.trusted_origins),
        token_service=token_service,
        accounts=AccountQueries(session_factory),
    )
    try:
        yield active
    finally:
        try:
            await redis.aclose()
        finally:
            await engine.dispose()


def services(request: Request) -> ApplicationServices:
    value = getattr(request.app.state, "services", None)
    if not isinstance(value, ApplicationServices):
        raise RuntimeError("application services are unavailable")
    return value


Services = Annotated[ApplicationServices, Depends(services)]


def audit_context(request: Request) -> AuditContext:
    trace_id = getattr(request.state, "trace_id", "missing-trace-id")
    key = request.app.state.settings.secret_key.encode("utf-8")
    client = request.client.host if request.client is not None else "unknown"
    user_agent = request.headers.get("user-agent", "")[:512]
    return AuditContext(
        trace_id=trace_id,
        client_ip_hash=hmac.digest(key, f"client-ip:v1:{client}".encode(), sha256),
        user_agent_hash=hmac.digest(key, f"user-agent:v1:{user_agent}".encode(), sha256),
    )


Audit = Annotated[AuditContext, Depends(audit_context)]


def require_trusted_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin is None or origin not in request.app.state.settings.trusted_origins:
        raise ApiProblem(403, "csrf_origin_rejected", "Request origin is not trusted")


TrustedOrigin = Annotated[None, Depends(require_trusted_origin)]


def bearer_token(authorization: Annotated[str | None, Header()] = None) -> str:
    if authorization is None:
        raise ApiProblem(401, "authentication_failed", "Authentication failed")
    scheme, separator, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or separator != " " or not token or " " in token:
        raise ApiProblem(401, "authentication_failed", "Authentication failed")
    return token


Bearer = Annotated[str, Depends(bearer_token)]


async def account_session(token: Bearer, app_services: Services) -> Any:
    return await app_services.sessions.validate_access(token, audience=Audience.ACCOUNT)


AccountSession = Annotated[Any, Depends(account_session)]


def _claims(app_services: ApplicationServices, token: str, audience: Audience) -> Any:
    try:
        return app_services.token_service.verify(
            token,
            audience=audience,
            now=datetime.now(UTC),
        )
    except InvalidToken:
        raise ApiProblem(401, "authentication_failed", "Authentication failed") from None


async def tenant_actor(token: Bearer, app_services: Services) -> TenantActor:
    claims = _claims(app_services, token, Audience.TENANT)
    validated = await app_services.sessions.validate_access(token, audience=Audience.TENANT)
    if validated.tenant_id is None or validated.membership_id is None:
        raise ApiProblem(401, "authentication_failed", "Authentication failed")
    principal = Principal(
        user_id=validated.user_id,
        session_id=validated.session_id,
        audience=PrincipalAudience.TENANT,
        tenant_id=validated.tenant_id,
        membership_id=validated.membership_id,
        user_status="active",
        session_valid=True,
        auth_version=claims.auth_version,
        session_auth_version=claims.auth_version,
        permissions=frozenset(),
        role_codes=frozenset(),
        authenticated_at=claims.issued_at,
    )
    context = TenantContext(
        tenant_id=validated.tenant_id,
        membership_id=validated.membership_id,
        membership_user_id=validated.user_id,
        department_id=None,
        tenant_status=TenantStatus.ACTIVE,
        membership_status=MembershipStatus.ACTIVE,
        valid_from=claims.issued_at,
        valid_until=None,
        authz_version=claims.authz_version,
        session_authz_version=claims.authz_version,
        scope=AuthorizationScope(allow_tenant_wide=True),
    )
    return TenantActor(principal, context)


TenantActorDependency = Annotated[TenantActor, Depends(tenant_actor)]


async def platform_actor(token: Bearer, app_services: Services) -> PlatformActor:
    claims = _claims(app_services, token, Audience.PLATFORM)
    validated = await app_services.sessions.validate_access(token, audience=Audience.PLATFORM)
    principal = Principal(
        user_id=validated.user_id,
        session_id=validated.session_id,
        audience=PrincipalAudience.PLATFORM,
        tenant_id=None,
        membership_id=None,
        user_status="active",
        session_valid=True,
        auth_version=claims.auth_version,
        session_auth_version=claims.auth_version,
        permissions=frozenset(),
        role_codes=frozenset(),
        authenticated_at=claims.issued_at,
    )
    return PlatformActor(principal)


PlatformActorDependency = Annotated[PlatformActor, Depends(platform_actor)]


def require_path_tenant(path_tenant_id: UUID, actor: TenantActor) -> None:
    if path_tenant_id != actor.context.tenant_id:
        raise ApiProblem(404, "tenant_resource_not_found", "Resource not found")
