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
from lawyer_agent.infrastructure.documents.docx_loader import ZipDocxLoader
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


async def _seed_document_context(mysql_url: URL, tenant_id: str) -> dict[str, str]:
    """Seed matter/document/active pack/rule under the given tenant."""
    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    document_id = new_uuid7()
    try:
        async with factory() as session:
            tenant = UUID(tenant_id)
            user_id = new_uuid7()
            matter_id = new_uuid7()
            await session.execute(
                text(
                    "INSERT INTO tenant_matters "
                    "(id,tenant_id,title,kind,status,created_by_user_id,"
                    "created_by_membership_id,version) "
                    "VALUES (:id,:tenant,'租赁合同审查','contract_review','open',:u,:u,1)"
                ),
                {"id": matter_id.bytes, "tenant": tenant.bytes, "u": user_id.bytes},
            )
            await session.execute(
                text(
                    "INSERT INTO tenant_documents "
                    "(id,tenant_id,matter_id,display_name,current_version_no,version) "
                    "VALUES (:id,:tenant,:matter,'租赁合同.docx',1,1)"
                ),
                {"id": document_id.bytes, "tenant": tenant.bytes, "matter": matter_id.bytes},
            )
            await session.execute(
                text(
                    "INSERT INTO tenant_document_versions "
                    "(id,tenant_id,document_id,version_no,kind,object_key,sha256,"
                    "upload_status,file_name,mime_type,size_bytes) "
                    "VALUES (:id,:tenant,:doc,1,'original',:key,:sha,'accepted',"
                    "'租赁合同.docx','application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document',1024)"
                ),
                {
                    "id": new_uuid7().bytes,
                    "tenant": tenant.bytes,
                    "doc": document_id.bytes,
                    "key": f"tenant_{tenant.hex}/doc/lease.docx",
                    "sha": bytes(32),
                },
            )
            pack_id = new_uuid7()
            await session.execute(
                text(
                    "INSERT INTO tenant_rule_packs "
                    "(id,tenant_id,name,version,active) VALUES (:id,:tenant,'p',1,1)"
                ),
                {"id": pack_id.bytes, "tenant": tenant.bytes},
            )
            await session.execute(
                text(
                    "INSERT INTO tenant_rule_pack_rules "
                    "(id,tenant_id,pack_id,trigger_kind,label,pattern,risk_level,"
                    "suggestion_template,enabled) "
                    "VALUES (:id,:tenant,:pack,:kind,:label,:pattern,:level,:suggestion,1)"
                ),
                {
                    "id": new_uuid7().bytes,
                    "tenant": tenant.bytes,
                    "pack": pack_id.bytes,
                    "kind": "risk_phrase",
                    "label": "高额违约金",
                    "pattern": r"(?:百分之[一二三四五六七八九十百0-9]+|[0-9]+\s*%)",
                    "level": "high",
                    "suggestion": "违约金比例较高，请人工核验。",
                },
            )
            await session.commit()
    finally:
        await engine.dispose()
    return {"document_id": str(document_id), "pack_id": str(pack_id)}


