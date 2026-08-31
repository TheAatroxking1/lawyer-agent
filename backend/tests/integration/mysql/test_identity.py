from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from time import time_ns
from uuid import UUID

import pytest
from alembic.config import Config
from argon2 import PasswordHasher
from sqlalchemy import delete, func, select
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
    BlindIndexKeyUnavailableError,
    IdentityConflictError,
    IdentityService,
    LoginIdentifier,
    NewIdentity,
    NewUser,
    RegisterCommand,
)
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.identity import IdentityKind
from lawyer_agent.infrastructure.persistence.models import (
    AuditEventModel,
    AuthIdentityModel,
    PasswordCredentialModel,
    UserModel,
)
from lawyer_agent.infrastructure.persistence.repositories.identity import (
    SqlAlchemyIdentityUnitOfWork,
)
from lawyer_agent.infrastructure.security.blind_index import BlindIndexService
from lawyer_agent.infrastructure.security.cipher import SensitiveValueCipher
from lawyer_agent.infrastructure.security.passwords import Argon2PasswordHasher

pytestmark = [pytest.mark.integration, pytest.mark.mysql]

_VALID_PASSWORD = "correct horse battery staple"  # noqa: S105
_AUDIT_CONTEXT = AuditContext("trace-integration", b"i" * 32, b"u" * 32)


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
        async with factory.begin() as session:
            await session.execute(delete(AuditEventModel))
            await session.execute(delete(PasswordCredentialModel))
            await session.execute(delete(AuthIdentityModel))
            await session.execute(delete(UserModel))
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
            uow_factory=lambda: SqlAlchemyIdentityUnitOfWork(session_factory),
            password_hasher=Argon2PasswordHasher(),
            cipher=cipher,
            blind_index=blind,
        )

    return build


