from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from time import time_ns
from uuid import UUID

import pytest
from alembic.config import Config
from argon2 import PasswordHasher
from sqlalchemy import func, select
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from alembic import command
from lawyer_agent.application.identity import (
    IdentityConflictError,
    IdentityService,
    LoginIdentifier,
    RegisterCommand,
)
from lawyer_agent.domain.identity import IdentityKind
from lawyer_agent.infrastructure.persistence.models import (
    AuditEventModel,
    AuthIdentityModel,
    PasswordCredentialModel,
    UserModel,
)
from lawyer_agent.infrastructure.persistence.uow import SqlAlchemyUnitOfWork
from lawyer_agent.infrastructure.security.blind_index import BlindIndexService
from lawyer_agent.infrastructure.security.cipher import SensitiveValueCipher
from lawyer_agent.infrastructure.security.passwords import Argon2PasswordHasher

pytestmark = [pytest.mark.integration, pytest.mark.mysql]

_VALID_PASSWORD = "correct horse battery staple"  # noqa: S105


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
async def identity_database(
    migrated_mysql_url: URL,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    engine = create_async_engine(migrated_mysql_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        yield engine, factory
    finally:
        await engine.dispose()


@pytest.fixture
def service_factory(
    identity_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> Callable[[], IdentityService]:
    _, session_factory = identity_database
    cipher = SensitiveValueCipher({1: b"d" * 32}, active_key_version=1)
    blind = BlindIndexService({1: b"i" * 32}, active_key_version=1)

    def build() -> IdentityService:
        return IdentityService(
            uow_factory=lambda: SqlAlchemyUnitOfWork(session_factory),
            password_hasher=Argon2PasswordHasher(),
            cipher=cipher,
            blind_index=blind,
        )

    return build


def _username(prefix: str) -> str:
    return f"{prefix}-{UUID(int=time_ns() % (1 << 128))}"


@pytest.mark.asyncio
async def test_register_writes_user_verified_identity_credential_and_audit_atomically(
    identity_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    service_factory: Callable[[], IdentityService],
) -> None:
    _, session_factory = identity_database
    username = _username("register")
    registered = await service_factory().register(
        RegisterCommand(
            username=f"  {username.upper()}  ",
            password=_VALID_PASSWORD,
            display_name="Synthetic Lawyer",
        )
    )

    async with session_factory() as session:
        user = await session.get(UserModel, registered.user_id)
        identity = await session.scalar(
            select(AuthIdentityModel).where(AuthIdentityModel.user_id == registered.user_id)
        )
        credential = await session.get(PasswordCredentialModel, registered.user_id)
        audit = await session.scalar(
            select(AuditEventModel).where(
                AuditEventModel.target_id == registered.user_id,
                AuditEventModel.action == "identity.register",
            )
        )

    assert user is not None and user.status == "active"
    assert identity is not None and identity.verified_at is not None
    assert identity.display_value == username.casefold()
    assert credential is not None and credential.password_hash.startswith("$argon2id$")
    assert audit is not None and audit.metadata_json is None


@pytest.mark.asyncio
async def test_normalized_global_identity_conflict_is_generic_and_rolls_back_user(
    identity_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    service_factory: Callable[[], IdentityService],
) -> None:
    _, session_factory = identity_database
    username = _username("conflict")
    service = service_factory()
    await service.register(RegisterCommand(username, "a secure password 1", "First"))

    async with session_factory() as session:
        before = await session.scalar(select(func.count()).select_from(UserModel))

    with pytest.raises(IdentityConflictError) as captured:
        await service.register(
            RegisterCommand(f"  {username.upper()}  ", "a secure password 2", "Second")
        )

    async with session_factory() as session:
        after = await session.scalar(select(func.count()).select_from(UserModel))

    assert captured.value.code == "identity_conflict"
    assert username not in str(captured.value)
    assert after == before


@pytest.mark.asyncio
async def test_authentication_returns_the_same_failure_for_missing_wrong_locked_and_disabled(
    identity_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    service_factory: Callable[[], IdentityService],
) -> None:
    _, session_factory = identity_database
    username = _username("authenticate")
    password = _VALID_PASSWORD
    registered = await service_factory().register(
        RegisterCommand(username, password, "Authentication User")
    )
    service = service_factory()

    missing = await service.authenticate(
        LoginIdentifier(IdentityKind.USERNAME, _username("missing")),
        password,
    )
    wrong = await service.authenticate(
        LoginIdentifier(IdentityKind.USERNAME, username),
        "wrong password value",
    )

    async with session_factory.begin() as session:
        credential = await session.get(PasswordCredentialModel, registered.user_id)
        assert credential is not None
        credential.status = "locked"
    locked = await service.authenticate(LoginIdentifier(IdentityKind.USERNAME, username), password)

    async with session_factory.begin() as session:
        credential = await session.get(PasswordCredentialModel, registered.user_id)
        user = await session.get(UserModel, registered.user_id)
        assert credential is not None and user is not None
        credential.status = "active"
        user.status = "disabled"
    disabled = await service.authenticate(
        LoginIdentifier(IdentityKind.USERNAME, username),
        password,
    )

    assert missing is wrong is locked is disabled is None


@pytest.mark.asyncio
async def test_password_rehash_occurs_only_after_successful_authentication(
    identity_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    service_factory: Callable[[], IdentityService],
) -> None:
    _, session_factory = identity_database
    username = _username("rehash")
    password = _VALID_PASSWORD
    registered = await service_factory().register(RegisterCommand(username, password, "Rehash"))
    old_hash = PasswordHasher(memory_cost=8192, time_cost=1, parallelism=1).hash(password)

    async with session_factory.begin() as session:
        credential = await session.get(PasswordCredentialModel, registered.user_id)
        assert credential is not None
        credential.password_hash = old_hash

    assert (
        await service_factory().authenticate(
            LoginIdentifier(IdentityKind.USERNAME, username),
            "wrong password value",
        )
        is None
    )
    async with session_factory() as session:
        unchanged = await session.get(PasswordCredentialModel, registered.user_id)
        assert unchanged is not None and unchanged.password_hash == old_hash

    authenticated = await service_factory().authenticate(
        LoginIdentifier(IdentityKind.USERNAME, username),
        password,
    )
    async with session_factory() as session:
        updated = await session.get(PasswordCredentialModel, registered.user_id)

    assert authenticated is not None and authenticated.user_id == registered.user_id
    assert updated is not None and updated.password_hash != old_hash
    assert updated.password_hash.startswith("$argon2id$v=19$m=65536,t=3,p=1$")
