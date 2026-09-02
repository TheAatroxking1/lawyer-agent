from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import delete, select, update
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from alembic import command
from lawyer_agent.application.identity import AuditContext
from lawyer_agent.application.sessions import (
    InvalidRefreshToken,
    InvalidRevocationReason,
    InvalidSession,
    RefreshReplayDetected,
    RefreshResult,
    SessionService,
    SwitchTenantCommand,
)
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.sessions import AccessTokenClaims, Audience, RevocationReason
from lawyer_agent.infrastructure.persistence.models import (
    AuditEventModel,
    AuthSessionModel,
    RefreshTokenRecordModel,
    TenantMembershipModel,
    TenantModel,
    UserModel,
)
from lawyer_agent.infrastructure.persistence.repositories.sessions import (
    SqlAlchemySessionUnitOfWork,
)
from lawyer_agent.infrastructure.security.jwt_tokens import TokenService

pytestmark = [pytest.mark.integration, pytest.mark.mysql]

NOW = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
AUDIT = AuditContext("trace-session", b"i" * 32, b"u" * 32)


def _alembic_config(mysql_url: URL) -> Config:
    backend_dir = Path(__file__).parents[3]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "alembic"))
    config.set_main_option(
        "sqlalchemy.url",
        mysql_url.render_as_string(hide_password=False).replace("%", "%%"),
    )
    return config


@pytest.fixture(scope="module")
def migrated_mysql_url(mysql_url: URL) -> Iterator[URL]:
    command.upgrade(_alembic_config(mysql_url), "head")
    yield mysql_url


