from __future__ import annotations

import asyncio
from datetime import date
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_ai_job_migration import _alembic_config

from alembic import command
from lawyer_agent.application.legal_corpus import (
    CorpusFile,
    LegalCorpusInventoryService,
)
from lawyer_agent.domain.legal_corpus import (
    LegalVersionStatus,
    ProvisionLevel,
    content_sha256,
)
from lawyer_agent.infrastructure.persistence.repositories.legal_corpus import (
    SqlAlchemyLegalCorpusRepository,
)
from lawyer_agent.infrastructure.persistence.repositories.legal_corpus_inventory import (
    SqlAlchemyLegalCorpusInventoryRepository,
)

pytestmark = [pytest.mark.integration, pytest.mark.mysql]

_LEVEL_MAP = {
    ProvisionLevel.PART: "part",
    ProvisionLevel.CHAPTER: "chapter",
    ProvisionLevel.SECTION: "section",
    ProvisionLevel.ARTICLE: "article",
    ProvisionLevel.PARAGRAPH: "paragraph",
    ProvisionLevel.ITEM: "item",
    ProvisionLevel.SUB_ITEM: "sub_item",
}


async def _insert_two_versions(mysql_url: URL) -> tuple[UUID, UUID]:
    engine = create_async_engine(mysql_url)
    instrument_id = UUID("01a06ae2-6000-7000-8000-0000000000c1")
    version_old = UUID("01a06ae2-6100-7000-8000-0000000000c2")
    version_new = UUID("01a06ae2-6200-7000-8000-0000000000c3")
    provision_old = UUID("01a06ae2-6300-7000-8000-0000000000c4")
    provision_new = UUID("01a06ae2-6400-7000-8000-0000000000c5")
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
            await connection.execute(
                text(
                    "INSERT INTO legal_versions "
                    "(id,instrument_id,version_label,status,published_on,"
                    "effective_on,content_hash) "
                    "VALUES (:id,:instrument,'2020版','current',"
                    "'2020-01-01','2020-03-01',:h)"
                ),
                {"id": version_old.bytes, "instrument": instrument_id.bytes, "h": bytes(32)},
            )
            await connection.execute(
                text(
                    "INSERT INTO legal_versions "
                    "(id,instrument_id,version_label,status,published_on,"
                    "effective_on,content_hash) "
                    "VALUES (:id,:instrument,'2024版','current',"
                    "'2024-01-01','2024-05-01',:h)"
                ),
                {"id": version_new.bytes, "instrument": instrument_id.bytes, "h": bytes(32)},
            )
            await connection.execute(
                text(
                    "INSERT INTO legal_provisions "
                    "(id,version_id,provision_no,level,full_text,content_hash,"
                    "char_start,char_end) "
                    "VALUES (:id,:version,'第一条','article',"
                    ":text,:h,0,:end)"
                ),
                {
                    "id": provision_old.bytes,
                    "version": version_old.bytes,
                    "text": "第一条 2020 内容。",
                    "h": content_sha256("第一条 2020 内容。"),
                    "end": len("第一条 2020 内容。"),
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO legal_provisions "
                    "(id,version_id,provision_no,level,full_text,content_hash,"
                    "char_start,char_end) "
                    "VALUES (:id,:version,'第二条','article',"
                    ":text,:h,0,:end)"
                ),
                {
                    "id": provision_new.bytes,
                    "version": version_new.bytes,
                    "text": "第二条 2024 内容。",
                    "h": content_sha256("第二条 2024 内容。"),
                    "end": len("第二条 2024 内容。"),
                },
            )
    finally:
        await engine.dispose()
    return version_old, version_new


async def _cleanup(mysql_url: URL) -> None:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM legal_provisions"))
            await connection.execute(text("DELETE FROM legal_versions"))
            await connection.execute(text("DELETE FROM legal_instruments"))
    finally:
        await engine.dispose()


async def _run_query_checks(mysql_url: URL) -> None:
    old_id, new_id = await _insert_two_versions(mysql_url)
    from sqlalchemy.ext.asyncio import async_sessionmaker

    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            repo = SqlAlchemyLegalCorpusRepository(session)
            version_2021 = await repo.version_at(
                UUID("01a06ae2-6000-7000-8000-0000000000c1"), date(2021, 6, 1)
            )
            assert version_2021 is not None
            assert version_2021.id == old_id
            assert version_2021.status is LegalVersionStatus.CURRENT

            version_2025 = await repo.version_at(
                UUID("01a06ae2-6000-7000-8000-0000000000c1"), date(2025, 6, 1)
            )
            assert version_2025 is not None
            assert version_2025.id == new_id

            older = await repo.version_at(
                UUID("01a06ae2-6000-7000-8000-0000000000c1"), date(2019, 6, 1)
            )
            assert older is None

            provisions = await repo.provisions_for_version(new_id)
            assert len(provisions) == 1
            assert provisions[0].level is ProvisionLevel.ARTICLE
            assert "2024" in provisions[0].full_text
    finally:
        await engine.dispose()


def test_legal_corpus_query_version_at_and_provisions(mysql_url: URL) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    try:
        asyncio.run(_run_query_checks(mysql_url))
    finally:
        asyncio.run(_cleanup(mysql_url))


async def _run_inventory_check(mysql_url: URL) -> None:
    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with factory() as session:
            repo = SqlAlchemyLegalCorpusInventoryRepository(session)
            service = LegalCorpusInventoryService(repo, repo, "docx-zip-v1")
            payload_a = "第一条 内容A。".encode()
            payload_b = "第二条 内容B。".encode()
            result = await service.inventory(
                (
                    CorpusFile("object://corpus/a.docx", payload_a),
                    CorpusFile("object://corpus/b.docx", payload_b),
                    CorpusFile("object://corpus/a-copy.docx", payload_a),
                )
            )
            await session.commit()
            assert result.quality_metrics["counts"]["duplicates"] == 1
            found = await repo.find_batch_by_sha256(result.batch.file_sha256)
            assert found is not None
            assert found.batch_no == result.batch.batch_no
    finally:
        await engine.dispose()


def test_legal_corpus_inventory_writes_batch(mysql_url: URL) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    try:
        asyncio.run(_run_inventory_check(mysql_url))
    finally:
        asyncio.run(_cleanup(mysql_url))
