from __future__ import annotations

import asyncio
import json
import os
import re
from base64 import b64encode
from collections.abc import AsyncIterator, Iterator
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from lawyer_agent.application.legal_chat import LegalChatTimeout
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

        # 5d. A single version is readable directly by id.
        version_by_id = client.get(
            f"{base}/versions/{version_new}", headers=headers
        )
        assert version_by_id.status_code == 200, version_by_id.text
        assert version_by_id.json()["id"] == str(version_new)
        assert version_by_id.json()["version_label"] == "2024 修正"
        assert version_by_id.json()["status"] == "current"
        assert version_by_id.json()["dataset_version"] == "dataset_v1"
        assert version_by_id.json()["instrument_id"] == str(instrument_id)
        unknown_version = client.get(
            f"{base}/versions/{new_uuid7()}", headers=headers
        )
        assert unknown_version.status_code == 404
        assert unknown_version.json()["code"] == "legal_corpus_version_not_found"
        unauth_version = client.get(f"{base}/versions/{version_new}")
        assert unauth_version.status_code == 401

        # 6. Unauthenticated -> 401.
        unauth = client.get(
            f"{base}/versions/{version_new}/provisions",
        )
        assert unauth.status_code == 401
        unauth_history = client.get(
            f"{base}/instruments/{instrument_id}/versions",
        )
        assert unauth_history.status_code == 401


async def _seed_list_instruments(
    mysql_url: URL,
) -> list[UUID]:
    """Three national instruments with distinct titles/authorities/regions."""
    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    ids = [new_uuid7() for _ in range(3)]
    rows = [
        (
            ids[0],
            "中华人民共和国耕地占用税法",
            "全国人民代表大会常务委员会",
            "national",
            None,
            "2026-01-01 00:00:00.000001",
        ),
        (
            ids[1],
            "中华人民共和国契税法",
            "全国人民代表大会常务委员会",
            "national",
            "110000",
            "2026-01-01 00:00:00.000002",
        ),
        (
            ids[2],
            "北京市大气污染防治条例",
            "北京市人民代表大会常务委员会",
            "national",
            "310000",
            "2026-01-01 00:00:00.000003",
        ),
    ]
    try:
        async with factory() as session:
            for instrument_id, title, authority, jurisdiction, region, created in rows:
                await session.execute(
                    text(
                        "INSERT INTO legal_instruments "
                        "(id,title,issuing_authority,jurisdiction,region_code,"
                        "version,created_at) "
                        "VALUES (:id,:title,:authority,:jurisdiction,:region,1,:created)"
                    ),
                    {
                        "id": instrument_id.bytes,
                        "title": title,
                        "authority": authority,
                        "jurisdiction": jurisdiction,
                        "region": region,
                        "created": created,
                    },
                )
            await session.commit()
    finally:
        await engine.dispose()
    return ids


