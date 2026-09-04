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
    mysql_url: URL,
    tenant_id: UUID,
    actions: tuple[str, ...],
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


def test_rejected_phase2_write_attempts_produce_denied_audit_rows(
    migrated_mysql_url: URL,
) -> None:
    redis_url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    if not asyncio.run(_redis_available(redis_url)):
        pytest.skip("test Redis unavailable")
    prefix = f"lawyer-test-rejected-audit:{uuid4().hex}:"
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
                "username": "rejected-audit-owner",
                "password": _PASSWORD,
                "display_name": "拒绝审计用户",
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
                    "Idempotency-Key": f"rejected-audit-tenant-{index}",
                },
                json={"name": f"拒绝审计租户{index}", "tenant_type": "law_firm"},
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
                "identifier": "rejected-audit-owner",
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

        # --- Tenant A happy-path prerequisites (activate/review/dispose all succeed). ---
        pack = client.post(f"{base_a}/rule-packs", headers=headers_a, json={"name": "租赁"})
        assert pack.status_code == 201, pack.text
        pack_id = pack.json()["id"]
        activated = client.post(f"{base_a}/rule-packs/{pack_id}/activate", headers=headers_a)
        assert activated.status_code == 200, activated.text
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
        matter = client.post(
            f"{base_a}/matters",
            headers=headers_a,
            json={"title": "租赁合同", "kind": "contract_review"},
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
        review_url = f"{base_a}/documents/{document_id}/versions/1/review"
        submitted = client.post(review_url, headers=headers_a, json={"decision": "submit"})
        assert submitted.status_code == 200, submitted.text

        # 422: request_changes without a human reason -> failure row.
        no_reason = client.post(
            review_url,
            headers=headers_a,
            json={"decision": "request_changes", "reason": "  "},
        )
        assert no_reason.status_code == 422
        assert no_reason.json()["code"] == "document_review_invalid_request"

        # 409: approve from PENDING_REVIEW succeeds, then terminal approve conflicts.
        approved = client.post(review_url, headers=headers_a, json={"decision": "approve"})
        assert approved.status_code == 200, approved.text
        again = client.post(review_url, headers=headers_a, json={"decision": "approve"})
        assert again.status_code == 409
        assert again.json()["code"] == "document_review_conflict"

        # 404: review of a version that does not exist -> denied row.
        missing_review = client.post(
            f"{base_a}/documents/{document_id}/versions/42/review",
            headers=headers_a,
            json={"decision": "reject", "reason": "人工复核"},
        )
        assert missing_review.status_code == 404
        assert missing_review.json()["code"] == "document_review_not_found"

        # Run a real risk check so we have an open issue to dispose.
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
        assert len(ran.json()) == 1
        issue_id = ran.json()[0]["id"]
        disposed = client.post(
            f"{base_a}/risk-issues/{issue_id}/disposition",
            headers=headers_a,
            json={"status": "accepted", "reason": "律师确认"},
        )
        assert disposed.status_code == 200, disposed.text
        # 409: disposing the same issue again conflicts.
        disposed_again = client.post(
            f"{base_a}/risk-issues/{issue_id}/disposition",
            headers=headers_a,
            json={"status": "rejected", "reason": "重复处置"},
        )
        assert disposed_again.status_code == 409
        assert disposed_again.json()["code"] == "rule_check_conflict"

        # 404 activate of a pack id that does not exist -> denied row.
        foreign_pack_id = "00000000-0000-7000-8000-000000000001"
        missing_activate = client.post(
            f"{base_a}/rule-packs/{foreign_pack_id}/activate", headers=headers_a
        )
        assert missing_activate.status_code == 404
        assert missing_activate.json()["code"] == "rule_pack_admin_not_found"

        # --- Tenant B resource-layer attempts against A's ids (B path). ---
        logged_in_again = client.post(
            "/api/v1/auth/login",
            headers={"Origin": _ORIGIN},
            json={
                "kind": "username",
                "identifier": "rejected-audit-owner",
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
        stolen_review = client.post(
            f"{base_b}/documents/{document_id}/versions/1/review",
            headers=headers_b,
            json={"decision": "reject", "reason": "越权复核"},
        )
        assert stolen_review.status_code == 404
        assert stolen_review.json()["code"] == "document_review_not_found"
        stolen_dispose = client.post(
            f"{base_b}/risk-issues/{issue_id}/disposition",
            headers=headers_b,
            json={"status": "rejected", "reason": "越权处置"},
        )
        assert stolen_dispose.status_code == 404
        assert stolen_dispose.json()["code"] == "rule_check_issue_not_found"
        stolen_activate = client.post(
            f"{base_b}/rule-packs/{pack_id}/activate", headers=headers_b
        )
        assert stolen_activate.status_code == 404
        assert stolen_activate.json()["code"] == "rule_pack_admin_not_found"

        # --- Assert audit rows: success facts survive and each rejection is denied/failure. ---
        rows_a = asyncio.run(
            _audit_rows(
                migrated_mysql_url,
                UUID(tenant_ids[0]),
                (
                    "rule_pack.activate",
                    "document.review",
                    "risk_issue.dispose",
                ),
            )
        )
        activate_rows = [r for r in rows_a if r["action"] == "rule_pack.activate"]
        review_rows = [r for r in rows_a if r["action"] == "document.review"]
        dispose_rows = [r for r in rows_a if r["action"] == "risk_issue.dispose"]

        assert {(r["result"], r["reason_code"]) for r in activate_rows} == {
            ("success", "activated"),
            ("denied", "rule_pack_admin_not_found"),
        }

        review_facts = {(r["result"], r["reason_code"]) for r in review_rows}
        assert review_facts == {
            ("success", "reviewed"),
            ("failure", "document_review_invalid_request"),
            ("denied", "document_review_conflict"),
            ("denied", "document_review_not_found"),
        }

        dispose_facts = {(r["result"], r["reason_code"]) for r in dispose_rows}
        assert dispose_facts == {
            ("success", "disposed"),
            ("denied", "rule_check_conflict"),
        }
        # The 404 dispose of an unknown issue also records a denied row in A only
        # when A itself attempted it (B's attempt lands in tenant B below).

        denied_review_rows = [
            r
            for r in review_rows
            if r["result"] == "denied" and r["reason_code"] == "document_review_not_found"
        ]
        assert denied_review_rows
        assert all(
            r["actor_user_id"] is not None and r["actor_membership_id"] is not None
            for r in review_rows + dispose_rows + activate_rows
        )

        rows_b = asyncio.run(
            _audit_rows(
                migrated_mysql_url,
                UUID(tenant_ids[1]),
                (
                    "rule_pack.activate",
                    "document.review",
                    "risk_issue.dispose",
                ),
            )
        )
        b_facts = {
            (r["action"], r["result"], r["reason_code"], str(r["target_id"]))
            for r in rows_b
        }
        assert b_facts == {
            ("document.review", "denied", "document_review_not_found", document_id),
            ("risk_issue.dispose", "denied", "rule_check_issue_not_found", issue_id),
            ("rule_pack.activate", "denied", "rule_pack_admin_not_found", pack_id),
        }
