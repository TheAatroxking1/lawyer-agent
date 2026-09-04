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
    LegalCorpusImportService,
    LegalImportCommand,
    LegalProvisionDraft,
)
from lawyer_agent.application.legal_corpus_onboarding import (
    LegalCorpusOnboardingService,
)
from lawyer_agent.domain.legal_corpus import (
    LegalVersionStatus,
    ProvisionLevel,
)
from lawyer_agent.infrastructure.persistence.repositories.legal_corpus import (
    SqlAlchemyLegalCorpusChunkRepository,
    SqlAlchemyLegalCorpusImportRepository,
    SqlAlchemyLegalCorpusRepository,
)

pytestmark = [pytest.mark.integration, pytest.mark.mysql]


def _command(
    version_label: str,
    *texts: tuple[str, str],
) -> LegalImportCommand:
    return LegalImportCommand(
        title="中华人民共和国民法典",
        issuing_authority="全国人民代表大会",
        jurisdiction="national",
        region_code=None,
        version_label=version_label,
        status=LegalVersionStatus.CURRENT,
        published_on=date(2020, 5, 28),
        effective_on=date(2021, 1, 1),
        repealed_on=None,
        law_number="主席令第四十五号",
        source_ref="object://corpus/civil-code.docx",
        dataset_version="dataset_v1",
        parser_version="docx-zip-v1",
        provisions=tuple(
            LegalProvisionDraft(
                provision_no=no,
                level=ProvisionLevel.ARTICLE,
                structure_path=(),
                title=None,
                full_text=full_text,
            )
            for no, full_text in texts
        ),
    )


async def _run(mysql_url: URL) -> None:
    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            import_repo = SqlAlchemyLegalCorpusImportRepository(session)
            importer = LegalCorpusImportService(import_repo)
            onboarding = LegalCorpusOnboardingService(
                import_version=importer.import_version,
                provisions=SqlAlchemyLegalCorpusRepository(session).provisions_for_version,
                chunks=SqlAlchemyLegalCorpusChunkRepository(session).replace_chunks_for_version,
            )

            # Version A with two provisions -> two chunks.
            result_a = await onboarding.onboard_version(
                _command(
                    "2020 公布版",
                    ("第一条", "第一条 内容甲。"),
                    ("第二条", "第二条 内容乙。"),
                )
            )
            assert result_a.chunk_count == 2

            # Re-running the same command replays import and rebuilds chunks.
            result_replay = await onboarding.onboard_version(
                _command(
                    "2020 公布版",
                    ("第一条", "第一条 内容甲。"),
                    ("第二条", "第二条 内容乙。"),
                )
            )
            assert result_replay.version_id == result_a.version_id
            assert result_replay.chunk_count == 2

            chunk_repo = SqlAlchemyLegalCorpusChunkRepository(session)
            chunks_a = await chunk_repo.chunks_for_version(result_a.version_id)
            assert len(chunks_a) == 2
            assert sorted(chunk.content for chunk in chunks_a) == [
                "第一条 内容甲。",
                "第二条 内容乙。",
            ]
            assert {chunk.parser_version for chunk in chunks_a} == {"docx-zip-v1"}

            # Version B with one provision -> exactly one chunk, no leakage.
            result_b = await onboarding.onboard_version(
                _command("2024 修正版", ("第三条", "第三条 内容丙。"))
            )
            assert result_b.version_id != result_a.version_id
            assert result_b.chunk_count == 1
            assert len(await chunk_repo.chunks_for_version(result_b.version_id)) == 1
    finally:
        await engine.dispose()


async def _cleanup(mysql_url: URL) -> None:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM legal_chunks"))
            await connection.execute(text("DELETE FROM legal_provisions"))
            await connection.execute(text("DELETE FROM legal_versions"))
            await connection.execute(text("DELETE FROM legal_instruments"))
    finally:
        await engine.dispose()


def test_legal_corpus_onboarding_mysql(mysql_url: URL) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    try:
        asyncio.run(_run(mysql_url))
    finally:
        asyncio.run(_cleanup(mysql_url))