def test_legal_instrument_list_search_over_real_mysql(
    migrated_mysql_url: URL,
) -> None:
    redis_url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    if not asyncio.run(_redis_available(redis_url)):
        pytest.skip("test Redis unavailable")
    prefix = f"lawyer-test-corpus-list:{uuid4().hex}:"
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
    ids = asyncio.run(_seed_list_instruments(migrated_mysql_url))
    client = TestClient(create_app(settings), base_url="https://testserver")
    try:
        with client:
            registered = client.post(
                "/api/v1/auth/register",
                headers={"Origin": _ORIGIN},
                json={
                    "username": "corpus-list-owner",
                    "password": _PASSWORD,
                    "display_name": "语料列表用户",
                },
            )
            assert registered.status_code == 201, registered.text
            account_token = registered.json()["access_token"]
            base = "/api/v1/legal"
            headers = {"Authorization": f"Bearer {account_token}"}

            # 1. National-listed instruments include all three new rows.
            page = client.get(
                f"{base}/instruments?jurisdiction=national", headers=headers
            )
            assert page.status_code == 200, page.text
            body = page.json()
            listed = {row["id"] for row in body["items"]}
            assert {str(instrument_id) for instrument_id in ids} <= listed
            assert body["next_before_id"] is None or len(body["items"]) < 20

            # 2. Filtered by title substring narrows to the one matching row.
            by_title = client.get(
                f"{base}/instruments?jurisdiction=national&title=契税法",
                headers=headers,
            )
            assert by_title.status_code == 200, by_title.text
            title_items = by_title.json()["items"]
            assert [row["id"] for row in title_items] == [str(ids[1])]

            # 3. Authority + jurisdiction + region filters combine.
            filtered = client.get(
                f"{base}/instruments?issuing_authority=北京&jurisdiction=national"
                "&region_code=310000",
                headers=headers,
            )
            assert filtered.status_code == 200, filtered.text
            filtered_items = filtered.json()["items"]
            assert [row["id"] for row in filtered_items] == [str(ids[2])]

            # 4. Keyset pagination: limit=1 walks all three without loss/dup.
            seen: list[UUID] = []
            cursor: str | None = None
            for _ in range(3):
                url = f"{base}/instruments?limit=1&jurisdiction=national"
                if cursor is not None:
                    url += f"&before_id={cursor}"
                walked = client.get(url, headers=headers)
                assert walked.status_code == 200, walked.text
                walked_body = walked.json()
                assert len(walked_body["items"]) == 1
                seen.append(UUID(walked_body["items"][0]["id"]))
                cursor = walked_body["next_before_id"]
            assert len(set(seen)) == 3
            assert set(seen) == set(ids)
            # Newest-first: ids[2] was created latest.
            assert seen[0] == ids[2]

            # 5. Unknown cursor -> 404 stable cursor-invalid error.
            unknown_cursor = client.get(
                f"{base}/instruments?before_id={new_uuid7()}", headers=headers
            )
            assert unknown_cursor.status_code == 404
            assert (
                unknown_cursor.json()["code"]
                == "legal_corpus_instrument_cursor_invalid"
            )

            # 6. Blank search text -> 422 stable invalid-request error.
            blank = client.get(
                f"{base}/instruments?title=%20%20", headers=headers
            )
            assert blank.status_code == 422
            assert blank.json()["code"] == "legal_corpus_invalid_request"

            # 7. Unauthenticated -> 401.
            unauth = client.get(f"{base}/instruments")
            assert unauth.status_code == 401
    finally:
        asyncio.run(_cleanup_list_instruments(migrated_mysql_url, ids))


async def _cleanup_list_instruments(mysql_url: URL, ids: list[UUID]) -> None:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.begin() as connection:
            for instrument_id in ids:
                await connection.execute(
                    text("DELETE FROM legal_instruments WHERE id=:id"),
                    {"id": instrument_id.bytes},
                )
    finally:
        await engine.dispose()


async def _seed_dataset_snapshots(mysql_url: URL) -> list[str]:
    """Two snapshots: one published (dated) and one pending (undated)."""
    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    names = ["dataset_v1", "dataset_v2"]
    try:
        async with factory() as session:
            published_id = new_uuid7()
            await session.execute(
                text(
                    "INSERT INTO legal_dataset_snapshots "
                    "(id,dataset_name,parser_version,state,manifest_json,"
                    "quality_metrics_json,released_at) "
                    "VALUES (:id,'dataset_v1','docx-v1','published',"
                    "'{\"files\": 134}',"
                    "'{\"article_count\": 134, \"coverage\": 1.0, "
                    "\"parse_failures\": 0, \"indexed_documents\": 134, "
                    "\"dimension\": 512}',"
                    "'2026-01-15 00:00:00.000000')"
                ),
                {"id": published_id.bytes},
            )
            pending_id = new_uuid7()
            await session.execute(
                text(
                    "INSERT INTO legal_dataset_snapshots "
                    "(id,dataset_name,parser_version,state,manifest_json,"
                    "quality_metrics_json,released_at) "
                    "VALUES (:id,'dataset_v2','docx-v2','pending',"
                    "'{\"files\": 0}',"
                    "'{\"article_count\": 0, \"coverage\": 0.0, "
                    "\"parse_failures\": 0, \"required_field_missing\": []}',NULL)"
                ),
                {"id": pending_id.bytes},
            )
            await session.commit()
    finally:
        await engine.dispose()
    return names


