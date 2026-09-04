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


def test_matter_party_conflict_check_over_real_mysql(migrated_mysql_url: URL) -> None:
    redis_url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    if not asyncio.run(_redis_available(redis_url)):
        pytest.skip("test Redis unavailable")
    prefix = f"lawyer-test-conflict:{uuid4().hex}:"
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
                "username": "conflict-owner",
                "password": _PASSWORD,
                "display_name": "冲突检查用户",
            },
        )
        assert registered.status_code == 201, registered.text
        account_token = registered.json()["access_token"]
        tenant_ids: list[str] = []
        membership_ids: list[str] = []
        for index in (1, 2):
            created = client.post(
                "/api/v1/tenants",
                headers={
                    "Authorization": f"Bearer {account_token}",
                    "Idempotency-Key": f"conflict-tenant-{index}",
                },
                json={"name": f"冲突租户{index}", "tenant_type": "law_firm"},
            )
            assert created.status_code == 201, created.text
            tenant_ids.append(created.json()["tenant"]["id"])
            membership_ids.append(created.json()["owner_membership"]["id"])
        for tenant_id in tenant_ids:
            asyncio.run(_activate_tenant(migrated_mysql_url, tenant_id))

        logged_in = client.post(
            "/api/v1/auth/login",
            headers={"Origin": _ORIGIN},
            json={
                "kind": "username",
                "identifier": "conflict-owner",
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
            json={"tenant_id": tenant_ids[0], "membership_id": membership_ids[0]},
        )
        assert switched.status_code == 200, switched.text
        token_a = switched.json()["access_token"]
        base_a = f"/api/v1/tenants/{tenant_ids[0]}"
        headers_a = {"Authorization": f"Bearer {token_a}"}
        check_url = f"{base_a}/matter-party-conflict-checks"

        matter_a = client.post(
            f"{base_a}/matters",
            headers=headers_a,
            json={"title": "租赁纠纷", "kind": "litigation"},
        )
        assert matter_a.status_code == 201, matter_a.text
        matter_a_id = matter_a.json()["id"]
        matter_b = client.post(
            f"{base_a}/matters",
            headers=headers_a,
            json={"title": "借款纠纷", "kind": "litigation"},
        )
        assert matter_b.status_code == 201, matter_b.text
        matter_b_id = matter_b.json()["id"]
        for matter_id, party in (
            (matter_a_id, ("甲公司", "tenant")),
            (matter_a_id, ("乙公司", "counterparty")),
            (matter_b_id, ("乙公司", "counterparty")),
            (matter_b_id, ("丙公司", "tenant")),
        ):
            added = client.post(
                f"{base_a}/matters/{matter_id}/parties",
                headers=headers_a,
                json={"display_name": party[0], "kind": party[1]},
            )
            assert added.status_code == 201, added.text

        # 1. 乙公司 appears in two Matters -> conflict with a count of 2.
        hit = client.post(
            check_url,
            headers=headers_a,
            json={"display_name": "乙公司"},
        )
        assert hit.status_code == 200, hit.text
        assert hit.json() == {"conflict": True, "other_matter_count": 2}

        # 2. Narrow by kind keeps exact-match semantics.
        hit_kind = client.post(
            check_url,
            headers=headers_a,
            json={"display_name": "乙公司", "kind": "counterparty"},
        )
        assert hit_kind.json() == {"conflict": True, "other_matter_count": 2}
        miss_kind = client.post(
            check_url,
            headers=headers_a,
            json={"display_name": "乙公司", "kind": "tenant"},
        )
        assert miss_kind.json() == {"conflict": False, "other_matter_count": 0}

        # 3. Excluding one of the two Matters leaves exactly one.
        excluded = client.post(
            check_url,
            headers=headers_a,
            json={"display_name": "乙公司", "exclude_matter_id": matter_b_id},
        )
        assert excluded.json() == {"conflict": True, "other_matter_count": 1}

        # 4. A party in a single Matter -> conflict exists only for that Matter.
        single = client.post(
            check_url,
            headers=headers_a,
            json={"display_name": "甲公司"},
        )
        assert single.json() == {"conflict": True, "other_matter_count": 1}
        cleared = client.post(
            check_url,
            headers=headers_a,
            json={"display_name": "甲公司", "exclude_matter_id": matter_a_id},
        )
        assert cleared.json() == {"conflict": False, "other_matter_count": 0}

        # 5. Response never contains other Matter identifiers or content.
        response_text = hit.text
        assert matter_a_id not in response_text
        assert matter_b_id not in response_text
        assert "租赁纠纷" not in response_text
        assert "借款纠纷" not in response_text

        # 6. Blank display_name / kind -> 422.
        blank = client.post(check_url, headers=headers_a, json={"display_name": "  "})
        assert blank.status_code == 422
        blank_kind = client.post(
            check_url, headers=headers_a, json={"display_name": "甲", "kind": "  "}
        )
        assert blank_kind.status_code == 422

        # 7. Tenant B cannot reach A's endpoint via path or resource layer.
        logged_in_again = client.post(
            "/api/v1/auth/login",
            headers={"Origin": _ORIGIN},
            json={
                "kind": "username",
                "identifier": "conflict-owner",
                "password": _PASSWORD,
            },
        )
        assert logged_in_again.status_code == 200, logged_in_again.text
        switched_b = client.post(
            "/api/v1/auth/switch-tenant",
            headers={
                "Authorization": f"Bearer {logged_in_again.json()['access_token']}",
                "Origin": _ORIGIN,
            },
            json={"tenant_id": tenant_ids[1], "membership_id": membership_ids[1]},
        )
        assert switched_b.status_code == 200, switched_b.text
        token_b = switched_b.json()["access_token"]
        stolen_path = client.post(
            check_url,
            headers={"Authorization": f"Bearer {token_b}"},
            json={"display_name": "甲公司"},
        )
        assert stolen_path.status_code == 404
        assert stolen_path.json()["code"] == "tenant_resource_not_found"