def _rotated_service(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    cipher_keys: dict[int, bytes],
    cipher_active: int,
    blind_keys: dict[int, bytes],
    blind_active: int,
) -> IdentityService:
    return IdentityService(
        uow_factory=lambda: SqlAlchemyIdentityUnitOfWork(session_factory),
        password_hasher=Argon2PasswordHasher(),
        cipher=SensitiveValueCipher(cipher_keys, active_key_version=cipher_active),
        blind_index=BlindIndexService(blind_keys, active_key_version=blind_active),
    )


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
        ),
        audit_context=_AUDIT_CONTEXT,
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
    await service.register(
        RegisterCommand(username, "a secure password 1", "First"),
        audit_context=_AUDIT_CONTEXT,
    )

    async with session_factory() as session:
        before = await session.scalar(select(func.count()).select_from(UserModel))

    with pytest.raises(IdentityConflictError) as captured:
        await service.register(
            RegisterCommand(f"  {username.upper()}  ", "a secure password 2", "Second"),
            audit_context=_AUDIT_CONTEXT,
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
        RegisterCommand(username, password, "Authentication User"),
        audit_context=_AUDIT_CONTEXT,
    )
    service = service_factory()

    missing = await service.authenticate(
        LoginIdentifier(IdentityKind.USERNAME, _username("missing")),
        password,
        audit_context=_AUDIT_CONTEXT,
    )
    wrong = await service.authenticate(
        LoginIdentifier(IdentityKind.USERNAME, username),
        "wrong password value",
        audit_context=_AUDIT_CONTEXT,
    )

    async with session_factory.begin() as session:
        credential = await session.get(PasswordCredentialModel, registered.user_id)
        assert credential is not None
        credential.status = "locked"
    locked = await service.authenticate(
        LoginIdentifier(IdentityKind.USERNAME, username),
        password,
        audit_context=_AUDIT_CONTEXT,
    )

    async with session_factory.begin() as session:
        credential = await session.get(PasswordCredentialModel, registered.user_id)
        user = await session.get(UserModel, registered.user_id)
        assert credential is not None and user is not None
        credential.status = "active"
        user.status = "disabled"
    disabled = await service.authenticate(
        LoginIdentifier(IdentityKind.USERNAME, username),
        password,
        audit_context=_AUDIT_CONTEXT,
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
    registered = await service_factory().register(
        RegisterCommand(username, password, "Rehash"),
        audit_context=_AUDIT_CONTEXT,
    )
    old_hash = PasswordHasher(memory_cost=8192, time_cost=1, parallelism=1).hash(password)

    async with session_factory.begin() as session:
        credential = await session.get(PasswordCredentialModel, registered.user_id)
        assert credential is not None
        credential.password_hash = old_hash

    assert (
        await service_factory().authenticate(
            LoginIdentifier(IdentityKind.USERNAME, username),
            "wrong password value",
            audit_context=_AUDIT_CONTEXT,
        )
        is None
    )
    async with session_factory() as session:
        unchanged = await session.get(PasswordCredentialModel, registered.user_id)
        assert unchanged is not None and unchanged.password_hash == old_hash

    authenticated = await service_factory().authenticate(
        LoginIdentifier(IdentityKind.USERNAME, username),
        password,
        audit_context=_AUDIT_CONTEXT,
    )
    async with session_factory() as session:
        updated = await session.get(PasswordCredentialModel, registered.user_id)

    assert authenticated is not None and authenticated.user_id == registered.user_id
    assert updated is not None and updated.password_hash != old_hash
    assert updated.password_hash.startswith("$argon2id$v=19$m=65536,t=3,p=1$")


@pytest.mark.asyncio
async def test_blind_index_rotation_authenticates_reindexes_and_blocks_reregistration(
    identity_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = identity_database
    username = _username("blind-rotation")
    v1 = _rotated_service(
        session_factory,
        cipher_keys={7: b"c" * 32},
        cipher_active=7,
        blind_keys={1: b"i" * 32},
        blind_active=1,
    )
    registered = await v1.register(
        RegisterCommand(username, _VALID_PASSWORD, "Blind Rotation"),
        audit_context=_AUDIT_CONTEXT,
    )

    v2 = _rotated_service(
        session_factory,
        cipher_keys={7: b"c" * 32},
        cipher_active=7,
        blind_keys={1: b"i" * 32, 2: b"j" * 32},
        blind_active=2,
    )
    authenticated = await v2.authenticate(
        LoginIdentifier(IdentityKind.USERNAME, username),
        _VALID_PASSWORD,
        audit_context=_AUDIT_CONTEXT,
    )
    with pytest.raises(IdentityConflictError):
        await v2.register(
            RegisterCommand(username.upper(), _VALID_PASSWORD, "Duplicate"),
            audit_context=_AUDIT_CONTEXT,
        )

    async with session_factory() as session:
        identity = await session.scalar(
            select(AuthIdentityModel).where(AuthIdentityModel.user_id == registered.user_id)
        )
    assert authenticated is not None
    assert identity is not None and identity.blind_index_key_version == 2


@pytest.mark.asyncio
async def test_cipher_and_blind_index_rotate_independently(
    identity_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = identity_database
    username = _username("independent-rotation")
    service = _rotated_service(
        session_factory,
        cipher_keys={8: b"e" * 32},
        cipher_active=8,
        blind_keys={3: b"b" * 32},
        blind_active=3,
    )
    registered = await service.register(
        RegisterCommand(username, _VALID_PASSWORD, "Independent Rotation"),
        audit_context=_AUDIT_CONTEXT,
    )

    async with session_factory() as session:
        identity = await session.scalar(
            select(AuthIdentityModel).where(AuthIdentityModel.user_id == registered.user_id)
        )

    assert identity is not None
    assert identity.key_version == 8
    assert identity.blind_index_key_version == 3


@pytest.mark.asyncio
async def test_removing_blind_index_key_with_remaining_rows_fails_closed(
    identity_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = identity_database
    username = _username("removed-key")
    await _rotated_service(
        session_factory,
        cipher_keys={1: b"c" * 32},
        cipher_active=1,
        blind_keys={11: b"k" * 32},
        blind_active=11,
    ).register(
        RegisterCommand(username, _VALID_PASSWORD, "Removed Key"),
        audit_context=_AUDIT_CONTEXT,
    )
    without_old_key = _rotated_service(
        session_factory,
        cipher_keys={1: b"c" * 32},
        cipher_active=1,
        blind_keys={12: b"m" * 32},
        blind_active=12,
    )

    with pytest.raises(BlindIndexKeyUnavailableError):
        await without_old_key.register(
            RegisterCommand(username, _VALID_PASSWORD, "Must Not Duplicate"),
            audit_context=_AUDIT_CONTEXT,
        )
    with pytest.raises(BlindIndexKeyUnavailableError):
        await without_old_key.authenticate(
            LoginIdentifier(IdentityKind.USERNAME, username),
            _VALID_PASSWORD,
            audit_context=_AUDIT_CONTEXT,
        )


@pytest.mark.asyncio
async def test_only_target_identity_duplicate_is_mapped_to_identity_conflict(
    identity_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = identity_database
    now = datetime.now(UTC).replace(tzinfo=None)
    blind_value = b"z" * 32
    first_user_id = new_uuid7()
    async with SqlAlchemyIdentityUnitOfWork(session_factory) as uow:
        await uow.identities.add_user(NewUser(first_user_id, "active", "First", 1))
        await uow.identities.flush()
        await uow.identities.add_identity(
            NewIdentity(
                new_uuid7(),
                first_user_id,
                "username",
                "local",
                "local",
                "duplicate-key",
                b"ciphertext",
                blind_value,
                1,
                1,
                now,
                "active",
            )
        )
        await uow.identities.flush()

    second_user_id = new_uuid7()
    with pytest.raises(IdentityConflictError):
        async with SqlAlchemyIdentityUnitOfWork(session_factory) as uow:
            await uow.identities.add_user(NewUser(second_user_id, "active", "Second", 1))
            await uow.identities.flush()
            await uow.identities.add_identity(
                NewIdentity(
                    new_uuid7(),
                    second_user_id,
                    "username",
                    "local",
                    "local",
                    "duplicate-key",
                    b"ciphertext",
                    blind_value,
                    1,
                    1,
                    now,
                    "active",
                )
            )
            await uow.identities.flush()

    with pytest.raises(IntegrityError) as captured:
        async with SqlAlchemyIdentityUnitOfWork(session_factory) as uow:
            await uow.identities.add_identity(
                NewIdentity(
                    new_uuid7(),
                    new_uuid7(),
                    "username",
                    "local",
                    "local",
                    "foreign-key",
                    b"ciphertext",
                    b"f" * 32,
                    1,
                    1,
                    now,
                    "active",
                )
            )
            await uow.identities.flush()
    assert not isinstance(captured.value, IdentityConflictError)