def test_legal_dataset_snapshot_read_http_over_real_mysql(
    migrated_mysql_url: URL,
) -> None:
    redis_url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    if not asyncio.run(_redis_available(redis_url)):
        pytest.skip("test Redis unavailable")
    prefix = f"lawyer-test-corpus-dataset:{uuid4().hex}:"
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
    names = asyncio.run(_seed_dataset_snapshots(migrated_mysql_url))
    client = TestClient(create_app(settings), base_url="https://testserver")
    try:
        with client:
            registered = client.post(
                "/api/v1/auth/register",
                headers={"Origin": _ORIGIN},
                json={
                    "username": "corpus-dataset-owner",
                    "password": _PASSWORD,
                    "display_name": "语料数据集用户",
                },
            )
            assert registered.status_code == 201, registered.text
            account_token = registered.json()["access_token"]
            base = "/api/v1/legal"
            headers = {"Authorization": f"Bearer {account_token}"}

            # 1. Listing returns the published (dated) snapshot first.
            listing = client.get(f"{base}/datasets", headers=headers)
            assert listing.status_code == 200, listing.text
            rows = listing.json()
            assert [row["dataset_name"] for row in rows] == ["dataset_v1", "dataset_v2"]
            assert rows[0]["state"] == "published"
            assert rows[0]["released_at"] is not None
            assert rows[0]["quality_metrics"] == {
                "article_count": 134,
                "coverage": 1.0,
                "parse_failures": 0,
                "indexed_documents": 134,
                "dimension": 512,
            }
            assert rows[1]["state"] == "pending"
            assert rows[1]["released_at"] is None

            # 2. Reading by name returns that snapshot's whitelisted fields.
            by_name = client.get(f"{base}/datasets/dataset_v1", headers=headers)
            assert by_name.status_code == 200, by_name.text
            detail = by_name.json()
            assert detail["dataset_name"] == "dataset_v1"
            assert detail["parser_version"] == "docx-v1"
            assert detail["quality_metrics"]["article_count"] == 134

            # 3. Unknown dataset -> 404 stable not-found error.
            unknown = client.get(f"{base}/datasets/dataset_unknown", headers=headers)
            assert unknown.status_code == 404
            assert (
                unknown.json()["code"]
                == "legal_dataset_snapshot_not_found"
            )

            # 4. Invalid name -> 422.
            invalid = client.get(f"{base}/datasets/not%20valid", headers=headers)
            assert invalid.status_code == 422
            assert invalid.json()["code"] == "legal_corpus_invalid_request"

            # 5. Unauthenticated -> 401.
            unauth = client.get(f"{base}/datasets")
            assert unauth.status_code == 401
    finally:
        asyncio.run(_cleanup_dataset_snapshots(migrated_mysql_url, names))


async def _cleanup_dataset_snapshots(mysql_url: URL, names: list[str]) -> None:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.begin() as connection:
            for name in names:
                await connection.execute(
                    text("DELETE FROM legal_dataset_snapshots WHERE dataset_name=:name"),
                    {"name": name},
                )
    finally:
        await engine.dispose()


async def _seed_load_batches(mysql_url: URL) -> list[UUID]:
    """Three load batches with distinct created_at timestamps."""
    import json as _json

    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    ids = [new_uuid7() for _ in range(3)]
    hashes = [bytes([index + 1]) * 32 for index in range(3)]
    rows = [
        (
            ids[0],
            hashes[0],
            "B20260101000001AA",
            "object://corpus/c1.docx",
            "docx-v1",
            "completed",
            {"files": 2, "unique": 2, "duplicates": 0},
            "2026-01-01 00:00:00.000001",
            "2026-01-01 00:05:00.000000",
            None,
        ),
        (
            ids[1],
            hashes[1],
            "B20260101000002BB",
            "object://corpus/c2.docx",
            "docx-v1",
            "inventoried",
            {"files": 1, "unique": 1, "duplicates": 0},
            "2026-01-01 00:00:00.000002",
            None,
            None,
        ),
        (
            ids[2],
            hashes[2],
            "B20260101000003CC",
            "object://corpus/c3.docx",
            "docx-v2",
            "failed",
            {"files": 1, "unique": 1, "duplicates": 0},
            "2026-01-01 00:00:00.000003",
            "2026-01-01 00:09:00.000000",
            "parse failure",
        ),
    ]
    try:
        async with factory() as session:
            for row in rows:
                batch_id, file_sha, batch_no, source_ref = row[:4]
                parser, status, counts, started, completed, error = row[4:]
                await session.execute(
                    text(
                        "INSERT INTO legal_load_batches "
                        "(id,batch_no,source_ref,file_sha256,parser_version,status,"
                        "item_counts_json,started_at,completed_at,error_message,created_at) "
                        "VALUES (:id,:no,:source,:sha,:parser,:status,:counts,"
                        ":started,:completed,:error,:created)"
                    ),
                    {
                        "id": batch_id.bytes,
                        "no": batch_no,
                        "source": source_ref,
                        "sha": file_sha,
                        "parser": parser,
                        "status": status,
                        "counts": _json.dumps(counts, ensure_ascii=False),
                        "started": started,
                        "completed": completed,
                        "error": error,
                        "created": started,
                    },
                )
            await session.commit()
    finally:
        await engine.dispose()
    return ids


