from __future__ import annotations

import asyncio
import os
import re
from base64 import b64encode
from collections.abc import Iterator
from uuid import uuid4

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from lawyer_agent.config import Settings
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


def _client(mysql_url: URL, *, label: str) -> TestClient:
    redis_url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    settings = Settings(
        environment="test",
        secret_key="h" * 32,
        database_url=mysql_url.render_as_string(hide_password=False),
        redis_url=redis_url,
        redis_key_prefix=f"{label}:{uuid4().hex}:",
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
    return TestClient(create_app(settings), base_url="https://testserver")


def _register(client: TestClient, username: str) -> str:
    response = client.post(
        "/api/v1/auth/register",
        headers={"Origin": _ORIGIN},
        json={
            "username": username,
            "password": _PASSWORD,
            "display_name": "Agent 网关用户",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["access_token"]


def test_agent_gateway_requires_authentication(migrated_mysql_url: URL) -> None:
    redis_url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    if not asyncio.run(_redis_available(redis_url)):
        pytest.skip("test Redis unavailable")
    client = _client(migrated_mysql_url, label="lawyer-test-agent-unauth")
    with client:
        assert client.get("/api/v1/platform/agent/tools").status_code == 401
        assert (
            client.post(
                "/api/v1/platform/agent/tools/call",
                json={"tool": "meta.list_tools"},
            ).status_code
            == 401
        )


def test_agent_gateway_lists_and_calls_tools_over_real_mysql(
    migrated_mysql_url: URL,
) -> None:
    redis_url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    if not asyncio.run(_redis_available(redis_url)):
        pytest.skip("test Redis unavailable")
    client = _client(migrated_mysql_url, label="lawyer-test-agent-ok")
    with client:
        token = _register(client, "agent-gateway-owner")
        headers = {"Authorization": f"Bearer {token}"}

        listed = client.get("/api/v1/platform/agent/tools", headers=headers)
        assert listed.status_code == 200
        tools = listed.json()
        names = [tool["name"] for tool in tools]
        assert "meta.list_tools" in names
        meta = next(tool for tool in tools if tool["name"] == "meta.list_tools")
        assert meta["description"]

        called = client.post(
            "/api/v1/platform/agent/tools/call",
            headers=headers,
            json={"tool": "meta.list_tools", "args": {}},
        )
        assert called.status_code == 200
        body = called.json()
        assert body["ok"] is True
        assert any(entry["name"] == "meta.list_tools" for entry in body["output"])

        unknown = client.post(
            "/api/v1/platform/agent/tools/call",
            headers=headers,
            json={"tool": "unknown.tool", "args": {}},
        )
        assert unknown.status_code == 200
        assert unknown.json()["ok"] is False
        assert unknown.json()["error_code"] == "unknown_tool"

        invalid = client.post(
            "/api/v1/platform/agent/tools/call",
            headers=headers,
            json={"tool": "meta.list_tools", "args": {"surprise": 1}},
        )
        assert invalid.status_code == 200
        assert invalid.json()["ok"] is False
        assert invalid.json()["error_code"] == "invalid_arguments"

        malformed = client.post(
            "/api/v1/platform/agent/tools/call",
            headers=headers,
            json={"tool": "meta.list_tools", "extra": True},
        )
        assert malformed.status_code == 422
