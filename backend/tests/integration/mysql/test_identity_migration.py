from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from alembic.config import Config
from sqlalchemy import column, delete, inspect, select, table, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from lawyer_agent.application.identity import AuditContext, IdentityService, LoginIdentifier
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.identity import IdentityKind, normalize_username
from lawyer_agent.infrastructure.persistence.repositories.identity import (
    SqlAlchemyIdentityUnitOfWork,
)
from lawyer_agent.infrastructure.security.blind_index import BlindIndexService
from lawyer_agent.infrastructure.security.cipher import SensitiveValueCipher
from lawyer_agent.infrastructure.security.passwords import Argon2PasswordHasher

pytestmark = [pytest.mark.integration, pytest.mark.mysql]

_PASSWORD = "correct horse battery staple"  # noqa: S105
_AUDIT_CONTEXT = AuditContext("trace-migration", b"i" * 32, b"u" * 32)
_CIPHER_KEY = b"c" * 32
_BLIND_KEY_V1 = b"b" * 32
_AUDIT_EVENTS = table("audit_events")
_PASSWORD_CREDENTIALS = table("password_credentials", column("user_id"))
_AUTH_IDENTITIES = table(
    "auth_identities",
    column("user_id"),
    column("blind_index_key_version"),
)
_USERS = table("users", column("id"))


def _alembic_config(mysql_url: URL) -> Config:
    backend_dir = Path(__file__).parents[3]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "alembic"))
    config.set_main_option(
        "sqlalchemy.url",
        mysql_url.render_as_string(hide_password=False).replace("%", "%%"),
    )
    return config


async def _insert_legacy_identity(mysql_url: URL, username: str) -> UUID:
    engine = create_async_engine(mysql_url)
    user_id = new_uuid7()
    identity_id = new_uuid7()
    now = datetime.now(UTC).replace(tzinfo=None)
    normalized = normalize_username(username)
    cipher = SensitiveValueCipher({7: _CIPHER_KEY}, active_key_version=7)
    blind = BlindIndexService({1: _BLIND_KEY_V1}, active_key_version=1)
    hasher = Argon2PasswordHasher()
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users (id, status, display_name, auth_version) "
                    "VALUES (:id, 'active', 'Legacy User', 1)"
                ),
                {"id": user_id.bytes},
            )
            await connection.execute(
                text(
                    "INSERT INTO auth_identities "
                    "(id, user_id, kind, provider, issuer, display_value, "
                    "subject_ciphertext, subject_blind_index, key_version, verified_at, status) "
                    "VALUES (:id, :user_id, 'username', 'local', 'local', :display_value, "
                    ":ciphertext, :blind_index, 7, :verified_at, 'active')"
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
                    "password_changed_at, status) "
                    "VALUES (:user_id, :password_hash, 'argon2id', :parameters, "
                    ":changed_at, 'active')"
                ),
                {
                    "user_id": user_id.bytes,
                    "password_hash": hasher.hash(_PASSWORD),
                    "parameters": json.dumps(hasher.parameters),
                    "changed_at": now,
                },
            )
    finally:
        await engine.dispose()
    return user_id


async def _insert_with_old_binary_shape(mysql_url: URL, username: str) -> UUID:
    engine = create_async_engine(mysql_url)
    user_id = new_uuid7()
    identity_id = new_uuid7()
    normalized = normalize_username(username)
    now = datetime.now(UTC).replace(tzinfo=None)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users (id, status, display_name, auth_version) "
                    "VALUES (:id, 'active', 'Old Binary', 1)"
                ),
                {"id": user_id.bytes},
            )
            await connection.execute(
                text(
                    "INSERT INTO auth_identities "
                    "(id, user_id, kind, provider, issuer, display_value, "
                    "subject_ciphertext, subject_blind_index, key_version, verified_at, status) "
                    "VALUES (:id, :user_id, 'username', 'local', 'local', :display_value, "
                    ":ciphertext, :blind_index, 7, :verified_at, 'active')"
                ),
                {
                    "id": identity_id.bytes,
                    "user_id": user_id.bytes,
                    "display_value": normalized,
                    "ciphertext": b"legacy-shape",
                    "blind_index": BlindIndexService(
                        {1: _BLIND_KEY_V1}, active_key_version=1
                    ).digest("identity:username", normalized),
                    "verified_at": now,
                },
            )
    finally:
        await engine.dispose()
    return user_id


async def _migration_state(mysql_url: URL, user_ids: tuple[UUID, ...]) -> dict[str, object]:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.connect() as connection:
            schema = await connection.run_sync(
                lambda sync_connection: {
                    "columns": inspect(sync_connection).get_columns("auth_identities"),
                    "uniques": inspect(sync_connection).get_unique_constraints(
                        "auth_identities"
                    ),
                    "indexes": inspect(sync_connection).get_indexes("auth_identities"),
                    "checks": inspect(sync_connection).get_check_constraints("auth_identities"),
                }
            )
            versions = (
                await connection.execute(
                    select(
                        _AUTH_IDENTITIES.c.user_id,
                        _AUTH_IDENTITIES.c.blind_index_key_version,
                    ).where(
                        _AUTH_IDENTITIES.c.user_id.in_(
                            tuple(user_id.bytes for user_id in user_ids)
                        )
                    )
                )
            ).all()
        return {**schema, "versions": {bytes(row[0]): row[1] for row in versions}}
    finally:
        await engine.dispose()