def test_legal_load_batch_read_http_over_real_mysql(migrated_mysql_url: URL) -> None:
    redis_url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    if not asyncio.run(_redis_available(redis_url)):
        pytest.skip("test Redis unavailable")
    prefix = f"lawyer-test-corpus-batch:{uuid4().hex}:"
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
    batch_ids = asyncio.run(_seed_load_batches(migrated_mysql_url))
    client = TestClient(create_app(settings), base_url="https://testserver")
    try:
        with client:
            registered = client.post(
                "/api/v1/auth/register",
                headers={"Origin": _ORIGIN},
                json={
                    "username": "corpus-batch-owner",
                    "password": _PASSWORD,
                    "display_name": "语料批次用户",
                },
            )
            assert registered.status_code == 201, registered.text
            account_token = registered.json()["access_token"]
            base = "/api/v1/legal"
            headers = {"Authorization": f"Bearer {account_token}"}

            # 1. Keyset pagination limit=2 walks all three newest first.
            seen: list[UUID] = []
            cursor: str | None = None
            for _ in range(2):
                url = f"{base}/load-batches?limit=2"
                if cursor is not None:
                    url += f"&before_id={cursor}"
                walked = client.get(url, headers=headers)
                assert walked.status_code == 200, walked.text
                body = walked.json()
                seen.extend(UUID(item["id"]) for item in body["items"])
                cursor = body["next_before_id"]
            assert len(seen) == 3
            assert set(seen) == set(batch_ids)
            assert cursor is None
            assert seen[0] == batch_ids[2]  # newest first

            # 2. Single batch by id returns whitelisted fields.
            single = client.get(f"{base}/load-batches/{batch_ids[0]}", headers=headers)
            assert single.status_code == 200, single.text
            detail = single.json()
            assert detail["batch_no"] == "B20260101000001AA"
            assert detail["status"] == "completed"
            assert detail["source_ref"] == "object://corpus/c1.docx"
            assert detail["item_counts"]["files"] == 2

            # 3. Unknown batch -> 404 stable code.
            unknown = client.get(f"{base}/load-batches/{new_uuid7()}", headers=headers)
            assert unknown.status_code == 404
            assert unknown.json()["code"] == "legal_corpus_load_batch_not_found"

            # 4. Unknown cursor -> 404 stable code.
            bad_cursor = client.get(
                f"{base}/load-batches?before_id={new_uuid7()}", headers=headers
            )
            assert bad_cursor.status_code == 404
            assert (
                bad_cursor.json()["code"]
                == "legal_corpus_load_batch_cursor_invalid"
            )

            # 5. Unauthenticated -> 401.
            unauth = client.get(f"{base}/load-batches")
            assert unauth.status_code == 401
    finally:
        asyncio.run(_cleanup_load_batches(migrated_mysql_url, batch_ids))


async def _cleanup_load_batches(mysql_url: URL, ids: list[UUID]) -> None:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.begin() as connection:
            for batch_id in ids:
                await connection.execute(
                    text("DELETE FROM legal_load_batches WHERE id=:id"),
                    {"id": batch_id.bytes},
                )
    finally:
        await engine.dispose()


