from __future__ import annotations

import asyncio
import json
import os
import re
from base64 import b64encode
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import jwt
import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from lawyer_agent.config import Settings
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.identity import normalize_username
from lawyer_agent.domain.sessions import Audience
from lawyer_agent.infrastructure.persistence.models import (
    AuditEventModel,
    AuthIdentityModel,
    IdempotencyRecordModel,
    PasswordCredentialModel,
    PlatformRoleAssignmentModel,
    PlatformRoleModel,
    TenantInvitationModel,
    TenantModel,
    TenantRoleModel,
    UserModel,
)
from lawyer_agent.infrastructure.persistence.repositories.sessions import SessionRepository
from lawyer_agent.infrastructure.persistence.seed_authz import seed_authorization_catalog
from lawyer_agent.infrastructure.providers.development import TestInvitationDeliveryAdapter
from lawyer_agent.infrastructure.security.blind_index import BlindIndexService
from lawyer_agent.infrastructure.security.cipher import SensitiveValueCipher
from lawyer_agent.infrastructure.security.passwords import Argon2PasswordHasher
from lawyer_agent.main import create_app

pytestmark = [pytest.mark.integration, pytest.mark.mysql]

_ORIGIN = "https://app.test"
_PASSWORD = "correct horse battery staple"  # noqa: S105
_TEST_DATABASE_PATTERN = re.compile(r"^lawyer_test_[a-f0-9]{32}$")
_CIPHER_V7 = bytes([71]) * 32
_CIPHER_V8 = bytes([72]) * 32
_BLIND_V7 = bytes([73]) * 32
_BLIND_V8 = bytes([74]) * 32


def _encoded(value: bytes) -> str:
    return b64encode(value).decode("ascii")


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
    database_name = f"lawyer_test_{uuid4().hex}"
    if not _TEST_DATABASE_PATTERN.fullmatch(database_name):
        raise RuntimeError("refusing to manage an unexpected database name")
    isolated_url = mysql_url.set(database=database_name)
    asyncio.run(_execute_admin(mysql_url, f"CREATE DATABASE `{database_name}`"))
    try:
        command.upgrade(_alembic_config(isolated_url), "head")
        asyncio.run(_seed_catalog(isolated_url))
        yield isolated_url
    finally:
        asyncio.run(_execute_admin(mysql_url, f"DROP DATABASE `{database_name}`"))


async def _execute_admin(mysql_url: URL, statement: str) -> None:
    engine = create_async_engine(mysql_url.set(database="mysql"))
    try:
        async with engine.begin() as connection:
            await connection.execute(text(statement))
    finally:
        await engine.dispose()


async def _seed_catalog(mysql_url: URL) -> None:
    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await seed_authorization_catalog(session)
    finally:
        await engine.dispose()


async def _redis_available(url: str) -> bool:
    client = Redis.from_url(
        url,
        socket_connect_timeout=1.0,
        socket_timeout=1.0,
        retry_on_timeout=False,
    )
    try:
        return bool(await client.ping())
    except Exception:
        return False
    finally:
        await client.aclose()


async def _redis_keys(url: str, prefix: str) -> list[bytes]:
    client = Redis.from_url(url, decode_responses=False)
    try:
        return [key async for key in client.scan_iter(match=f"{prefix}*", count=100)]
    finally:
        await client.aclose()


async def _delete_redis_keys(url: str, keys: list[bytes]) -> None:
    client = Redis.from_url(url, decode_responses=False)
    try:
        if keys:
            await client.delete(*keys)
    finally:
        await client.aclose()


async def _row_counts(mysql_url: URL) -> tuple[int, int]:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.connect() as connection:
            users = await connection.scalar(select(func.count()).select_from(UserModel))
            tenants = await connection.scalar(select(func.count()).select_from(TenantModel))
        return int(users or 0), int(tenants or 0)
    finally:
        await engine.dispose()


async def _tenant_role_id(mysql_url: URL, tenant_id: str, code: str) -> UUID:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.connect() as connection:
            role_id = await connection.scalar(
                select(TenantRoleModel.id).where(
                    TenantRoleModel.tenant_id == UUID(tenant_id),
                    TenantRoleModel.code == code,
                )
            )
        assert role_id is not None
        return role_id
    finally:
        await engine.dispose()


