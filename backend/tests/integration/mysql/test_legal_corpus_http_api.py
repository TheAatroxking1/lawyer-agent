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


async def _seed_corpus(mysql_url: URL) -> tuple[UUID, UUID, UUID]:
    """One instrument with two versions (v2020 effective 2020-01-01, v2024 effective 2024-01-01)."""
    instrument_id = new_uuid7()
    version_old = new_uuid7()
    version_new = new_uuid7()
    provision_old = new_uuid7()
    provision_new = new_uuid7()
    old_text = "第一条 2020 年版条文。"
    new_text = "第一条 2024 年版条文。"
    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await session.execute(
                text(
                    "INSERT INTO legal_instruments "
                    "(id,title,issuing_authority,jurisdiction,version) "
                    "VALUES (:id,'中华人民共和国示例法','全国人民代表大会常务委员会',"
                    "'mainland_china',1)"
                ),
                {"id": instrument_id.bytes},
            )
            for version_id, label, status, effective, text_body, provision_id in (
                (
                    version_old,
                    "2020 修正",
                    "historical",
                    "2020-01-01",
                    old_text,
                    provision_old,
                ),
                (
                    version_new,
                    "2024 修正",
                    "current",
                    "2024-01-01",
                    new_text,
                    provision_new,
                ),
            ):
                await session.execute(
                    text(
                        "INSERT INTO legal_versions "
                        "(id,instrument_id,version_label,status,published_on,effective_on,"
                        "content_hash,source_ref,dataset_version,parser_version) "
                        "VALUES (:id,:inst,:label,:status,:pub,:eff,:hash,"
                        "'corpus/sample.docx','dataset_v1','docx-zip-v1')"
                    ),
                    {
                        "id": version_id.bytes,
                        "inst": instrument_id.bytes,
                        "label": label,
                        "status": status,
                        "pub": effective,
                        "eff": effective,
                        "hash": content_sha256(text_body),
                    },
                )
                await session.execute(
                    text(
                        "INSERT INTO legal_provisions "
                        "(id,version_id,provision_no,level,full_text,content_hash,"
                        "char_start,char_end) "
                        "VALUES (:id,:ver,'第一条','article',:text,:hash,0,:len)"
                    ),
                    {
                        "id": provision_id.bytes,
                        "ver": version_id.bytes,
                        "text": text_body,
                        "hash": content_sha256(text_body),
                        "len": len(text_body),
                    },
                )
            await session.commit()
    finally:
        await engine.dispose()
    return instrument_id, version_old, version_new


def test_legal_corpus_read_http_over_real_mysql(migrated_mysql_url: URL) -> None:
    redis_url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    if not asyncio.run(_redis_available(redis_url)):
        pytest.skip("test Redis unavailable")
    prefix = f"lawyer-test-corpus-http:{uuid4().hex}:"
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
    instrument_id, version_old, version_new = asyncio.run(
        _seed_corpus(migrated_mysql_url)
    )
    client = TestClient(create_app(settings), base_url="https://testserver")
    with client:
        registered = client.post(
            "/api/v1/auth/register",
            headers={"Origin": _ORIGIN},
            json={
                "username": "corpus-http-owner",
                "password": _PASSWORD,
                "display_name": "语料用户",
            },
        )
        assert registered.status_code == 201, registered.text
        account_token = registered.json()["access_token"]

        # 1. as_of before the first effective date -> 404.
        base = "/api/v1/legal"
        headers = {"Authorization": f"Bearer {account_token}"}

        # 0. Instrument identity is readable by id.
        identity = client.get(
            f"{base}/instruments/{instrument_id}", headers=headers
        )
        assert identity.status_code == 200, identity.text
        assert identity.json()["id"] == str(instrument_id)
        assert identity.json()["title"] == "中华人民共和国示例法"
        assert identity.json()["issuing_authority"] == "全国人民代表大会常务委员会"
        unknown_identity = client.get(
            f"{base}/instruments/{new_uuid7()}", headers=headers
        )
        assert unknown_identity.status_code == 404
        assert (
            unknown_identity.json()["code"]
            == "legal_corpus_instrument_not_found"
        )
        unauth_identity = client.get(
            f"{base}/instruments/{instrument_id}",
        )
        assert unauth_identity.status_code == 401

        none_yet = client.get(
            f"{base}/instruments/{instrument_id}/version?as_of=2019-06-01",
            headers=headers,
        )
        assert none_yet.status_code == 404
        assert none_yet.json()["code"] == "legal_corpus_version_not_found"

        # 2. as_of between the two versions -> the old one.
        old = client.get(
            f"{base}/instruments/{instrument_id}/version?as_of=2023-01-01",
            headers=headers,
        )
        assert old.status_code == 200, old.text
        assert old.json()["id"] == str(version_old)
        assert old.json()["status"] == "historical"
        assert old.json()["dataset_version"] == "dataset_v1"

        # 3. as_of at/after the new effective date -> the new one.
        new = client.get(
            f"{base}/instruments/{instrument_id}/version?as_of=2024-03-01",
            headers=headers,
        )
        assert new.status_code == 200, new.text
        assert new.json()["id"] == str(version_new)
        assert new.json()["status"] == "current"

        # 4. Invalid as_of -> 422.
        invalid = client.get(
            f"{base}/instruments/{instrument_id}/version?as_of=not-a-date",
            headers=headers,
        )
        assert invalid.status_code == 422

        # 5. Provisions are returned in order with full text.
        provisions = client.get(
            f"{base}/versions/{version_new}/provisions", headers=headers
        )
        assert provisions.status_code == 200, provisions.text
        rows = provisions.json()
        assert len(rows) == 1
        assert rows[0]["provision_no"] == "第一条"
        assert rows[0]["level"] == "article"
        assert "2024 年版条文" in rows[0]["full_text"]

        # 5b. Version history lists both versions newest-published first.
        history = client.get(
            f"{base}/instruments/{instrument_id}/versions", headers=headers
        )
        assert history.status_code == 200, history.text
        history_rows = history.json()
        assert [row["id"] for row in history_rows] == [
            str(version_new),
            str(version_old),
        ]
        assert history_rows[0]["version_label"] == "2024 修正"
        assert history_rows[0]["status"] == "current"
        assert history_rows[0]["dataset_version"] == "dataset_v1"
        assert history_rows[1]["status"] == "historical"

        # 5c. Unknown instrument -> 404 instrument_not_found.
        unknown_instrument = client.get(
            f"{base}/instruments/{new_uuid7()}/versions", headers=headers
        )
        assert unknown_instrument.status_code == 404
        assert (
            unknown_instrument.json()["code"]
            == "legal_corpus_instrument_not_found"
        )

        # 6. Unauthenticated -> 401.
        unauth = client.get(
            f"{base}/versions/{version_new}/provisions",
        )
        assert unauth.status_code == 401
        unauth_history = client.get(
            f"{base}/instruments/{instrument_id}/versions",
        )
        assert unauth_history.status_code == 401
