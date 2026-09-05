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
from lawyer_agent.application.legal_corpus_diff import LegalVersionDiffService
from lawyer_agent.application.legal_corpus_import import LegalCorpusImportService
from lawyer_agent.application.legal_corpus_import_mapping import (
    LegalImportMetadata,
    map_parsed_articles,
)
from lawyer_agent.application.legal_source_diff import diff_source_articles
from lawyer_agent.domain.legal_corpus import LegalVersionStatus
from lawyer_agent.infrastructure.documents.docx_loader import ZipDocxLoader
from lawyer_agent.infrastructure.documents.parsers import LegalStructureParser
from lawyer_agent.infrastructure.persistence.repositories.legal_corpus import (
    SqlAlchemyLegalCorpusImportRepository,
    SqlAlchemyLegalCorpusRepository,
)

pytestmark = [pytest.mark.integration, pytest.mark.mysql]

_XML_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_TITLE = "中华人民共和国文件级差异示例法"
_SOURCE_OLD = "object://corpus/source-diff-old.docx"
_SOURCE_NEW = "object://corpus/source-diff-new.docx"

_OLD_LINES = [
    _TITLE,
    "第一条 保留条文内容。",
    "第二条 旧版独有条文。",
    "第三条 修改前条文内容。",
]
_NEW_LINES = [
    _TITLE,
    "第一条 保留条文内容。",
    "第三条 修改后条文内容。",
    "第四条 新版新增条文。",
]


def _docx_bytes(lines: list[str]) -> bytes:
    runs = "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in lines)
    xml = (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
        f'<w:document xmlns:w="{_XML_NS}"><w:body>{runs}</w:body></w:document>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


def _metadata(source_ref: str, version_label: str) -> LegalImportMetadata:
    return LegalImportMetadata(
        title=_TITLE,
        issuing_authority="全国人民代表大会",
        jurisdiction="national",
        region_code=None,
        version_label=version_label,
        status=LegalVersionStatus.CURRENT,
        published_on=date(2020, 5, 28),
        effective_on=date(2021, 1, 1),
        repealed_on=None,
        law_number="示例文号",
        source_ref=source_ref,
        dataset_version="dataset_v1",
        parser_version="docx-v1",
    )


async def _parse(payload: bytes, expected: int) -> tuple[object, ...]:
    loader = ZipDocxLoader(source_ref=_SOURCE_OLD)
    document = loader.load(_SOURCE_OLD, payload)
    instrument = LegalStructureParser().parse(document)
    assert len(instrument.articles) == expected
    return instrument.articles


async def _run(mysql_url: URL) -> None:
    old_payload = _docx_bytes(_OLD_LINES)
    new_payload = _docx_bytes(_NEW_LINES)
    old_articles = await _parse(old_payload, expected=3)
    new_articles = await _parse(new_payload, expected=3)

    file_diff = diff_source_articles(tuple(old_articles), tuple(new_articles))
    assert file_diff.changed is True
    assert [entry.provision_no for entry in file_diff.removed] == ["第二条"]
    assert [entry.provision_no for entry in file_diff.added] == ["第四条"]
    assert [entry.provision_no for entry in file_diff.unchanged] == ["第一条"]
    assert [item.provision_no for item in file_diff.modified] == ["第三条"]
    (modified,) = file_diff.modified
    assert modified.previous.full_text == "第三条 修改前条文内容。"
    assert modified.current.full_text == "第三条 修改后条文内容。"

    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    import_repo = SqlAlchemyLegalCorpusImportRepository
    try:
        async with factory() as session, session.begin():
            service = LegalCorpusImportService(import_repo(session))
            old_command = map_parsed_articles(
                _metadata(_SOURCE_OLD, "2020-05-28 公布版"), tuple(old_articles)
            )
            old_result = await service.import_version(old_command)
            new_command = map_parsed_articles(
                _metadata(_SOURCE_NEW, "2024-01-01 修正版"), tuple(new_articles)
            )
            new_result = await service.import_version(new_command)
            assert old_result.instrument_id == new_result.instrument_id

            repo = SqlAlchemyLegalCorpusRepository(session)
            db_diff = await LegalVersionDiffService(repo).diff(
                from_version_id=old_result.version_id,
                to_version_id=new_result.version_id,
            )
        # DB-level diff groups must match the file-level diff grouping.
        assert [p.provision_no for p in db_diff.removed] == ["第二条"]
        assert [p.provision_no for p in db_diff.added] == ["第四条"]
        assert [p.provision_no for p in db_diff.unchanged] == ["第一条"]
        assert [m.provision_no for m in db_diff.modified] == ["第三条"]
        (db_modified,) = db_diff.modified
        assert db_modified.previous.full_text == "第三条 修改前条文内容。"
        assert db_modified.current.full_text == "第三条 修改后条文内容。"
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


def test_source_file_diff_matches_imported_version_diff(mysql_url: URL) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    try:
        asyncio.run(_run(mysql_url))
    finally:
        asyncio.run(_cleanup(mysql_url))
