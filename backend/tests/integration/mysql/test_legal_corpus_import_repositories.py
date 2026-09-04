from __future__ import annotations

import asyncio
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
    LegalImportCommand,
    LegalProvisionDraft,
)
from lawyer_agent.domain.legal_corpus import (
    LegalVersionStatus,
    ProvisionLevel,
)
from lawyer_agent.infrastructure.persistence.repositories.legal_corpus import (
    SqlAlchemyLegalCorpusImportRepository,
    SqlAlchemyLegalCorpusRepository,
)

pytestmark = [pytest.mark.integration, pytest.mark.mysql]


def _command(
    *,
    version_label: str = "2020-05-28 公布版",
    published_on: date = date(2020, 5, 28),
    effective_on: date = date(2021, 1, 1),
) -> LegalImportCommand:
    return LegalImportCommand(
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
        source_ref="object://corpus/civil-code.docx",
        dataset_version="dataset_v1",
        parser_version="docx-v1",
        provisions=(
            LegalProvisionDraft(
                provision_no="第一条",
                level=ProvisionLevel.ARTICLE,
                structure_path=("第一编", "第一章", "第一条"),
                title=None,
                full_text="第一条 为了保护民事主体的合法权益，调整民事关系，制定本法。",
            ),
            LegalProvisionDraft(
                provision_no="第二条",
                level=ProvisionLevel.ARTICLE,
                structure_path=("第一编", "第一章", "第二条"),
                title=None,
                full_text=(
                    "第二条 民法调整平等主体的自然人、法人和非法人组织之间的"
                    "人身关系和财产关系。"
                ),
            ),
        ),
    )


async def _seed(mysql_url: URL) -> None:
    del mysql_url  # service writes everything; nothing to pre-seed


async def _run(mysql_url: URL) -> None:
    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    import_repo = SqlAlchemyLegalCorpusImportRepository
    read_repo = SqlAlchemyLegalCorpusRepository
    try:
        async with factory() as session, session.begin():
            service = LegalCorpusImportService(import_repo(session))
            result = await service.import_version(_command())
            assert result.replayed is False
            instrument_id = result.instrument_id
            version_id = result.version_id

            repo = read_repo(session)
            version = await repo.version_at(instrument_id, date(2025, 6, 1))
            assert version is not None
            assert version.id == version_id
            assert version.version_label == "2020-05-28 公布版"
            assert version.status is LegalVersionStatus.CURRENT
            assert version.source_ref == "object://corpus/civil-code.docx"
            assert version.dataset_version == "dataset_v1"

            provisions = await repo.provisions_for_version(version_id)
            assert [p.provision_no for p in provisions] == ["第一条", "第二条"]
            assert provisions[0].full_text.startswith("第一条")
            assert provisions[1].char_start == len(provisions[0].full_text)
            assert version.content_hash is not None

        # Idempotent replay of the identical command inside a new transaction.
        async with factory() as session, session.begin():
            service = LegalCorpusImportService(import_repo(session))
            replay = await service.import_version(_command())
            assert replay.replayed is True
            assert replay.instrument_id == instrument_id
            assert replay.version_id == version_id

        # Same label but different content must conflict and write nothing.
        async with factory() as session, session.begin():
            service = LegalCorpusImportService(import_repo(session))
            changed = _command()
            changed = LegalImportCommand(
                title=changed.title,
                issuing_authority=changed.issuing_authority,
                jurisdiction=changed.jurisdiction,
                region_code=changed.region_code,
                version_label="2020-05-28 公布版",
                status=changed.status,
                published_on=changed.published_on,
                effective_on=changed.effective_on,
                repealed_on=changed.repealed_on,
                law_number=changed.law_number,
                source_ref=changed.source_ref,
                dataset_version=changed.dataset_version,
                parser_version=changed.parser_version,
                provisions=(
                    LegalProvisionDraft(
                        provision_no="第一条",
                        level=ProvisionLevel.ARTICLE,
                        structure_path=(),
                        title=None,
                        full_text="第一条 完全不同内容的条文。",
                    ),
                ),
            )
            try:
                await service.import_version(changed)
            except LegalCorpusImportConflict:
                pass
            else:
                raise AssertionError("expected version conflict")
            await session.rollback()

        # A second, distinct version of the same instrument is independent.
        async with factory() as session, session.begin():
            service = LegalCorpusImportService(import_repo(session))
            second = await service.import_version(
                _command(
                    version_label="2023-12-29 修正版",
                    published_on=date(2023, 12, 29),
                    effective_on=date(2024, 1, 1),
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


def test_legal_corpus_import_persists_versions(mysql_url: URL) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    try:
        asyncio.run(_seed(mysql_url))
        asyncio.run(_run(mysql_url))
    finally:
        asyncio.run(_cleanup(mysql_url))
