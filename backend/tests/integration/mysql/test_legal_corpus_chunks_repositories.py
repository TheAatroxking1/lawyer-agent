from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_ai_job_migration import _alembic_config

from alembic import command
from lawyer_agent.application.legal_corpus_chunks import derive_chunks
from lawyer_agent.domain.legal_corpus import (
    ChunkType,
    content_sha256,
)
from lawyer_agent.infrastructure.persistence.repositories.legal_corpus import (
    SqlAlchemyLegalCorpusChunkRepository,
    SqlAlchemyLegalCorpusRepository,
)

pytestmark = [pytest.mark.integration, pytest.mark.mysql]

_VERSION_OLD = UUID("01a06ae2-6100-7000-8000-0000000000c2")
_VERSION_NEW = UUID("01a06ae2-6200-7000-8000-0000000000c3")


async def _seed(mysql_url: URL) -> None:
    instrument_id = UUID("01a06ae2-6000-7000-8000-0000000000c1")
    provision_old = UUID("01a06ae2-6300-7000-8000-0000000000c4")
    provision_new = UUID("01a06ae2-6400-7000-8000-0000000000c5")
    engine = create_async_engine(mysql_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO legal_instruments "
                    "(id,title,issuing_authority,jurisdiction,version) "
                    "VALUES (:id,'示范法','机关','national',1)"
                ),
                {"id": instrument_id.bytes},
            )
            for version_id, label, text_body, provision_id in (
                (_VERSION_OLD, "2020版", "第一条 2020 内容。", provision_old),
                (_VERSION_NEW, "2024版", "第一条 2024 内容。", provision_new),
            ):
                await connection.execute(
                    text(
                        "INSERT INTO legal_versions "
                        "(id,instrument_id,version_label,status,published_on,"
                        "effective_on,content_hash) "
                        "VALUES (:id,:instrument,:label,'current',"
                        "'2020-01-01','2020-03-01',:h)"
                    ),
                    {
                        "id": version_id.bytes,
                        "instrument": instrument_id.bytes,
                        "label": label,
                        "h": bytes(32),
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO legal_provisions "
                        "(id,version_id,provision_no,level,full_text,content_hash,"
                        "char_start,char_end) "
                        "VALUES (:id,:version,'第一条','article',:text,:h,0,:end)"
                    ),
                    {
                        "id": provision_id.bytes,
                        "version": version_id.bytes,
                        "text": text_body,
                        "h": content_sha256(text_body),
                        "end": len(text_body),
                    },
                )
    finally:
        await engine.dispose()


async def _run(mysql_url: URL) -> None:
    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            repo = SqlAlchemyLegalCorpusRepository(session)
            chunk_repo = SqlAlchemyLegalCorpusChunkRepository(session)
            provisions = await repo.provisions_for_version(_VERSION_NEW)
            chunks = derive_chunks(
                version_id=_VERSION_NEW,
                provisions=provisions,
                parser_version="docx-zip-v1",
            )
            await chunk_repo.replace_chunks_for_version(_VERSION_NEW, chunks)
    finally:
        await engine.dispose()


async def _row_count(mysql_url: URL) -> tuple[int, int]:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.connect() as connection:
            new_rows = await connection.scalar(
                text("SELECT COUNT(*) FROM legal_chunks WHERE version_id = :v").bindparams(
                    v=_VERSION_NEW.bytes
                )
            )
            old_rows = await connection.scalar(
                text("SELECT COUNT(*) FROM legal_chunks WHERE version_id = :v").bindparams(
                    v=_VERSION_OLD.bytes
                )
            )
            return int(new_rows or 0), int(old_rows or 0)
    finally:
        await engine.dispose()


async def _read_chunk(mysql_url: URL) -> tuple[str, bytes, str]:
    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            repo = SqlAlchemyLegalCorpusChunkRepository(session)
            chunks = await repo.chunks_for_version(_VERSION_NEW)
            assert len(chunks) == 1
            chunk = chunks[0]
            assert chunk.chunk_type is ChunkType.PROVISION
            assert chunk.version_id == _VERSION_NEW
            return chunk.content, chunk.content_hash, chunk.parser_version or ""
    finally:
        await engine.dispose()


def test_legal_chunk_persistence_over_real_mysql(mysql_url: URL) -> None:
    database_name = f"lawyer_test_{__import__('uuid').uuid4().hex}"
    if not __import__("re").match(r"^lawyer_test_[a-f0-9]{32}$", database_name):
        raise RuntimeError("refusing to manage an unexpected database name")
    isolated_url = mysql_url.set(database=database_name)
    asyncio.run(_create_database(mysql_url, database_name))
    try:
        command.upgrade(_alembic_config(isolated_url), "head")
        asyncio.run(_seed(isolated_url))
        asyncio.run(_run(isolated_url))

        # 1. One chunk persisted for the new version with correct content.
        content, digest, parser_version = asyncio.run(_read_chunk(isolated_url))
        assert content == "第一条 2024 内容。"
        assert digest == content_sha256(content)
        assert parser_version == "docx-zip-v1"

        # 2. Re-running replace is idempotent (still one row).
        asyncio.run(_run(isolated_url))
        new_rows, old_rows = asyncio.run(_row_count(isolated_url))
        assert new_rows == 1
        # 3. The other version has no chunks (no cross-version leakage).
        assert old_rows == 0
    finally:
        asyncio.run(_drop_database(mysql_url, database_name))


async def _create_database(mysql_url: URL, database_name: str) -> None:
    engine = create_async_engine(mysql_url.set(database="mysql"))
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f"CREATE DATABASE `{database_name}`"))
    finally:
        await engine.dispose()


async def _drop_database(mysql_url: URL, database_name: str) -> None:
    engine = create_async_engine(mysql_url.set(database="mysql"))
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f"DROP DATABASE `{database_name}`"))
    finally:
        await engine.dispose()
