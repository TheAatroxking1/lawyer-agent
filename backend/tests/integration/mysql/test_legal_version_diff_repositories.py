from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_ai_job_migration import _alembic_config

from alembic import command
from lawyer_agent.application.legal_corpus_diff import (
    LegalVersionDiffError,
    LegalVersionDiffService,
)
from lawyer_agent.domain.legal_corpus import content_sha256
from lawyer_agent.infrastructure.persistence.repositories.legal_corpus import (
    SqlAlchemyLegalCorpusRepository,
)

pytestmark = [pytest.mark.integration, pytest.mark.mysql]

_INSTRUMENT_A = UUID("01a06ae2-6000-7000-8000-0000000000c1")
_VERSION_OLD_A = UUID("01a06ae2-6100-7000-8000-0000000000c2")
_VERSION_NEW_A = UUID("01a06ae2-6200-7000-8000-0000000000c3")
_INSTRUMENT_B = UUID("01a06ae2-6000-7000-8000-0000000000d1")
_VERSION_B = UUID("01a06ae2-6100-7000-8000-0000000000d2")


async def _seed(mysql_url: URL) -> None:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.begin() as connection:
            # Two instruments so the cross-instrument refusal can be exercised.
            for instrument_id, title in (
                (_INSTRUMENT_A, "中华人民共和国民法典"),
                (_INSTRUMENT_B, "中华人民共和国刑法"),
            ):
                await connection.execute(
                    text(
                        "INSERT INTO legal_instruments "
                        "(id,title,issuing_authority,jurisdiction,version) "
                        "VALUES (:id,:title,'全国人民代表大会','national',1)"
                    ),
                    {"id": instrument_id.bytes, "title": title},
                )
            versions = (
                (_VERSION_OLD_A, _INSTRUMENT_A, "2020 公布版", "2020-05-28", "2021-01-01"),
                (_VERSION_NEW_A, _INSTRUMENT_A, "2023 修正版", "2023-05-28", "2024-01-01"),
                (_VERSION_B, _INSTRUMENT_B, "2020 公布版", "2020-05-28", "2021-01-01"),
            )
            for version_id, instrument_id, label, published, effective in versions:
                await connection.execute(
                    text(
                        "INSERT INTO legal_versions "
                        "(id,instrument_id,version_label,status,published_on,"
                        "effective_on,content_hash) "
                        "VALUES (:id,:instrument,:label,'current',:published,"
                        ":effective,:h)"
                    ),
                    {
                        "id": version_id.bytes,
                        "instrument": instrument_id.bytes,
                        "label": label,
                        "published": published,
                        "effective": effective,
                        "h": bytes(32),
                    },
                )

            async def insert_provision(
                cursor,
                provision_id: UUID,
                version_id: UUID,
                no: str,
                full_text: str,
            ) -> None:
                await cursor.execute(
                    text(
                        "INSERT INTO legal_provisions "
                        "(id,version_id,provision_no,level,full_text,content_hash,"
                        "char_start,char_end) "
                        "VALUES (:id,:version,:no,'article',:text,:h,0,:end)"
                    ),
                    {
                        "id": provision_id.bytes,
                        "version": version_id.bytes,
                        "no": no,
                        "text": full_text,
                        "h": content_sha256(full_text),
                        "end": len(full_text),
                    },
                )

            base = 0xA0
            # Old A: 第一条 same, 第二条 changed later, 第三条 removed later.
            await insert_provision(
                connection,
                UUID(f"01a06ae2-6300-7000-8000-0000000000{base + 1:x}"),
                _VERSION_OLD_A,
                "第一条",
                "第一条 保持不变。",
            )
            await insert_provision(
                connection,
                UUID(f"01a06ae2-6300-7000-8000-0000000000{base + 2:x}"),
                _VERSION_OLD_A,
                "第二条",
                "第二条 旧条文。",
            )
            await insert_provision(
                connection,
                UUID(f"01a06ae2-6300-7000-8000-0000000000{base + 3:x}"),
                _VERSION_OLD_A,
                "第三条",
                "第三条 被删除。",
            )
            # New A: 第一条 same, 第二条 changed, 第四条 added.
            await insert_provision(
                connection,
                UUID(f"01a06ae2-6400-7000-8000-0000000000{base + 1:x}"),
                _VERSION_NEW_A,
                "第一条",
                "第一条 保持不变。",
            )
            await insert_provision(
                connection,
                UUID(f"01a06ae2-6400-7000-8000-0000000000{base + 2:x}"),
                _VERSION_NEW_A,
                "第二条",
                "第二条 新条文。",
            )
            await insert_provision(
                connection,
                UUID(f"01a06ae2-6400-7000-8000-0000000000{base + 4:x}"),
                _VERSION_NEW_A,
                "第四条",
                "第四条 新增条文。",
            )
            # Instrument B has a single provision to prove isolation.
            await insert_provision(
                connection,
                UUID(f"01a06ae2-6500-7000-8000-0000000000{base + 1:x}"),
                _VERSION_B,
                "第一条",
                "刑法第一条。",
            )
    finally:
        await engine.dispose()


async def _run(mysql_url: URL) -> None:
    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            service = LegalVersionDiffService(SqlAlchemyLegalCorpusRepository(session))
            diff = await service.diff(
                from_version_id=_VERSION_OLD_A, to_version_id=_VERSION_NEW_A
            )
            assert diff.instrument_id == _INSTRUMENT_A
            assert [p.provision_no for p in diff.unchanged] == ["第一条"]
            assert [p.provision_no for p in diff.removed] == ["第三条"]
            assert [p.provision_no for p in diff.added] == ["第四条"]
            assert [m.provision_no for m in diff.modified] == ["第二条"]
            modified = diff.modified[0]
            assert modified.previous.full_text == "第二条 旧条文。"
            assert modified.current.full_text == "第二条 新条文。"

        # Cross-instrument versions are refused, never diffed.
        async with factory() as session:
            service = LegalVersionDiffService(SqlAlchemyLegalCorpusRepository(session))
            try:
                await service.diff(
                    from_version_id=_VERSION_OLD_A, to_version_id=_VERSION_B
                )
            except LegalVersionDiffError as exc:
                assert "instrument" in str(exc)
            else:
                raise AssertionError("expected cross-instrument refusal")
    finally:
        await engine.dispose()


async def _cleanup(mysql_url: URL) -> None:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM legal_provisions"))
            await connection.execute(text("DELETE FROM legal_versions"))
            await connection.execute(text("DELETE FROM legal_instruments"))
    finally:
        await engine.dispose()


def test_legal_version_diff_mysql(mysql_url: URL) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    try:
        asyncio.run(_seed(mysql_url))
        asyncio.run(_run(mysql_url))
    finally:
        asyncio.run(_cleanup(mysql_url))
