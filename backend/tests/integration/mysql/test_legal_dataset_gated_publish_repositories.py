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
from lawyer_agent.application.legal_corpus_import import (
    LegalCorpusImportService,
    LegalImportCommand,
    LegalProvisionDraft,
)
from lawyer_agent.application.legal_corpus_publish import LegalCorpusQualityGate
from lawyer_agent.application.legal_dataset_gated_publish import (
    LegalDatasetGatePublishService,
)
from lawyer_agent.application.legal_index_publish import (
    DatasetPublishResult,
    LegalDatasetPublishError,
)
from lawyer_agent.domain.legal_corpus import LegalVersionStatus, ProvisionLevel
from lawyer_agent.infrastructure.persistence.repositories.legal_corpus import (
    SqlAlchemyLegalCorpusImportRepository,
    SqlAlchemyLegalCorpusRepository,
)

pytestmark = [pytest.mark.integration, pytest.mark.mysql]

_TITLE = "中华人民共和国门禁发布示例法"


def _command(
    *, version_label: str, texts: tuple[str, ...]
) -> LegalImportCommand:
    return LegalImportCommand(
        title=_TITLE,
        issuing_authority="全国人民代表大会",
        jurisdiction="national",
        region_code=None,
        version_label=version_label,
        status=LegalVersionStatus.CURRENT,
        published_on=date(2026, 1, 1),
        effective_on=date(2026, 2, 1),
        repealed_on=None,
        law_number="示例文号",
        source_ref="object://corpus/gated.docx",
        dataset_version="dataset_v1",
        parser_version="docx-v1",
        provisions=tuple(
            LegalProvisionDraft(
                provision_no=f"第{number}条",
                level=ProvisionLevel.ARTICLE,
                structure_path=(),
                title=None,
                full_text=f"第{number}条 条文内容。",
            )
            for number in texts
        ),
    )


class _FakePublisher:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def publish_version(self, **kwargs: object) -> DatasetPublishResult:
        self.calls.append(kwargs)
        return DatasetPublishResult(
            index_name="legal_idx_gated",
            indexed_documents=2,
            previous_target=None,
        )


async def _run(mysql_url: URL) -> None:
    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    import_repo = SqlAlchemyLegalCorpusImportRepository
    try:
        # Well-formed version: first and second article in sequence.
        async with factory() as session, session.begin():
            service = LegalCorpusImportService(import_repo(session))
            good = await service.import_version(
                _command(version_label="2026-02-01 达标版", texts=("一", "二"))
            )
            publisher = _FakePublisher()
            gated = LegalDatasetGatePublishService(
                gate=LegalCorpusQualityGate(),
                corpus=SqlAlchemyLegalCorpusRepository(session),
                publish=publisher,
            )
            result = await gated.publish_version(
                version_id=good.version_id,
                index_name="legal_idx_gated",
                alias="dataset_v1",
                model_ref="m",
                dimension=8,
            )
            assert result.indexed_documents == 2
            assert len(publisher.calls) == 1
            assert publisher.calls[0]["version_id"] == good.version_id

        # Broken version: article sequence jumps first -> third.
        async with factory() as session, session.begin():
            service = LegalCorpusImportService(import_repo(session))
            broken = await service.import_version(
                _command(version_label="2026-02-01 断裂版", texts=("一", "三"))
            )
            publisher = _FakePublisher()
            gated = LegalDatasetGatePublishService(
                gate=LegalCorpusQualityGate(),
                corpus=SqlAlchemyLegalCorpusRepository(session),
                publish=publisher,
            )
            try:
                await gated.publish_version(
                    version_id=broken.version_id,
                    index_name="legal_idx_gated_bad",
                    alias="dataset_v1",
                    model_ref="m",
                    dimension=8,
                )
            except LegalDatasetPublishError as exc:
                assert "sequence" in str(exc)
            else:
                raise AssertionError("expected quality gate refusal")
            assert publisher.calls == []

        # Unknown version is refused before any publish attempt.
        async with factory() as session, session.begin():
            publisher = _FakePublisher()
            gated = LegalDatasetGatePublishService(
                gate=LegalCorpusQualityGate(),
                corpus=SqlAlchemyLegalCorpusRepository(session),
                publish=publisher,
            )
            try:
                await gated.publish_version(
                    version_id=UUID("01a06ae2-9900-7000-8000-0000000000ff"),
                    index_name="legal_idx_gated_missing",
                    alias="dataset_v1",
                    model_ref="m",
                    dimension=8,
                )
            except LegalDatasetPublishError as exc:
                assert "no legal version" in str(exc)
            else:
                raise AssertionError("expected missing version refusal")
            assert publisher.calls == []
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


def test_quality_gated_dataset_publish(mysql_url: URL) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    try:
        asyncio.run(_run(mysql_url))
    finally:
        asyncio.run(_cleanup(mysql_url))
