from __future__ import annotations

import asyncio
import os
import re
from base64 import b64encode
from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from sqlalchemy import text, update
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from lawyer_agent.config import Settings
from lawyer_agent.infrastructure.persistence.models import TenantModel
from lawyer_agent.infrastructure.persistence.seed_authz import seed_authorization_catalog
from lawyer_agent.main import create_app

pytestmark = [pytest.mark.integration, pytest.mark.mysql]

_ORIGIN = "https://app.test"
_PASSWORD = "correct horse battery staple"  # noqa: S105
_TEST_DATABASE_PATTERN = re.compile(r"^lawyer_test_[a-f0-9]{32}$")
_CIPHER_KEY = bytes([71]) * 32
_BLIND_KEY = bytes([73]) * 32


def _encoded(value: bytes) -> str:
    return b64encode(value).decode("ascii")


def _alembic_config(mysql_url: URL) -> Config:
    backend_dir = __import__("pathlib").Path(__file__).parents[3]
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
        url, socket_connect_timeout=1.0, socket_timeout=1.0, retry_on_timeout=False
    )
    try:
        return bool(await client.ping())
    finally:
        await client.aclose()


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


async def _matter_count(mysql_url: URL, tenant_id: UUID) -> int:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.connect() as connection:
            value = await connection.scalar(
                text(
                    "SELECT COUNT(*) FROM tenant_matters "
                    "WHERE tenant_id = :t"
                ).bindparams(t=tenant_id.bytes)
            )
            return int(value or 0)
    finally:
        await engine.dispose()


def test_matter_creation_is_idempotent_over_real_mysql(
    migrated_mysql_url: URL,
) -> None:
    redis_url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    if not asyncio.run(_redis_available(redis_url)):
        pytest.skip("test Redis unavailable")
    prefix = f"lawyer-test-idem-http:{uuid4().hex}:"
    settings = Settings(
        environment="test",
        secret_key="h" * 32,
        database_url=migrated_mysql_url.render_as_string(hide_password=False),
        redis_url=redis_url,
        redis_key_prefix=prefix,
        trusted_origins=(_ORIGIN,),
        cookie_secure=True,
        data_encryption_key_ring={7: _encoded(_CIPHER_KEY)},
        data_encryption_active_key_version=7,
        blind_index_key_ring={7: _encoded(_BLIND_KEY)},
        blind_index_active_key_version=7,
        blind_index_rollout_phase="legacy-compatible",
        blind_index_legacy_key_version=7,
        blind_index_legacy_writers_drained=False,
    )
    client = TestClient(create_app(settings), base_url="https://testserver")
    with client:
        registered = client.post(
            "/api/v1/auth/register",
            headers={"Origin": _ORIGIN},
            json={
                "username": "idem-http-owner",
                "password": _PASSWORD,
                "display_name": "幂等用户",
            },
        )
        assert registered.status_code == 201, registered.text
        account_token = registered.json()["access_token"]
        created = client.post(
            "/api/v1/tenants",
            headers={
                "Authorization": f"Bearer {account_token}",
                "Idempotency-Key": "idem-http-tenant-1",
            },
            json={"name": "幂等租户", "tenant_type": "law_firm"},
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
                "identifier": "idem-http-owner",
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
        token = switched.json()["access_token"]
        base = f"/api/v1/tenants/{tenant_id}"
        body = {"title": "租赁合同审查", "kind": "contract_review"}
        headers = {
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": "matter-create-0000000000001",
        }

        first = client.post(f"{base}/matters", headers=headers, json=body)
        assert first.status_code == 201, first.text
        first_id = first.json()["id"]

        # Replay with the same key + body returns the same matter id (no new row).
        replay = client.post(f"{base}/matters", headers=headers, json=body)
        assert replay.status_code == 201, replay.text
        assert replay.json()["id"] == first_id
        assert asyncio.run(_matter_count(migrated_mysql_url, UUID(tenant_id))) == 1

        # Same key with a different body is a conflict.
        conflict = client.post(
            f"{base}/matters",
            headers=headers,
            json={"title": "不同标题", "kind": "litigation"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "idempotency_conflict"

        # A different key creates a second matter (no cross-key dedup).
        second = client.post(
            f"{base}/matters",
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": "matter-create-0000000000002",
            },
            json={"title": "第二事项", "kind": "other"},
        )
        assert second.status_code == 201, second.text
        assert second.json()["id"] != first_id
        assert asyncio.run(_matter_count(migrated_mysql_url, UUID(tenant_id))) == 2