async def _seed_batch_with_issues(mysql_url: URL) -> tuple[UUID, list[UUID]]:
    """One failed batch with two quality issue rows."""
    import json as _json

    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    batch_id = new_uuid7()
    issue_ids = [new_uuid7(), new_uuid7()]
    file_sha = bytes([9]) * 32
    counts = {"files": 1, "unique": 1, "duplicates": 0}
    try:
        async with factory() as session:
            await session.execute(
                text(
                    "INSERT INTO legal_load_batches "
                    "(id,batch_no,source_ref,file_sha256,parser_version,status,"
                    "item_counts_json,started_at,created_at) "
                    "VALUES (:id,'B20260606000001ZZ','object://corpus/q.docx',"
                    ":sha,'docx-v1','inventoried',:counts,"
                    "'2026-06-06 00:00:00.000000','2026-06-06 00:00:00.000000')"
                ),
                {
                    "id": batch_id.bytes,
                    "sha": file_sha,
                    "counts": _json.dumps(counts, ensure_ascii=False),
                },
            )
            for index, issue_id in enumerate(issue_ids):
                await session.execute(
                    text(
                        "INSERT INTO legal_quality_issues "
                        "(id,batch_id,file_sha256,issue_type,message,created_at) "
                        "VALUES (:id,:batch,:sha,:type,:message,"
                        "'2026-06-06 00:00:00.000000')"
                    ),
                    {
                        "id": issue_id.bytes,
                        "batch": batch_id.bytes,
                        "sha": file_sha,
                        "type": (
                            "missing_required_fields"
                            if index == 0
                            else "article_sequence_break"
                        ),
                        "message": (
                            "missing required fields"
                            if index == 0
                            else "article_sequence_break:第三条"
                        ),
                    },
                )
            await session.commit()
    finally:
        await engine.dispose()
    return batch_id, issue_ids


def test_legal_load_batch_quality_issues_http_over_real_mysql(
    migrated_mysql_url: URL,
) -> None:
    redis_url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    if not asyncio.run(_redis_available(redis_url)):
        pytest.skip("test Redis unavailable")
    prefix = f"lawyer-test-corpus-quality:{uuid4().hex}:"
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
    batch_id, issue_ids = asyncio.run(
        _seed_batch_with_issues(migrated_mysql_url)
    )
    client = TestClient(create_app(settings), base_url="https://testserver")
    try:
        with client:
            registered = client.post(
                "/api/v1/auth/register",
                headers={"Origin": _ORIGIN},
                json={
                    "username": "corpus-quality-owner",
                    "password": _PASSWORD,
                    "display_name": "语料质量用户",
                },
            )
            assert registered.status_code == 201, registered.text
            account_token = registered.json()["access_token"]
            base = "/api/v1/legal"
            headers = {"Authorization": f"Bearer {account_token}"}

            issues = client.get(
                f"{base}/load-batches/{batch_id}/quality-issues", headers=headers
            )
            assert issues.status_code == 200, issues.text
            rows = issues.json()
            assert {row["issue_type"] for row in rows} == {
                "missing_required_fields",
                "article_sequence_break",
            }
            assert {row["message"] for row in rows} == {
                "missing required fields",
                "article_sequence_break:第三条",
            }

            unknown = client.get(
                f"{base}/load-batches/{new_uuid7()}/quality-issues", headers=headers
            )
            assert unknown.status_code == 404
            assert unknown.json()["code"] == "legal_corpus_load_batch_not_found"

            unauth = client.get(f"{base}/load-batches/{batch_id}/quality-issues")
            assert unauth.status_code == 401
    finally:
        asyncio.run(
            _cleanup_batch_with_issues(migrated_mysql_url, batch_id, issue_ids)
        )


async def _cleanup_batch_with_issues(
    mysql_url: URL, batch_id: UUID, issue_ids: list[UUID]
) -> None:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "DELETE FROM legal_quality_issues "
                    "WHERE id IN (:first,:second)"
                ),
                {"first": issue_ids[0].bytes, "second": issue_ids[1].bytes},
            )
            await connection.execute(
                text("DELETE FROM legal_load_batches WHERE id=:id"),
                {"id": batch_id.bytes},
            )
    finally:
        await engine.dispose()


