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


def _client(mysql_url: URL, *, label: str) -> TestClient:
    redis_url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    settings = Settings(
        environment="test",
        secret_key="h" * 32,
        database_url=mysql_url.render_as_string(hide_password=False),
        redis_url=redis_url,
        redis_key_prefix=f"{label}:{uuid4().hex}:",
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


def _register(client: TestClient, username: str) -> str:
    response = client.post(
        "/api/v1/auth/register",
        headers={"Origin": _ORIGIN},
        json={
            "username": username,
            "password": _PASSWORD,
            "display_name": "Agent 网关用户",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["access_token"]


def test_agent_gateway_requires_authentication(migrated_mysql_url: URL) -> None:
    redis_url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    if not asyncio.run(_redis_available(redis_url)):
        pytest.skip("test Redis unavailable")
    client = _client(migrated_mysql_url, label="lawyer-test-agent-unauth")
    with client:
        assert client.get("/api/v1/platform/agent/tools").status_code == 401
        assert (
            client.post(
                "/api/v1/platform/agent/tools/call",
                json={"tool": "meta.list_tools"},
            ).status_code
            == 401
        )


def test_agent_gateway_lists_and_calls_tools_over_real_mysql(
    migrated_mysql_url: URL,
) -> None:
    redis_url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    if not asyncio.run(_redis_available(redis_url)):
        pytest.skip("test Redis unavailable")
    client = _client(migrated_mysql_url, label="lawyer-test-agent-ok")
    with client:
        token = _register(client, "agent-gateway-owner")
        headers = {"Authorization": f"Bearer {token}"}

        listed = client.get("/api/v1/platform/agent/tools", headers=headers)
        assert listed.status_code == 200
        tools = listed.json()
        names = [tool["name"] for tool in tools]
        assert "meta.list_tools" in names
        assert "corpus.instruments_search" in names
        meta = next(tool for tool in tools if tool["name"] == "meta.list_tools")
        assert meta["description"]

        called = client.post(
            "/api/v1/platform/agent/tools/call",
            headers=headers,
            json={"tool": "meta.list_tools", "args": {}},
        )
        assert called.status_code == 200
        body = called.json()
        assert body["ok"] is True
        assert any(entry["name"] == "meta.list_tools" for entry in body["output"])

        # Real corpus tool: empty public corpus -> ok with an empty result list.
        search = client.post(
            "/api/v1/platform/agent/tools/call",
            headers=headers,
            json={"tool": "corpus.instruments_search", "args": {"title": "契"}},
        )
        assert search.status_code == 200
        assert search.json()["ok"] is True
        assert search.json()["output"] == []

        unknown = client.post(
            "/api/v1/platform/agent/tools/call",
            headers=headers,
            json={"tool": "unknown.tool", "args": {}},
        )
        assert unknown.status_code == 200
        assert unknown.json()["ok"] is False
        assert unknown.json()["error_code"] == "unknown_tool"

        invalid = client.post(
            "/api/v1/platform/agent/tools/call",
            headers=headers,
            json={"tool": "corpus.instruments_search", "args": {"limit": "many"}},
        )
        assert invalid.status_code == 200
        assert invalid.json()["ok"] is False
        assert invalid.json()["error_code"] == "invalid_arguments"

        malformed = client.post(
            "/api/v1/platform/agent/tools/call",
            headers=headers,
            json={"tool": "meta.list_tools", "extra": True},
        )
        assert malformed.status_code == 422


async def _seed_corpus_read_tools(mysql_url: URL) -> tuple[UUID, UUID, UUID]:
    """One instrument with a full 2024 version and an empty 2020 version."""
    instrument_id = new_uuid7()
    version_full = new_uuid7()
    version_empty = new_uuid7()
    provision_id = new_uuid7()
    full_text = "第一条 逾期支付租金的，应当支付违约金。"
    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await session.execute(
                text(
                    "INSERT INTO legal_instruments "
                    "(id,title,issuing_authority,jurisdiction,category,version) "
                    "VALUES (:id,'中华人民共和国示例法','全国人民代表大会常务委员会',"
                    "'mainland_china','judicial_interpretation',1)"
                ),
                {"id": instrument_id.bytes},
            )
            for version_id, label, status, effective, text_body, provision_row in (
                (
                    version_full,
                    "2024 修正",
                    "current",
                    "2024-01-01",
                    full_text,
                    provision_id,
                ),
                (
                    version_empty,
                    "2020 修正",
                    "historical",
                    "2020-01-01",
                    "",
                    None,
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
                if provision_row is not None:
                    await session.execute(
                        text(
                            "INSERT INTO legal_provisions "
                            "(id,version_id,provision_no,level,full_text,content_hash,"
                            "char_start,char_end) "
                            "VALUES (:id,:ver,'第一条','article',:text,:hash,0,:len)"
                        ),
                        {
                            "id": provision_row.bytes,
                            "ver": version_id.bytes,
                            "text": text_body,
                            "hash": content_sha256(text_body),
                            "len": len(text_body),
                        },
                    )
            await session.commit()
    finally:
        await engine.dispose()
    return instrument_id, version_full, version_empty


def test_agent_gateway_corpus_read_tools_over_real_mysql(
    migrated_mysql_url: URL,
) -> None:
    redis_url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    if not asyncio.run(_redis_available(redis_url)):
        pytest.skip("test Redis unavailable")
    instrument_id, version_full, version_empty = asyncio.run(
        _seed_corpus_read_tools(migrated_mysql_url)
    )
    client = _client(migrated_mysql_url, label="lawyer-test-agent-read")
    with client:
        token = _register(client, "agent-gateway-reader")
        headers = {"Authorization": f"Bearer {token}"}

        def call(tool: str, args: dict[str, object]) -> dict[str, object]:
            response = client.post(
                "/api/v1/platform/agent/tools/call",
                headers=headers,
                json={"tool": tool, "args": args},
            )
            assert response.status_code == 200, response.text
            return response.json()

        listed = client.get("/api/v1/platform/agent/tools", headers=headers)
        assert listed.status_code == 200
        names = [tool["name"] for tool in listed.json()]
        for expected in (
            "meta.list_tools",
            "corpus.instruments_search",
            "corpus.instrument_get",
            "corpus.versions_list",
            "corpus.provisions_list",
        ):
            assert expected in names

        # instrument_get: known instrument -> whitelisted identity.
        hit = call("corpus.instrument_get", {"instrument_id": str(instrument_id)})
        assert hit["ok"] is True
        assert hit["output"]["found"] is True
        assert hit["output"]["instrument"]["id"] == str(instrument_id)
        assert hit["output"]["instrument"]["title"] == "中华人民共和国示例法"
        assert (
            hit["output"]["instrument"]["category"]
            == "judicial_interpretation"
        )
        assert set(hit["output"]["instrument"]) == {
            "id",
            "title",
            "issuing_authority",
            "jurisdiction",
            "region_code",
            "category",
        }

        # instrument_get: unknown uuid7 -> structured miss, not a tool failure.
        missing = call("corpus.instrument_get", {"instrument_id": str(new_uuid7())})
        assert missing["ok"] is True
        assert missing["output"] == {"found": False, "instrument": None}

        # instrument_get: malformed id -> stable invalid_arguments.
        malformed = call("corpus.instrument_get", {"instrument_id": "not-a-uuid"})
        assert malformed["ok"] is False
        assert malformed["error_code"] == "invalid_arguments"

        # versions_list: both versions come back with whitelisted metadata.
        versions = call("corpus.versions_list", {"instrument_id": str(instrument_id)})
        assert versions["ok"] is True
        assert versions["output"]["found"] is True
        assert versions["output"]["version_count"] == 2
        assert versions["output"]["instrument"]["id"] == str(instrument_id)
        labels = {entry["version_label"] for entry in versions["output"]["versions"]}
        assert labels == {"2020 修正", "2024 修正"}
        by_label = {
            entry["version_label"]: entry for entry in versions["output"]["versions"]
        }
        assert by_label["2024 修正"]["status"] == "current"
        assert by_label["2024 修正"]["effective_on"] == "2024-01-01"
        assert set(by_label["2024 修正"]) == {
            "id",
            "version_label",
            "status",
            "published_on",
            "effective_on",
            "repealed_on",
            "law_number",
            "dataset_version",
            "parser_version",
        }

        # versions_list: unknown instrument -> structured miss.
        unknown_versions = call(
            "corpus.versions_list", {"instrument_id": str(new_uuid7())}
        )
        assert unknown_versions["ok"] is True
        assert unknown_versions["output"]["found"] is False
        assert unknown_versions["output"]["versions"] == []

        # provisions_list: full version returns the provision text.
        provisions = call("corpus.provisions_list", {"version_id": str(version_full)})
        assert provisions["ok"] is True
        assert provisions["output"]["found"] is True
        assert provisions["output"]["provision_count"] == 1
        assert provisions["output"]["version"]["version_label"] == "2024 修正"
        first = provisions["output"]["provisions"][0]
        assert first["provision_no"] == "第一条"
        assert first["level"] == "article"
        assert first["full_text"] == "第一条 逾期支付租金的，应当支付违约金。"
        assert set(first) == {
            "id",
            "provision_no",
            "level",
            "structure_path",
            "title",
            "full_text",
        }

        # provisions_list: version without provisions -> found with zero rows.
        empty = call("corpus.provisions_list", {"version_id": str(version_empty)})
        assert empty["ok"] is True
        assert empty["output"]["found"] is True
        assert empty["output"]["provision_count"] == 0
        assert empty["output"]["provisions"] == []

        # provisions_list: unknown version -> structured miss.
        unknown_provisions = call(
            "corpus.provisions_list", {"version_id": str(new_uuid7())}
        )
        assert unknown_provisions["ok"] is True
        assert unknown_provisions["output"]["found"] is False
        assert unknown_provisions["output"]["provisions"] == []
