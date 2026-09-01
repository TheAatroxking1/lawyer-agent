from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from time import time_ns
from uuid import UUID

import pytest
from alembic.config import Config
from sqlalchemy import delete, func, select, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from alembic import command
from lawyer_agent.application.identity import (
    AuditContext,
    BlindIndexRolloutConfigurationError,
    BlindIndexRolloutPhase,
    BlindIndexRolloutPolicy,
    IdentityConflictError,
    IdentityService,
    LoginIdentifier,
    RegisterCommand,
    RegisteredUser,
)
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.identity import IdentityKind, normalize_username
from lawyer_agent.infrastructure.persistence.models import (
    AuditEventModel,
    AuthIdentityModel,
    PasswordCredentialModel,
    UserModel,
)
from lawyer_agent.infrastructure.persistence.repositories.identity import (
    SqlAlchemyIdentityUnitOfWork,
    identity_advisory_lock_name,
)
from lawyer_agent.infrastructure.security.blind_index import BlindIndexService
from lawyer_agent.infrastructure.security.cipher import SensitiveValueCipher
from lawyer_agent.infrastructure.security.passwords import Argon2PasswordHasher

pytestmark = [pytest.mark.integration, pytest.mark.mysql]

_PASSWORD = "correct horse battery staple"  # noqa: S105
_AUDIT_CONTEXT = AuditContext("trace-locks", b"i" * 32, b"u" * 32)
_CIPHER_KEY_V7 = b"c" * 32
_BLIND_KEY_V7 = b"g" * 32
_BLIND_KEY_V8 = b"h" * 32


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
async def lock_database(
    migrated_mysql_url: URL,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    engine = create_async_engine(migrated_mysql_url, pool_pre_ping=True, pool_size=8)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        yield engine, factory
    finally:
        async with factory.begin() as session:
            await session.execute(delete(AuditEventModel))
            await session.execute(delete(PasswordCredentialModel))
            await session.execute(delete(AuthIdentityModel))
            await session.execute(delete(UserModel))
        await engine.dispose()


async def _lock_owner(engine: AsyncEngine, lock_name: str) -> int | None:
    async with engine.connect() as connection:
        return await connection.scalar(
            text("SELECT IS_USED_LOCK(:lock_name)"),
            {"lock_name": lock_name},
        )


def test_lock_names_are_stable_secret_and_shared_across_overlapping_keyrings() -> None:
    username = "Sensitive-Lawyer-Identity"
    purpose = "identity:username"
    expanded = BlindIndexService(
        {1: b"a" * 32, 2: b"b" * 32},
        active_key_version=2,
    )
    contracted = BlindIndexService({2: b"b" * 32}, active_key_version=2)

    expanded_names = tuple(
        identity_advisory_lock_name(item.digest)
        for item in expanded.digests(purpose, username)
    )
    contracted_names = tuple(
        identity_advisory_lock_name(item.digest)
        for item in contracted.digests(purpose, username)
    )

    assert contracted_names == (expanded_names[1],)
    assert expanded_names == tuple(dict.fromkeys(expanded_names))
    assert all(username not in lock_name for lock_name in expanded_names)
    assert all(len(lock_name) <= 64 for lock_name in expanded_names)


@pytest.mark.asyncio
async def test_second_connection_blocks_until_first_uow_commits_and_lock_owner_releases(
    lock_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    engine, session_factory = lock_database
    digest = b"c" * 32
    lock_name = identity_advisory_lock_name(digest)
    second_acquired = asyncio.Event()

    async def acquire_second() -> None:
        async with SqlAlchemyIdentityUnitOfWork(
            session_factory,
            lock_timeout_seconds=2,
        ) as second:
            await second.lock_identity(digest)
            second_acquired.set()

    async with SqlAlchemyIdentityUnitOfWork(session_factory) as first:
        await first.identities.has_unsupported_blind_index_versions((1,))
        await first.lock_identity(digest)
        assert first._lock_connection is not None
        owner_connection_id = await first._lock_connection.scalar(
            text("SELECT CONNECTION_ID()")
        )
        transaction_connection_id = await first.identities._session.scalar(
            text("SELECT CONNECTION_ID()")
        )
        assert await _lock_owner(engine, lock_name) == owner_connection_id
        assert owner_connection_id != transaction_connection_id

        second_task = asyncio.create_task(acquire_second())
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(second_acquired.wait(), timeout=0.15)

    await asyncio.wait_for(second_task, timeout=2)
    assert await _lock_owner(engine, lock_name) is None


@pytest.mark.asyncio
async def test_timeout_releases_partially_acquired_locks_and_rollback_releases_owner(
    lock_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    engine, session_factory = lock_database
    first_digest = b"d" * 32
    blocked_digest = b"e" * 32

    async with SqlAlchemyIdentityUnitOfWork(session_factory) as blocker:
        await blocker.lock_identity(blocked_digest)
        with pytest.raises(TimeoutError, match="unavailable"):
            async with SqlAlchemyIdentityUnitOfWork(
                session_factory,
                lock_timeout_seconds=0,
            ) as contender:
                await contender.lock_identity(first_digest)
                await contender.lock_identity(blocked_digest)

        async with SqlAlchemyIdentityUnitOfWork(
            session_factory,
            lock_timeout_seconds=0,
        ) as probe:
            await probe.lock_identity(first_digest)

    class BusinessFailure(RuntimeError):
        pass

    with pytest.raises(BusinessFailure):
        async with SqlAlchemyIdentityUnitOfWork(session_factory) as failing:
            await failing.identities.has_unsupported_blind_index_versions((1,))
            await failing.lock_identity(blocked_digest)
            raise BusinessFailure("rollback the transaction")

    async with SqlAlchemyIdentityUnitOfWork(
        session_factory,
        lock_timeout_seconds=0,
    ) as final_probe:
        await final_probe.lock_identity(blocked_digest)

    assert await _lock_owner(engine, identity_advisory_lock_name(first_digest)) is None
    assert await _lock_owner(engine, identity_advisory_lock_name(blocked_digest)) is None


def _service(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    blind_keys: dict[int, bytes],
) -> IdentityService:
    return IdentityService(
        uow_factory=lambda: SqlAlchemyIdentityUnitOfWork(session_factory),
        password_hasher=Argon2PasswordHasher(),
        cipher=SensitiveValueCipher({1: b"f" * 32}, active_key_version=1),
        blind_index=BlindIndexService(blind_keys, active_key_version=2),
        rollout_policy=BlindIndexRolloutPolicy(
            BlindIndexRolloutPhase.LEGACY_COMPATIBLE,
            2,
            False,
        ),
    )


def _rollout_service(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    active_version: int,
    phase: BlindIndexRolloutPhase,
    drained: bool,
) -> IdentityService:
    return IdentityService(
        uow_factory=lambda: SqlAlchemyIdentityUnitOfWork(session_factory),
        password_hasher=Argon2PasswordHasher(),
        cipher=SensitiveValueCipher({7: _CIPHER_KEY_V7}, active_key_version=7),
        blind_index=BlindIndexService(
            {7: _BLIND_KEY_V7, 8: _BLIND_KEY_V8},
            active_key_version=active_version,
        ),
        rollout_policy=BlindIndexRolloutPolicy(phase, 7, drained),
    )


async def _legacy_v7_writer_register(
    engine: AsyncEngine,
    username: str,
    *,
    ready: asyncio.Event | None = None,
    proceed: asyncio.Event | None = None,
) -> UUID:
    user_id = new_uuid7()
    identity_id = new_uuid7()
    normalized = normalize_username(username)
    now = datetime.now(UTC).replace(tzinfo=None)
    cipher = SensitiveValueCipher({7: _CIPHER_KEY_V7}, active_key_version=7)
    blind = BlindIndexService({7: _BLIND_KEY_V7}, active_key_version=7)
    hasher = Argon2PasswordHasher()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO users "
                "(id, status, display_name, auth_version, version) "
                "VALUES (:id, 'active', 'Legacy Writer', 1, 1)"
            ),
            {"id": user_id.bytes},
        )
        if ready is not None:
            ready.set()
        if proceed is not None:
            await asyncio.wait_for(proceed.wait(), timeout=2)
        await connection.execute(
            text(
                "INSERT INTO auth_identities "
                "(id, user_id, kind, provider, issuer, display_value, "
                "subject_ciphertext, subject_blind_index, key_version, "
                "verified_at, status) "
                "VALUES (:id, :user_id, 'username', 'local', 'local', "
                ":display_value, :ciphertext, :blind_index, 7, "
                ":verified_at, 'active')"
            ),
            {
                "id": identity_id.bytes,
                "user_id": user_id.bytes,
                "display_value": normalized,
                "ciphertext": cipher.encrypt(
                    normalized,
                    aad=f"auth_identity:{identity_id}:subject".encode("ascii"),
                ),
                "blind_index": blind.digest("identity:username", normalized),
                "verified_at": now,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO password_credentials "
                "(user_id, password_hash, algorithm, parameters_json, "
                "password_changed_at, status, version) "
                "VALUES (:user_id, :password_hash, 'argon2id', :parameters, "
                ":changed_at, 'active', 1)"
            ),
            {
                "user_id": user_id.bytes,
                "password_hash": hasher.hash(_PASSWORD),
                "parameters": json.dumps(hasher.parameters),
                "changed_at": now,
            },
        )
    return user_id


@pytest.mark.asyncio
async def test_legacy_v7_writer_prevents_legacy_phase_v8_service_from_starting(
    lock_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    engine, session_factory = lock_database
    username = f"legacy-gate-{UUID(int=time_ns() % (1 << 128))}"
    await _legacy_v7_writer_register(engine, username)

    with pytest.raises(BlindIndexRolloutConfigurationError, match="legacy key version"):
        _rollout_service(
            session_factory,
            active_version=8,
            phase=BlindIndexRolloutPhase.LEGACY_COMPATIBLE,
            drained=False,
        )

    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(UserModel)) == 1
        assert await session.scalar(select(func.count()).select_from(AuthIdentityModel)) == 1


@pytest.mark.asyncio
async def test_legacy_and_new_v7_writers_concurrently_leave_one_identity(
    lock_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    engine, session_factory = lock_database
    username = f"legacy-concurrent-{UUID(int=time_ns() % (1 << 128))}"
    ready = asyncio.Event()
    proceed = asyncio.Event()
    legacy_task = asyncio.create_task(
        _legacy_v7_writer_register(engine, username, ready=ready, proceed=proceed)
    )
    await asyncio.wait_for(ready.wait(), timeout=2)
    new_task = asyncio.create_task(
        _rollout_service(
            session_factory,
            active_version=7,
            phase=BlindIndexRolloutPhase.LEGACY_COMPATIBLE,
            drained=False,
        ).register(
            RegisterCommand(username.upper(), _PASSWORD, "New Writer"),
            audit_context=_AUDIT_CONTEXT,
        )
    )
    proceed.set()
    results = await asyncio.wait_for(
        asyncio.gather(legacy_task, new_task, return_exceptions=True),
        timeout=3,
    )

    assert sum(isinstance(result, (UUID, RegisteredUser)) for result in results) == 1
    assert sum(
        isinstance(result, (IntegrityError, IdentityConflictError))
        for result in results
    ) == 1
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(UserModel)) == 1
        assert await session.scalar(select(func.count()).select_from(AuthIdentityModel)) == 1


@pytest.mark.asyncio
async def test_rotation_ready_v8_service_authenticates_and_reindexes_legacy_v7(
    lock_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    engine, session_factory = lock_database
    username = f"rotation-ready-{UUID(int=time_ns() % (1 << 128))}"
    legacy_user_id = await _legacy_v7_writer_register(engine, username)
    service = _rollout_service(
        session_factory,
        active_version=8,
        phase=BlindIndexRolloutPhase.ROTATION_READY,
        drained=True,
    )

    authenticated = await service.authenticate(
        LoginIdentifier(IdentityKind.USERNAME, username),
        _PASSWORD,
        audit_context=_AUDIT_CONTEXT,
    )
    with pytest.raises(IdentityConflictError):
        await service.register(
            RegisterCommand(username.upper(), _PASSWORD, "Duplicate"),
            audit_context=_AUDIT_CONTEXT,
        )

    async with session_factory() as session:
        identity = await session.scalar(
            select(AuthIdentityModel).where(AuthIdentityModel.user_id == legacy_user_id)
        )
    assert authenticated is not None and authenticated.user_id == legacy_user_id
    assert identity is not None and identity.blind_index_key_version == 8


@pytest.mark.asyncio
async def test_overlapping_keyrings_concurrently_register_only_one_global_identity(
    lock_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = lock_database
    username = f"concurrent-{UUID(int=time_ns() % (1 << 128))}"
    expanded = _service(
        session_factory,
        blind_keys={1: b"a" * 32, 2: b"b" * 32},
    )
    contracted = _service(session_factory, blind_keys={2: b"b" * 32})

    results = await asyncio.gather(
        expanded.register(
            RegisterCommand(username, _PASSWORD, "Expanded"),
            audit_context=_AUDIT_CONTEXT,
        ),
        contracted.register(
            RegisterCommand(username.upper(), _PASSWORD, "Contracted"),
            audit_context=_AUDIT_CONTEXT,
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(result, RegisteredUser) for result in results) == 1
    assert sum(isinstance(result, IdentityConflictError) for result in results) == 1
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(UserModel)) == 1
        assert await session.scalar(select(func.count()).select_from(AuthIdentityModel)) == 1
