from __future__ import annotations

import asyncio
import base64
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
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_DOCX_PAYLOAD = b"PK\x03\x04 fake-docx-bytes for upload metadata only"


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


def _create_app_and_session(mysql_url: URL):
    """Return a prepared client + helpers to build tenant tokens."""
    redis_url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    if not asyncio.run(_redis_available(redis_url)):
        pytest.skip("test Redis unavailable")
    prefix = f"lawyer-test-audit-query:{uuid4().hex}:"
    settings = Settings(
        environment="test",
        secret_key="h" * 32,
        database_url=mysql_url.render_as_string(hide_password=False),
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
    return TestClient(create_app(settings), base_url="https://testserver")


def test_tenant_audit_query_over_real_mysql(migrated_mysql_url: URL) -> None:
    client = _create_app_and_session(migrated_mysql_url)
    with client:
        registered = client.post(
            "/api/v1/auth/register",
            headers={"Origin": _ORIGIN},
            json={
                "username": "audit-query-owner",
                "password": _PASSWORD,
                "display_name": "审计查询用户",
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
                    "Idempotency-Key": f"audit-query-tenant-{index}",
                },
                json={"name": f"审计查询租户{index}", "tenant_type": "law_firm"},
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
                "identifier": "audit-query-owner",
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

        # Tenant A: activate pack + review document + dispose risk issue.
        pack = client.post(f"{base_a}/rule-packs", headers=headers_a, json={"name": "租赁"})
        assert pack.status_code == 201, pack.text
        pack_id = pack.json()["id"]
        activated = client.post(
            f"{base_a}/rule-packs/{pack_id}/activate", headers=headers_a
        )
        assert activated.status_code == 200, activated.text
        matter = client.post(
            f"{base_a}/matters",
            headers=headers_a,
            json={"title": "租赁审查", "kind": "contract_review"},
        )
        assert matter.status_code == 201, matter.text
        matter_id = matter.json()["id"]
        uploaded = client.post(
            f"{base_a}/matters/{matter_id}/documents",
            headers=headers_a,
            json={
                "file_name": "租赁合同.docx",
                "mime_type": _DOCX_MIME,
                "payload_b64": base64.b64encode(_DOCX_PAYLOAD).decode("ascii"),
            },
        )
        assert uploaded.status_code == 201, uploaded.text
        document_id = uploaded.json()["document_id"]
        assert (
            client.post(
                f"{base_a}/documents/{document_id}/versions/1/review",
                headers=headers_a,
                json={"decision": "submit"},
            ).status_code
            == 200
        )
        rule = client.post(
            f"{base_a}/rule-packs/{pack_id}/rules",
            headers=headers_a,
            json={
                "trigger_kind": "risk_phrase",
                "label": "高额违约金",
                "pattern": r"(?:百分之[一二三四五六七八九十百0-9]+|[0-9]+\s*%)",
                "risk_level": "high",
                "suggestion": "请人工核验。",
            },
        )
        assert rule.status_code == 201, rule.text
        ran = client.post(
            f"{base_a}/documents/{document_id}/risk-checks",
            headers=headers_a,
            json={
                "provisions": [
                    {
                        "provision_no": "第十条",
                        "text": "第十条 逾期交付应按日租金的百分之三支付违约金。",
                    }
                ]
            },
        )
        assert ran.status_code == 200, ran.text
        issue_id = ran.json()[0]["id"]
        assert (
            client.post(
                f"{base_a}/risk-issues/{issue_id}/disposition",
                headers=headers_a,
                json={"status": "accepted", "reason": "律师确认"},
            ).status_code
            == 200
        )

        # 1. Full list newest first: dispose/review/activate.
        listed = client.get(f"{base_a}/audit", headers=headers_a)
        assert listed.status_code == 200, listed.text
        events = listed.json()["events"]
        actions = [event["action"] for event in events]
        assert "risk_issue.dispose" in actions
        assert "document.review" in actions
        assert "rule_pack.activate" in actions

        # 2. Action filter.
        filtered = client.get(f"{base_a}/audit?action=risk_issue.", headers=headers_a)
        assert filtered.status_code == 200, filtered.text
        filtered_actions = {event["action"] for event in filtered.json()["events"]}
        assert filtered_actions == {"risk_issue.dispose"}

        # 3. Invalid action prefix -> 422.
        bad = client.get(f"{base_a}/audit?action=xss.", headers=headers_a)
        assert bad.status_code == 422
        assert bad.json()["code"] == "audit_query_invalid_request"

        # 4. Paging: limit=1 returns newest first with a stable cursor.
        page1 = client.get(f"{base_a}/audit?limit=1", headers=headers_a)
        assert page1.status_code == 200, page1.text
        first_page = page1.json()
        assert len(first_page["events"]) == 1
        next_cursor = first_page["next_before_id"]
        assert next_cursor is not None
        page2 = client.get(
            f"{base_a}/audit?limit=1&before_id={next_cursor}", headers=headers_a
        )
        assert page2.status_code == 200, page2.text
        second = page2.json()
        assert len(second["events"]) == 1
        assert second["events"][0]["id"] != first_page["events"][0]["id"]

        # 5. Cross-tenant: B's token cannot read A's audit (path boundary),
        #    and B's own audit list only ever contains its own rows.
        logged_in_again = client.post(
            "/api/v1/auth/login",
            headers={"Origin": _ORIGIN},
            json={
                "kind": "username",
                "identifier": "audit-query-owner",
                "password": _PASSWORD,
            },
        )
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
        stolen = client.get(
            f"{base_a}/audit", headers={"Authorization": f"Bearer {token_b}"}
        )
        assert stolen.status_code == 404
        assert stolen.json()["code"] == "tenant_resource_not_found"

        base_b = f"/api/v1/tenants/{tenant_ids[1]}"
        own_b = client.get(
            f"{base_b}/audit", headers={"Authorization": f"Bearer {token_b}"}
        )
        assert own_b.status_code == 200, own_b.text
        b_actions = {event["action"] for event in own_b.json()["events"]}
        # B only ever sees its own rows; tenant A's phase2 actions never leak.
        assert not any(
            action.startswith(
                ("risk_issue.", "document.", "rule_pack.", "matter.")
            )
            for action in b_actions
        )