async def _authenticate_legacy(mysql_url: URL, username: str) -> UUID | None:
    engine = create_async_engine(mysql_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    service = IdentityService(
        uow_factory=lambda: SqlAlchemyIdentityUnitOfWork(session_factory),
        password_hasher=Argon2PasswordHasher(),
        cipher=SensitiveValueCipher({7: _CIPHER_KEY}, active_key_version=7),
        blind_index=BlindIndexService({1: _BLIND_KEY_V1}, active_key_version=1),
    )
    try:
        authenticated = await service.authenticate(
            LoginIdentifier(IdentityKind.USERNAME, username),
            _PASSWORD,
            audit_context=_AUDIT_CONTEXT,
        )
        return None if authenticated is None else authenticated.user_id
    finally:
        await engine.dispose()


async def _delete_users(mysql_url: URL, user_ids: tuple[UUID, ...]) -> None:
    engine = create_async_engine(mysql_url)
    encoded_ids = tuple(user_id.bytes for user_id in user_ids)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                delete(_AUDIT_EVENTS)
            )
            await connection.execute(
                delete(_PASSWORD_CREDENTIALS).where(
                    _PASSWORD_CREDENTIALS.c.user_id.in_(encoded_ids)
                )
            )
            await connection.execute(
                delete(_AUTH_IDENTITIES).where(_AUTH_IDENTITIES.c.user_id.in_(encoded_ids))
            )
            await connection.execute(
                delete(_USERS).where(_USERS.c.id.in_(encoded_ids))
            )
    finally:
        await engine.dispose()


def test_legacy_v1_identity_upgrades_with_online_default_and_authenticates(
    mysql_url: URL,
) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    command.downgrade(config, "20260901_01")
    username = f"legacy-{new_uuid7()}"
    legacy_user_id = asyncio.run(_insert_legacy_identity(mysql_url, username))
    old_binary_user_id: UUID | None = None
    try:
        command.upgrade(config, "20260901_02")
        old_binary_user_id = asyncio.run(
            _insert_with_old_binary_shape(mysql_url, f"old-binary-{new_uuid7()}")
        )
        state = asyncio.run(
            _migration_state(mysql_url, (legacy_user_id, old_binary_user_id))
        )
        version_column = next(
            column
            for column in state["columns"]
            if column["name"] == "blind_index_key_version"
        )
        unique_columns = {
            tuple(item["column_names"]): item["name"] for item in state["uniques"]
        }
        index_columns = {
            tuple(item["column_names"]): item["name"] for item in state["indexes"]
        }

        assert version_column["nullable"] is False
        assert str(version_column["default"]).strip("'") == "1"
        assert state["versions"] == {
            legacy_user_id.bytes: 1,
            old_binary_user_id.bytes: 1,
        }
        assert unique_columns[
            ("kind", "issuer", "blind_index_key_version", "subject_blind_index")
        ] == "uq_auth_identities_subject"
        assert index_columns[("blind_index_key_version",)] == (
            "ix_auth_identities_blind_key_version"
        )
        assert any(
            "blind_index_key_version" in str(check["sqltext"])
            and "32767" in str(check["sqltext"])
            for check in state["checks"]
        )
        assert asyncio.run(_authenticate_legacy(mysql_url, username)) == legacy_user_id
        command.downgrade(config, "20260901_01")
    finally:
        cleanup_ids = (
            (legacy_user_id,)
            if old_binary_user_id is None
            else (legacy_user_id, old_binary_user_id)
        )
        asyncio.run(_delete_users(mysql_url, cleanup_ids))
        command.upgrade(config, "head")


async def _insert_v2_identity(mysql_url: URL) -> UUID:
    engine = create_async_engine(mysql_url)
    user_id = new_uuid7()
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users (id, status, display_name, auth_version) "
                    "VALUES (:id, 'active', 'V2 User', 1)"
                ),
                {"id": user_id.bytes},
            )
            await connection.execute(
                text(
                    "INSERT INTO auth_identities "
                    "(id, user_id, kind, provider, issuer, display_value, "
                    "subject_ciphertext, subject_blind_index, key_version, "
                    "blind_index_key_version, verified_at, status) "
                    "VALUES (:id, :user_id, 'username', 'local', 'local', 'v2', "
                    ":ciphertext, :blind_index, 1, 2, :verified_at, 'active')"
                ),
                {
                    "id": new_uuid7().bytes,
                    "user_id": user_id.bytes,
                    "ciphertext": b"ciphertext",
                    "blind_index": b"v" * 32,
                    "verified_at": datetime.now(UTC).replace(tzinfo=None),
                },
            )
    finally:
        await engine.dispose()
    return user_id


def test_downgrade_refuses_non_v1_blind_index_rows(mysql_url: URL) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "20260901_02")
    user_id = asyncio.run(_insert_v2_identity(mysql_url))
    try:
        with pytest.raises(RuntimeError, match="non-v1 blind-index rows"):
            command.downgrade(config, "20260901_01")
    finally:
        asyncio.run(_delete_users(mysql_url, (user_id,)))
        command.upgrade(config, "head")
