from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_ai_job_migration import _alembic_config

from alembic import command
from lawyer_agent.application.legal_index_publish import (
    LegalDatasetIndexPublishService,
)
from lawyer_agent.domain.legal_corpus import DatasetState
from lawyer_agent.infrastructure.persistence.repositories.legal_corpus_inventory import (
    SqlAlchemyLegalCorpusInventoryRepository,
)

pytestmark = [pytest.mark.integration, pytest.mark.mysql]

_FIXED_NOW = datetime(2026, 9, 10, 6, 0, tzinfo=UTC)
VERSION = UUID("01a06ae2-6200-7000-8000-0000000000c3")


class _Indexer(Protocol):
    async def index_version(
        self,
        *,
        version_id: UUID,
        index_name: str,
        model_ref: str,
        dimension: int,
        batch_size: int,
    ) -> int: ...


class _Alias(Protocol):
    async def publish_dataset(
        self, alias: str, index_name: str
    ) -> str | None: ...


class _FakeIndexer:
    def __init__(self, indexed: int = 3) -> None:
        self._indexed = indexed

    async def index_version(self, **_: object) -> int:
        return self._indexed


class _FakeAlias:
    def __init__(self, previous: str | None = None) -> None:
        self._previous = previous
        self.calls: list[tuple[str, str]] = []

    async def publish_dataset(
        self, alias: str, index_name: str
    ) -> str | None:
        self.calls.append((alias, index_name))
        return self._previous


async def _run(mysql_url: URL) -> None:
    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            repo = SqlAlchemyLegalCorpusInventoryRepository(session)
            alias = _FakeAlias(previous=None)
            service = LegalDatasetIndexPublishService(
                indexer=_FakeIndexer(),
                alias=alias,
                snapshot=repo,
                dataset_parser_version="docx-v1",
                now=lambda: _FIXED_NOW,
            )
            first = await service.publish_version(
                version_id=VERSION,
                index_name="legal_idx_v1",
                alias="dataset_v2",
                model_ref="bge-small-zh",
                dimension=512,
            )
            assert first.indexed_documents == 3
            assert alias.calls == [("dataset_v2", "legal_idx_v1")]

            stored = await repo.find_dataset("dataset_v2")
            assert stored is not None
            assert stored.dataset_name == "dataset_v2"
            assert stored.parser_version == "docx-v1"
            assert stored.state is DatasetState.PUBLISHED
            assert stored.released_at is not None
            assert stored.manifest == {
                "index_name": "legal_idx_v1",
                "alias": "dataset_v2",
                "version_id": str(VERSION),
                "model_ref": "bge-small-zh",
                "dimension": 512,
                "indexed_documents": 3,
            }
            assert stored.quality_metrics == {
                "indexed_documents": 3,
                "dimension": 512,
            }
            first_row_id = stored.id
            await session.commit()

        # Re-publishing to the same alias upserts one row: same id, new target.
        async with factory() as session:
            repo = SqlAlchemyLegalCorpusInventoryRepository(session)
            service = LegalDatasetIndexPublishService(
                indexer=_FakeIndexer(),
                alias=_FakeAlias(previous="legal_idx_v1"),
                snapshot=repo,
                dataset_parser_version="docx-v1",
                now=lambda: _FIXED_NOW,
            )
            second = await service.publish_version(
                version_id=VERSION,
                index_name="legal_idx_v2",
                alias="dataset_v2",
                model_ref="bge-small-zh",
                dimension=512,
            )
            assert second.indexed_documents == 3
            updated = await repo.find_dataset("dataset_v2")
            assert updated is not None
            assert updated.id == first_row_id
            assert updated.manifest["index_name"] == "legal_idx_v2"
            assert updated.quality_metrics["indexed_documents"] == 3
            rows = await repo.find_dataset("dataset_v2")
            assert rows is not None and rows.id == first_row_id
            await session.commit()

        # No documents indexed -> publish refused and nothing is recorded.
        async with factory() as session:
            repo = SqlAlchemyLegalCorpusInventoryRepository(session)
            service = LegalDatasetIndexPublishService(
                indexer=_FakeIndexer(indexed=0),
                alias=_FakeAlias(),
                snapshot=repo,
                dataset_parser_version="docx-v1",
                now=lambda: _FIXED_NOW,
            )
            try:
                await service.publish_version(
                    version_id=VERSION,
                    index_name="legal_idx_v3",
                    alias="dataset_v2",
                    model_ref="m",
                    dimension=8,
                )
            except ValueError:
                pass
            else:
                raise AssertionError("expected empty-index refusal")
            stored = await repo.find_dataset("dataset_v2")
            assert stored is not None
            assert stored.manifest["index_name"] == "legal_idx_v2"
            assert stored.id == first_row_id
            await session.rollback()
    finally:
        await engine.dispose()


async def _cleanup(mysql_url: URL) -> None:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM legal_dataset_snapshots"))
    finally:
        await engine.dispose()


def test_index_publish_records_dataset_snapshot(mysql_url: URL) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    try:
        asyncio.run(_run(mysql_url))
    finally:
        asyncio.run(_cleanup(mysql_url))