async def _invitation_side_effect_counts(mysql_url: URL) -> tuple[int, int, int]:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.connect() as connection:
            values = []
            for model in (TenantInvitationModel, IdempotencyRecordModel, AuditEventModel):
                value = await connection.scalar(select(func.count()).select_from(model))
                values.append(int(value or 0))
        return values[0], values[1], values[2]
    finally:
        await engine.dispose()


async def _insert_verified_email(
    mysql_url: URL,
    *,
    user_id: str,
    email: str,
) -> None:
    engine = create_async_engine(mysql_url)
    identity_id = new_uuid7()
    cipher = SensitiveValueCipher(
        {7: _CIPHER_V7, 8: _CIPHER_V8},
        active_key_version=8,
    )
    blind = BlindIndexService(
        {7: _BLIND_V7, 8: _BLIND_V8},
        active_key_version=8,
    )
    try:
        async with engine.begin() as connection:
            await connection.execute(
                AuthIdentityModel.__table__.insert().values(
                    id=identity_id,
                    user_id=UUID(user_id),
                    kind="email",
                    provider="email",
                    issuer="email",
                    display_value=None,
                    subject_ciphertext=cipher.encrypt(
                        email,
                        aad=f"auth_identity:{identity_id}:subject".encode("ascii"),
                    ),
                    subject_blind_index=blind.digest("identity:email", email),
                    key_version=8,
                    blind_index_key_version=8,
                    verified_at=datetime.now(UTC).replace(tzinfo=None),
                    status="active",
                )
            )
    finally:
        await engine.dispose()


async def _insert_legacy_v7_user(mysql_url: URL, username: str) -> UUID:
    engine = create_async_engine(mysql_url)
    user_id = new_uuid7()
    identity_id = new_uuid7()
    normalized = normalize_username(username)
    cipher = SensitiveValueCipher({7: _CIPHER_V7}, active_key_version=7)
    blind = BlindIndexService({7: _BLIND_V7}, active_key_version=7)
    hasher = Argon2PasswordHasher()
    now = datetime.now(UTC).replace(tzinfo=None)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                UserModel.__table__.insert().values(
                    id=user_id,
                    status="active",
                    display_name="Legacy HTTP User",
                    auth_version=1,
                    version=1,
                )
            )
            await connection.execute(
                AuthIdentityModel.__table__.insert().values(
                    id=identity_id,
                    user_id=user_id,
                    kind="username",
                    provider="local",
                    issuer="local",
                    display_value=normalized,
                    subject_ciphertext=cipher.encrypt(
                        normalized,
                        aad=f"auth_identity:{identity_id}:subject".encode("ascii"),
                    ),
                    subject_blind_index=blind.digest("identity:username", normalized),
                    key_version=7,
                    blind_index_key_version=7,
                    verified_at=now,
                    status="active",
                )
            )
            await connection.execute(
                PasswordCredentialModel.__table__.insert().values(
                    user_id=user_id,
                    password_hash=hasher.hash(_PASSWORD),
                    algorithm="argon2id",
                    parameters_json=json.dumps(hasher.parameters),
                    password_changed_at=now,
                    failed_attempt_count=0,
                    locked_until=None,
                    status="active",
                    version=1,
                )
            )
        return user_id
    finally:
        await engine.dispose()


async def _identity_key_versions(mysql_url: URL, user_id: UUID) -> tuple[int, int | None]:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    select(
                        AuthIdentityModel.key_version,
                        AuthIdentityModel.blind_index_key_version,
                    ).where(AuthIdentityModel.user_id == user_id)
                )
            ).one()
        return int(row.key_version), row.blind_index_key_version
    finally:
        await engine.dispose()


async def _assign_platform_role(
    mysql_url: URL,
    *,
    user_id: str,
    role_code: str,
) -> None:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.begin() as connection:
            role_id = await connection.scalar(
                select(PlatformRoleModel.id).where(PlatformRoleModel.code == role_code)
            )
            assert role_id is not None
            await connection.execute(
                PlatformRoleAssignmentModel.__table__.insert().values(
                    id=new_uuid7(),
                    user_id=UUID(user_id),
                    platform_role_id=role_id,
                    assigned_by_user_id=None,
                    status="active",
                    expires_at=None,
                    revoked_at=None,
                    revocation_reason=None,
                )
            )
    finally:
        await engine.dispose()