@pytest.fixture
async def session_database(
    migrated_mysql_url: URL,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    engine = create_async_engine(migrated_mysql_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        yield engine, factory
    finally:
        async with factory.begin() as session:
            await session.execute(delete(AuditEventModel))
            await session.execute(delete(RefreshTokenRecordModel))
            await session.execute(delete(AuthSessionModel))
            await session.execute(delete(TenantMembershipModel))
            await session.execute(delete(TenantModel))
            await session.execute(delete(UserModel))
        await engine.dispose()


@pytest.fixture
async def principal(
    session_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> tuple[UUID, UUID, UUID]:
    _, factory = session_database
    user_id = new_uuid7()
    tenant_id = new_uuid7()
    membership_id = new_uuid7()
    now = NOW.replace(tzinfo=None)
    async with factory.begin() as session:
        session.add(
            UserModel(
                id=user_id,
                status="active",
                display_name="Session User",
                auth_version=3,
            )
        )
        await session.flush()
        session.add(
            TenantModel(
                id=tenant_id,
                name="Session Tenant",
                normalized_name=f"session-{tenant_id}",
                tenant_type="enterprise",
                status="active",
                created_by_user_id=user_id,
                review_status="approved",
            )
        )
        await session.flush()
        session.add(
            TenantMembershipModel(
                id=membership_id,
                tenant_id=tenant_id,
                user_id=user_id,
                department_id=None,
                member_type="internal",
                status="active",
                valid_from=now - timedelta(days=1),
                valid_until=None,
                authz_version=7,
            )
        )
    return user_id, tenant_id, membership_id


@pytest.fixture
def token_service() -> TokenService:
    private_key = Ed25519PrivateKey.generate()
    return TokenService(
        issuer="https://identity.lawyer-agent.test",
        active_kid="integration-key",
        signing_keys={"integration-key": private_key},
        verification_keys={"integration-key": private_key.public_key()},
    )


@pytest.fixture
def service_factory(
    session_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    token_service: TokenService,
) -> Callable[..., SessionService]:
    _, factory = session_database

    def build(
        *,
        validation_cache: object | None = None,
        token_override: object | None = None,
    ) -> SessionService:
        return SessionService(
            uow_factory=lambda: SqlAlchemySessionUnitOfWork(factory),
            token_service=(
                token_service if token_override is None else token_override
            ),  # type: ignore[arg-type]
            refresh_hash_key=b"r" * 32,
            clock=lambda: NOW,
            validation_cache=validation_cache,  # type: ignore[arg-type]
        )

    return build


@pytest.mark.asyncio
async def test_start_refresh_and_authoritative_account_session_validation(
    principal: tuple[UUID, UUID, UUID],
    session_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    service_factory: Callable[[], SessionService],
) -> None:
    user_id, _, _ = principal
    _, factory = session_database
    started = await service_factory().start(user_id=user_id, audit_context=AUDIT)

    validated = await service_factory().validate_access(
        started.access_token,
        audience=Audience.ACCOUNT,
    )
    assert validated.user_id == user_id
    assert validated.session_id == started.session_id

    refreshed = await service_factory().refresh(started.refresh_token, audit_context=AUDIT)
    assert refreshed.session_id == started.session_id
    assert refreshed.refresh_token != started.refresh_token

    async with factory() as session:
        stored_hashes = (await session.scalars(select(RefreshTokenRecordModel.token_hash))).all()
        audit_payloads = (await session.scalars(select(AuditEventModel.metadata_json))).all()
    assert started.refresh_token.encode() not in stored_hashes
    assert refreshed.refresh_token.encode() not in stored_hashes
    assert all(started.refresh_token not in str(payload) for payload in audit_payloads)

    async with factory.begin() as session:
        await session.execute(
            update(UserModel).where(UserModel.id == user_id).values(auth_version=4)
        )
    with pytest.raises(InvalidSession):
        await service_factory().validate_access(
            refreshed.access_token,
            audience=Audience.ACCOUNT,
        )


@pytest.mark.asyncio
async def test_tenant_session_checks_tenant_membership_status_and_authz_version(
    principal: tuple[UUID, UUID, UUID],
    session_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    service_factory: Callable[[], SessionService],
) -> None:
    user_id, tenant_id, membership_id = principal
    _, factory = session_database
    account = await service_factory().start(user_id=user_id, audit_context=AUDIT)
    tenant = await service_factory().switch_tenant(
        SwitchTenantCommand(account.session_id, tenant_id, membership_id),
        audit_context=AUDIT,
    )

    validated = await service_factory().validate_access(
        tenant.access_token,
        audience=Audience.TENANT,
    )
    assert validated.tenant_id == tenant_id
    assert validated.membership_id == membership_id

    with pytest.raises(InvalidSession):
        await service_factory().validate_access(
            account.access_token,
            audience=Audience.ACCOUNT,
        )

    async with factory.begin() as session:
        await session.execute(
            update(TenantMembershipModel)
            .where(TenantMembershipModel.id == membership_id)
            .values(authz_version=8)
        )
    with pytest.raises(InvalidSession):
        await service_factory().validate_access(
            tenant.access_token,
            audience=Audience.TENANT,
        )


@pytest.mark.asyncio
async def test_parallel_refresh_allows_one_success_then_revokes_family_for_replay(
    principal: tuple[UUID, UUID, UUID],
    session_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    service_factory: Callable[[], SessionService],
) -> None:
    user_id, _, _ = principal
    _, factory = session_database
    started = await service_factory().start(user_id=user_id, audit_context=AUDIT)

    first, second = await asyncio.gather(
        service_factory().refresh(started.refresh_token, audit_context=AUDIT),
        service_factory().refresh(started.refresh_token, audit_context=AUDIT),
        return_exceptions=True,
    )
    values = (first, second)
    assert sum(isinstance(value, RefreshResult) for value in values) == 1
    assert sum(isinstance(value, RefreshReplayDetected) for value in values) == 1
    success = next(value for value in values if isinstance(value, RefreshResult))

    async with factory() as session:
        family_records = (
            await session.scalars(
                select(RefreshTokenRecordModel).where(
                    RefreshTokenRecordModel.family_id == success.family_id
                )
            )
        ).all()
        auth_session = await session.get(AuthSessionModel, success.session_id)
    assert family_records and all(record.revoked_at is not None for record in family_records)
    assert auth_session is not None and auth_session.revoked_at is not None

    with pytest.raises(InvalidSession):
        await service_factory().validate_access(
            success.access_token,
            audience=Audience.ACCOUNT,
        )


class _MemoryValidationCache:
    def __init__(self) -> None:
        self.values: dict[UUID, object] = {}
        self.invalidated: list[UUID] = []

    async def get(self, session_id: UUID) -> object | None:
        return self.values.get(session_id)

    async def set(
        self,
        session_id: UUID,
        state: object,
        *,
        ttl_seconds: int,
    ) -> None:
        assert ttl_seconds <= 60
        self.values[session_id] = state

    async def invalidate(self, session_id: UUID) -> None:
        self.invalidated.append(session_id)
        self.values.pop(session_id, None)


@pytest.mark.asyncio
async def test_positive_cache_never_replaces_authoritative_mysql_validation(
    principal: tuple[UUID, UUID, UUID],
    session_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    service_factory: Callable[..., SessionService],
) -> None:
    user_id, _, _ = principal
    _, factory = session_database
    cache = _MemoryValidationCache()
    service = service_factory(validation_cache=cache)
    started = await service.start(user_id=user_id, audit_context=AUDIT)
    await service.validate_access(started.access_token, audience=Audience.ACCOUNT)
    assert started.session_id in cache.values

    async with factory.begin() as session:
        await session.execute(
            update(UserModel).where(UserModel.id == user_id).values(auth_version=4)
        )

    with pytest.raises(InvalidSession):
        await service.validate_access(started.access_token, audience=Audience.ACCOUNT)


class _FailingValidationCache:
    async def get(self, session_id: UUID) -> None:
        del session_id
        raise ConnectionError("synthetic redis outage")

    async def set(
        self,
        session_id: UUID,
        state: object,
        *,
        ttl_seconds: int,
    ) -> None:
        del session_id, state, ttl_seconds
        raise ConnectionError("synthetic redis outage")

    async def invalidate(self, session_id: UUID) -> None:
        del session_id
        raise ConnectionError("synthetic redis outage")


@pytest.mark.asyncio
async def test_validation_cache_outage_falls_back_to_mysql_without_fail_open(
    principal: tuple[UUID, UUID, UUID],
    session_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    service_factory: Callable[..., SessionService],
) -> None:
    user_id, _, _ = principal
    _, factory = session_database
    service = service_factory(validation_cache=_FailingValidationCache())
    started = await service.start(user_id=user_id, audit_context=AUDIT)

    assert (
        await service.validate_access(started.access_token, audience=Audience.ACCOUNT)
    ).user_id == user_id

    async with factory.begin() as session:
        await session.execute(
            update(UserModel).where(UserModel.id == user_id).values(status="disabled")
        )
    with pytest.raises(InvalidSession):
        await service.validate_access(started.access_token, audience=Audience.ACCOUNT)


class _FailingTokenService:
    def issue_account(self, claims: object) -> str:
        del claims
        raise RuntimeError("synthetic signer outage")

    def issue_tenant(self, claims: object) -> str:
        del claims
        raise RuntimeError("synthetic signer outage")

    def issue_platform(self, claims: object) -> str:
        del claims
        raise RuntimeError("synthetic signer outage")

    def verify(self, encoded: str, *, audience: object, now: object = None) -> object:
        del encoded, audience, now
        raise AssertionError("verify is not expected")


@pytest.mark.asyncio
async def test_start_rolls_back_session_when_access_token_signing_fails(
    principal: tuple[UUID, UUID, UUID],
    session_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    service_factory: Callable[..., SessionService],
) -> None:
    user_id, _, _ = principal
    _, factory = session_database

    with pytest.raises(RuntimeError, match="signer outage"):
        await service_factory(token_override=_FailingTokenService()).start(
            user_id=user_id,
            audit_context=AUDIT,
        )

    async with factory() as session:
        assert (await session.scalars(select(AuthSessionModel))).all() == []
        assert (await session.scalars(select(RefreshTokenRecordModel))).all() == []


@pytest.mark.asyncio
async def test_refresh_rejects_token_outside_session_current_family(
    principal: tuple[UUID, UUID, UUID],
    session_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    service_factory: Callable[..., SessionService],
) -> None:
    user_id, _, _ = principal
    _, factory = session_database
    started = await service_factory().start(user_id=user_id, audit_context=AUDIT)
    async with factory.begin() as session:
        await session.execute(
            update(AuthSessionModel)
            .where(AuthSessionModel.id == started.session_id)
            .values(current_family_id=new_uuid7())
        )

    with pytest.raises(InvalidRefreshToken):
        await service_factory().refresh(started.refresh_token, audit_context=AUDIT)

    async with factory() as session:
        auth_session = await session.get(AuthSessionModel, started.session_id)
    assert auth_session is not None and auth_session.revoked_at is not None


@pytest.mark.asyncio
async def test_malformed_refresh_token_is_a_generic_failure_without_state_change(
    principal: tuple[UUID, UUID, UUID],
    session_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    service_factory: Callable[..., SessionService],
) -> None:
    user_id, _, _ = principal
    _, factory = session_database
    started = await service_factory().start(user_id=user_id, audit_context=AUDIT)

    with pytest.raises(InvalidRefreshToken):
        await service_factory().refresh("\ud800", audit_context=AUDIT)

    async with factory() as session:
        auth_session = await session.get(AuthSessionModel, started.session_id)
    assert auth_session is not None and auth_session.revoked_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("tenant_status", ["suspended", "closed"])
async def test_tenant_access_rejects_non_active_tenant_statuses(
    principal: tuple[UUID, UUID, UUID],
    session_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    service_factory: Callable[..., SessionService],
    tenant_status: str,
) -> None:
    user_id, tenant_id, membership_id = principal
    _, factory = session_database
    service = service_factory()
    account = await service.start(user_id=user_id, audit_context=AUDIT)
    tenant = await service.switch_tenant(
        SwitchTenantCommand(account.session_id, tenant_id, membership_id),
        audit_context=AUDIT,
    )
    async with factory.begin() as session:
        await session.execute(
            update(TenantModel)
            .where(TenantModel.id == tenant_id)
            .values(status=tenant_status)
        )

    with pytest.raises(InvalidSession):
        await service.validate_access(tenant.access_token, audience=Audience.TENANT)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "membership_values",
    [
        {"status": "suspended"},
        {"status": "revoked"},
        {"valid_from": (NOW + timedelta(seconds=1)).replace(tzinfo=None)},
        {"valid_until": NOW.replace(tzinfo=None)},
    ],
)
async def test_tenant_access_rejects_inactive_or_out_of_window_membership(
    principal: tuple[UUID, UUID, UUID],
    session_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    service_factory: Callable[..., SessionService],
    membership_values: dict[str, object],
) -> None:
    user_id, tenant_id, membership_id = principal
    _, factory = session_database
    service = service_factory()
    account = await service.start(user_id=user_id, audit_context=AUDIT)
    tenant = await service.switch_tenant(
        SwitchTenantCommand(account.session_id, tenant_id, membership_id),
        audit_context=AUDIT,
    )
    async with factory.begin() as session:
        await session.execute(
            update(TenantMembershipModel)
            .where(TenantMembershipModel.id == membership_id)
            .values(**membership_values)
        )

    with pytest.raises(InvalidSession):
        await service.validate_access(tenant.access_token, audience=Audience.TENANT)


@pytest.mark.asyncio
async def test_explicit_revoke_invalidates_session_family_and_cache(
    principal: tuple[UUID, UUID, UUID],
    session_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    service_factory: Callable[..., SessionService],
) -> None:
    user_id, _, _ = principal
    _, factory = session_database
    cache = _MemoryValidationCache()
    service = service_factory(validation_cache=cache)
    started = await service.start(user_id=user_id, audit_context=AUDIT)
    await service.validate_access(started.access_token, audience=Audience.ACCOUNT)
    assert started.session_id in cache.values

    await service.revoke(
        started.session_id,
        reason=RevocationReason.LOGOUT,
        audit_context=AUDIT,
    )

    async with factory() as session:
        auth_session = await session.get(AuthSessionModel, started.session_id)
        family = (
            await session.scalars(
                select(RefreshTokenRecordModel).where(
                    RefreshTokenRecordModel.family_id == started.family_id
                )
            )
        ).all()
        revoke_audit = await session.scalar(
            select(AuditEventModel).where(AuditEventModel.action == "session.revoke")
        )
    assert auth_session is not None
    assert auth_session.revoked_at is not None
    assert auth_session.revocation_reason == RevocationReason.LOGOUT.value
    assert family and all(record.revoked_at is not None for record in family)
    assert all(
        record.revocation_reason == RevocationReason.LOGOUT.value for record in family
    )
    assert revoke_audit is not None
    assert revoke_audit.reason_code == RevocationReason.LOGOUT.value
    assert revoke_audit.metadata_json is None
    assert started.session_id not in cache.values
    assert cache.invalidated == [started.session_id]
    with pytest.raises(InvalidSession):
        await service.validate_access(started.access_token, audience=Audience.ACCOUNT)
    with pytest.raises(InvalidRefreshToken):
        await service.refresh(started.refresh_token, audit_context=AUDIT)


@pytest.mark.asyncio
async def test_explicit_revoke_handles_active_session_without_refresh_rows(
    principal: tuple[UUID, UUID, UUID],
    session_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    service_factory: Callable[..., SessionService],
) -> None:
    user_id, _, _ = principal
    _, factory = session_database
    service = service_factory()
    started = await service.start(user_id=user_id, audit_context=AUDIT)
    async with factory.begin() as session:
        await session.execute(
            delete(RefreshTokenRecordModel).where(
                RefreshTokenRecordModel.session_id == started.session_id
            )
        )

    await service.revoke(
        started.session_id,
        reason=RevocationReason.ADMIN_REVOKED,
        audit_context=AUDIT,
    )

    async with factory() as session:
        auth_session = await session.get(AuthSessionModel, started.session_id)
        revoke_audits = (
            await session.scalars(
                select(AuditEventModel).where(
                    AuditEventModel.action == "session.revoke",
                    AuditEventModel.target_id == started.session_id,
                )
            )
        ).all()
    assert auth_session is not None and auth_session.revoked_at is not None
    assert auth_session.revocation_reason == RevocationReason.ADMIN_REVOKED.value
    assert len(revoke_audits) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_reason",
    [
        "logout",
        "unknown_reason",
        "Bearer synthetic.raw.token",
        "user@example.test",
        "x" * 129,
        None,
    ],
)
async def test_revoke_rejects_untyped_reason_before_opening_transaction(
    token_service: TokenService,
    invalid_reason: object,
) -> None:
    uow_opened = False

    def forbidden_uow() -> object:
        nonlocal uow_opened
        uow_opened = True
        raise AssertionError("unit of work must not open for an invalid reason")

    service = SessionService(
        uow_factory=forbidden_uow,  # type: ignore[arg-type]
        token_service=token_service,
        refresh_hash_key=b"r" * 32,
        clock=lambda: NOW,
    )

    with pytest.raises(InvalidRevocationReason):
        await service.revoke(
            new_uuid7(),
            reason=invalid_reason,  # type: ignore[arg-type]
            audit_context=AUDIT,
        )
    assert uow_opened is False


class _FailOnSecondAccountIssueTokenService:
    def __init__(self, delegate: TokenService) -> None:
        self._delegate = delegate
        self._account_issues = 0

    def issue_account(self, claims: AccessTokenClaims) -> str:
        self._account_issues += 1
        if self._account_issues == 2:
            raise RuntimeError("synthetic replacement signer outage")
        return self._delegate.issue_account(claims)

    def issue_tenant(self, claims: AccessTokenClaims) -> str:
        return self._delegate.issue_tenant(claims)

    def issue_platform(self, claims: AccessTokenClaims) -> str:
        return self._delegate.issue_platform(claims)

    def verify(
        self,
        encoded: str,
        *,
        audience: Audience,
        now: datetime | None = None,
    ) -> AccessTokenClaims:
        return self._delegate.verify(encoded, audience=audience, now=now)


@pytest.mark.asyncio
async def test_refresh_signing_failure_rolls_back_rotation_and_audit(
    principal: tuple[UUID, UUID, UUID],
    session_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    service_factory: Callable[..., SessionService],
    token_service: TokenService,
) -> None:
    user_id, _, _ = principal
    _, factory = session_database
    signer = _FailOnSecondAccountIssueTokenService(token_service)
    service = service_factory(token_override=signer)
    started = await service.start(user_id=user_id, audit_context=AUDIT)

    with pytest.raises(RuntimeError, match="replacement signer outage"):
        await service.refresh(started.refresh_token, audit_context=AUDIT)

    async with factory() as session:
        records = (
            await session.scalars(
                select(RefreshTokenRecordModel).where(
                    RefreshTokenRecordModel.family_id == started.family_id
                )
            )
        ).all()
        refresh_audits = (
            await session.scalars(
                select(AuditEventModel).where(AuditEventModel.action == "session.refresh")
            )
        ).all()
    assert len(records) == 1
    assert records[0].used_at is None
    assert records[0].replaced_by_id is None
    assert records[0].revoked_at is None
    assert refresh_audits == []
