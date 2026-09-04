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
from lawyer_agent.domain.common import new_uuid7
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


def _make_b_pack_and_rules(client: TestClient, base_b: str, token_b: str) -> str:
    made = client.post(
        f"{base_b}/rule-packs",
        headers={"Authorization": f"Bearer {token_b}"},
        json={"name": "乙租户规则"},
    )
    assert made.status_code == 201, made.text
    pack_id = made.json()["id"]
    added = client.post(
        f"{base_b}/rule-packs/{pack_id}/rules",
        headers={"Authorization": f"Bearer {token_b}"},
        json={
            "trigger_kind": "risk_phrase",
            "label": "乙规则",
            "pattern": r"乙方责任",
            "risk_level": "medium",
            "suggestion": "请核验。",
        },
    )
    assert added.status_code == 201, added.text
    return pack_id


def test_rule_pack_rules_read_back_over_real_mysql(migrated_mysql_url: URL) -> None:
    redis_url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    if not asyncio.run(_redis_available(redis_url)):
        pytest.skip("test Redis unavailable")
    prefix = f"lawyer-test-pack-rules:{uuid4().hex}:"
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
                "username": "pack-rules-reader",
                "password": _PASSWORD,
                "display_name": "规则读回用户",
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
                    "Idempotency-Key": f"pack-rules-tenant-{index}",
                },
                json={"name": f"规则读回租户{index}", "tenant_type": "law_firm"},
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
                "identifier": "pack-rules-reader",
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

        def make_pack(name: str) -> str:
            made = client.post(
                f"{base_a}/rule-packs",
                headers=headers_a,
                json={"name": name},
            )
            assert made.status_code == 201, made.text
            return made.json()["id"]

        def add_rule(
            pack_id: str, *, trigger: str, label: str, risk: str
        ) -> str:
            added = client.post(
                f"{base_a}/rule-packs/{pack_id}/rules",
                headers=headers_a,
                json={
                    "trigger_kind": trigger,
                    "label": label,
                    "pattern": r"违约(?:金|责任)",
                    "risk_level": risk,
                    "suggestion": "请人工核验相关条款。",
                },
            )
            assert added.status_code == 201, added.text
            return added.json()["id"]

        pack_id = make_pack("租赁审查规则")
        empty_pack_id = make_pack("尚未配置")

        # 1. Empty pack reads back as [].
        empty = client.get(
            f"{base_a}/rule-packs/{empty_pack_id}/rules", headers=headers_a
        )
        assert empty.status_code == 200, empty.text
        assert empty.json() == []

        # 2. Rules read back in created order, both enabled states visible.
        rule_high = add_rule(pack_id, trigger="risk_phrase", label="高额违约金", risk="high")
        rule_low = add_rule(pack_id, trigger="clause_type", label="逾期责任条款", risk="low")
        listed = client.get(f"{base_a}/rule-packs/{pack_id}/rules", headers=headers_a)
        assert listed.status_code == 200, listed.text
        items = listed.json()
        assert [item["id"] for item in items] == [rule_high, rule_low]
        assert {item["enabled"] for item in items} == {True}
        assert {item["risk_level"] for item in items} == {"high", "low"}
        assert {item["trigger_kind"] for item in items} == {"risk_phrase", "clause_type"}

        # 3. Disabled rules stay visible in read-back.
        disabled = client.patch(
            f"{base_a}/rule-packs/{pack_id}/rules/{rule_low}",
            headers=headers_a,
            json={"enabled": False},
        )
        assert disabled.status_code == 204, disabled.text
        after = client.get(f"{base_a}/rule-packs/{pack_id}/rules", headers=headers_a)
        assert after.status_code == 200, after.text
        by_id = {item["id"]: item for item in after.json()}
        assert by_id[rule_low]["enabled"] is False
        assert by_id[rule_high]["enabled"] is True
        assert len(after.json()) == 2

        # 4. Unknown / foreign pack id -> 404 (resource layer).
        missing = client.get(
            f"{base_a}/rule-packs/{new_uuid7()}/rules", headers=headers_a
        )
        assert missing.status_code == 404
        assert missing.json()["code"] == "rule_pack_admin_not_found"

        # 5. Tenant B isolation: path layer 404, resource layer 404, and B's own
        #    pack rules never show A's content.
        logged_in_again = client.post(
            "/api/v1/auth/login",
            headers={"Origin": _ORIGIN},
            json={
                "kind": "username",
                "identifier": "pack-rules-reader",
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
        stolen_path = client.get(
            f"{base_a}/rule-packs/{pack_id}/rules",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert stolen_path.status_code == 404
        assert stolen_path.json()["code"] == "tenant_resource_not_found"
        stolen_resource = client.get(
            f"{base_b}/rule-packs/{pack_id}/rules",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert stolen_resource.status_code == 404
        assert stolen_resource.json()["code"] == "rule_pack_admin_not_found"

        own_pack = _make_b_pack_and_rules(client, base_b, token_b)
        own_list = client.get(
            f"{base_b}/rule-packs/{own_pack}/rules",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert own_list.status_code == 200, own_list.text
        assert {item["id"] for item in own_list.json()}.isdisjoint(
            {rule_high, rule_low}
        )
