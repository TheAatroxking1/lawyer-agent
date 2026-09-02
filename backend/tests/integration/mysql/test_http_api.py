from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from lawyer_agent.config import Settings
from lawyer_agent.infrastructure.persistence.models import TenantModel, UserModel
from lawyer_agent.infrastructure.persistence.repositories.sessions import SessionRepository
from lawyer_agent.infrastructure.persistence.seed_authz import seed_authorization_catalog
from lawyer_agent.main import create_app

pytestmark = [pytest.mark.integration, pytest.mark.mysql]

_ORIGIN = "https://app.test"
_PASSWORD = "correct horse battery staple"  # noqa: S105
_TEST_DATABASE_PATTERN = re.compile(r"^lawyer_test_[a-f0-9]{32}$")


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

        assert asyncio.run(_row_counts(migrated_mysql_url)) == (1, 2)
        keys = asyncio.run(_redis_keys(redis_url, prefix))
        assert keys
        serialized_keys = b"\n".join(keys).lower()
        assert b"http-integration-lawyer" not in serialized_keys
    finally:
        keys = asyncio.run(_redis_keys(redis_url, prefix))
        asyncio.run(_delete_redis_keys(redis_url, keys))
