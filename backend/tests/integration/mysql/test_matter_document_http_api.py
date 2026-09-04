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


def test_matter_document_http_full_flow_over_mysql_redis(
    migrated_mysql_url: URL,
) -> None:
    redis_url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    if not asyncio.run(_redis_available(redis_url)):
        pytest.skip("test Redis unavailable")
    prefix = f"lawyer-test-md-http:{uuid4().hex}:"
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
                "username": "md-http-owner",
                "password": _PASSWORD,
                "display_name": "事项用户",
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
                    "Idempotency-Key": f"md-http-tenant-{index}",
                },
                json={"name": f"事项租户{index}", "tenant_type": "law_firm"},
            )
            assert created.status_code == 201, created.text
            tenant_ids.append(created.json()["tenant"]["id"])
            membership_ids.append(created.json()["owner_membership"]["id"])
        asyncio.run(_activate_tenant(migrated_mysql_url, tenant_ids[0]))
        asyncio.run(_activate_tenant(migrated_mysql_url, tenant_ids[1]))

        logged_in = client.post(
            "/api/v1/auth/login",
            headers={"Origin": _ORIGIN},
            json={
                "kind": "username",
                "identifier": "md-http-owner",
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

        # 1. Create a matter.
        matter = client.post(
            f"{base_a}/matters",
            headers={"Authorization": f"Bearer {token_a}"},
            json={"title": "租赁合同审查", "kind": "contract_review"},
        )
        assert matter.status_code == 201, matter.text
        matter_body = matter.json()
        matter_id = matter_body["id"]
        assert matter_body["status"] == "open"
        assert matter_body["kind"] == "contract_review"

        # 2. Read the matter back.
        got = client.get(
            f"{base_a}/matters/{matter_id}",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert got.status_code == 200, got.text
        assert got.headers.get("etag") is not None

        # 3. Register a docx original version (accepted).
        payload_b64 = base64.b64encode(_DOCX_PAYLOAD).decode("ascii")
        registered_doc = client.post(
            f"{base_a}/matters/{matter_id}/documents",
            headers={"Authorization": f"Bearer {token_a}"},
            json={
                "file_name": "租赁合同.docx",
                "mime_type": _DOCX_MIME,
                "payload_b64": payload_b64,
            },
        )
        assert registered_doc.status_code == 201, registered_doc.text
        version = registered_doc.json()
        assert version["kind"] == "original"
        assert version["upload_status"] == "accepted"
        assert version["version_no"] == 1

        # 4. Register a non-docx payload => needs_review (never silently accepted).
        plain = client.post(
            f"{base_a}/matters/{matter_id}/documents",
            headers={"Authorization": f"Bearer {token_a}"},
            json={
                "file_name": "说明.txt",
                "mime_type": "text/plain",
                "payload_b64": base64.b64encode(b"hello").decode("ascii"),
            },
        )
        assert plain.status_code == 201, plain.text
        assert plain.json()["upload_status"] == "needs_review"

        # 5. List documents under the matter.
        listed = client.get(
            f"{base_a}/matters/{matter_id}/documents",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert listed.status_code == 200, listed.text
        headers = listed.json()
        assert len(headers) == 2

        # 6. Create a matter on tenant B too and confirm isolation.
        logged_in_again = client.post(
            "/api/v1/auth/login",
            headers={"Origin": _ORIGIN},
            json={
                "kind": "username",
                "identifier": "md-http-owner",
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

        # 6a. B's token against A's path is rejected at the tenant boundary.
        stolen_path = client.get(
            f"{base_a}/matters/{matter_id}",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert stolen_path.status_code == 404
        assert stolen_path.json()["code"] == "tenant_resource_not_found"

        # 6b. Same path as B, but A's matter id => resource layer rejects.
        stolen_resource = client.get(
            f"{base_b}/matters/{matter_id}",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert stolen_resource.status_code == 404
        assert stolen_resource.json()["code"] == "matter_document_not_found"

        # 7. Registering into A's matter from B (same path as B) also 404s.
        stolen_register = client.post(
            f"{base_b}/matters/{matter_id}/documents",
            headers={"Authorization": f"Bearer {token_b}"},
            json={
                "file_name": "x.docx",
                "mime_type": _DOCX_MIME,
                "payload_b64": base64.b64encode(b"x").decode("ascii"),
            },
        )
        assert stolen_register.status_code == 404
