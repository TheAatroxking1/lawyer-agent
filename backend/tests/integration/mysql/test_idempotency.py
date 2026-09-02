from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Self

import pytest
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import delete, event, func, select, update
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from alembic import command
from lawyer_agent.application.idempotency import (
    IdempotencyConflictError,
    IdempotencyFingerprintPayload,
    IdempotencyRequest,
    IdempotencyResultReference,
    IdempotencyScope,
    IdempotencyScopeType,
    IdempotencyService,
    IdempotencyStatus,
)
from lawyer_agent.application.identity import AuditContext
from lawyer_agent.application.sessions import (
    InvalidRefreshToken,
    InvalidSession,
    RefreshReplayDetected,
    SessionService,
    SwitchTenantCommand,
)
from lawyer_agent.application.tenancy import (
    AuthorizationCacheInvalidationDispatcher,
    CreateTenantApplicationCommand,
    MemberPageQuery,
    MembershipMutationDenied,
    PostCommitCacheInvalidationError,
    RevokeMemberCommand,
    RoleTemplateUnavailable,
    TenantActor,
    TenantAuthorizationDenied,
    TenantResourceNotFound,
    TenantService,
    TenantType,
    TenantWorkflowUnitOfWork,
    UpdateMemberCommand,
    UpdateTenantCommand,
    VersionConflict,
)
from lawyer_agent.domain.authorization import (
    AuthorizationScope,
    Principal,
    PrincipalAudience,
)
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.sessions import RevocationReason
from lawyer_agent.domain.tenancy import MembershipStatus, TenantContext, TenantStatus
from lawyer_agent.infrastructure.persistence.models import (
    AuditEventModel,
    AuthorizationCacheInvalidationOutboxModel,
    AuthSessionModel,
    DepartmentModel,
    IdempotencyRecordModel,
    MembershipRoleAssignmentModel,
    PermissionModel,
    PlatformRoleAssignmentModel,
    RefreshTokenRecordModel,
    RoleTemplateModel,
    RoleTemplatePermissionModel,
    TenantMembershipModel,
    TenantModel,
    TenantRoleModel,
    TenantRolePermissionModel,
    UserModel,
)
from lawyer_agent.infrastructure.persistence.repositories.idempotency import (
    SqlAlchemyIdempotencyRepository,
)
from lawyer_agent.infrastructure.persistence.repositories.sessions import (
    SqlAlchemySessionUnitOfWork,
)
from lawyer_agent.infrastructure.persistence.seed_authz import (
    TENANT_ROLE_TEMPLATES,
    seed_authorization_catalog,
)
from lawyer_agent.infrastructure.persistence.tenancy_uow import SqlAlchemyTenantWorkflowUnitOfWork
from lawyer_agent.infrastructure.security.jwt_tokens import TokenService

pytestmark = [pytest.mark.integration, pytest.mark.mysql]