async def _activate_tenant(mysql_url: URL, tenant_id: str) -> None:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                update(TenantModel)
                .where(TenantModel.id == UUID(tenant_id))
                .values(status="active", review_status="approved")
            )
    finally:
        await engine.dispose()


async def _load_tenant_context(
    mysql_url: URL,
    *,
    user_id: str,
    tenant_id: str,
    membership_id: str,
) -> object:
    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            return await SessionRepository(session).get_tenant_context(
                user_id=UUID(user_id),
                tenant_id=UUID(tenant_id),
                membership_id=UUID(membership_id),
            )
    finally:
        await engine.dispose()


def test_real_http_lifespan_mysql_redis_and_cross_tenant_isolation(
    migrated_mysql_url: URL,
) -> None:
    redis_url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    if not asyncio.run(_redis_available(redis_url)):
        pytest.skip("test Redis unavailable")
    prefix = f"lawyer-test-http:{uuid4().hex}:"
    settings = Settings(
        environment="test",
        secret_key="h" * 32,
        database_url=migrated_mysql_url.render_as_string(hide_password=False),
        redis_url=redis_url,
        redis_key_prefix=prefix,
        trusted_origins=(_ORIGIN,),
        cookie_secure=True,
        data_encryption_key_ring={7: _encoded(_CIPHER_V7), 8: _encoded(_CIPHER_V8)},
        data_encryption_active_key_version=8,
        blind_index_key_ring={7: _encoded(_BLIND_V7), 8: _encoded(_BLIND_V8)},
        blind_index_active_key_version=8,
        blind_index_rollout_phase="rotation-ready",
        blind_index_legacy_key_version=7,
        blind_index_legacy_writers_drained=True,
    )
    client = TestClient(create_app(settings), base_url="https://testserver")
    try:
        with client:
            registered = client.post(
                "/api/v1/auth/register",
                headers={"Origin": _ORIGIN},
                json={
                    "username": "http-integration-lawyer",
                    "password": _PASSWORD,
                    "display_name": "HTTP 集成律师",
                },
            )
            assert registered.status_code == 201, registered.text
            account_token = registered.json()["access_token"]
            csrf = client.cookies.get("__Host-lawyer_csrf")
            assert csrf is not None

            account = client.get(
                "/api/v1/me",
                headers={"Authorization": f"Bearer {account_token}"},
            )
            assert account.status_code == 200, account.text

            refreshed = client.post(
                "/api/v1/auth/refresh",
                headers={"Origin": _ORIGIN, "X-CSRF-Token": csrf},
            )
            assert refreshed.status_code == 200, refreshed.text
            account_token = refreshed.json()["access_token"]

            tenant_ids: list[str] = []
            membership_ids: list[str] = []
            for index in (1, 2):
                created = client.post(
                    "/api/v1/tenants",
                    headers={
                        "Authorization": f"Bearer {account_token}",
                        "Idempotency-Key": f"http-create-tenant-{index}",
                    },
                    json={"name": f"集成租户{index}", "tenant_type": "law_firm"},
                )
                assert created.status_code == 201, created.text
                tenant_ids.append(created.json()["tenant"]["id"])
                membership_ids.append(created.json()["owner_membership"]["id"])

            asyncio.run(_activate_tenant(migrated_mysql_url, tenant_ids[0]))
            tenant_context = asyncio.run(
                _load_tenant_context(
                    migrated_mysql_url,
                    user_id=account.json()["id"],
                    tenant_id=tenant_ids[0],
                    membership_id=membership_ids[0],
                )
            )
            assert tenant_context is not None
            assert tenant_context.tenant_status == "active"
            assert tenant_context.membership_status == "active"
            logged_in = client.post(
                "/api/v1/auth/login",
                headers={"Origin": _ORIGIN},
                json={
                    "kind": "username",
                    "identifier": "http-integration-lawyer",
                    "password": _PASSWORD,
                },
            )
            assert logged_in.status_code == 200, logged_in.text
            account_token = logged_in.json()["access_token"]
            switched = client.post(
                "/api/v1/auth/switch-tenant",
                headers={
                    "Authorization": f"Bearer {account_token}",
                    "Origin": _ORIGIN,
                },
                json={
                    "tenant_id": tenant_ids[0],
                    "membership_id": membership_ids[0],
                },
            )
            assert switched.status_code == 200, switched.text
            tenant_token = switched.json()["access_token"]

            cross_tenant = client.get(
                f"/api/v1/tenants/{tenant_ids[1]}",
                headers={"Authorization": f"Bearer {tenant_token}"},
            )
            assert cross_tenant.status_code == 404
            assert cross_tenant.json()["code"] == "tenant_resource_not_found"

            role_id = asyncio.run(
                _tenant_role_id(migrated_mysql_url, tenant_ids[0], "assistant")
            )
            invitee_email = "http-invitee@example.cn"
            invited = client.post(
                f"/api/v1/tenants/{tenant_ids[0]}/invitations",
                headers={
                    "Authorization": f"Bearer {tenant_token}",
                    "Idempotency-Key": "http-invite-delivery-1",
                },
                json={
                    "target_kind": "email",
                    "target": invitee_email,
                    "role_ids": [str(role_id)],
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                },
            )
            assert invited.status_code == 201, invited.text
            assert "token" not in invited.text.lower()
            invitation_id = UUID(invited.json()["invitation_id"])
            delivery = client.app.state.services.invitation_delivery.require()
            assert isinstance(delivery, TestInvitationDeliveryAdapter)
            invitation_token = delivery.take(invitation_id)
            assert invitation_token is not None
            assert delivery.take(invitation_id) is None

            invitee = client.post(
                "/api/v1/auth/register",
                headers={"Origin": _ORIGIN},
                json={
                    "username": "http-integration-invitee",
                    "password": _PASSWORD,
                    "display_name": "HTTP 集成受邀人",
                },
            )
            assert invitee.status_code == 201, invitee.text
            invitee_token = invitee.json()["access_token"]
            invitee_account = client.get(
                "/api/v1/me",
                headers={"Authorization": f"Bearer {invitee_token}"},
            )
            assert invitee_account.status_code == 200, invitee_account.text
            asyncio.run(
                _insert_verified_email(
                    migrated_mysql_url,
                    user_id=invitee_account.json()["id"],
                    email=invitee_email,
                )
            )
            accepted = client.post(
                "/api/v1/invitations/accept",
                headers={
                    "Authorization": f"Bearer {invitee_token}",
                    "Idempotency-Key": "http-accept-delivery-1",
                },
                json={"token": invitation_token},
            )
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["tenant_id"] == tenant_ids[0]

        assert asyncio.run(_row_counts(migrated_mysql_url)) == (2, 2)
        keys = asyncio.run(_redis_keys(redis_url, prefix))
        assert keys
        serialized_keys = b"\n".join(keys).lower()
        assert b"http-integration-lawyer" not in serialized_keys
    finally:
        keys = asyncio.run(_redis_keys(redis_url, prefix))
        asyncio.run(_delete_redis_keys(redis_url, keys))


