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
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from lawyer_agent.config import Settings
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import content_sha256
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


async def _seed_corpus(
    mysql_url: URL,
) -> dict[str, UUID]:
    """One instrument with two versions and one other instrument for isolation."""
    instrument_a = new_uuid7()
    version_old_a = new_uuid7()
    version_new_a = new_uuid7()
    instrument_b = new_uuid7()
    version_b = new_uuid7()

    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            for instrument_id, title, authority in (
                (
                    instrument_a,
                    "中华人民共和国示例法",
                    "全国人民代表大会常务委员会",
                ),
                (
                    instrument_b,
                    "中华人民共和国其他示例法",
                    "国务院",
                ),
            ):
                await session.execute(
                    text(
                        "INSERT INTO legal_instruments "
                        "(id,title,issuing_authority,jurisdiction,version) "
                        "VALUES (:id,:title,:authority,'mainland_china',1)"
                    ),
                    {
                        "id": instrument_id.bytes,
                        "title": title,
                        "authority": authority,
                    },
                )

            # Versions of A: old (2020), new (2024). Versions of B: one.
            version_rows = (
                (
                    version_old_a,
                    instrument_a,
                    "2020 修正",
                    "2020-01-01",
                    "2021-01-01",
                ),
                (
                    version_new_a,
                    instrument_a,
                    "2024 修正",
                    "2024-01-01",
                    "2024-03-01",
                ),
                (version_b, instrument_b, "2020 公布", "2020-06-01", "2020-08-01"),
            )
            for version_id, instrument_id, label, published, effective in version_rows:
                await session.execute(
                    text(
                        "INSERT INTO legal_versions "
                        "(id,instrument_id,version_label,status,published_on,"
                        "effective_on,content_hash,source_ref,dataset_version,"
                        "parser_version) "
                        "VALUES (:id,:inst,:label,'current',:pub,:eff,:hash,"
                        "'corpus/sample.docx','dataset_v1','docx-zip-v1')"
                    ),
                    {
                        "id": version_id.bytes,
                        "inst": instrument_id.bytes,
                        "label": label,
                        "pub": published,
                        "eff": effective,
                        "hash": bytes(32),
                    },
                )

            async def insert_provision(
                version_id: UUID,
                provision_id: UUID,
                provision_no: str,
                full_text: str,
            ) -> None:
                await session.execute(
                    text(
                        "INSERT INTO legal_provisions "
                        "(id,version_id,provision_no,level,full_text,content_hash,"
                        "char_start,char_end) "
                        "VALUES (:id,:ver,:no,'article',:text,:hash,0,:len)"
                    ),
                    {
                        "id": provision_id.bytes,
                        "ver": version_id.bytes,
                        "no": provision_no,
                        "text": full_text,
                        "hash": content_sha256(full_text),
                        "len": len(full_text),
                    },
                )

            # Old A: 第一条 unchanged, 第二条 removed in new, 第三条 changed.
            await insert_provision(
                version_old_a,
                new_uuid7(),
                "第一条",
                "第一条 稳定条文。",
            )
            await insert_provision(
                version_old_a,
                new_uuid7(),
                "第二条",
                "第二条 旧条文后来删除。",
            )
            await insert_provision(
                version_old_a,
                new_uuid7(),
                "第三条",
                "第三条 旧版内容。",
            )
            # New A: 第一条 unchanged, 第三条 changed, 第四条 added.
            await insert_provision(
                version_new_a,
                new_uuid7(),
                "第一条",
                "第一条 稳定条文。",
            )
            await insert_provision(
                version_new_a,
                new_uuid7(),
                "第三条",
                "第三条 新版内容。",
            )
            await insert_provision(
                version_new_a,
                new_uuid7(),
                "第四条",
                "第四条 新增条文。",
            )
            # Instrument B single provision to prove cross-instrument refusal.
            await insert_provision(
                version_b,
                new_uuid7(),
                "第一条",
                "其他法规第一条。",
            )
            await session.commit()
    finally:
        await engine.dispose()
    return {
        "instrument_a": instrument_a,
        "version_old_a": version_old_a,
        "version_new_a": version_new_a,
        "instrument_b": instrument_b,
        "version_b": version_b,
    }


def test_legal_version_diff_http_over_real_mysql(migrated_mysql_url: URL) -> None:
    redis_url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    if not asyncio.run(_redis_available(redis_url)):
        pytest.skip("test Redis unavailable")
    prefix = f"lawyer-test-version-diff:{uuid4().hex}:"
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
    ids = asyncio.run(_seed_corpus(migrated_mysql_url))
    client = TestClient(create_app(settings), base_url="https://testserver")
    with client:
        registered = client.post(
            "/api/v1/auth/register",
            headers={"Origin": _ORIGIN},
            json={
                "username": "version-diff-user",
                "password": _PASSWORD,
                "display_name": "版本对比用户",
            },
        )
        assert registered.status_code == 201, registered.text
        account_token = registered.json()["access_token"]
        headers = {"Authorization": f"Bearer {account_token}"}

        # 1. Same-instrument diff groups added/removed/modified/unchanged.
        base = "/api/v1/legal"
        diff_url = (
            f"{base}/version-diff?from_version_id={ids['version_old_a']}"
            f"&to_version_id={ids['version_new_a']}"
        )
        response = client.get(diff_url, headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["instrument_id"] == str(ids["instrument_a"])
        assert [p["provision_no"] for p in body["added"]] == ["第四条"]
        assert [p["provision_no"] for p in body["removed"]] == ["第二条"]
        assert [p["provision_no"] for p in body["unchanged"]] == ["第一条"]
        assert [p["provision_no"] for p in body["modified"]] == ["第三条"]
        modified = body["modified"][0]
        assert "旧版内容" in modified["previous"]["full_text"]
        assert "新版内容" in modified["current"]["full_text"]

        # 2. Cross-instrument versions -> 409.
        cross = (
            f"{base}/version-diff?from_version_id={ids['version_old_a']}"
            f"&to_version_id={ids['version_b']}"
        )
        refused = client.get(cross, headers=headers)
        assert refused.status_code == 409, refused.text
        assert refused.json()["code"] == "legal_version_diff_cross_instrument"

        # 3. Unknown version -> 404.
        missing = (
            f"{base}/version-diff?from_version_id={ids['version_old_a']}"
            f"&to_version_id={new_uuid7()}"
        )
        not_found = client.get(missing, headers=headers)
        assert not_found.status_code == 404, not_found.text
        assert not_found.json()["code"] == "legal_corpus_version_not_found"

        # 4. Unauthenticated -> 401.
        unauth = client.get(diff_url)
        assert unauth.status_code == 401