NOW = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
KEY = "idempotency-key-000001"


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
async def database(
    migrated_mysql_url: URL,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(migrated_mysql_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        yield factory
    finally:
        async with factory.begin() as session:
            await session.execute(delete(AuditEventModel))
            await session.execute(delete(AuthorizationCacheInvalidationOutboxModel))
            await session.execute(delete(IdempotencyRecordModel))
            await session.execute(delete(RefreshTokenRecordModel))
            await session.execute(delete(AuthSessionModel))
            await session.execute(delete(MembershipRoleAssignmentModel))
            await session.execute(delete(TenantRolePermissionModel))
            await session.execute(delete(TenantRoleModel))
            await session.execute(delete(TenantMembershipModel))
            await session.execute(delete(DepartmentModel))
            await session.execute(delete(TenantModel))
            await session.execute(delete(UserModel))
        await engine.dispose()


def _request(*, name: str = "合成律所") -> IdempotencyRequest:
    return IdempotencyRequest(
        key=KEY,
        method="POST",
        canonical_route="/api/v1/tenants",
        body=IdempotencyFingerprintPayload(
            values={"name": name, "tenant_type": "law_firm"},
            business_paths=frozenset({("name",), ("tenant_type",)}),
        ),
    )


def _service() -> IdempotencyService:
    return IdempotencyService(key_hash_secret=b"i" * 32)


def _audit_context() -> AuditContext:
    return AuditContext(
        trace_id="trace-task7-bootstrap",
        client_ip_hash=b"p" * 32,
        user_agent_hash=b"u" * 32,
    )


def _tenant_service(
    database: async_sessionmaker[AsyncSession],
    *,
    uow_factory: Callable[[], TenantWorkflowUnitOfWork] | None = None,
    cache: object | None = None,
) -> TenantService:
    effective_cache = _RecordingAuthorizationCache() if cache is None else cache
    return TenantService(
        uow_factory=uow_factory or (lambda: SqlAlchemyTenantWorkflowUnitOfWork(database)),
        idempotency=_service(),
        cursor_secret=b"c" * 32,
        clock=lambda: NOW,
        authorization_cache=effective_cache,  # type: ignore[arg-type]
    )


async def _add_active_user(
    database: async_sessionmaker[AsyncSession],
    *,
    user_id: object | None = None,
) -> object:
    actual_id = new_uuid7() if user_id is None else user_id
    async with database.begin() as session:
        await seed_authorization_catalog(session)
        session.add(UserModel(id=actual_id, status="active", display_name="合成申请人"))
    return actual_id


def _create_command(user_id: object, *, name: str = "合成律所") -> CreateTenantApplicationCommand:
    return CreateTenantApplicationCommand(
        actor_user_id=user_id,  # type: ignore[arg-type]
        name=name,
        tenant_type=TenantType.LAW_FIRM,
        idempotency_key=KEY,
        audit_context=_audit_context(),
    )


class _FailingRoleRepository:
    async def clone_templates_for_tenant(self, tenant_id: object) -> dict[str, object]:
        del tenant_id
        raise RoleTemplateUnavailable


class _FailingRoleCloneUnitOfWork:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._inner = SqlAlchemyTenantWorkflowUnitOfWork(factory)

    async def __aenter__(self) -> Self:
        await self._inner.__aenter__()
        self.tenants = self._inner.tenants
        self.memberships = self._inner.memberships
        self.roles = _FailingRoleRepository()
        self.authorization = self._inner.authorization
        self.sessions = self._inner.sessions
        self.security_locks = self._inner.security_locks
        self.cache_outbox = self._inner.cache_outbox
        self.idempotency = self._inner.idempotency
        self.audit = self._inner.audit
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._inner.__aexit__(exc_type, exc_value, traceback)


class _RecordingAuthorizationCache:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.invalidations: list[tuple[object, object, int]] = []

    async def invalidate(
        self,
        *,
        tenant_id: object,
        membership_id: object,
        authz_version: int,
    ) -> None:
        self.invalidations.append((tenant_id, membership_id, authz_version))
        if self.fail:
            raise RuntimeError("synthetic cache outage")


class _PauseAfterRefreshLockRepository:
    def __init__(self, inner: object, locked: asyncio.Event, release: asyncio.Event) -> None:
        self._inner = inner
        self._locked = locked
        self._release = release

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    async def lock_refresh(self, token_hash: bytes) -> object:
        result = await self._inner.lock_refresh(token_hash)  # type: ignore[attr-defined,no-any-return]
        self._locked.set()
        await self._release.wait()
        return result


class _PauseAfterFamilySecurityLockRepository:
    def __init__(self, inner: object, locked: asyncio.Event, release: asyncio.Event) -> None:
        self._inner = inner
        self._locked = locked
        self._release = release

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    async def acquire_session_family(self, request: object) -> object:
        result = await self._inner.acquire_session_family(request)  # type: ignore[attr-defined,no-any-return]
        self._locked.set()
        await self._release.wait()
        return result


class _PauseAfterRefreshLockUow:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        locked: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        self._inner = SqlAlchemySessionUnitOfWork(factory)
        self._locked = locked
        self._release = release

    async def __aenter__(self) -> Self:
        await self._inner.__aenter__()
        self.sessions = _PauseAfterRefreshLockRepository(
            self._inner.sessions, self._locked, self._release
        )
        self.security_locks = _PauseAfterFamilySecurityLockRepository(
            self._inner.security_locks, self._locked, self._release
        )
        self.audit = self._inner.audit
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._inner.__aexit__(exc_type, exc_value, traceback)


class _TwoRefreshBarrier:
    def __init__(self) -> None:
        self._arrivals = 0
        self._mutex = asyncio.Lock()
        self._release = asyncio.Event()

    async def arrive(self) -> None:
        async with self._mutex:
            self._arrivals += 1
            if self._arrivals == 2:
                self._release.set()
        await self._release.wait()


class _BarrierAfterTokenRepository:
    def __init__(self, inner: object, barrier: _TwoRefreshBarrier) -> None:
        self._inner = inner
        self._barrier = barrier

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    async def lock_refresh(self, token_hash: bytes) -> object:
        result = await self._inner.lock_refresh(token_hash)  # type: ignore[attr-defined,no-any-return]
        await self._barrier.arrive()
        return result


class _BarrierBeforeFamilySecurityLockRepository:
    def __init__(self, inner: object, barrier: _TwoRefreshBarrier) -> None:
        self._inner = inner
        self._barrier = barrier

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    async def acquire_session_family(self, request: object) -> object:
        await self._barrier.arrive()
        return await self._inner.acquire_session_family(request)  # type: ignore[attr-defined,no-any-return]


class _TwoRefreshBarrierUow:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        barrier: _TwoRefreshBarrier,
    ) -> None:
        self._inner = SqlAlchemySessionUnitOfWork(factory)
        self._barrier = barrier

    async def __aenter__(self) -> Self:
        await self._inner.__aenter__()
        self.sessions = _BarrierAfterTokenRepository(self._inner.sessions, self._barrier)
        self.security_locks = _BarrierBeforeFamilySecurityLockRepository(
            self._inner.security_locks, self._barrier
        )
        self.audit = self._inner.audit
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._inner.__aexit__(exc_type, exc_value, traceback)


class _PauseAfterSessionLocatorRepository:
    def __init__(self, inner: object, located: asyncio.Event, release: asyncio.Event) -> None:
        self._inner = inner
        self._located = located
        self._release = release

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    async def locate_session(self, session_id: object) -> object:
        result = await self._inner.locate_session(session_id)  # type: ignore[attr-defined,no-any-return]
        self._located.set()
        await self._release.wait()
        return result


class _PauseAfterSessionLocatorUow:
    def __init__(
        self,
        owner: _SwitchSnapshotUowFactory,
        factory: async_sessionmaker[AsyncSession],
        located: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        self._owner = owner
        self._inner = SqlAlchemySessionUnitOfWork(factory)
        self._located = located
        self._release = release

    async def __aenter__(self) -> Self:
        await self._inner.__aenter__()
        self.sessions = _PauseAfterSessionLocatorRepository(
            self._inner.sessions, self._located, self._release
        )
        self.security_locks = self._inner.security_locks
        self.audit = self._inner.audit
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._inner.__aexit__(exc_type, exc_value, traceback)
        self._owner.locator_uow_closed = True


class _SwitchSnapshotUowFactory:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        located: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        self._factory = factory
        self._located = located
        self._release = release
        self._calls = 0
        self.locator_uow_closed = False

    def __call__(self) -> object:
        self._calls += 1
        if self._calls == 1:
            return _PauseAfterSessionLocatorUow(
                self,
                self._factory,
                self._located,
                self._release,
            )
        return SqlAlchemySessionUnitOfWork(self._factory)


class _FailingCompletionOutbox:
    def __init__(self, stage: str) -> None:
        self._stage = stage

    async def mark_immediate_completed(self, **kwargs: object) -> None:
        del kwargs
        if self._stage == "read":
            raise RuntimeError("sensitive-completion-material")


class _FailingCompletionUow:
    def __init__(self, stage: str) -> None:
        self._stage = stage

    async def __aenter__(self) -> Self:
        if self._stage == "enter":
            raise RuntimeError("sensitive-completion-material")
        self.cache_outbox = _FailingCompletionOutbox(self._stage)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        if self._stage == "commit":
            raise RuntimeError("sensitive-completion-material")


class _FailingCompletionUowFactory:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        stage: str,
    ) -> None:
        self._factory = factory
        self._stage = stage
        self._calls = 0

    def __call__(self) -> object:
        self._calls += 1
        if self._calls == 1:
            return SqlAlchemyTenantWorkflowUnitOfWork(self._factory)
        return _FailingCompletionUow(self._stage)


class _PauseBeforeSecurityLockRepository:
    def __init__(self, inner: object, reached: asyncio.Event, release: asyncio.Event) -> None:
        self._inner = inner
        self._reached = reached
        self._release = release

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    async def acquire_tenant_write(self, request: object) -> object:
        self._reached.set()
        await self._release.wait()
        return await self._inner.acquire_tenant_write(request)  # type: ignore[attr-defined,no-any-return]


class _PauseBeforeLockedAuthorizationUow:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        reached: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        self._inner = SqlAlchemyTenantWorkflowUnitOfWork(factory)
        self._reached = reached
        self._release = release

    async def __aenter__(self) -> Self:
        await self._inner.__aenter__()
        self.tenants = self._inner.tenants
        self.memberships = self._inner.memberships
        self.roles = self._inner.roles
        self.authorization = self._inner.authorization
        self.sessions = self._inner.sessions
        self.security_locks = _PauseBeforeSecurityLockRepository(
            self._inner.security_locks, self._reached, self._release
        )
        self.cache_outbox = self._inner.cache_outbox
        self.idempotency = self._inner.idempotency
        self.audit = self._inner.audit
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._inner.__aexit__(exc_type, exc_value, traceback)


def test_task_7_idempotency_service_is_available() -> None:
    assert _service() is not None


@pytest.mark.asyncio
async def test_completed_request_replays_reference_without_duplicate_record(
    database: async_sessionmaker[AsyncSession],
) -> None:
    user_id = new_uuid7()
    result = IdempotencyResultReference("tenant", new_uuid7())
    scope = IdempotencyScope(IdempotencyScopeType.USER, user_id)

    async with database.begin() as session:
        repository = SqlAlchemyIdempotencyRepository(session)
        reservation = await _service().reserve(
            repository,
            scope=scope,
            operation="tenant.create",
            request=_request(),
            now=NOW,
        )
        assert not reservation.is_replay
        await _service().complete(repository, reservation, result, now=NOW)

    async with database.begin() as session:
        repository = SqlAlchemyIdempotencyRepository(session)
        replay = await _service().reserve(
            repository,
            scope=scope,
            operation="tenant.create",
            request=_request(),
            now=NOW,
        )
        assert replay.replay == result
        assert await session.scalar(select(func.count()).select_from(IdempotencyRecordModel)) == 1


@pytest.mark.asyncio
async def test_same_key_with_other_fingerprint_is_conflict(
    database: async_sessionmaker[AsyncSession],
) -> None:
    scope = IdempotencyScope(IdempotencyScopeType.USER, new_uuid7())
    async with database.begin() as session:
        repository = SqlAlchemyIdempotencyRepository(session)
        reservation = await _service().reserve(
            repository,
            scope=scope,
            operation="tenant.create",
            request=_request(),
            now=NOW,
        )
        await _service().complete(
            repository,
            reservation,
            IdempotencyResultReference("tenant", new_uuid7()),
            now=NOW,
        )

    async with database.begin() as session:
        with pytest.raises(IdempotencyConflictError):
            await _service().reserve(
                SqlAlchemyIdempotencyRepository(session),
                scope=scope,
                operation="tenant.create",
                request=_request(name="另一合成律所"),
                now=NOW,
            )


@pytest.mark.asyncio
async def test_expiry_never_allows_a_different_fingerprint_to_reuse_any_state(
    database: async_sessionmaker[AsyncSession],
) -> None:
    for terminal_status in (
        IdempotencyStatus.RESERVED,
        IdempotencyStatus.FAILED,
        IdempotencyStatus.COMPLETED,
    ):
        scope = IdempotencyScope(IdempotencyScopeType.USER, new_uuid7())
        async with database.begin() as session:
            repository = SqlAlchemyIdempotencyRepository(session)
            reservation = await _service().reserve(
                repository, scope=scope, operation="tenant.create", request=_request(), now=NOW
            )
            if terminal_status is IdempotencyStatus.FAILED:
                await _service().fail(repository, reservation, now=NOW)
            elif terminal_status is IdempotencyStatus.COMPLETED:
                await _service().complete(
                    repository,
                    reservation,
                    IdempotencyResultReference("tenant", new_uuid7()),
                    now=NOW,
                )

        for attempted_at in (NOW + timedelta(minutes=1), NOW + timedelta(days=2)):
            async with database.begin() as session:
                with pytest.raises(IdempotencyConflictError):
                    await _service().reserve(
                        SqlAlchemyIdempotencyRepository(session),
                        scope=scope,
                        operation="tenant.create",
                        request=_request(name="不同业务请求"),
                        now=attempted_at,
                    )


@pytest.mark.asyncio
async def test_same_fingerprint_replays_completed_after_expiry_and_recovers_expired_reserved(
    database: async_sessionmaker[AsyncSession],
) -> None:
    completed_scope = IdempotencyScope(IdempotencyScopeType.USER, new_uuid7())
    completed_result = IdempotencyResultReference("tenant", new_uuid7())
    reserved_scope = IdempotencyScope(IdempotencyScopeType.USER, new_uuid7())
    async with database.begin() as session:
        repository = SqlAlchemyIdempotencyRepository(session)
        completed = await _service().reserve(
            repository,
            scope=completed_scope,
            operation="tenant.create",
            request=_request(),
            now=NOW,
        )
        await _service().complete(repository, completed, completed_result, now=NOW)
        await _service().reserve(
            repository,
            scope=reserved_scope,
            operation="tenant.create",
            request=_request(),
            now=NOW,
        )

    async with database.begin() as session:
        repository = SqlAlchemyIdempotencyRepository(session)
        replay = await _service().reserve(
            repository,
            scope=completed_scope,
            operation="tenant.create",
            request=_request(),
            now=NOW + timedelta(days=2),
        )
        recovered = await _service().reserve(
            repository,
            scope=reserved_scope,
            operation="tenant.create",
            request=_request(),
            now=NOW + timedelta(days=2),
        )

        assert replay.replay == completed_result
        assert recovered.replay is None
        assert await session.scalar(select(func.count()).select_from(IdempotencyRecordModel)) == 2


@pytest.mark.asyncio
async def test_repository_restart_is_a_fingerprint_status_and_expiry_cas(
    database: async_sessionmaker[AsyncSession],
) -> None:
    scope = IdempotencyScope(IdempotencyScopeType.USER, new_uuid7())
    async with database.begin() as session:
        repository = SqlAlchemyIdempotencyRepository(session)
        reservation = await _service().reserve(
            repository, scope=scope, operation="tenant.create", request=_request(), now=NOW
        )

    async with database.begin() as session:
        repository = SqlAlchemyIdempotencyRepository(session)
        assert not await repository.restart(
            record_id=reservation.record_id,
            old_request_fingerprint=b"x" * 32,
            new_request_fingerprint=reservation.request_fingerprint,
            allowed_statuses=frozenset({IdempotencyStatus.RESERVED}),
            expired_before=NOW + timedelta(days=2),
            expires_at=NOW + timedelta(days=3),
        )
        assert not await repository.restart(
            record_id=reservation.record_id,
            old_request_fingerprint=reservation.request_fingerprint,
            new_request_fingerprint=reservation.request_fingerprint,
            allowed_statuses=frozenset({IdempotencyStatus.FAILED}),
            expired_before=None,
            expires_at=NOW + timedelta(days=3),
        )


@pytest.mark.asyncio
async def test_failed_same_fingerprint_can_be_recovered_without_new_record(
    database: async_sessionmaker[AsyncSession],
) -> None:
    scope = IdempotencyScope(IdempotencyScopeType.USER, new_uuid7())
    async with database.begin() as session:
        repository = SqlAlchemyIdempotencyRepository(session)
        first = await _service().reserve(
            repository,
            scope=scope,
            operation="tenant.create",
            request=_request(),
            now=NOW,
        )
        await _service().fail(repository, first, now=NOW)

    async with database.begin() as session:
        repository = SqlAlchemyIdempotencyRepository(session)
        recovered = await _service().reserve(
            repository,
            scope=scope,
            operation="tenant.create",
            request=_request(),
            now=NOW,
        )
        assert recovered.record_id == first.record_id
        assert not recovered.is_replay


@pytest.mark.asyncio
async def test_concurrent_same_key_allows_only_one_execution(
    database: async_sessionmaker[AsyncSession],
) -> None:
    scope = IdempotencyScope(IdempotencyScopeType.USER, new_uuid7())
    entered = asyncio.Event()
    release = asyncio.Event()
    executed = 0
    result = IdempotencyResultReference("tenant", new_uuid7())

    async def run(*, first: bool) -> IdempotencyResultReference:
        nonlocal executed
        async with database.begin() as session:
            repository = SqlAlchemyIdempotencyRepository(session)
            reservation = await _service().reserve(
                repository,
                scope=scope,
                operation="tenant.create",
                request=_request(),
                now=NOW,
            )
            if reservation.replay is not None:
                return reservation.replay
            executed += 1
            if first:
                entered.set()
                await release.wait()
            await _service().complete(repository, reservation, result, now=NOW)
            return result

    first_task = asyncio.create_task(run(first=True))
    await entered.wait()
    second_task = asyncio.create_task(run(first=False))
    await asyncio.sleep(0.05)
    release.set()
    assert await asyncio.gather(first_task, second_task) == [result, result]
    assert executed == 1


@pytest.mark.asyncio
async def test_membership_scope_composite_fk_rejects_cross_tenant_reference(
    database: async_sessionmaker[AsyncSession],
) -> None:
    user_a, user_b = new_uuid7(), new_uuid7()
    tenant_a, tenant_b = new_uuid7(), new_uuid7()
    membership_b = new_uuid7()
    async with database.begin() as session:
        session.add_all(
            [
                UserModel(id=user_a, status="active", display_name="合成用户甲"),
                UserModel(id=user_b, status="active", display_name="合成用户乙"),
            ]
        )
        await session.flush()
        session.add_all(
            [
                TenantModel(
                    id=tenant_a,
                    name="租户甲",
                    normalized_name="租户甲",
                    tenant_type="law_firm",
                    status="active",
                    created_by_user_id=user_a,
                    review_status="approved",
                ),
                TenantModel(
                    id=tenant_b,
                    name="租户乙",
                    normalized_name="租户乙",
                    tenant_type="enterprise",
                    status="active",
                    created_by_user_id=user_b,
                    review_status="approved",
                ),
            ]
        )
        await session.flush()
        session.add(
            TenantMembershipModel(
                id=membership_b,
                tenant_id=tenant_b,
                user_id=user_b,
                department_id=None,
                member_type="owner",
                status="active",
                valid_from=NOW.replace(tzinfo=None),
                valid_until=None,
                authz_version=1,
            )
        )

    async with database.begin() as session:
        with pytest.raises(IntegrityError):
            await _service().reserve(
                SqlAlchemyIdempotencyRepository(session),
                scope=IdempotencyScope(
                    IdempotencyScopeType.MEMBERSHIP,
                    membership_b,
                    tenant_id=tenant_a,
                ),
                operation="membership.update",
                request=IdempotencyRequest(
                    key=KEY,
                    method="PATCH",
                    canonical_route=f"/api/v1/tenants/{tenant_a}/members/{membership_b}",
                    body=IdempotencyFingerprintPayload(
                        values={"status": "suspended"},
                        business_paths=frozenset({("status",)}),
                    ),
                ),
                now=NOW,
            )


@pytest.mark.asyncio
async def test_tenant_bootstrap_is_atomic_complete_and_has_no_platform_admin(
    database: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await _add_active_user(database)

    created = await _tenant_service(database).create_application(_create_command(user_id))

    assert created.tenant.status.value == "pending_verification"
    assert created.owner_membership.status.value == "active"
    assert created.owner_membership.member_type.value == "owner"
    assert not created.replayed
    async with database() as session:
        role_codes = set(
            await session.scalars(
                select(TenantRoleModel.code).where(
                    TenantRoleModel.tenant_id == created.tenant.id
                )
            )
        )
        owner_role_id = await session.scalar(
            select(TenantRoleModel.id).where(
                TenantRoleModel.tenant_id == created.tenant.id,
                TenantRoleModel.code == "tenant_owner",
            )
        )
        assert role_codes == {template.code for template in TENANT_ROLE_TEMPLATES}
        assert owner_role_id is not None
        assert await session.scalar(
            select(func.count()).select_from(TenantRolePermissionModel).where(
                TenantRolePermissionModel.tenant_id == created.tenant.id
            )
        ) > 0
        assert await session.scalar(
            select(func.count()).select_from(MembershipRoleAssignmentModel).where(
                MembershipRoleAssignmentModel.tenant_id == created.tenant.id,
                MembershipRoleAssignmentModel.membership_id == created.owner_membership.id,
                MembershipRoleAssignmentModel.tenant_role_id == owner_role_id,
            )
        ) == 1
        audit = await session.scalar(
            select(AuditEventModel).where(AuditEventModel.tenant_id == created.tenant.id)
        )
        assert audit is not None
        assert audit.action == "tenant.application.create"
        assert audit.actor_membership_id == created.owner_membership.id
        assert audit.metadata_json is None
        record = await session.scalar(
            select(IdempotencyRecordModel).where(
                IdempotencyRecordModel.result_id == created.tenant.id
            )
        )
        assert record is not None and record.status == "completed"
        assert await session.scalar(
            select(func.count()).select_from(PlatformRoleAssignmentModel)
        ) == 0


@pytest.mark.asyncio
async def test_tenant_bootstrap_rolls_back_every_side_effect_when_role_clone_fails(
    database: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await _add_active_user(database)
    service = _tenant_service(
        database,
        uow_factory=lambda: _FailingRoleCloneUnitOfWork(database),
    )

    with pytest.raises(RoleTemplateUnavailable):
        await service.create_application(_create_command(user_id))

    async with database() as session:
        for model in (
            TenantModel,
            TenantRoleModel,
            TenantMembershipModel,
            MembershipRoleAssignmentModel,
            AuditEventModel,
            IdempotencyRecordModel,
        ):
            assert await session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.asyncio
async def test_tenant_bootstrap_refuses_semantically_drifted_role_template(
    database: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await _add_active_user(database)
    async with database.begin() as session:
        owner_template_id = await session.scalar(
            select(RoleTemplateModel.id).where(RoleTemplateModel.code == "tenant_owner")
        )
        tenant_update_id = await session.scalar(
            select(PermissionModel.id).where(PermissionModel.code == "tenant.update")
        )
        assert owner_template_id is not None and tenant_update_id is not None
        await session.execute(
            delete(RoleTemplatePermissionModel).where(
                RoleTemplatePermissionModel.role_template_id == owner_template_id,
                RoleTemplatePermissionModel.permission_id == tenant_update_id,
            )
        )

    try:
        with pytest.raises(RoleTemplateUnavailable):
            await _tenant_service(database).create_application(_create_command(user_id))

        async with database() as session:
            assert await session.scalar(select(func.count()).select_from(TenantModel)) == 0
            assert (
                await session.scalar(select(func.count()).select_from(IdempotencyRecordModel))
                == 0
            )
    finally:
        async with database.begin() as session:
            session.add(
                RoleTemplatePermissionModel(
                    role_template_id=owner_template_id,
                    permission_id=tenant_update_id,
                )
            )


@pytest.mark.asyncio
async def test_tenant_bootstrap_same_request_replays_authoritative_objects(
    database: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await _add_active_user(database)
    service = _tenant_service(database)

    first = await service.create_application(_create_command(user_id))
    replay = await service.create_application(_create_command(user_id))

    assert replay.replayed
    assert replay.tenant == first.tenant
    assert replay.owner_membership == first.owner_membership
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(TenantModel)) == 1
        assert await session.scalar(select(func.count()).select_from(AuditEventModel)) == 1


@pytest.mark.asyncio
async def test_tenant_bootstrap_same_key_other_payload_is_conflict(
    database: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await _add_active_user(database)
    service = _tenant_service(database)
    await service.create_application(_create_command(user_id))

    with pytest.raises(IdempotencyConflictError):
        await service.create_application(_create_command(user_id, name="另一合成律所"))


def _actor(created: object, user_id: object) -> TenantActor:
    tenant = created.tenant  # type: ignore[attr-defined]
    membership = created.owner_membership  # type: ignore[attr-defined]
    return TenantActor(
        principal=Principal(
            user_id=user_id,  # type: ignore[arg-type]
            session_id=new_uuid7(),
            audience=PrincipalAudience.TENANT,
            tenant_id=tenant.id,
            membership_id=membership.id,
            user_status="active",
            session_valid=True,
            auth_version=1,
            session_auth_version=1,
            permissions=frozenset(),
            role_codes=frozenset(),
            authenticated_at=NOW - timedelta(minutes=1),
        ),
        context=TenantContext(
            tenant_id=tenant.id,
            membership_id=membership.id,
            membership_user_id=user_id,  # type: ignore[arg-type]
            department_id=None,
            tenant_status=TenantStatus.PENDING_VERIFICATION,
            membership_status=MembershipStatus.ACTIVE,
            valid_from=NOW,
            valid_until=None,
            authz_version=1,
            session_authz_version=1,
            scope=AuthorizationScope(allow_tenant_wide=True),
        ),
    )


async def _actor_with_session(
    database: async_sessionmaker[AsyncSession],
    created: object,
    user_id: object,
    *,
    department_id: object | None = None,
) -> TenantActor:
    actor = _actor(created, user_id)
    session_id = actor.principal.session_id
    family_id = new_uuid7()
    async with database.begin() as session:
        session.add(
            AuthSessionModel(
                id=session_id,
                user_id=user_id,
                tenant_id=actor.context.tenant_id,
                membership_id=actor.context.membership_id,
                current_family_id=family_id,
                auth_version_at_issue=1,
                authz_version_at_issue=1,
                revoked_at=None,
                revocation_reason=None,
                last_seen_at=NOW.replace(tzinfo=None),
                expires_at=(NOW + timedelta(days=1)).replace(tzinfo=None),
            )
        )
    if department_id is None:
        return actor
    return TenantActor(
        principal=actor.principal,
        context=TenantContext(
            tenant_id=actor.context.tenant_id,
            membership_id=actor.context.membership_id,
            membership_user_id=actor.context.membership_user_id,
            department_id=department_id,  # type: ignore[arg-type]
            tenant_status=actor.context.tenant_status,
            membership_status=actor.context.membership_status,
            valid_from=actor.context.valid_from,
            valid_until=actor.context.valid_until,
            authz_version=actor.context.authz_version,
            session_authz_version=actor.context.session_authz_version,
            scope=actor.context.scope,
        ),
    )


async def _add_internal_member_with_session(
    database: async_sessionmaker[AsyncSession],
    *,
    tenant_id: object,
) -> tuple[object, object, object, object]:
    user_id = new_uuid7()
    membership_id = new_uuid7()
    session_id = new_uuid7()
    refresh_id = new_uuid7()
    family_id = new_uuid7()
    async with database.begin() as session:
        session.add(UserModel(id=user_id, status="active", display_name="合成成员"))
        await session.flush()
        session.add(
            TenantMembershipModel(
                id=membership_id,
                tenant_id=tenant_id,
                user_id=user_id,
                department_id=None,
                member_type="internal",
                status="active",
                valid_from=NOW.replace(tzinfo=None),
                valid_until=None,
                authz_version=1,
                version=1,
            )
        )
        await session.flush()
        session.add(
            AuthSessionModel(
                id=session_id,
                user_id=user_id,
                tenant_id=tenant_id,
                membership_id=membership_id,
                current_family_id=family_id,
                auth_version_at_issue=1,
                authz_version_at_issue=1,
                revoked_at=None,
                revocation_reason=None,
                last_seen_at=NOW.replace(tzinfo=None),
                expires_at=(NOW + timedelta(days=1)).replace(tzinfo=None),
            )
        )
        await session.flush()
        session.add(
            RefreshTokenRecordModel(
                id=refresh_id,
                token_hash=bytes(refresh_id.bytes + refresh_id.bytes),
                family_id=family_id,
                session_id=session_id,
                user_id=user_id,
                tenant_id=tenant_id,
                membership_id=membership_id,
                issued_at=NOW.replace(tzinfo=None),
                expires_at=(NOW + timedelta(days=30)).replace(tzinfo=None),
                idle_expires_at=(NOW + timedelta(days=7)).replace(tzinfo=None),
                used_at=None,
                revoked_at=None,
                replaced_by_id=None,
                revocation_reason=None,
                device_label=None,
                user_agent_hash=None,
                ip_hash=None,
            )
        )
    return user_id, membership_id, session_id, refresh_id


async def _tenant_roles(
    database: async_sessionmaker[AsyncSession], tenant_id: object
) -> dict[str, object]:
    async with database() as session:
        rows = (
            await session.execute(
                select(TenantRoleModel.code, TenantRoleModel.id).where(
                    TenantRoleModel.tenant_id == tenant_id
                )
            )
        ).all()
    return dict(rows)


async def _add_member(
    database: async_sessionmaker[AsyncSession],
    *,
    tenant_id: object,
    member_type: str,
    status: str = "active",
    department_id: object | None = None,
    role_codes: tuple[str, ...] = (),
) -> tuple[object, object]:
    user_id, membership_id = new_uuid7(), new_uuid7()
    roles = await _tenant_roles(database, tenant_id)
    async with database.begin() as session:
        session.add(UserModel(id=user_id, status="active", display_name="合成权限成员"))
        await session.flush()
        session.add(
            TenantMembershipModel(
                id=membership_id,
                tenant_id=tenant_id,
                user_id=user_id,
                department_id=department_id,
                member_type=member_type,
                status=status,
                valid_from=NOW.replace(tzinfo=None),
                valid_until=None,
                authz_version=1,
                version=1,
            )
        )
        await session.flush()
        for code in role_codes:
            session.add(
                MembershipRoleAssignmentModel(
                    tenant_id=tenant_id,
                    membership_id=membership_id,
                    tenant_role_id=roles[code],
                    assigned_by_membership_id=membership_id,
                    created_at=NOW.replace(tzinfo=None),
                )
            )
    return user_id, membership_id


async def _actor_for_membership(
    database: async_sessionmaker[AsyncSession],
    created: object,
    *,
    user_id: object,
    membership_id: object,
    department_id: object | None,
    scope: AuthorizationScope,
) -> TenantActor:
    session_id = new_uuid7()
    async with database.begin() as session:
        session.add(
            AuthSessionModel(
                id=session_id,
                user_id=user_id,
                tenant_id=created.tenant.id,  # type: ignore[attr-defined]
                membership_id=membership_id,
                current_family_id=new_uuid7(),
                auth_version_at_issue=1,
                authz_version_at_issue=1,
                revoked_at=None,
                revocation_reason=None,
                last_seen_at=NOW.replace(tzinfo=None),
                expires_at=(NOW + timedelta(days=1)).replace(tzinfo=None),
            )
        )
    return TenantActor(
        principal=Principal(
            user_id=user_id,  # type: ignore[arg-type]
            session_id=session_id,
            audience=PrincipalAudience.TENANT,
            tenant_id=created.tenant.id,  # type: ignore[attr-defined]
            membership_id=membership_id,  # type: ignore[arg-type]
            user_status="active",
            session_valid=True,
            auth_version=1,
            session_auth_version=1,
            permissions=frozenset(),
            role_codes=frozenset(),
            authenticated_at=NOW - timedelta(minutes=1),
        ),
        context=TenantContext(
            tenant_id=created.tenant.id,  # type: ignore[attr-defined]
            membership_id=membership_id,  # type: ignore[arg-type]
            membership_user_id=user_id,  # type: ignore[arg-type]
            department_id=department_id,  # type: ignore[arg-type]
            tenant_status=TenantStatus.PENDING_VERIFICATION,
            membership_status=MembershipStatus.ACTIVE,
            valid_from=NOW,
            valid_until=None,
            authz_version=1,
            session_authz_version=1,
            scope=scope,
        ),
    )


@pytest.mark.asyncio
async def test_get_and_update_tenant_use_authoritative_policy_etag_and_idempotency(
    database: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await _add_active_user(database)
    service = _tenant_service(database)
    created = await service.create_application(_create_command(user_id))
    actor = await _actor_with_session(database, created, user_id)

    assert await service.get(actor) == created.tenant
    updated = await service.update(
        actor,
        UpdateTenantCommand(
            name="  ＮＥＷ　律所  ",
            expected_version=1,
            idempotency_key="tenant-update-key-001",
            audit_context=_audit_context(),
        ),
    )
    assert updated.tenant.name == "NEW 律所"
    assert updated.tenant.normalized_name == "new 律所"
    assert updated.tenant.version == 2
    assert not updated.replayed

    replay = await service.update(
        actor,
        UpdateTenantCommand(
            name="  ＮＥＷ　律所  ",
            expected_version=1,
            idempotency_key="tenant-update-key-001",
            audit_context=_audit_context(),
        ),
    )
    assert replay.replayed and replay.tenant.version == 2
    with pytest.raises(VersionConflict):
        await service.update(
            actor,
            UpdateTenantCommand(
                name="第三名称",
                expected_version=1,
                idempotency_key="tenant-update-key-002",
                audit_context=_audit_context(),
            ),
        )


@pytest.mark.asyncio
async def test_tenant_name_normalized_no_op_keeps_version_and_writes_no_update_audit(
    database: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await _add_active_user(database)
    service = _tenant_service(database)
    created = await service.create_application(_create_command(user_id))
    actor = await _actor_with_session(database, created, user_id)

    result = await service.update(
        actor,
        UpdateTenantCommand(
            name="　合成律所　",
            expected_version=1,
            idempotency_key="tenant-no-change-key-01",
            audit_context=_audit_context(),
        ),
    )

    assert result.changed is False
    assert result.tenant.version == 1
    async with database() as session:
        assert await session.scalar(
            select(func.count()).select_from(AuditEventModel).where(
                AuditEventModel.tenant_id == created.tenant.id,
                AuditEventModel.action == "tenant.update",
            )
        ) == 0


@pytest.mark.asyncio
async def test_concurrent_tenant_updates_lock_actor_before_idempotency_and_return_version_conflict(
    database: async_sessionmaker[AsyncSession],
) -> None:
    owner = await _add_active_user(database)
    service = _tenant_service(database)
    created = await service.create_application(_create_command(owner))
    actor_a = await _actor_with_session(database, created, owner)
    actor_b = await _actor_with_session(database, created, owner)

    outcomes = await asyncio.gather(
        service.update(
            actor_a,
            UpdateTenantCommand(
                name="并发名称甲",
                expected_version=1,
                idempotency_key="tenant-concurrent-update-01",
                audit_context=_audit_context(),
            ),
        ),
        service.update(
            actor_b,
            UpdateTenantCommand(
                name="并发名称乙",
                expected_version=1,
                idempotency_key="tenant-concurrent-update-02",
                audit_context=_audit_context(),
            ),
        ),
        return_exceptions=True,
    )

    assert not any(isinstance(value, OperationalError) for value in outcomes), outcomes
    assert sum(not isinstance(value, BaseException) for value in outcomes) == 1
    assert sum(isinstance(value, VersionConflict) for value in outcomes) == 1, outcomes


@pytest.mark.asyncio
async def test_member_role_change_increments_versions_revokes_sessions_and_invalidates_old_cache(
    database: async_sessionmaker[AsyncSession],
) -> None:
    owner_user_id = await _add_active_user(database)
    cache = _RecordingAuthorizationCache()
    service = _tenant_service(database, cache=cache)
    created = await service.create_application(_create_command(owner_user_id))
    target_user_id, target_id, session_id, refresh_id = await _add_internal_member_with_session(
        database,
        tenant_id=created.tenant.id,
    )
    async with database() as session:
        role_id = await session.scalar(
            select(TenantRoleModel.id).where(
                TenantRoleModel.tenant_id == created.tenant.id,
                TenantRoleModel.code == "tenant_admin",
            )
        )
        assert role_id is not None

    changed = await service.update_member(
        await _actor_with_session(database, created, owner_user_id),
        UpdateMemberCommand(
            membership_id=target_id,  # type: ignore[arg-type]
            status=MembershipStatus.SUSPENDED,
            role_ids=(role_id,),
            expected_version=1,
            idempotency_key="member-update-key-001",
            audit_context=_audit_context(),
        ),
    )

    assert changed.membership.user_id == target_user_id
    assert changed.membership.status is MembershipStatus.SUSPENDED
    assert changed.membership.version == 2
    assert changed.membership.authz_version == 2
    assert changed.role_ids == (role_id,)
    assert cache.invalidations == [(created.tenant.id, target_id, 1)]
    async with database() as session:
        auth_session = await session.get(AuthSessionModel, session_id)
        refresh = await session.get(RefreshTokenRecordModel, refresh_id)
        assert auth_session is not None and auth_session.revoked_at is not None
        assert auth_session.revocation_reason == "authorization_changed"
        assert refresh is not None and refresh.revoked_at is not None
        assert refresh.revocation_reason == "authorization_changed"
        assert await session.scalar(
            select(func.count()).select_from(MembershipRoleAssignmentModel).where(
                MembershipRoleAssignmentModel.tenant_id == created.tenant.id,
                MembershipRoleAssignmentModel.membership_id == target_id,
                MembershipRoleAssignmentModel.tenant_role_id == role_id,
            )
        ) == 1

    replay = await service.update_member(
        await _actor_with_session(database, created, owner_user_id),
        UpdateMemberCommand(
            membership_id=target_id,  # type: ignore[arg-type]
            status=MembershipStatus.SUSPENDED,
            role_ids=(role_id,),
            expected_version=1,
            idempotency_key="member-update-key-001",
            audit_context=_audit_context(),
        ),
    )
    assert replay.replayed and replay.membership.version == 2
    assert cache.invalidations == [(created.tenant.id, target_id, 1)]


@pytest.mark.asyncio
async def test_revoke_member_uses_strong_version_and_is_cross_tenant_isolated(
    database: async_sessionmaker[AsyncSession],
) -> None:
    owner_a = await _add_active_user(database)
    created_a = await _tenant_service(database).create_application(_create_command(owner_a))
    owner_b = new_uuid7()
    await _add_active_user(database, user_id=owner_b)
    created_b = await _tenant_service(database).create_application(
        CreateTenantApplicationCommand(
            actor_user_id=owner_b,
            name="租户乙",
            tenant_type=TenantType.ENTERPRISE,
            idempotency_key="tenant-create-key-0002",
            audit_context=_audit_context(),
        )
    )
    _, target_b, _, _ = await _add_internal_member_with_session(
        database,
        tenant_id=created_b.tenant.id,
    )

    with pytest.raises(TenantResourceNotFound):
        await _tenant_service(database).revoke_member(
            await _actor_with_session(database, created_a, owner_a),
            RevokeMemberCommand(
                membership_id=target_b,  # type: ignore[arg-type]
                expected_version=1,
                idempotency_key="member-revoke-key-001",
                audit_context=_audit_context(),
            ),
        )
    async with database() as session:
        untouched = await session.get(TenantMembershipModel, target_b)
        assert untouched is not None and untouched.status == "active" and untouched.version == 1

    target_a_user, target_a, _, _ = await _add_internal_member_with_session(
        database,
        tenant_id=created_a.tenant.id,
    )
    revoked = await _tenant_service(database).revoke_member(
        await _actor_with_session(database, created_a, owner_a),
        RevokeMemberCommand(
            membership_id=target_a,  # type: ignore[arg-type]
            expected_version=1,
            idempotency_key="member-revoke-key-002",
            audit_context=_audit_context(),
        ),
    )
    assert revoked.membership.user_id == target_a_user
    assert revoked.membership.status is MembershipStatus.REVOKED
    assert revoked.membership.authz_version == 2


@pytest.mark.asyncio
async def test_cache_invalidation_failure_is_explicit_after_committed_fact(
    database: async_sessionmaker[AsyncSession],
) -> None:
    owner = await _add_active_user(database)
    created = await _tenant_service(database).create_application(_create_command(owner))
    _, target, _, _ = await _add_internal_member_with_session(
        database,
        tenant_id=created.tenant.id,
    )
    cache = _RecordingAuthorizationCache(fail=True)

    service = _tenant_service(database, cache=cache)
    command = RevokeMemberCommand(
        membership_id=target,  # type: ignore[arg-type]
        expected_version=1,
        idempotency_key="member-revoke-key-003",
        audit_context=_audit_context(),
    )
    actor = await _actor_with_session(database, created, owner)
    with pytest.raises(PostCommitCacheInvalidationError) as caught:
        await service.revoke_member(actor, command)
    assert caught.value.committed is True
    cache.fail = False
    dispatched = await AuthorizationCacheInvalidationDispatcher(
        uow_factory=lambda: SqlAlchemyTenantWorkflowUnitOfWork(database),
        authorization_cache=cache,
        clock=lambda: NOW + timedelta(minutes=1),
    ).run_once(limit=10)
    assert dispatched.completed == 1
    assert cache.invalidations == [
        (created.tenant.id, target, 1),
        (created.tenant.id, target, 1),
    ]
    async with database() as session:
        committed = await session.get(TenantMembershipModel, target)
        assert committed is not None and committed.status == "revoked" and committed.version == 2
        assert await session.scalar(
            select(func.count()).select_from(AuditEventModel).where(
                AuditEventModel.tenant_id == created.tenant.id,
                AuditEventModel.action == "membership.revoke",
            )
        ) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["enter", "read", "commit"])
async def test_cache_completion_failure_is_stable_and_worker_recoverable(
    database: async_sessionmaker[AsyncSession],
    failure_stage: str,
) -> None:
    owner = await _add_active_user(database)
    created = await _tenant_service(database).create_application(_create_command(owner))
    _, target, _, _ = await _add_internal_member_with_session(
        database,
        tenant_id=created.tenant.id,
    )
    cache = _RecordingAuthorizationCache()
    service = _tenant_service(
        database,
        uow_factory=_FailingCompletionUowFactory(database, failure_stage),  # type: ignore[arg-type]
        cache=cache,
    )

    with pytest.raises(PostCommitCacheInvalidationError) as caught:
        await service.revoke_member(
            await _actor_with_session(database, created, owner),
            RevokeMemberCommand(
                membership_id=target,  # type: ignore[arg-type]
                expected_version=1,
                idempotency_key=f"cache-completion-{failure_stage}-01",
                audit_context=_audit_context(),
            ),
        )
    assert caught.value.committed is True
    assert str(caught.value) == "authorization cache invalidation failed after commit"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    chain_text = " ".join(
        str(value)
        for value in (caught.value, caught.value.__cause__, caught.value.__context__)
        if value is not None
    )
    assert "sensitive-completion-material" not in chain_text
    assert "runtimeerror" not in chain_text.casefold()
    assert "sql" not in chain_text.casefold()

    dispatched = await AuthorizationCacheInvalidationDispatcher(
        uow_factory=lambda: SqlAlchemyTenantWorkflowUnitOfWork(database),
        authorization_cache=cache,
        clock=lambda: NOW + timedelta(minutes=1),
    ).run_once(limit=10)
    assert dispatched.completed == 1 and dispatched.failed == 0
    assert cache.invalidations == [
        (created.tenant.id, target, 1),
        (created.tenant.id, target, 1),
    ]
    async with database() as session:
        membership = await session.get(TenantMembershipModel, target)
        outbox = await session.scalar(select(AuthorizationCacheInvalidationOutboxModel))
        audit_count = await session.scalar(
            select(func.count()).select_from(AuditEventModel).where(
                AuditEventModel.tenant_id == created.tenant.id,
                AuditEventModel.action == "membership.revoke",
            )
        )
        session_count = await session.scalar(
            select(func.count()).select_from(AuthSessionModel).where(
                AuthSessionModel.membership_id == target,
                AuthSessionModel.version > 2,
            )
        )
    assert membership is not None and membership.status == "revoked" and membership.version == 2
    assert outbox is not None and outbox.status == "completed"
    assert audit_count == 1
    assert session_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["suspend", "revoke", "remove_owner"])
async def test_durable_cache_outbox_recovers_self_mutation_without_actor_retry(
    database: async_sessionmaker[AsyncSession],
    mutation: str,
) -> None:
    owner = await _add_active_user(database)
    cache = _RecordingAuthorizationCache(fail=True)
    service = _tenant_service(database, cache=cache)
    created = await service.create_application(_create_command(owner))
    await _add_member(
        database,
        tenant_id=created.tenant.id,
        member_type="owner",
        role_codes=("tenant_owner",),
    )
    actor = await _actor_with_session(database, created, owner)
    if mutation == "revoke":
        operation = service.revoke_member(
            actor,
            RevokeMemberCommand(
                membership_id=created.owner_membership.id,
                expected_version=1,
                idempotency_key="self-revoke-outbox-0001",
                audit_context=_audit_context(),
            ),
        )
        expected_status = "revoked"
        expected_action = "membership.revoke"
    else:
        operation = service.update_member(
            actor,
            UpdateMemberCommand(
                membership_id=created.owner_membership.id,
                expected_version=1,
                idempotency_key=f"self-{mutation}-outbox-0001",
                audit_context=_audit_context(),
                status=(MembershipStatus.SUSPENDED if mutation == "suspend" else None),
                role_ids=(() if mutation == "remove_owner" else None),
            ),
        )
        expected_status = "suspended" if mutation == "suspend" else "active"
        expected_action = "membership.update"

    with pytest.raises(PostCommitCacheInvalidationError) as committed:
        await operation
    assert committed.value.committed is True
    async with database() as session:
        pending = await session.scalar(select(AuthorizationCacheInvalidationOutboxModel))
        membership = await session.get(TenantMembershipModel, created.owner_membership.id)
        auth_session = await session.get(AuthSessionModel, actor.principal.session_id)
        assert pending is not None and pending.status == "pending"
        assert pending.tenant_id == created.tenant.id
        assert pending.membership_id == created.owner_membership.id
        assert pending.authz_version == 1
        assert membership is not None and membership.status == expected_status
        assert membership.version == 2 and membership.authz_version == 2
        assert auth_session is not None and auth_session.revoked_at is not None
        committed_session_version = auth_session.version

    cache.fail = False
    dispatcher = AuthorizationCacheInvalidationDispatcher(
        uow_factory=lambda: SqlAlchemyTenantWorkflowUnitOfWork(database),
        authorization_cache=cache,
        clock=lambda: NOW + timedelta(minutes=1),
    )
    dispatched = await dispatcher.run_once(limit=10)
    assert dispatched.completed == 1 and dispatched.failed == 0
    assert (await dispatcher.run_once(limit=10)).claimed == 0
    assert cache.invalidations == [
        (created.tenant.id, created.owner_membership.id, 1),
        (created.tenant.id, created.owner_membership.id, 1),
    ]
    async with database() as session:
        completed = await session.scalar(select(AuthorizationCacheInvalidationOutboxModel))
        auth_session = await session.get(AuthSessionModel, actor.principal.session_id)
        assert completed is not None and completed.status == "completed"
        assert completed.attempt_count == 1 and completed.completed_at is not None
        assert auth_session is not None and auth_session.version == committed_session_version
        assert await session.scalar(
            select(func.count()).select_from(AuditEventModel).where(
                AuditEventModel.tenant_id == created.tenant.id,
                AuditEventModel.action == expected_action,
            )
        ) == 1


@pytest.mark.asyncio
async def test_cache_outbox_recovers_expired_processing_claim_and_uses_skip_locked(
    database: async_sessionmaker[AsyncSession],
) -> None:
    owner = await _add_active_user(database)
    cache = _RecordingAuthorizationCache(fail=True)
    service = _tenant_service(database, cache=cache)
    created = await service.create_application(_create_command(owner))
    targets = [
        (await _add_internal_member_with_session(database, tenant_id=created.tenant.id))[1]
        for _ in range(2)
    ]
    for index, target in enumerate(targets):
        with pytest.raises(PostCommitCacheInvalidationError):
            await service.update_member(
                await _actor_with_session(database, created, owner),
                UpdateMemberCommand(
                    membership_id=target,  # type: ignore[arg-type]
                    expected_version=1,
                    idempotency_key=f"outbox-crash-recovery-{index:02d}",
                    audit_context=_audit_context(),
                    status=MembershipStatus.SUSPENDED,
                ),
            )
    async with database.begin() as session:
        first = await session.scalar(
            select(AuthorizationCacheInvalidationOutboxModel)
            .order_by(AuthorizationCacheInvalidationOutboxModel.id)
            .limit(1)
        )
        assert first is not None
        first.status = "processing"
        first.processing_started_at = (NOW - timedelta(minutes=10)).replace(tzinfo=None)
        first.attempt_count = 1

    cache.fail = False
    dispatchers = [
        AuthorizationCacheInvalidationDispatcher(
            uow_factory=lambda: SqlAlchemyTenantWorkflowUnitOfWork(database),
            authorization_cache=cache,
            clock=lambda: NOW + timedelta(minutes=1),
        )
        for _ in range(2)
    ]
    outcomes = await asyncio.gather(
        *(dispatcher.run_once(limit=1) for dispatcher in dispatchers),
        return_exceptions=True,
    )
    assert not any(isinstance(value, BaseException) for value in outcomes), outcomes
    concurrent_completed = sum(
        value.completed for value in outcomes  # type: ignore[union-attr]
    )
    follow_up = await dispatchers[0].run_once(limit=10)
    assert concurrent_completed + follow_up.completed == 2
    async with database() as session:
        rows = (
            await session.scalars(select(AuthorizationCacheInvalidationOutboxModel))
        ).all()
        assert len(rows) == 2 and all(row.status == "completed" for row in rows)
        assert sorted(row.attempt_count for row in rows) == [1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("late_a_action", "current_b_action", "expected_status"),
    [
        ("fail", "complete", "completed"),
        ("complete", "fail", "pending"),
        ("fail", "fail", "pending"),
    ],
)
async def test_cache_outbox_claim_fencing_rejects_late_worker_results(
    database: async_sessionmaker[AsyncSession],
    late_a_action: str,
    current_b_action: str,
    expected_status: str,
) -> None:
    owner = await _add_active_user(database)
    cache = _RecordingAuthorizationCache(fail=True)
    service = _tenant_service(database, cache=cache)
    created = await service.create_application(_create_command(owner))
    _, target, _, _ = await _add_internal_member_with_session(
        database,
        tenant_id=created.tenant.id,
    )
    with pytest.raises(PostCommitCacheInvalidationError):
        await service.update_member(
            await _actor_with_session(database, created, owner),
            UpdateMemberCommand(
                membership_id=target,  # type: ignore[arg-type]
                expected_version=1,
                idempotency_key=f"fencing-{late_a_action}-{current_b_action}-01",
                audit_context=_audit_context(),
                status=MembershipStatus.SUSPENDED,
            ),
        )

    async with SqlAlchemyTenantWorkflowUnitOfWork(database) as uow_a:
        claim_a = (
            await uow_a.cache_outbox.claim_batch(
                now=NOW + timedelta(minutes=1),
                processing_expired_before=NOW,
                limit=1,
            )
        )[0]
    async with SqlAlchemyTenantWorkflowUnitOfWork(database) as uow_b:
        claim_b = (
            await uow_b.cache_outbox.claim_batch(
                now=NOW + timedelta(minutes=10),
                processing_expired_before=NOW + timedelta(minutes=5),
                limit=1,
            )
        )[0]
    assert claim_a.claim_token != claim_b.claim_token

    async with SqlAlchemyTenantWorkflowUnitOfWork(database) as stale_uow:
        if late_a_action == "complete":
            stale_applied = await stale_uow.cache_outbox.mark_completed(
                task_id=claim_a.id,
                claim_token=claim_a.claim_token,
                now=NOW + timedelta(minutes=11),
            )
        else:
            stale_applied = await stale_uow.cache_outbox.release_failed(
                task_id=claim_a.id,
                claim_token=claim_a.claim_token,
                available_at=NOW + timedelta(minutes=12),
                error_code="cache_unavailable",
                now=NOW + timedelta(minutes=11),
            )
    assert stale_applied is False

    async with SqlAlchemyTenantWorkflowUnitOfWork(database) as current_uow:
        if current_b_action == "complete":
            current_applied = await current_uow.cache_outbox.mark_completed(
                task_id=claim_b.id,
                claim_token=claim_b.claim_token,
                now=NOW + timedelta(minutes=11),
            )
        else:
            current_applied = await current_uow.cache_outbox.release_failed(
                task_id=claim_b.id,
                claim_token=claim_b.claim_token,
                available_at=NOW + timedelta(minutes=12),
                error_code="cache_unavailable",
                now=NOW + timedelta(minutes=11),
            )
    assert current_applied is True
    async with database() as session:
        row = await session.scalar(select(AuthorizationCacheInvalidationOutboxModel))
        assert row is not None and row.status == expected_status

    if expected_status == "pending":
        async with SqlAlchemyTenantWorkflowUnitOfWork(database) as recovery_uow:
            recovered = (
                await recovery_uow.cache_outbox.claim_batch(
                    now=NOW + timedelta(minutes=13),
                    processing_expired_before=NOW + timedelta(minutes=12),
                    limit=1,
                )
            )[0]
            assert await recovery_uow.cache_outbox.mark_completed(
                task_id=recovered.id,
                claim_token=recovered.claim_token,
                now=NOW + timedelta(minutes=13),
            )
        async with database() as session:
            row = await session.scalar(select(AuthorizationCacheInvalidationOutboxModel))
            assert row is not None and row.status == "completed"


@pytest.mark.asyncio
async def test_cache_outbox_rejects_cross_tenant_membership_and_idempotency_references(
    database: async_sessionmaker[AsyncSession],
) -> None:
    owner_a = await _add_active_user(database)
    tenant_a = await _tenant_service(database).create_application(_create_command(owner_a))
    _, member_a = await _add_member(
        database, tenant_id=tenant_a.tenant.id, member_type="internal"
    )
    owner_b = await _add_active_user(database)
    tenant_b = await _tenant_service(database).create_application(
        CreateTenantApplicationCommand(
            actor_user_id=owner_b,  # type: ignore[arg-type]
            name="Outbox 租户乙",
            tenant_type=TenantType.ENTERPRISE,
            idempotency_key="outbox-cross-tenant-create-b",
            audit_context=_audit_context(),
        )
    )
    _, member_b = await _add_member(
        database, tenant_id=tenant_b.tenant.id, member_type="internal"
    )
    await _tenant_service(database).update_member(
        await _actor_with_session(database, tenant_b, owner_b),
        UpdateMemberCommand(
            membership_id=member_b,  # type: ignore[arg-type]
            expected_version=1,
            idempotency_key="outbox-cross-tenant-mutation-b",
            audit_context=_audit_context(),
            status=MembershipStatus.SUSPENDED,
        ),
    )
    async with database() as session:
        tenant_b_outbox = await session.scalar(
            select(AuthorizationCacheInvalidationOutboxModel).where(
                AuthorizationCacheInvalidationOutboxModel.tenant_id == tenant_b.tenant.id
            )
        )
        assert tenant_b_outbox is not None
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(
                    AuthorizationCacheInvalidationOutboxModel(
                        id=new_uuid7(),
                        tenant_id=tenant_a.tenant.id,
                        membership_id=member_a,
                        authz_version=1,
                        idempotency_record_id=tenant_b_outbox.idempotency_record_id,
                        status="pending",
                        attempt_count=0,
                        available_at=NOW.replace(tzinfo=None),
                    )
                )
                await session.flush()
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(
                    AuthorizationCacheInvalidationOutboxModel(
                        id=new_uuid7(),
                        tenant_id=tenant_a.tenant.id,
                        membership_id=member_b,
                        authz_version=1,
                        idempotency_record_id=tenant_b_outbox.idempotency_record_id,
                        status="pending",
                        attempt_count=0,
                        available_at=NOW.replace(tzinfo=None),
                    )
                )
                await session.flush()


@pytest.mark.asyncio
async def test_member_cursor_pagination_is_stable_bounded_and_tenant_bound(
    database: async_sessionmaker[AsyncSession],
) -> None:
    owner = await _add_active_user(database)
    service = _tenant_service(database)
    created = await service.create_application(_create_command(owner))
    for _ in range(3):
        await _add_internal_member_with_session(database, tenant_id=created.tenant.id)

    seen: list[object] = []
    cursor = None
    for _ in range(4):
        page = await service.list_members(
            await _actor_with_session(database, created, owner),
            MemberPageQuery(limit=1, cursor=cursor),
        )
        assert len(page.items) == 1
        seen.append(page.items[0].membership.id)
        cursor = page.next_cursor
    assert len(set(seen)) == 4
    assert cursor is None

    other_owner = new_uuid7()
    await _add_active_user(database, user_id=other_owner)
    other = await _tenant_service(database).create_application(
        CreateTenantApplicationCommand(
            actor_user_id=other_owner,
            name="分页租户乙",
            tenant_type=TenantType.UNIVERSITY,
            idempotency_key="tenant-create-key-0003",
            audit_context=_audit_context(),
        )
    )
    first_page = await service.list_members(
        await _actor_with_session(database, created, owner),
        MemberPageQuery(limit=1, cursor=None),
    )
    assert first_page.next_cursor is not None
    with pytest.raises(Exception, match="cursor"):
        await service.list_members(
            await _actor_with_session(database, other, other_owner),
            MemberPageQuery(limit=1, cursor=first_page.next_cursor),
        )


@pytest.mark.asyncio
async def test_member_list_enforces_authoritative_department_scope_and_binds_cursor(
    database: async_sessionmaker[AsyncSession],
) -> None:
    owner = await _add_active_user(database)
    service = _tenant_service(database)
    created = await service.create_application(_create_command(owner))
    department_a, department_b = new_uuid7(), new_uuid7()
    async with database.begin() as session:
        session.add_all(
            [
                DepartmentModel(
                    id=department_a,
                    tenant_id=created.tenant.id,
                    name="部门甲",
                    status="active",
                ),
                DepartmentModel(
                    id=department_b,
                    tenant_id=created.tenant.id,
                    name="部门乙",
                    status="active",
                ),
            ]
        )
    admin_user, admin_membership = await _add_member(
        database,
        tenant_id=created.tenant.id,
        member_type="internal",
        department_id=department_a,
        role_codes=("department_admin",),
    )
    _, member_a = await _add_member(
        database,
        tenant_id=created.tenant.id,
        member_type="internal",
        department_id=department_a,
    )
    _, member_b = await _add_member(
        database,
        tenant_id=created.tenant.id,
        member_type="internal",
        department_id=department_b,
    )
    department_actor = await _actor_for_membership(
        database,
        created,
        user_id=admin_user,
        membership_id=admin_membership,
        department_id=department_a,
        scope=AuthorizationScope(department_ids=frozenset({department_a})),
    )

    first_page = await service.list_members(
        department_actor,
        MemberPageQuery(limit=1),
    )
    assert len(first_page.items) == 1
    assert first_page.items[0].membership.department_id == department_a
    assert first_page.next_cursor is not None
    remaining = await service.list_members(
        department_actor,
        MemberPageQuery(limit=10, cursor=first_page.next_cursor),
    )
    assert {
        first_page.items[0].membership.id,
        *(item.membership.id for item in remaining.items),
    } == {admin_membership, member_a}
    assert member_b not in {item.membership.id for item in remaining.items}

    changed_scope_actor = TenantActor(
        principal=department_actor.principal,
        context=replace(
            department_actor.context,
            scope=AuthorizationScope(department_ids=frozenset({department_b})),
        ),
    )
    with pytest.raises(TenantAuthorizationDenied) as denied:
        await service.list_members(
            changed_scope_actor,
            MemberPageQuery(limit=10, cursor=first_page.next_cursor),
        )
    assert denied.value.reason_code == "resource_scope_denied"

    owner_actor = await _actor_with_session(database, created, owner)
    owner_first_page = await service.list_members(
        owner_actor,
        MemberPageQuery(limit=1),
    )
    assert owner_first_page.next_cursor is not None
    department_limited_owner = TenantActor(
        principal=owner_actor.principal,
        context=replace(
            owner_actor.context,
            scope=AuthorizationScope(department_ids=frozenset({department_a})),
        ),
    )
    with pytest.raises(Exception, match="scope"):
        await service.list_members(
            department_limited_owner,
            MemberPageQuery(limit=10, cursor=owner_first_page.next_cursor),
        )
    owner_page = await service.list_members(owner_actor, MemberPageQuery(limit=10))
    assert {item.membership.id for item in owner_page.items} >= {
        created.owner_membership.id,
        admin_membership,
        member_a,
        member_b,
    }


@pytest.mark.asyncio
async def test_concurrent_member_updates_with_same_etag_allow_only_one_commit(
    database: async_sessionmaker[AsyncSession],
) -> None:
    owner = await _add_active_user(database)
    service = _tenant_service(database)
    created = await service.create_application(_create_command(owner))
    _, target, _, _ = await _add_internal_member_with_session(
        database,
        tenant_id=created.tenant.id,
    )

    async def update_with(key: str, status: MembershipStatus) -> object:
        return await service.update_member(
            await _actor_with_session(database, created, owner),
            UpdateMemberCommand(
                membership_id=target,  # type: ignore[arg-type]
                status=status,
                expected_version=1,
                idempotency_key=key,
                audit_context=_audit_context(),
            ),
        )

    outcomes = await asyncio.gather(
        update_with("member-concurrent-key-01", MembershipStatus.SUSPENDED),
        update_with("member-concurrent-key-02", MembershipStatus.ACTIVE),
        return_exceptions=True,
    )
    assert sum(not isinstance(value, BaseException) for value in outcomes) == 1
    assert sum(isinstance(value, VersionConflict) for value in outcomes) == 1, outcomes
    async with database() as session:
        stored = await session.get(TenantMembershipModel, target)
        assert stored is not None and stored.version == 2 and stored.authz_version == 2


@pytest.mark.asyncio
async def test_cross_tenant_role_id_is_rejected_before_assignment(
    database: async_sessionmaker[AsyncSession],
) -> None:
    owner_a = await _add_active_user(database)
    service = _tenant_service(database)
    created_a = await service.create_application(_create_command(owner_a))
    _, target_a, _, _ = await _add_internal_member_with_session(
        database,
        tenant_id=created_a.tenant.id,
    )
    owner_b = new_uuid7()
    await _add_active_user(database, user_id=owner_b)
    created_b = await service.create_application(
        CreateTenantApplicationCommand(
            actor_user_id=owner_b,
            name="角色租户乙",
            tenant_type=TenantType.ENTERPRISE,
            idempotency_key="tenant-create-key-0004",
            audit_context=_audit_context(),
        )
    )
    async with database() as session:
        role_b = await session.scalar(
            select(TenantRoleModel.id).where(
                TenantRoleModel.tenant_id == created_b.tenant.id,
                TenantRoleModel.code == "tenant_admin",
            )
        )
        assert role_b is not None

    with pytest.raises(TenantResourceNotFound):
        await service.update_member(
            await _actor_with_session(database, created_a, owner_a),
            UpdateMemberCommand(
                membership_id=target_a,  # type: ignore[arg-type]
                role_ids=(role_b,),
                expected_version=1,
                idempotency_key="member-role-cross-tenant-01",
                audit_context=_audit_context(),
            ),
        )
    async with database() as session:
        stored = await session.get(TenantMembershipModel, target_a)
        assert stored is not None and stored.version == 1 and stored.authz_version == 1
        assert await session.scalar(
            select(func.count()).select_from(MembershipRoleAssignmentModel).where(
                MembershipRoleAssignmentModel.tenant_id == created_a.tenant.id,
                MembershipRoleAssignmentModel.membership_id == target_a,
            )
        ) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["invited", "revoked"])
async def test_invited_and_revoked_memberships_cannot_be_changed_by_task_7_patch(
    database: async_sessionmaker[AsyncSession], status: str
) -> None:
    owner = await _add_active_user(database)
    service = _tenant_service(database)
    created = await service.create_application(_create_command(owner))
    _, target = await _add_member(
        database,
        tenant_id=created.tenant.id,
        member_type="internal",
        status=status,
    )

    with pytest.raises(MembershipMutationDenied, match="state"):
        await service.update_member(
            await _actor_with_session(database, created, owner),
            UpdateMemberCommand(
                membership_id=target,  # type: ignore[arg-type]
                status=MembershipStatus.SUSPENDED,
                expected_version=1,
                idempotency_key=f"member-state-{status}-0001",
                audit_context=_audit_context(),
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("member_type", "requested_role"),
    [("student", "lawyer_or_legal"), ("external_client", "tenant_admin")],
)
async def test_external_and_student_members_cannot_receive_internal_or_privileged_roles(
    database: async_sessionmaker[AsyncSession], member_type: str, requested_role: str
) -> None:
    owner = await _add_active_user(database)
    service = _tenant_service(database)
    created = await service.create_application(_create_command(owner))
    _, target = await _add_member(
        database,
        tenant_id=created.tenant.id,
        member_type=member_type,
        role_codes=(("student",) if member_type == "student" else ("external_client",)),
    )
    roles = await _tenant_roles(database, created.tenant.id)

    with pytest.raises(MembershipMutationDenied, match="member type"):
        await service.update_member(
            await _actor_with_session(database, created, owner),
            UpdateMemberCommand(
                membership_id=target,  # type: ignore[arg-type]
                role_ids=(roles[requested_role],),  # type: ignore[arg-type]
                expected_version=1,
                idempotency_key=f"member-type-{member_type}-0001",
                audit_context=_audit_context(),
            ),
        )


@pytest.mark.asyncio
async def test_tenant_admin_cannot_touch_owner_role_or_promote_itself_to_owner(
    database: async_sessionmaker[AsyncSession],
) -> None:
    owner = await _add_active_user(database)
    service = _tenant_service(database)
    created = await service.create_application(_create_command(owner))
    admin_user, admin_membership = await _add_member(
        database,
        tenant_id=created.tenant.id,
        member_type="internal",
        role_codes=("tenant_admin",),
    )
    roles = await _tenant_roles(database, created.tenant.id)
    admin_actor = await _actor_for_membership(
        database,
        created,
        user_id=admin_user,
        membership_id=admin_membership,
        department_id=None,
        scope=AuthorizationScope(allow_tenant_wide=True),
    )

    with pytest.raises(MembershipMutationDenied, match="owner"):
        await service.update_member(
            admin_actor,
            UpdateMemberCommand(
                membership_id=created.owner_membership.id,
                role_ids=(),
                expected_version=1,
                idempotency_key="tenant-admin-remove-owner-01",
                audit_context=_audit_context(),
            ),
        )
    with pytest.raises(MembershipMutationDenied, match="owner"):
        await service.update_member(
            admin_actor,
            UpdateMemberCommand(
                membership_id=admin_membership,  # type: ignore[arg-type]
                role_ids=(roles["tenant_admin"], roles["tenant_owner"]),  # type: ignore[arg-type]
                expected_version=1,
                idempotency_key="tenant-admin-self-owner-001",
                audit_context=_audit_context(),
            ),
        )


@pytest.mark.asyncio
async def test_last_active_owner_cannot_be_revoked_or_have_owner_role_removed(
    database: async_sessionmaker[AsyncSession],
) -> None:
    owner = await _add_active_user(database)
    service = _tenant_service(database)
    created = await service.create_application(_create_command(owner))
    actor = await _actor_with_session(database, created, owner)

    with pytest.raises(MembershipMutationDenied, match="last active owner"):
        await service.revoke_member(
            actor,
            RevokeMemberCommand(
                membership_id=created.owner_membership.id,
                expected_version=1,
                idempotency_key="last-owner-revoke-0001",
                audit_context=_audit_context(),
            ),
        )
    with pytest.raises(MembershipMutationDenied, match="last active owner"):
        await service.update_member(
            actor,
            UpdateMemberCommand(
                membership_id=created.owner_membership.id,
                role_ids=(),
                expected_version=1,
                idempotency_key="last-owner-remove-role-1",
                audit_context=_audit_context(),
            ),
        )


@pytest.mark.asyncio
async def test_concurrent_owner_revocations_never_remove_the_last_active_owner(
    database: async_sessionmaker[AsyncSession],
) -> None:
    owner_a = await _add_active_user(database)
    service = _tenant_service(database)
    created = await service.create_application(_create_command(owner_a))
    owner_b, membership_b = await _add_member(
        database,
        tenant_id=created.tenant.id,
        member_type="owner",
        role_codes=("tenant_owner",),
    )
    actor_a = await _actor_with_session(database, created, owner_a)
    actor_b = await _actor_for_membership(
        database,
        created,
        user_id=owner_b,
        membership_id=membership_b,
        department_id=None,
        scope=AuthorizationScope(allow_tenant_wide=True),
    )

    outcomes = await asyncio.gather(
        service.revoke_member(
            actor_a,
            RevokeMemberCommand(
                membership_id=membership_b,  # type: ignore[arg-type]
                expected_version=1,
                idempotency_key="owners-concurrent-revoke-1",
                audit_context=_audit_context(),
            ),
        ),
        service.revoke_member(
            actor_b,
            RevokeMemberCommand(
                membership_id=created.owner_membership.id,
                expected_version=1,
                idempotency_key="owners-concurrent-revoke-2",
                audit_context=_audit_context(),
            ),
        ),
        return_exceptions=True,
    )

    assert not any(isinstance(value, OperationalError) for value in outcomes), outcomes
    assert sum(not isinstance(value, BaseException) for value in outcomes) == 1
    failures = [value for value in outcomes if isinstance(value, BaseException)]
    assert len(failures) == 1
    assert isinstance(failures[0], (TenantAuthorizationDenied, MembershipMutationDenied))
    async with database() as session:
        active_owners = await session.scalar(
            select(func.count()).select_from(TenantMembershipModel).join(
                MembershipRoleAssignmentModel,
                MembershipRoleAssignmentModel.membership_id == TenantMembershipModel.id,
            ).join(
                TenantRoleModel,
                TenantRoleModel.id == MembershipRoleAssignmentModel.tenant_role_id,
            ).where(
                TenantMembershipModel.tenant_id == created.tenant.id,
                TenantMembershipModel.status == "active",
                TenantRoleModel.code == "tenant_owner",
            )
        )
        assert active_owners == 1


@pytest.mark.asyncio
async def test_disjoint_owner_revocations_use_one_stable_owner_lock_order(
    database: async_sessionmaker[AsyncSession],
) -> None:
    owner_a = await _add_active_user(database)
    service = _tenant_service(database)
    created = await service.create_application(_create_command(owner_a))
    owners = [
        await _add_member(
            database,
            tenant_id=created.tenant.id,
            member_type="owner",
            role_codes=("tenant_owner",),
        )
        for _ in range(3)
    ]
    (owner_b, membership_b), (owner_c, membership_c), (_, membership_d) = owners
    actor_a = await _actor_with_session(database, created, owner_a)
    actor_c = await _actor_for_membership(
        database,
        created,
        user_id=owner_c,
        membership_id=membership_c,
        department_id=None,
        scope=AuthorizationScope(allow_tenant_wide=True),
    )
    del owner_b

    outcomes = await asyncio.gather(
        service.revoke_member(
            actor_a,
            RevokeMemberCommand(
                membership_id=membership_b,  # type: ignore[arg-type]
                expected_version=1,
                idempotency_key="owners-disjoint-revoke-key-1",
                audit_context=_audit_context(),
            ),
        ),
        service.revoke_member(
            actor_c,
            RevokeMemberCommand(
                membership_id=membership_d,  # type: ignore[arg-type]
                expected_version=1,
                idempotency_key="owners-disjoint-revoke-key-2",
                audit_context=_audit_context(),
            ),
        ),
        return_exceptions=True,
    )

    assert all(not isinstance(value, BaseException) for value in outcomes), outcomes


@pytest.mark.asyncio
async def test_department_admin_is_limited_to_same_department_and_cannot_grant_higher_role(
    database: async_sessionmaker[AsyncSession],
) -> None:
    owner = await _add_active_user(database)
    service = _tenant_service(database)
    created = await service.create_application(_create_command(owner))
    department_a, department_b = new_uuid7(), new_uuid7()
    async with database.begin() as session:
        session.add_all(
            [
                DepartmentModel(
                    id=department_a,
                    tenant_id=created.tenant.id,
                    name="部门甲",
                    status="active",
                ),
                DepartmentModel(
                    id=department_b,
                    tenant_id=created.tenant.id,
                    name="部门乙",
                    status="active",
                ),
            ]
        )
    admin_user, admin_membership = await _add_member(
        database,
        tenant_id=created.tenant.id,
        member_type="internal",
        department_id=department_a,
        role_codes=("department_admin",),
    )
    _, target_a = await _add_member(
        database, tenant_id=created.tenant.id, member_type="internal", department_id=department_a
    )
    _, target_b = await _add_member(
        database, tenant_id=created.tenant.id, member_type="internal", department_id=department_b
    )
    actor = await _actor_for_membership(
        database,
        created,
        user_id=admin_user,
        membership_id=admin_membership,
        department_id=department_a,
        scope=AuthorizationScope(department_ids=frozenset({department_a})),
    )
    roles = await _tenant_roles(database, created.tenant.id)

    changed = await service.update_member(
        actor,
        UpdateMemberCommand(
            membership_id=target_a,  # type: ignore[arg-type]
            status=MembershipStatus.SUSPENDED,
            expected_version=1,
            idempotency_key="department-admin-same-dept-1",
            audit_context=_audit_context(),
        ),
    )
    assert changed.membership.status is MembershipStatus.SUSPENDED
    with pytest.raises(TenantAuthorizationDenied) as other_department:
        await service.update_member(
            actor,
            UpdateMemberCommand(
                membership_id=target_b,  # type: ignore[arg-type]
                status=MembershipStatus.SUSPENDED,
                expected_version=1,
                idempotency_key="department-admin-other-dept-1",
                audit_context=_audit_context(),
            ),
        )
    assert other_department.value.reason_code == "resource_scope_denied"
    with pytest.raises(TenantAuthorizationDenied) as higher_role:
        await service.update_member(
            actor,
            UpdateMemberCommand(
                membership_id=target_a,  # type: ignore[arg-type]
                role_ids=(roles["tenant_admin"],),  # type: ignore[arg-type]
                expected_version=2,
                idempotency_key="department-admin-high-role-01",
                audit_context=_audit_context(),
            ),
        )
    assert higher_role.value.reason_code == "permission_denied"


@pytest.mark.asyncio
@pytest.mark.parametrize("session_change", ["revoked", "expired", "auth_version_drift"])
async def test_member_write_rejects_authoritatively_invalid_actor_session(
    database: async_sessionmaker[AsyncSession], session_change: str
) -> None:
    owner = await _add_active_user(database)
    service = _tenant_service(database)
    created = await service.create_application(_create_command(owner))
    _, target = await _add_member(
        database, tenant_id=created.tenant.id, member_type="internal"
    )
    actor = await _actor_with_session(database, created, owner)
    async with database.begin() as session:
        stored = await session.get(AuthSessionModel, actor.principal.session_id)
        assert stored is not None
        if session_change == "revoked":
            stored.revoked_at = (NOW - timedelta(seconds=1)).replace(tzinfo=None)
            stored.revocation_reason = "user_requested"
        elif session_change == "expired":
            stored.expires_at = (NOW - timedelta(seconds=1)).replace(tzinfo=None)
        else:
            user = await session.get(UserModel, owner)
            assert user is not None
            user.auth_version = 2
            stored.auth_version_at_issue = 1
            actor = TenantActor(
                principal=replace(
                    actor.principal,
                    auth_version=2,
                    session_auth_version=2,
                ),
                context=actor.context,
            )

    with pytest.raises(TenantAuthorizationDenied) as denied:
        await service.update_member(
            actor,
            UpdateMemberCommand(
                membership_id=target,  # type: ignore[arg-type]
                status=MembershipStatus.SUSPENDED,
                expected_version=1,
                idempotency_key=f"invalid-actor-session-{session_change}-01",
                audit_context=_audit_context(),
            ),
        )
    assert denied.value.reason_code == "session_invalid"


@pytest.mark.asyncio
async def test_member_write_rejects_unbound_principal_session_id(
    database: async_sessionmaker[AsyncSession],
) -> None:
    owner = await _add_active_user(database)
    service = _tenant_service(database)
    created = await service.create_application(_create_command(owner))
    _, target = await _add_member(
        database, tenant_id=created.tenant.id, member_type="internal"
    )
    actor = await _actor_with_session(database, created, owner)
    actor = TenantActor(
        principal=replace(actor.principal, session_id=new_uuid7()),
        context=actor.context,
    )

    with pytest.raises(TenantAuthorizationDenied) as denied:
        await service.update_member(
            actor,
            UpdateMemberCommand(
                membership_id=target,  # type: ignore[arg-type]
                status=MembershipStatus.SUSPENDED,
                expected_version=1,
                idempotency_key="unbound-principal-session-01",
                audit_context=_audit_context(),
            ),
        )
    assert denied.value.reason_code == "session_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize("actor_change", ["user_disabled", "session_revoked", "membership_revoked"])
async def test_actor_security_change_committed_before_locked_snapshot_rejects_write(
    database: async_sessionmaker[AsyncSession], actor_change: str
) -> None:
    owner = await _add_active_user(database)
    created = await _tenant_service(database).create_application(_create_command(owner))
    _, target = await _add_member(
        database, tenant_id=created.tenant.id, member_type="internal"
    )
    actor = await _actor_with_session(database, created, owner)
    reached, release = asyncio.Event(), asyncio.Event()
    service = _tenant_service(
        database,
        uow_factory=lambda: _PauseBeforeLockedAuthorizationUow(database, reached, release),
    )
    task = asyncio.create_task(
        service.update_member(
            actor,
            UpdateMemberCommand(
                membership_id=target,  # type: ignore[arg-type]
                status=MembershipStatus.SUSPENDED,
                expected_version=1,
                idempotency_key=f"actor-concurrent-change-{actor_change}",
                audit_context=_audit_context(),
            ),
        )
    )
    await asyncio.wait_for(reached.wait(), timeout=5)
    async with database.begin() as session:
        if actor_change == "user_disabled":
            await session.execute(
                update(UserModel).where(UserModel.id == owner).values(status="disabled")
            )
        elif actor_change == "session_revoked":
            await session.execute(
                update(AuthSessionModel)
                .where(AuthSessionModel.id == actor.principal.session_id)
                .values(
                    revoked_at=(NOW - timedelta(seconds=1)).replace(tzinfo=None),
                    revocation_reason="user_requested",
                )
            )
        else:
            await session.execute(
                update(TenantMembershipModel)
                .where(TenantMembershipModel.id == created.owner_membership.id)
                .values(status="revoked", authz_version=2, version=2)
            )
    release.set()
    outcome = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=5)

    assert isinstance(outcome[0], TenantAuthorizationDenied)
    async with database() as session:
        untouched = await session.get(TenantMembershipModel, target)
        assert untouched is not None and untouched.status == "active" and untouched.version == 1


@pytest.mark.asyncio
async def test_refresh_and_member_change_share_refresh_then_session_lock_order_without_deadlock(
    database: async_sessionmaker[AsyncSession],
) -> None:
    owner = await _add_active_user(database)
    tenant_service = _tenant_service(database)
    created = await tenant_service.create_application(_create_command(owner))
    target_user, target_membership = await _add_member(
        database, tenant_id=created.tenant.id, member_type="internal"
    )
    async with database.begin() as session:
        await session.execute(
            update(TenantModel)
            .where(TenantModel.id == created.tenant.id)
            .values(status="active", review_status="approved")
        )

    private_key = Ed25519PrivateKey.generate()
    tokens = TokenService(
        issuer="https://identity.task7.test",
        active_kid="task7-lock-key",
        signing_keys={"task7-lock-key": private_key},
        verification_keys={"task7-lock-key": private_key.public_key()},
    )
    base_sessions = SessionService(
        uow_factory=lambda: SqlAlchemySessionUnitOfWork(database),
        token_service=tokens,
        refresh_hash_key=b"r" * 32,
        clock=lambda: NOW,
        validation_cache=None,
    )
    account = await base_sessions.start(
        user_id=target_user,  # type: ignore[arg-type]
        audit_context=_audit_context(),
    )
    tenant_session = await base_sessions.switch_tenant(
        SwitchTenantCommand(
            account.session_id,
            created.tenant.id,
            target_membership,  # type: ignore[arg-type]
        ),
        audit_context=_audit_context(),
    )

    refresh_locked, release_refresh = asyncio.Event(), asyncio.Event()
    paused_sessions = SessionService(
        uow_factory=lambda: _PauseAfterRefreshLockUow(
            database, refresh_locked, release_refresh
        ),  # type: ignore[arg-type]
        token_service=tokens,
        refresh_hash_key=b"r" * 32,
        clock=lambda: NOW,
        validation_cache=None,
    )
    owner_actor = await _actor_with_session(database, created, owner)
    mutation_sql_started = asyncio.Event()
    engine = database.kw["bind"]

    def observe_mutation_sql(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del conn, cursor, parameters, context, executemany
        normalized = " ".join(statement.casefold().split())
        if (
            normalized.startswith("update auth_sessions")
            or ("from tenants" in normalized and "for update" in normalized)
            or (
                "from refresh_token_records" in normalized
                and "for update" in normalized
            )
        ):
            mutation_sql_started.set()

    refresh_task = asyncio.create_task(
        paused_sessions.refresh(tenant_session.refresh_token, audit_context=_audit_context())
    )
    await asyncio.wait_for(refresh_locked.wait(), timeout=5)
    event.listen(engine.sync_engine, "before_cursor_execute", observe_mutation_sql)
    try:
        mutation_task = asyncio.create_task(
            tenant_service.update_member(
                owner_actor,
                UpdateMemberCommand(
                    membership_id=target_membership,  # type: ignore[arg-type]
                    status=MembershipStatus.SUSPENDED,
                    expected_version=1,
                    idempotency_key="refresh-member-lock-order-01",
                    audit_context=_audit_context(),
                ),
            )
        )
        await asyncio.wait_for(mutation_sql_started.wait(), timeout=5)
        release_refresh.set()
        refresh_outcome, mutation_outcome = await asyncio.wait_for(
            asyncio.gather(refresh_task, mutation_task, return_exceptions=True),
            timeout=10,
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", observe_mutation_sql)
        release_refresh.set()

    assert not isinstance(refresh_outcome, BaseException)
    assert not isinstance(mutation_outcome, BaseException)
    async with database() as session:
        membership = await session.get(TenantMembershipModel, target_membership)
        auth_session = await session.get(AuthSessionModel, tenant_session.session_id)
        refresh_rows = (
            await session.scalars(
                select(RefreshTokenRecordModel).where(
                    RefreshTokenRecordModel.session_id == tenant_session.session_id
                )
            )
        ).all()
        assert membership is not None and membership.status == "suspended"
        assert auth_session is not None and auth_session.revoked_at is not None
        assert refresh_rows and all(row.revoked_at is not None for row in refresh_rows)


@pytest.mark.asyncio
async def test_refresh_and_explicit_revoke_use_refresh_then_session_lock_order(
    database: async_sessionmaker[AsyncSession],
) -> None:
    owner = await _add_active_user(database)
    created = await _tenant_service(database).create_application(_create_command(owner))
    target_user, target_membership = await _add_member(
        database, tenant_id=created.tenant.id, member_type="internal"
    )
    async with database.begin() as session:
        await session.execute(
            update(TenantModel)
            .where(TenantModel.id == created.tenant.id)
            .values(status="active", review_status="approved")
        )
    private_key = Ed25519PrivateKey.generate()
    tokens = TokenService(
        issuer="https://identity.task7.test",
        active_kid="task7-explicit-revoke-key",
        signing_keys={"task7-explicit-revoke-key": private_key},
        verification_keys={"task7-explicit-revoke-key": private_key.public_key()},
    )
    sessions = SessionService(
        uow_factory=lambda: SqlAlchemySessionUnitOfWork(database),
        token_service=tokens,
        refresh_hash_key=b"q" * 32,
        clock=lambda: NOW,
        validation_cache=None,
    )
    account = await sessions.start(
        user_id=target_user,  # type: ignore[arg-type]
        audit_context=_audit_context(),
    )
    tenant_session = await sessions.switch_tenant(
        SwitchTenantCommand(
            account.session_id,
            created.tenant.id,
            target_membership,  # type: ignore[arg-type]
        ),
        audit_context=_audit_context(),
    )
    refresh_locked, release_refresh = asyncio.Event(), asyncio.Event()
    paused_refresh = SessionService(
        uow_factory=lambda: _PauseAfterRefreshLockUow(
            database, refresh_locked, release_refresh
        ),  # type: ignore[arg-type]
        token_service=tokens,
        refresh_hash_key=b"q" * 32,
        clock=lambda: NOW,
        validation_cache=None,
    )
    revoke_lock_started = asyncio.Event()
    engine = database.kw["bind"]

    def observe_revoke_lock(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del conn, cursor, parameters, context, executemany
        normalized = " ".join(statement.casefold().split())
        if (
            normalized.startswith("update refresh_token_records")
            or ("from tenants" in normalized and "for update" in normalized)
            or (
                "from refresh_token_records" in normalized
                and "for update" in normalized
                and "session_id" in normalized
            )
        ):
            revoke_lock_started.set()

    refresh_task = asyncio.create_task(
        paused_refresh.refresh(tenant_session.refresh_token, audit_context=_audit_context())
    )
    await asyncio.wait_for(refresh_locked.wait(), timeout=5)
    event.listen(engine.sync_engine, "before_cursor_execute", observe_revoke_lock)
    try:
        revoke_task = asyncio.create_task(
            sessions.revoke(
                tenant_session.session_id,
                reason=RevocationReason.ADMIN_REVOKED,
                audit_context=_audit_context(),
            )
        )
        await asyncio.wait_for(revoke_lock_started.wait(), timeout=5)
        release_refresh.set()
        refresh_outcome, revoke_outcome = await asyncio.wait_for(
            asyncio.gather(refresh_task, revoke_task, return_exceptions=True),
            timeout=10,
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", observe_revoke_lock)
        release_refresh.set()
    assert not isinstance(refresh_outcome, OperationalError), refresh_outcome
    assert not isinstance(revoke_outcome, OperationalError), revoke_outcome
    assert not isinstance(refresh_outcome, BaseException), refresh_outcome
    assert not isinstance(revoke_outcome, BaseException), revoke_outcome
    async with database() as session:
        auth_session = await session.get(AuthSessionModel, tenant_session.session_id)
        refresh_rows = (
            await session.scalars(
                select(RefreshTokenRecordModel).where(
                    RefreshTokenRecordModel.session_id == tenant_session.session_id
                )
            )
        ).all()
        assert auth_session is not None and auth_session.revoked_at is not None
        assert refresh_rows and all(row.revoked_at is not None for row in refresh_rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("source_context", ["account", "tenant"])
async def test_refresh_and_switch_tenant_share_complete_family_lock_order(
    database: async_sessionmaker[AsyncSession],
    source_context: str,
) -> None:
    owner = await _add_active_user(database)
    created = await _tenant_service(database).create_application(_create_command(owner))
    target_user, target_membership = await _add_member(
        database, tenant_id=created.tenant.id, member_type="internal"
    )
    async with database.begin() as session:
        await session.execute(
            update(TenantModel)
            .where(TenantModel.id == created.tenant.id)
            .values(status="active", review_status="approved")
        )
    private_key = Ed25519PrivateKey.generate()
    tokens = TokenService(
        issuer="https://identity.task7.test",
        active_kid="task7-switch-lock-key",
        signing_keys={"task7-switch-lock-key": private_key},
        verification_keys={"task7-switch-lock-key": private_key.public_key()},
    )
    sessions = SessionService(
        uow_factory=lambda: SqlAlchemySessionUnitOfWork(database),
        token_service=tokens,
        refresh_hash_key=b"w" * 32,
        clock=lambda: NOW,
        validation_cache=None,
    )
    account = await sessions.start(
        user_id=target_user,  # type: ignore[arg-type]
        audit_context=_audit_context(),
    )
    source = account
    if source_context == "tenant":
        source = await sessions.switch_tenant(
            SwitchTenantCommand(
                account.session_id,
                created.tenant.id,
                target_membership,  # type: ignore[arg-type]
            ),
            audit_context=_audit_context(),
        )

    family_locked, release_family = asyncio.Event(), asyncio.Event()
    paused_refresh = SessionService(
        uow_factory=lambda: _PauseAfterRefreshLockUow(
            database, family_locked, release_family
        ),  # type: ignore[arg-type]
        token_service=tokens,
        refresh_hash_key=b"w" * 32,
        clock=lambda: NOW,
        validation_cache=None,
    )
    competing_lock_started = asyncio.Event()
    engine = database.kw["bind"]

    def observe_switch_lock(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del conn, cursor, parameters, context, executemany
        normalized = " ".join(statement.casefold().split())
        if (
            "for update" in normalized
            and ("from refresh_token_records" in normalized or "from tenants" in normalized)
        ) or normalized.startswith("update refresh_token_records"):
            competing_lock_started.set()

    refresh_task = asyncio.create_task(
        paused_refresh.refresh(source.refresh_token, audit_context=_audit_context())
    )
    await asyncio.wait_for(family_locked.wait(), timeout=5)
    event.listen(engine.sync_engine, "before_cursor_execute", observe_switch_lock)
    try:
        switch_task = asyncio.create_task(
            sessions.switch_tenant(
                SwitchTenantCommand(
                    source.session_id,
                    created.tenant.id,
                    target_membership,  # type: ignore[arg-type]
                ),
                audit_context=_audit_context(),
            )
        )
        await asyncio.wait_for(competing_lock_started.wait(), timeout=5)
        release_family.set()
        outcomes = await asyncio.wait_for(
            asyncio.gather(refresh_task, switch_task, return_exceptions=True),
            timeout=10,
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", observe_switch_lock)
        release_family.set()
    assert not any(isinstance(value, OperationalError) for value in outcomes), outcomes
    assert not any(isinstance(value, BaseException) for value in outcomes), outcomes
    async with database() as session:
        old_session = await session.get(AuthSessionModel, source.session_id)
        old_family = (
            await session.scalars(
                select(RefreshTokenRecordModel).where(
                    RefreshTokenRecordModel.family_id == source.family_id
                )
            )
        ).all()
    assert old_session is not None and old_session.revoked_at is not None
    assert old_family and all(row.revoked_at is not None for row in old_family)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target_mutation",
    ["membership_suspended", "membership_revoked", "tenant_suspended", "tenant_closed"],
)
async def test_switch_reloads_target_after_locator_transaction_closes(
    database: async_sessionmaker[AsyncSession],
    target_mutation: str,
) -> None:
    owner = await _add_active_user(database)
    created = await _tenant_service(database).create_application(_create_command(owner))
    target_user, target_membership = await _add_member(
        database, tenant_id=created.tenant.id, member_type="internal"
    )
    async with database.begin() as session:
        await session.execute(
            update(TenantModel)
            .where(TenantModel.id == created.tenant.id)
            .values(status="active", review_status="approved")
        )
    private_key = Ed25519PrivateKey.generate()
    tokens = TokenService(
        issuer="https://identity.task7.test",
        active_kid="task7-switch-snapshot-key",
        signing_keys={"task7-switch-snapshot-key": private_key},
        verification_keys={"task7-switch-snapshot-key": private_key.public_key()},
    )
    base = SessionService(
        uow_factory=lambda: SqlAlchemySessionUnitOfWork(database),
        token_service=tokens,
        refresh_hash_key=b"n" * 32,
        clock=lambda: NOW,
        validation_cache=None,
    )
    account = await base.start(
        user_id=target_user,  # type: ignore[arg-type]
        audit_context=_audit_context(),
    )
    async with database() as session:
        before_sessions = await session.scalar(select(func.count()).select_from(AuthSessionModel))
        before_refresh = await session.scalar(
            select(func.count()).select_from(RefreshTokenRecordModel)
        )
        before_audits = await session.scalar(select(func.count()).select_from(AuditEventModel))

    located, release = asyncio.Event(), asyncio.Event()
    uow_factory = _SwitchSnapshotUowFactory(database, located, release)
    service = SessionService(
        uow_factory=uow_factory,  # type: ignore[arg-type]
        token_service=tokens,
        refresh_hash_key=b"n" * 32,
        clock=lambda: NOW,
        validation_cache=None,
    )
    first_write_sql: list[str] = []
    engine = database.kw["bind"]

    def observe_first_write_uow_statement(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del conn, cursor, parameters, context, executemany
        if uow_factory.locator_uow_closed and not first_write_sql:
            first_write_sql.append(" ".join(statement.casefold().split()))

    event.listen(engine.sync_engine, "before_cursor_execute", observe_first_write_uow_statement)
    task = asyncio.create_task(
        service.switch_tenant(
            SwitchTenantCommand(
                account.session_id,
                created.tenant.id,
                target_membership,  # type: ignore[arg-type]
            ),
            audit_context=_audit_context(),
        )
    )
    try:
        await asyncio.wait_for(located.wait(), timeout=5)
        async with database.begin() as session:
            if target_mutation.startswith("membership_"):
                await session.execute(
                    update(TenantMembershipModel)
                    .where(TenantMembershipModel.id == target_membership)
                    .values(status=target_mutation.removeprefix("membership_"))
                )
            else:
                await session.execute(
                    update(TenantModel)
                    .where(TenantModel.id == created.tenant.id)
                    .values(status=target_mutation.removeprefix("tenant_"))
                )
        release.set()
        with pytest.raises(InvalidSession):
            await asyncio.wait_for(task, timeout=10)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", observe_first_write_uow_statement)
        release.set()

    assert first_write_sql
    assert "from tenants" in first_write_sql[0] and "for update" in first_write_sql[0]
    async with database() as session:
        assert (
            await session.scalar(select(func.count()).select_from(AuthSessionModel))
            == before_sessions
        )
        assert (
            await session.scalar(select(func.count()).select_from(RefreshTokenRecordModel))
            == before_refresh
        )
        assert (
            await session.scalar(select(func.count()).select_from(AuditEventModel))
            == before_audits
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh_state", ["used", "expired"])
async def test_used_or_expired_refresh_and_explicit_revoke_lock_whole_family(
    database: async_sessionmaker[AsyncSession],
    refresh_state: str,
) -> None:
    user = await _add_active_user(database)
    private_key = Ed25519PrivateKey.generate()
    tokens = TokenService(
        issuer="https://identity.task7.test",
        active_kid="task7-replay-revoke-key",
        signing_keys={"task7-replay-revoke-key": private_key},
        verification_keys={"task7-replay-revoke-key": private_key.public_key()},
    )
    sessions = SessionService(
        uow_factory=lambda: SqlAlchemySessionUnitOfWork(database),
        token_service=tokens,
        refresh_hash_key=b"e" * 32,
        clock=lambda: NOW,
        validation_cache=None,
    )
    started = await sessions.start(user_id=user, audit_context=_audit_context())  # type: ignore[arg-type]
    rotated = await sessions.refresh(started.refresh_token, audit_context=_audit_context())
    raw_token = started.refresh_token
    if refresh_state == "expired":
        raw_token = rotated.refresh_token
        async with database.begin() as session:
            replacement = await session.scalar(
                select(RefreshTokenRecordModel)
                .where(RefreshTokenRecordModel.family_id == started.family_id)
                .order_by(RefreshTokenRecordModel.id.desc())
                .limit(1)
            )
            assert replacement is not None
            replacement.idle_expires_at = (NOW - timedelta(seconds=1)).replace(tzinfo=None)

    family_locked, release_family = asyncio.Event(), asyncio.Event()
    paused_refresh = SessionService(
        uow_factory=lambda: _PauseAfterRefreshLockUow(
            database, family_locked, release_family
        ),  # type: ignore[arg-type]
        token_service=tokens,
        refresh_hash_key=b"e" * 32,
        clock=lambda: NOW,
        validation_cache=None,
    )
    revoke_lock_started = asyncio.Event()
    engine = database.kw["bind"]

    def observe_revoke_lock(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del conn, cursor, parameters, context, executemany
        normalized = " ".join(statement.casefold().split())
        if "from refresh_token_records" in normalized and "for update" in normalized:
            revoke_lock_started.set()

    refresh_task = asyncio.create_task(
        paused_refresh.refresh(raw_token, audit_context=_audit_context())
    )
    await asyncio.wait_for(family_locked.wait(), timeout=5)
    event.listen(engine.sync_engine, "before_cursor_execute", observe_revoke_lock)
    try:
        revoke_task = asyncio.create_task(
            sessions.revoke(
                started.session_id,
                reason=RevocationReason.ADMIN_REVOKED,
                audit_context=_audit_context(),
            )
        )
        await asyncio.wait_for(revoke_lock_started.wait(), timeout=5)
        release_family.set()
        refresh_outcome, revoke_outcome = await asyncio.wait_for(
            asyncio.gather(refresh_task, revoke_task, return_exceptions=True),
            timeout=10,
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", observe_revoke_lock)
        release_family.set()
    assert not isinstance(refresh_outcome, OperationalError), refresh_outcome
    assert not isinstance(revoke_outcome, OperationalError), revoke_outcome
    assert isinstance(refresh_outcome, (RefreshReplayDetected, InvalidRefreshToken))
    assert not isinstance(revoke_outcome, BaseException), revoke_outcome
    async with database() as session:
        auth_session = await session.get(AuthSessionModel, started.session_id)
        family = (
            await session.scalars(
                select(RefreshTokenRecordModel).where(
                    RefreshTokenRecordModel.family_id == started.family_id
                )
            )
        ).all()
    assert auth_session is not None and auth_session.revoked_at is not None
    assert family and all(row.revoked_at is not None for row in family)


@pytest.mark.asyncio
async def test_two_used_family_tokens_replay_without_deadlock_or_partial_revocation(
    database: async_sessionmaker[AsyncSession],
) -> None:
    user = await _add_active_user(database)
    private_key = Ed25519PrivateKey.generate()
    tokens = TokenService(
        issuer="https://identity.task7.test",
        active_kid="task7-two-replay-key",
        signing_keys={"task7-two-replay-key": private_key},
        verification_keys={"task7-two-replay-key": private_key.public_key()},
    )
    base = SessionService(
        uow_factory=lambda: SqlAlchemySessionUnitOfWork(database),
        token_service=tokens,
        refresh_hash_key=b"z" * 32,
        clock=lambda: NOW,
        validation_cache=None,
    )
    started = await base.start(user_id=user, audit_context=_audit_context())  # type: ignore[arg-type]
    first = await base.refresh(started.refresh_token, audit_context=_audit_context())
    await base.refresh(first.refresh_token, audit_context=_audit_context())
    barrier = _TwoRefreshBarrier()
    concurrent = SessionService(
        uow_factory=lambda: _TwoRefreshBarrierUow(database, barrier),  # type: ignore[arg-type]
        token_service=tokens,
        refresh_hash_key=b"z" * 32,
        clock=lambda: NOW,
        validation_cache=None,
    )

    outcomes = await asyncio.wait_for(
        asyncio.gather(
            concurrent.refresh(started.refresh_token, audit_context=_audit_context()),
            concurrent.refresh(first.refresh_token, audit_context=_audit_context()),
            return_exceptions=True,
        ),
        timeout=10,
    )
    assert not any(isinstance(value, OperationalError) for value in outcomes), outcomes
    assert all(isinstance(value, RefreshReplayDetected) for value in outcomes), outcomes
    async with database() as session:
        auth_session = await session.get(AuthSessionModel, started.session_id)
        family = (
            await session.scalars(
                select(RefreshTokenRecordModel).where(
                    RefreshTokenRecordModel.family_id == started.family_id
                )
            )
        ).all()
    assert auth_session is not None and auth_session.revoked_at is not None
    assert family and all(row.revoked_at is not None for row in family)