def test_unconfigured_invitation_provider_fails_before_any_database_side_effect(
    migrated_mysql_url: URL,
) -> None:
    redis_url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    if not asyncio.run(_redis_available(redis_url)):
        pytest.skip("test Redis unavailable")
    prefix = f"lawyer-test-no-delivery:{uuid4().hex}:"
    settings = Settings(
        environment="development",
        secret_key="d" * 32,
        database_url=migrated_mysql_url.render_as_string(hide_password=False),
        redis_url=redis_url,
        redis_key_prefix=prefix,
        trusted_origins=(_ORIGIN,),
        cookie_secure=True,
        data_encryption_key_ring={7: _encoded(_CIPHER_V7), 8: _encoded(_CIPHER_V8)},
        data_encryption_active_key_version=8,
        blind_index_key_ring={7: _encoded(_BLIND_V7), 8: _encoded(_BLIND_V8)},
        blind_index_active_key_version=8,
        blind_index_rollout_phase="rotation-ready",
        blind_index_legacy_key_version=7,
        blind_index_legacy_writers_drained=True,
    )
    client = TestClient(create_app(settings), base_url="https://testserver")
    try:
        with client:
            registered = client.post(
                "/api/v1/auth/register",
                headers={"Origin": _ORIGIN},
                json={
                    "username": "http-no-delivery-owner",
                    "password": _PASSWORD,
                    "display_name": "HTTP 无投递所有者",
                },
            )
            assert registered.status_code == 201, registered.text
            account_token = registered.json()["access_token"]
            created = client.post(
                "/api/v1/tenants",
                headers={
                    "Authorization": f"Bearer {account_token}",
                    "Idempotency-Key": "http-no-delivery-tenant",
                },
                json={"name": "无投递租户", "tenant_type": "law_firm"},
            )
            assert created.status_code == 201, created.text
            tenant_id = created.json()["tenant"]["id"]
            membership_id = created.json()["owner_membership"]["id"]
            asyncio.run(_activate_tenant(migrated_mysql_url, tenant_id))
            logged_in = client.post(
                "/api/v1/auth/login",
                headers={"Origin": _ORIGIN},
                json={
                    "kind": "username",
                    "identifier": "http-no-delivery-owner",
                    "password": _PASSWORD,
                },
            )
            assert logged_in.status_code == 200, logged_in.text
            switched = client.post(
                "/api/v1/auth/switch-tenant",
                headers={
                    "Authorization": f"Bearer {logged_in.json()['access_token']}",
                    "Origin": _ORIGIN,
                },
                json={"tenant_id": tenant_id, "membership_id": membership_id},
            )
            assert switched.status_code == 200, switched.text
            before = asyncio.run(_invitation_side_effect_counts(migrated_mysql_url))
            unavailable = client.post(
                f"/api/v1/tenants/{tenant_id}/invitations",
                headers={
                    "Authorization": f"Bearer {switched.json()['access_token']}",
                    "Idempotency-Key": "must-not-be-reserved",
                },
                json={
                    "target_kind": "email",
                    "target": "not-delivered@example.cn",
                    "role_ids": [str(new_uuid7())],
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                },
            )
            after = asyncio.run(_invitation_side_effect_counts(migrated_mysql_url))

            assert unavailable.status_code == 503
            assert unavailable.json()["code"] == "invitation_delivery_unavailable"
            assert client.app.state.services.invitation_delivery.available is False
            assert after == before
    finally:
        keys = asyncio.run(_redis_keys(redis_url, prefix))
        asyncio.run(_delete_redis_keys(redis_url, keys))


