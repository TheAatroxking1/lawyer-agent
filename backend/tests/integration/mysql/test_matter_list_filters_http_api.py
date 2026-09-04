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


def test_matter_list_filters_over_real_mysql(migrated_mysql_url: URL) -> None:
    redis_url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    if not asyncio.run(_redis_available(redis_url)):
        pytest.skip("test Redis unavailable")
    prefix = f"lawyer-test-list-filter:{uuid4().hex}:"
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
                "username": "matter-list-filter-owner",
                "password": _PASSWORD,
                "display_name": "列表过滤用户",
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
                    "Idempotency-Key": f"list-filter-tenant-create-{index}",
                },
                json={"name": f"列表过滤租户{index}", "tenant_type": "law_firm"},
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
                "identifier": "matter-list-filter-owner",
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

        def create_a(title: str, kind: str) -> str:
            made = client.post(
                f"{base_a}/matters",
                headers=headers_a,
                json={"title": title, "kind": kind},
            )
            assert made.status_code == 201, made.text
            return made.json()["id"]

        rent_id = create_a("租赁纠纷", "litigation")
        lease_review_id = create_a("房屋租赁合同审查", "contract_review")
        loan_id = create_a("借款纠纷", "litigation")
        equity_id = create_a("股权纠纷", "litigation")

        # Advance two matters to non-default statuses for status filtering.
        active = client.post(
            f"{base_a}/matters/{lease_review_id}/status",
            headers=headers_a,
            json={"status": "active"},
        )
        assert active.status_code == 200, active.text
        closed = client.post(
            f"{base_a}/matters/{equity_id}/status",
            headers=headers_a,
            json={"status": "closed"},
        )
        assert closed.status_code == 200, closed.text

        # Tenant B: one matter whose title shares the keyword but must never leak.
        logged_in_again = client.post(
            "/api/v1/auth/login",
            headers={"Origin": _ORIGIN},
            json={
                "kind": "username",
                "identifier": "matter-list-filter-owner",
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
        base_b = f"/api/v1/tenants/{tenant_ids[1]}"
        headers_b = {"Authorization": f"Bearer {token_b}"}
        made_b = client.post(
            f"{base_b}/matters",
            headers=headers_b,
            json={"title": "租赁在乙", "kind": "other"},
        )
        assert made_b.status_code == 201, made_b.text

        # 1. title substring filter narrows to matching matters only.
        by_title = client.get(f"{base_a}/matters?title=租赁", headers=headers_a)
        assert by_title.status_code == 200, by_title.text
        title_ids = {item["id"] for item in by_title.json()["items"]}
        assert title_ids == {rent_id, lease_review_id}

        # 2. Whitespace-trimmed keyword still matches; no over-match.
        spaced = client.get(
            f"{base_a}/matters?title=%20%20租赁%20", headers=headers_a
        )
        assert spaced.status_code == 200, spaced.text
        assert {item["id"] for item in spaced.json()["items"]} == title_ids

        # 3. status filter (default open excluded from shown set).
        active_only = client.get(f"{base_a}/matters?status=active", headers=headers_a)
        assert active_only.status_code == 200, active_only.text
        assert {item["id"] for item in active_only.json()["items"]} == {
            lease_review_id
        }
        closed_only = client.get(f"{base_a}/matters?status=closed", headers=headers_a)
        assert closed_only.status_code == 200, closed_only.text
        assert {item["id"] for item in closed_only.json()["items"]} == {equity_id}

        # 4. kind filter.
        litigation = client.get(f"{base_a}/matters?kind=litigation", headers=headers_a)
        assert litigation.status_code == 200, litigation.text
        assert {item["id"] for item in litigation.json()["items"]} == {
            rent_id,
            loan_id,
            equity_id,
        }

        # 5. Combined filters narrow further.
        combined = client.get(
            f"{base_a}/matters?title=纠纷&kind=litigation&status=open",
            headers=headers_a,
        )
        assert combined.status_code == 200, combined.text
        assert {item["id"] for item in combined.json()["items"]} == {rent_id, loan_id}
        combined_b = client.get(
            f"{base_a}/matters?title=纠纷&status=closed", headers=headers_a
        )
        assert combined_b.status_code == 200, combined_b.text
        assert {item["id"] for item in combined_b.json()["items"]} == {equity_id}

        # 6. Filtered pagination stays stable and complete across pages.
        page1 = client.get(
            f"{base_a}/matters?title=纠纷&limit=2", headers=headers_a
        )
        assert page1.status_code == 200, page1.text
        body1 = page1.json()
        assert len(body1["items"]) == 2
        assert body1["next_before_id"] is not None
        page2 = client.get(
            f"{base_a}/matters?title=纠纷&limit=2&before_id={body1['next_before_id']}",
            headers=headers_a,
        )
        assert page2.status_code == 200, page2.text
        body2 = page2.json()
        assert len(body2["items"]) == 1
        assert body2["next_before_id"] is None
        got = {item["id"] for item in body1["items"]} | {
            item["id"] for item in body2["items"]
        }
        assert got == {rent_id, loan_id, equity_id}

        # 7. Invalid filter values -> 422.
        blank = client.get(f"{base_a}/matters?title=%20%20", headers=headers_a)
        assert blank.status_code == 422
        overlong = client.get(
            f"{base_a}/matters?title={'案' * 513}", headers=headers_a
        )
        assert overlong.status_code == 422
        bad_status = client.get(f"{base_a}/matters?status=paused", headers=headers_a)
        assert bad_status.status_code == 422
        bad_kind = client.get(f"{base_a}/matters?kind=drafting", headers=headers_a)
        assert bad_kind.status_code == 422

        # 8. Cross-tenant: B path layer 404; B's filtered list never shows A ids.
        stolen = client.get(f"{base_a}/matters?title=租赁", headers=headers_b)
        assert stolen.status_code == 404
        assert stolen.json()["code"] == "tenant_resource_not_found"
        own = client.get(f"{base_b}/matters?title=租赁", headers=headers_b)
        assert own.status_code == 200, own.text
        own_ids = {item["id"] for item in own.json()["items"]}
        assert own_ids == {made_b.json()["id"]}
        assert own_ids.isdisjoint({rent_id, lease_review_id, loan_id, equity_id})
