from __future__ import annotations

import asyncio
import io
import zipfile
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_ai_job_migration import _alembic_config

from alembic import command
from lawyer_agent.application.legal_corpus_import import (
    LegalCorpusImportConflict,
    LegalCorpusImportService,
)
from lawyer_agent.application.legal_corpus_import_mapping import (
    LegalImportMetadata,
    map_parsed_articles,
)
from lawyer_agent.domain.legal_corpus import LegalVersionStatus
from lawyer_agent.infrastructure.documents.docx_loader import ZipDocxLoader
from lawyer_agent.infrastructure.documents.parsers import LegalStructureParser
from lawyer_agent.infrastructure.persistence.repositories.legal_corpus import (
    SqlAlchemyLegalCorpusImportRepository,
    SqlAlchemyLegalCorpusRepository,
)

pytestmark = [pytest.mark.integration, pytest.mark.mysql]

_XML_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

_SOURCE_REF = "object://corpus/civil-code.docx"
_DOC_LINES = [
    "中华人民共和国民法典",
    "第一编 总则",
    "第一章 一般规定",
    "第一条 为了保护民事主体的合法权益，调整民事关系，制定本法。",
    "本款为该条的延续，用以验证多段条文拼接。",
    "第二条 民法调整平等主体的自然人、法人和非法人组织之间的人身关系和财产关系。",
    "第三章 特别规定",
    "第三条 特别规定的内容。",
]


def _docx_bytes(lines: list[str]) -> bytes:
    runs = "".join(
        f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in lines
    )
    xml = (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
        f'<w:document xmlns:w="{_XML_NS}"><w:body>{runs}</w:body></w:document>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


def _metadata(
    *,
    version_label: str = "2020-05-28 公布版",
    published_on: date = date(2020, 5, 28),
    effective_on: date = date(2021, 1, 1),
) -> LegalImportMetadata:
    return LegalImportMetadata(
        title="中华人民共和国民法典",
        issuing_authority="全国人民代表大会",
        jurisdiction="national",
        region_code=None,
        version_label=version_label,
        status=LegalVersionStatus.CURRENT,
        published_on=published_on,
        effective_on=effective_on,
        repealed_on=None,
        law_number="主席令第四十五号",
        source_ref=_SOURCE_REF,
        dataset_version="dataset_v1",
        parser_version="docx-v1",
    )


async def _parse(payload: bytes, expected: int) -> tuple[object, ...]:
    loader = ZipDocxLoader(source_ref=_SOURCE_REF)
    document = loader.load(_SOURCE_REF, payload)
    instrument = LegalStructureParser().parse(document)
    assert len(instrument.articles) == expected
    return instrument.articles


async def _run(mysql_url: URL) -> None:
    payload = _docx_bytes(_DOC_LINES)
    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    import_repo = SqlAlchemyLegalCorpusImportRepository
    read_repo = SqlAlchemyLegalCorpusRepository
    try:
        articles = await _parse(payload, expected=3)
        async with factory() as session, session.begin():
            command = map_parsed_articles(_metadata(), tuple(articles))
            service = LegalCorpusImportService(import_repo(session))
            result = await service.import_version(command)
            assert result.replayed is False
            instrument_id = result.instrument_id
            version_id = result.version_id

            repo = read_repo(session)
            version = await repo.version_at(instrument_id, date(2025, 6, 1))
            assert version is not None
            assert version.id == version_id
            assert version.status is LegalVersionStatus.CURRENT
            assert version.source_ref == _SOURCE_REF
            assert version.parser_version == "docx-v1"

            provisions = await repo.provisions_for_version(version_id)
            assert [p.provision_no for p in provisions] == [
                "第一条",
                "第二条",
                "第三条",
            ]
            assert [p.structure_path for p in provisions] == [
                articles[0].structure_path,
                articles[1].structure_path,
                articles[2].structure_path,
            ]
            assert provisions[0].full_text == articles[0].text
            assert "多段条文拼接" in provisions[0].full_text
            assert provisions[2].structure_path == ("第一编 总则", "第三章 特别规定")

        # Same parse + metadata replays idempotently to the same version id.
        async with factory() as session, session.begin():
            articles = await _parse(payload, expected=3)
            service = LegalCorpusImportService(import_repo(session))
            replay = await service.import_version(
                map_parsed_articles(_metadata(), tuple(articles))
            )
            assert replay.replayed is True
            assert replay.version_id == version_id

        # Same instrument + label but different article text conflicts.
        changed_payload = _docx_bytes(
            [
                "中华人民共和国民法典",
                "第一条 完全不同内容的条文。",
            ]
        )
        async with factory() as session, session.begin():
            articles = await _parse(changed_payload, expected=1)
            service = LegalCorpusImportService(import_repo(session))
            try:
                await service.import_version(
                    map_parsed_articles(_metadata(), tuple(articles))
                )
            except LegalCorpusImportConflict:
                pass
            else:
                raise AssertionError("expected version conflict")
            await session.rollback()

        # A second label parsed from a newer source is an independent version.
        newer_payload = _docx_bytes(
            [
                "中华人民共和国民法典",
                "第一条 修正后的第一条。",
                "第二条 修正后的第二条。",
            ]
        )
        async with factory() as session, session.begin():
            articles = await _parse(newer_payload, expected=2)
            service = LegalCorpusImportService(import_repo(session))
            second = await service.import_version(
                map_parsed_articles(
                    _metadata(
                        version_label="2023-12-29 修正版",
                        published_on=date(2023, 12, 29),
                        effective_on=date(2024, 1, 1),
                    ),
                    tuple(articles),
                )
            )
            assert second.replayed is False
            assert second.instrument_id == instrument_id
            assert second.version_id != version_id
            repo = read_repo(session)
            older = await repo.version_at(instrument_id, date(2022, 6, 1))
            newer = await repo.version_at(instrument_id, date(2024, 6, 1))
            assert older is not None and older.id == version_id
            assert newer is not None and newer.id == second.version_id
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


def test_parsed_docx_maps_to_imported_version(mysql_url: URL) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    try:
        asyncio.run(_run(mysql_url))
    finally:
        asyncio.run(_cleanup(mysql_url))