def test_default_lifespan_rotation_ring_reads_v7_and_lazily_reindexes_to_v8(
    migrated_mysql_url: URL,
) -> None:
    redis_url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    if not asyncio.run(_redis_available(redis_url)):
        pytest.skip("test Redis unavailable")
    username = f"legacy-http-{uuid4().hex}"
    user_id = asyncio.run(_insert_legacy_v7_user(migrated_mysql_url, username))
    prefix = f"lawyer-test-rotation:{uuid4().hex}:"
    settings = Settings(
        environment="test",
        secret_key="r" * 32,
        database_url=migrated_mysql_url.render_as_string(hide_password=False),
        redis_url=redis_url,
        redis_key_prefix=prefix,
        trusted_origins=(_ORIGIN,),
        cookie_secure=True,
        data_encryption_key_ring={7: _encoded(_CIPHER_V7), 8: _encoded(_CIPHER_V8)},
        data_encryption_active_key_version=8,
        blind_index_key_ring={7: _encoded(_BLIND_V7), 8: _encoded(_BLIND_V8)},
        blind_index_active_key_version=8,
        blind_index_rollout_phase="rotation-ready",
        blind_index_legacy_key_version=7,
        blind_index_legacy_writers_drained=True,
    )
    client = TestClient(create_app(settings), base_url="https://testserver")
    try:
        with client:
            logged_in = client.post(
                "/api/v1/auth/login",
                headers={"Origin": _ORIGIN},
                json={
                    "kind": "username",
                    "identifier": username,
                    "password": _PASSWORD,
                },
            )
            assert logged_in.status_code == 200, logged_in.text
        assert asyncio.run(_identity_key_versions(migrated_mysql_url, user_id)) == (7, 8)
    finally:
        keys = asyncio.run(_redis_keys(redis_url, prefix))
        asyncio.run(_delete_redis_keys(redis_url, keys))


