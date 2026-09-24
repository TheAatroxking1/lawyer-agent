"""Synthetic corpus + actual MySQL/OpenSearch; never contacts a model provider."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import pytest
from sqlalchemy import select
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_ai_job_migration import _alembic_config
from test_legal_corpus_chunks_repositories import _create_database, _drop_database

from alembic import command
from lawyer_agent.application.legal_dataset_publication import LegalDatasetPublicationService
from lawyer_agent.application.legal_dataset_set_publish import LegalDatasetSetPublishService
from lawyer_agent.application.legal_index_alias import LegalDatasetAliasService
from lawyer_agent.application.legal_navigation_index import LegalNavigationIndexService
from lawyer_agent.cli import corpus_publish
from lawyer_agent.domain.legal_corpus import DatasetState
from lawyer_agent.domain.legal_dataset_publication import PublicationError, PublicationState
from lawyer_agent.domain.legal_navigation import navigation_index_name
from lawyer_agent.domain.model_gateway import EmbeddingVector
from lawyer_agent.infrastructure.persistence.models.legal_dataset_publication import (
    LegalDatasetPublicationModel,
)
from lawyer_agent.infrastructure.persistence.repositories.legal_corpus import (
    SqlAlchemyLegalCorpusChunkRepository,
    SqlAlchemyLegalCorpusRepository,
)
from lawyer_agent.infrastructure.persistence.repositories.legal_corpus_inventory import (
    SqlAlchemyLegalCorpusInventoryRepository,
)
from lawyer_agent.infrastructure.persistence.repositories.legal_dataset_publication import (
    SqlAlchemyDatasetPublicationStore,
)
from lawyer_agent.infrastructure.search.legal_navigation import OpenSearchNavigationClient
from lawyer_agent.infrastructure.search.opensearch import OpenSearchRestClient

pytestmark = [pytest.mark.integration, pytest.mark.mysql]


@pytest.fixture
def publication_e2e_url(mysql_url: URL) -> Iterator[URL]:
    if not os.getenv("LAWYER_TEST_PUBLICATION_OPENSEARCH_URL"):
        pytest.skip("explicit isolated publication OpenSearch URL is required")
    name = f"lawyer_test_{uuid4().hex}"
    asyncio.run(_create_database(mysql_url, name))
    url = mysql_url.set(database=name)
    try:
        command.upgrade(_alembic_config(url), "head")
        yield url
    finally:
        asyncio.run(_drop_database(mysql_url, name))


class _SyntheticGateway:
    calls = 0

    async def embed(self, *, model_ref, texts, dimension):
        assert model_ref == "synthetic-publication-test" and dimension == 4
        self.calls += 1
        return tuple(EmbeddingVector((0.5,) * 4, 4) for _ in texts)


class _ObservedAlias:
    def __init__(self, delegate, *, lose_ack=False):
        self.delegate = delegate
        self.lose_ack = lose_ack
        self.calls = 0

    async def active_dataset_index(self, alias):
        return await self.delegate.active_dataset_index(alias)

    async def publish_dataset(self, alias, index_name):
        self.calls += 1
        previous = await self.delegate.publish_dataset(alias, index_name)
        if self.lose_ack:
            raise TimeoutError("synthetic acknowledgement loss after actual switch")
        return previous


class _FailCompletionOnce(SqlAlchemyDatasetPublicationStore):
    async def complete(self, run_id):
        raise RuntimeError("synthetic crash before snapshot transaction")


@pytest.mark.parametrize("lose_ack", [False, True])
def test_actual_set_build_and_durable_recovery(
    publication_e2e_url: URL, tmp_path: Path, lose_ack: bool,
) -> None:
    asyncio.run(_exercise(publication_e2e_url, tmp_path, lose_ack=lose_ack))


async def _exercise(url: URL, tmp_path: Path, *, lose_ack: bool) -> None:
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    base_url = os.environ["LAWYER_TEST_PUBLICATION_OPENSEARCH_URL"]
    search = OpenSearchRestClient(base_url=base_url)
    alias = _ObservedAlias(LegalDatasetAliasService(search), lose_ack=lose_ack)
    token = uuid4().hex
    main, alias_name = f"lawyer_pub_test_{token}", f"lawyer_pub_alias_{token}"
    gateway = _SyntheticGateway()
    try:
        versions = []
        for number in range(2):
            source = tmp_path / f"合成法规{number}.docx"
            with ZipFile(source, "w") as archive:
                archive.writestr("word/document.xml", (
                    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                    f"<w:body><w:p><w:r><w:t>第一条 合成法规{number}的共同义务。</w:t>"
                    "</w:r></w:p></w:body></w:document>"
                ))
            args = corpus_publish.build_parser().parse_args([
                "--source", str(source), "--instrument-title", f"合成法规{number}",
                "--issuing-authority", "示例机关", "--import-only",
            ])
            imported = await corpus_publish._import_prepared(factory, corpus_publish._prepare(args))
            versions.append(imported.imported.version_id)
        async with factory() as session:
            builder = LegalDatasetSetPublishService(
                chunks=SqlAlchemyLegalCorpusChunkRepository(session), gateway=gateway,
                search=search, alias=alias,
                navigation=LegalNavigationIndexService(
                    SqlAlchemyLegalCorpusRepository(session),
                    OpenSearchNavigationClient(base_url=base_url),
                ), dataset_parser_version="corpus-docx-v2/hierarchical-v1",
            )
            candidate = await builder.build_set(
                version_ids=tuple(versions), index_name=main, alias=alias_name,
                model_ref="synthetic-publication-test", dimension=4,
            )
        assert candidate.state is DatasetState.PENDING
        assert candidate.manifest["indexed_documents"] == 2
        assert await search.resolve_alias(alias_name) is None
        assert alias.calls == 0
        fault_store = _FailCompletionOnce(factory)
        with pytest.raises((RuntimeError, TimeoutError, PublicationError)):
            await LegalDatasetPublicationService(fault_store, alias).publish(candidate)
        assert await search.resolve_alias(alias_name) == main
        assert alias.calls == 1
        async with factory() as session:
            run_id = (await session.scalars(select(LegalDatasetPublicationModel.id))).one()
            inventory = SqlAlchemyLegalCorpusInventoryRepository(session)
            assert await inventory.find_dataset(alias_name) is None
        fresh_store = SqlAlchemyDatasetPublicationStore(factory)
        saved = await fresh_store.get(run_id)
        assert saved is not None
        assert saved.state is (
            PublicationState.SWITCHING if lose_ack else PublicationState.ACKNOWLEDGED
        )
        previous_embedding_calls = gateway.calls
        recovery = LegalDatasetPublicationService(fresh_store, alias)
        if lose_ack:
            with pytest.raises(PublicationError) as caught:
                await recovery.resume(run_id)
            assert caught.value.code == "publication_outcome_unknown"
            other_index = f"lawyer_pub_other_{token}"
            other = replace(candidate, manifest={
                **candidate.manifest, "index_name": other_index,
                "navigation_index": navigation_index_name(other_index),
            })
            with pytest.raises(PublicationError) as conflict:
                await fresh_store.create(other, previous_target=main)
            assert conflict.value.code == "publication_conflict"
        else:
            completed = await recovery.resume(run_id)
            assert completed.state is PublicationState.COMPLETED
            async with factory() as session:
                inventory = SqlAlchemyLegalCorpusInventoryRepository(session)
                snapshot = await inventory.find_dataset(alias_name)
                assert snapshot is not None and snapshot.state is DatasetState.PUBLISHED
                assert snapshot.manifest == candidate.manifest
            assert (await recovery.resume(run_id)).state is PublicationState.COMPLETED
            hits = await search.search_bm25(alias_name, query="共同义务", limit=10)
            assert {hit.version_id for hit in hits} == set(versions)
        assert gateway.calls == previous_embedding_calls
        assert alias.calls == 1
    finally:
        # Only the generated test index and its deterministic sidecar are removed.
        await search.delete_index(navigation_index_name(main))
        await search.delete_index(main)
        await engine.dispose()