def test_rule_check_http_full_flow_over_mysql_redis(
    migrated_mysql_url: URL,
) -> None:
    redis_url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    if not asyncio.run(_redis_available(redis_url)):
        pytest.skip("test Redis unavailable")
    prefix = f"lawyer-test-rule-http:{uuid4().hex}:"
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
    try:
        with client:
            registered = client.post(
                "/api/v1/auth/register",
                headers={"Origin": _ORIGIN},
                json={
                    "username": "rule-http-owner",
                    "password": _PASSWORD,
                    "display_name": "规则检查用户",
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
                        "Idempotency-Key": f"rule-http-tenant-{index}",
                    },
                    json={"name": f"规则租户{index}", "tenant_type": "law_firm"},
                )
                assert created.status_code == 201, created.text
                tenant_ids.append(created.json()["tenant"]["id"])
                membership_ids.append(created.json()["owner_membership"]["id"])
            asyncio.run(_activate_tenant(migrated_mysql_url, tenant_ids[0]))
            asyncio.run(_activate_tenant(migrated_mysql_url, tenant_ids[1]))
            context_a = asyncio.run(
                _seed_document_context(migrated_mysql_url, tenant_ids[0])
            )
            asyncio.run(
                _seed_document_context(migrated_mysql_url, tenant_ids[1])
            )

            logged_in = client.post(
                "/api/v1/auth/login",
                headers={"Origin": _ORIGIN},
                json={
                    "kind": "username",
                    "identifier": "rule-http-owner",
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
                json={
                    "tenant_id": tenant_ids[0],
                    "membership_id": membership_ids[0],
                },
            )
            assert switched.status_code == 200, switched.text
            tenant_token = switched.json()["access_token"]
            base = f"/api/v1/tenants/{tenant_ids[0]}"
            document_id = context_a["document_id"]

            # 1. Run checks with an untrusted provision body (rule hits).
            ran = client.post(
                f"{base}/documents/{document_id}/risk-checks",
                headers={"Authorization": f"Bearer {tenant_token}"},
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
            created = ran.json()
            assert len(created) == 1
            assert created[0]["risk_level"] == "high"
            assert created[0]["evidence_level"] == "rule_based"
            assert created[0]["status"] == "open"
            issue_id = created[0]["id"]

            # 2. Re-run is idempotent: no duplicate issues.
            reran = client.post(
                f"{base}/documents/{document_id}/risk-checks",
                headers={"Authorization": f"Bearer {tenant_token}"},
                json={
                    "provisions": [
                        {
                            "provision_no": "第十条",
                            "text": "第十条 逾期交付应按日租金的百分之三支付违约金。",
                        }
                    ]
                },
            )
            assert reran.status_code == 200, reran.text
            assert reran.json() == []

            # 3. List risk issues for the document.
            listed = client.get(
                f"{base}/documents/{document_id}/risk-issues",
                headers={"Authorization": f"Bearer {tenant_token}"},
            )
            assert listed.status_code == 200, listed.text
            listed_issues = listed.json()
            assert len(listed_issues) == 1
            assert listed_issues[0]["provision_no"] == "第十条"

            # 4. Dispose the issue with a human reason.
            disposed = client.post(
                f"{base}/risk-issues/{issue_id}/disposition",
                headers={"Authorization": f"Bearer {tenant_token}"},
                json={"status": "accepted", "reason": "律师确认百分之三违约金可接受"},
            )
            assert disposed.status_code == 200, disposed.text
            assert disposed.json()["status"] == "accepted"
            assert disposed.json()["disposition_reason"] == "律师确认百分之三违约金可接受"

            # Disposing again conflicts (not open any more).
            again = client.post(
                f"{base}/risk-issues/{issue_id}/disposition",
                headers={"Authorization": f"Bearer {tenant_token}"},
                json={"status": "rejected", "reason": "重复处置"},
            )
            assert again.status_code == 409
            assert again.json()["code"] == "rule_check_conflict"

            # 5. Export the DOCX report and read it back with the loader.
            report = client.get(
                f"{base}/documents/{document_id}/report.docx",
                headers={"Authorization": f"Bearer {tenant_token}"},
            )
            assert report.status_code == 200, report.text
            assert report.headers["content-type"].startswith(
                "application/vnd.openxmlformats-officedocument.wordprocessingml"
            )
            back = ZipDocxLoader(source_ref="object://tenant/report.docx").load(
                "object://tenant/report.docx", report.content
            )
            joined = "\n".join(paragraph.text for paragraph in back.paragraphs)
            assert "非法律意见" in joined
            assert "百分之三" in joined
            assert "已接受" in joined

        # 6. Cross-tenant reverse: a member token of tenant B cannot touch A.
        with client:
            # Switch to tenant B and try to reach tenant A's document endpoints.
            logged_in = client.post(
                "/api/v1/auth/login",
                headers={"Origin": _ORIGIN},
                json={
                    "kind": "username",
                    "identifier": "rule-http-owner",
                    "password": _PASSWORD,
                },
            )
            switched_b = client.post(
                "/api/v1/auth/switch-tenant",
                headers={
                    "Authorization": f"Bearer {logged_in.json()['access_token']}",
                    "Origin": _ORIGIN,
                },
                json={
                    "tenant_id": tenant_ids[1],
                    "membership_id": membership_ids[1],
                },
            )
            assert switched_b.status_code == 200, switched_b.text
            token_b = switched_b.json()["access_token"]
            foreign = client.get(
                f"{base}/documents/{document_id}/risk-issues",
                headers={"Authorization": f"Bearer {token_b}"},
            )
            # Path tenant does not match the actor's tenant: rejected before any
            # document read (strongest reverse-isolation boundary).
            assert foreign.status_code == 404
            assert foreign.json()["code"] == "tenant_resource_not_found"

            # Same tenant path as B, but the document belongs to tenant A only:
            # the resource layer must not expose it either.
            base_b = f"/api/v1/tenants/{tenant_ids[1]}"
            stolen = client.get(
                f"{base_b}/documents/{document_id}/risk-issues",
                headers={"Authorization": f"Bearer {token_b}"},
            )
            assert stolen.status_code == 404
            assert stolen.json()["code"] == "rule_check_document_not_found"
    finally:
        pass