def test_real_http_read_only_platform_exchange_is_authoritative_and_not_step_up(
    migrated_mysql_url: URL,
) -> None:
    redis_url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    if not asyncio.run(_redis_available(redis_url)):
        pytest.skip("test Redis unavailable")
    prefix = f"lawyer-test-platform-read:{uuid4().hex}:"
    settings = Settings(
        environment="test",
        secret_key="p" * 32,
        database_url=migrated_mysql_url.render_as_string(hide_password=False),
        redis_url=redis_url,
        redis_key_prefix=prefix,
        trusted_origins=(_ORIGIN,),
        cookie_secure=True,
        data_encryption_key_ring={7: _encoded(_CIPHER_V7), 8: _encoded(_CIPHER_V8)},
        data_encryption_active_key_version=8,
        blind_index_key_ring={7: _encoded(_BLIND_V7), 8: _encoded(_BLIND_V8)},
        blind_index_active_key_version=8,
        blind_index_rollout_phase="rotation-ready",
        blind_index_legacy_key_version=7,
        blind_index_legacy_writers_drained=True,
    )
    client = TestClient(create_app(settings), base_url="https://testserver")
    body = {
        "kind": "username",
        "identifier": "http-security-auditor",
        "password": _PASSWORD,
        "action": "tenant_application.read",
    }
    try:
        with client:
            registered = client.post(
                "/api/v1/auth/register",
                headers={"Origin": _ORIGIN},
                json={
                    "username": body["identifier"],
                    "password": _PASSWORD,
                    "display_name": "HTTP 安全审计员",
                },
            )
            assert registered.status_code == 201, registered.text
            account_token = registered.json()["access_token"]
            account = client.get(
                "/api/v1/me",
                headers={"Authorization": f"Bearer {account_token}"},
            )
            assert account.status_code == 200, account.text

            ordinary = client.post(
                "/api/v1/auth/reauth",
                headers={
                    "Authorization": f"Bearer {account_token}",
                    "Origin": _ORIGIN,
                },
                json=body,
            )
            assert ordinary.status_code == 403

            asyncio.run(
                _assign_platform_role(
                    migrated_mysql_url,
                    user_id=account.json()["id"],
                    role_code="security_auditor",
                )
            )
            exchanged = client.post(
                "/api/v1/auth/reauth",
                headers={
                    "Authorization": f"Bearer {account_token}",
                    "Origin": _ORIGIN,
                },
                json=body,
            )
            assert exchanged.status_code == 200, exchanged.text
            assert set(exchanged.json()) == {"access_token", "token_type"}
            platform_token = exchanged.json()["access_token"]
            payload = jwt.decode(
                platform_token,
                options={"verify_signature": False},
                algorithms=["EdDSA"],
            )
            assert payload["aud"] == Audience.PLATFORM.value
            assert payload["exp"] - payload["iat"] <= 300
            assert "tenant_id" not in payload
            assert "membership_id" not in payload

            listed = client.get(
                "/api/v1/platform/tenant-applications",
                headers={"Authorization": f"Bearer {platform_token}"},
            )
            assert listed.status_code == 200, listed.text

            denied_review = client.post(
                f"/api/v1/platform/tenant-applications/{new_uuid7()}/approve",
                headers={
                    "Authorization": f"Bearer {platform_token}",
                    "Idempotency-Key": "auditor-must-not-review",
                    "X-Step-Up-Grant": "G" * 43,
                },
                json={"reason_code": "must_be_denied"},
            )
            assert denied_review.status_code == 403
            assert denied_review.json()["code"] == "authorization_denied"

            review_exchange = client.post(
                "/api/v1/auth/reauth",
                headers={
                    "Authorization": f"Bearer {account_token}",
                    "Origin": _ORIGIN,
                },
                json={
                    **body,
                    "tenant_id": str(new_uuid7()),
                    "action": "tenant_application.review:approve",
                },
            )
            assert review_exchange.status_code == 403
            assert review_exchange.json()["code"] == "authorization_denied"
    finally:
        keys = asyncio.run(_redis_keys(redis_url, prefix))
        asyncio.run(_delete_redis_keys(redis_url, keys))
