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
from sqlalchemy import select, text, update
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from lawyer_agent.config import Settings
from lawyer_agent.infrastructure.persistence.models import AuditEventModel, TenantModel
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


async def _audit_rows(
    mysql_url: URL, tenant_id: UUID, actions: tuple[str, ...]
) -> list[dict[str, object]]:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                select(
                    AuditEventModel.action,
                    AuditEventModel.reason_code,
                    AuditEventModel.result,
                    AuditEventModel.target_type,
                    AuditEventModel.target_id,
                    AuditEventModel.actor_user_id,
                    AuditEventModel.actor_membership_id,
                ).where(
                    AuditEventModel.tenant_id == tenant_id,
                    AuditEventModel.action.in_(actions),
                )
            )
            return [dict(row._mapping) for row in rows]
    finally:
        await engine.dispose()


def test_phase2_human_write_actions_produce_audit_rows(
    migrated_mysql_url: URL,
) -> None:
    redis_url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    if not asyncio.run(_redis_available(redis_url)):
        pytest.skip("test Redis unavailable")
    prefix = f"lawyer-test-audit-http:{uuid4().hex}:"
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
                "username": "audit-http-owner",
                "password": _PASSWORD,
                "display_name": "审计用户",
            },
        )
        assert registered.status_code == 201, registered.text
        account_token = registered.json()["access_token"]
        created = client.post(
            "/api/v1/tenants",
            headers={
                "Authorization": f"Bearer {account_token}",
                "Idempotency-Key": "audit-http-tenant-1",
            },
            json={"name": "审计租户", "tenant_type": "law_firm"},
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
                "identifier": "audit-http-owner",
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
        headers = {"Authorization": f"Bearer {token}"}

        # --- Rule Pack activation ---
        pack = client.post(f"{base}/rule-packs", headers=headers, json={"name": "租赁"})
        assert pack.status_code == 201, pack.text
        pack_id = pack.json()["id"]
        activated = client.post(f"{base}/rule-packs/{pack_id}/activate", headers=headers)
        assert activated.status_code == 200, activated.text

        # --- Document review ---
        matter = client.post(
            f"{base}/matters",
            headers=headers,
            json={"title": "租赁合同", "kind": "contract_review"},
        )
        assert matter.status_code == 201, matter.text
        matter_id = matter.json()["id"]
        uploaded = client.post(
            f"{base}/matters/{matter_id}/documents",
            headers=headers,
            json={
                "file_name": "租赁合同.docx",
                "mime_type": _DOCX_MIME,
                "payload_b64": base64.b64encode(_DOCX_PAYLOAD).decode("ascii"),
            },
        )
        assert uploaded.status_code == 201, uploaded.text
        document_id = uploaded.json()["document_id"]
        reviewed = client.post(
            f"{base}/documents/{document_id}/versions/1/review",
            headers=headers,
            json={"decision": "submit"},
        )
        assert reviewed.status_code == 200, reviewed.text

        # --- RiskIssue disposition ---
        rule_added = client.post(
            f"{base}/rule-packs/{pack_id}/rules",
            headers=headers,
            json={
                "trigger_kind": "risk_phrase",
                "label": "高额违约金",
                "pattern": r"(?:百分之[一二三四五六七八九十百0-9]+|[0-9]+\s*%)",
                "risk_level": "high",
                "suggestion": "请人工核验。",
            },
        )
        assert rule_added.status_code == 201, rule_added.text
        ran = client.post(
            f"{base}/documents/{document_id}/risk-checks",
            headers=headers,
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
        assert len(ran.json()) == 1
        issue_id = ran.json()[0]["id"]
        disposed = client.post(
            f"{base}/risk-issues/{issue_id}/disposition",
            headers=headers,
            json={"status": "accepted", "reason": "律师确认"},
        )
        assert disposed.status_code == 200, disposed.text

        # --- Audit rows exist for all three actions with correct facts. ---
        rows = asyncio.run(
            _audit_rows(
                migrated_mysql_url,
                UUID(tenant_id),
                ("rule_pack.activate", "document.review", "risk_issue.dispose"),
            )
        )
        actions = {row["action"]: row for row in rows}
        assert set(actions) == {
            "rule_pack.activate",
            "document.review",
            "risk_issue.dispose",
        }
        activate_row = actions["rule_pack.activate"]
        assert activate_row["reason_code"] == "activated"
        assert activate_row["result"] == "success"
        assert activate_row["target_type"] == "rule_pack"
        assert activate_row["target_id"] == UUID(pack_id)

        review_row = actions["document.review"]
        assert review_row["reason_code"] == "reviewed"
        assert review_row["target_type"] == "document_version"

        dispose_row = actions["risk_issue.dispose"]
        assert dispose_row["reason_code"] == "disposed"
        assert dispose_row["target_type"] == "risk_issue"
        assert dispose_row["target_id"] == UUID(issue_id)