def test_legal_chat_http_without_deepseek_key_over_real_mysql(
    migrated_mysql_url: URL,
) -> None:
    redis_url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    if not asyncio.run(_redis_available(redis_url)):
        pytest.skip("test Redis unavailable")
    prefix = f"lawyer-test-chat:{uuid4().hex}:"
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
                "username": "corpus-chat-owner",
                "password": _PASSWORD,
                "display_name": "语料对话用户",
            },
        )
        assert registered.status_code == 201, registered.text
        account_token = registered.json()["access_token"]
        base = "/api/v1/legal"
        headers = {"Authorization": f"Bearer {account_token}"}

        # 1. No DeepSeek API key configured -> stable 503, never a fake answer.
        reply = client.post(
            f"{base}/chat",
            headers=headers,
            json={
                "messages": [
                    {"role": "system", "content": "你是法律助手。"},
                    {"role": "user", "content": "违约金怎么算？"},
                ]
            },
        )
        assert reply.status_code == 503
        assert reply.json()["code"] == "model_provider_unavailable"

        # 2. Invalid body -> 422 problem details.
        bad_role = client.post(
            f"{base}/chat",
            headers=headers,
            json={
                "messages": [{"role": "admin", "content": "x"}]
            },
        )
        assert bad_role.status_code == 422
        empty = client.post(
            f"{base}/chat", headers=headers, json={"messages": []}
        )
        assert empty.status_code == 422

        # 3. Unauthenticated -> 401.
        unauth = client.post(
            f"{base}/chat",
            json={
                "messages": [{"role": "user", "content": "x"}]
            },
        )
        assert unauth.status_code == 401

        # 4. Stream endpoint: unauthenticated -> 401, no key -> 503 before stream.
        stream_unauth = client.post(
            f"{base}/chat/stream",
            json={
                "messages": [{"role": "user", "content": "x"}]
            },
        )
        assert stream_unauth.status_code == 401
        stream_no_key = client.post(
            f"{base}/chat/stream",
            headers=headers,
            json={
                "messages": [{"role": "user", "content": "违约金怎么算？"}]
            },
        )
        assert stream_no_key.status_code == 503
        assert stream_no_key.json()["code"] == "model_provider_unavailable"
        bad_stream = client.post(
            f"{base}/chat/stream",
            headers=headers,
            json={"messages": [{"role": "admin", "content": "x"}]},
        )
        assert bad_stream.status_code == 422
        empty_stream = client.post(
            f"{base}/chat/stream",
            headers=headers,
            json={"messages": []},
        )
        assert empty_stream.status_code == 422

        # 5. Stream endpoint with a stream-capable service -> token deltas over SSE.
        client.app.state.services.legal_chat_http = _FakeChatStreamService(
            deltas=("你", "好")
        )
        allowed = client.post(
            f"{base}/chat/stream",
            headers=headers,
            json={
                "messages": [{"role": "user", "content": "继续"}]
            },
        )
        assert allowed.status_code == 200, allowed.text
        events = _sse_events(allowed.text)
        assert [name for name, _ in events] == ["started", "delta", "delta", "done"]
        assert json.loads(events[1][1])["text"] == "你"
        assert json.loads(events[2][1])["text"] == "好"

        # 6. Mid-stream gateway failure -> partial delta then a stable error event.
        client.app.state.services.legal_chat_http = _FakeChatStreamService(
            deltas=("部分",), fail_after=LegalChatTimeout("provider timed out")
        )
        failed = client.post(
            f"{base}/chat/stream",
            headers=headers,
            json={
                "messages": [{"role": "user", "content": "继续"}]
            },
        )
        assert failed.status_code == 200, failed.text
        failed_events = _sse_events(failed.text)
        assert [name for name, _ in failed_events] == [
            "started",
            "delta",
            "error",
            "done",
        ]
        assert json.loads(failed_events[1][1])["text"] == "部分"
        error_payload = json.loads(failed_events[2][1])
        assert error_payload["status"] == 504
        assert error_payload["code"] == "model_provider_timeout"


class _FakeChatStreamService:
    """App-state stand-in for the legal chat service over SSE (deltas or error)."""

    def __init__(
        self,
        *,
        deltas: tuple[str, ...] = ("你", "好"),
        fail_after: Exception | None = None,
    ) -> None:
        self._deltas = tuple(deltas)
        self._fail_after = fail_after
        self.gateway_available = True

    async def chat_stream(self, messages: object) -> AsyncIterator[str]:
        del messages
        for index, delta in enumerate(self._deltas):
            yield delta
            if self._fail_after is not None and index == len(self._deltas) - 1:
                raise self._fail_after


def _sse_events(raw: str) -> list[tuple[str, str]]:
    events: list[tuple[str, str]] = []
    current: str | None = None
    data_lines: list[str] = []
    for line in raw.splitlines():
        if line.startswith("event: "):
            current = line[7:]
        elif line.startswith("data: "):
            data_lines.append(line[6:])
        elif not line and current is not None:
            events.append((current, "\n".join(data_lines)))
            current = None
            data_lines = []
    return events
