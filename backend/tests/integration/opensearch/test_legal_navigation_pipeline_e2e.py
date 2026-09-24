from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date
from uuid import UUID, uuid4

import pytest

from lawyer_agent.application.legal_dataset_search import LegalDatasetSearchService
from lawyer_agent.application.legal_dataset_set_publish import LegalDatasetSetPublishService
from lawyer_agent.application.legal_hybrid_search import LegalHybridSearchService
from lawyer_agent.application.legal_index_alias import LegalDatasetAliasService
from lawyer_agent.application.legal_navigation_index import LegalNavigationIndexService
from lawyer_agent.application.legal_navigation_search import LegalNavigationSearchService
from lawyer_agent.application.model_gateway import ModelGateway
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import (
    ChunkQuality,
    ChunkType,
    DatasetSnapshot,
    LegalChunk,
    LegalInstrument,
    LegalVersion,
    LegalVersionStatus,
    Provision,
    ProvisionLevel,
    content_sha256,
)
from lawyer_agent.domain.legal_navigation import navigation_index_name
from lawyer_agent.domain.model_gateway import (
    EmbeddingVector,
    ModelCallRecord,
    RankedDocument,
    TokenUsage,
)
from lawyer_agent.infrastructure.search.legal_navigation import OpenSearchNavigationClient
from lawyer_agent.infrastructure.search.opensearch import (
    OpenSearchError,
    OpenSearchRestClient,
    chunk_document,
)

pytestmark = pytest.mark.integration


class _Source:
    def __init__(self) -> None:
        self.versions: dict[UUID, tuple[LegalVersion, LegalInstrument]] = {}
        self.provisions: dict[UUID, tuple[Provision, ...]] = {}
        self.chunks: dict[UUID, tuple[LegalChunk, ...]] = {}
        for title in ("合成甲法", "合成乙法"):
            instrument = LegalInstrument(new_uuid7(), title, "示例机关", "national")
            version = LegalVersion(
                new_uuid7(), instrument.id, "合成版", LegalVersionStatus.CURRENT,
                date(2020, 1, 1), date(2020, 2, 1), None,
            )
            text = f"第一条 {title}规定共同义务。"
            provision = Provision(
                new_uuid7(), version.id, "第一条", ProvisionLevel.ARTICLE,
                ("第一章 共同义务",), None, text, content_sha256(text), 0, len(text),
            )
            chunk = LegalChunk(
                new_uuid7(), version.id, provision.id, ChunkType.PROVISION,
                ChunkQuality.OK, text, content_sha256(text), parser_version="synthetic-v1",
            )
            self.versions[version.id] = (version, instrument)
            self.provisions[version.id] = (provision,)
            self.chunks[version.id] = (chunk,)

    async def version_with_instrument(self, version_id: UUID):
        return self.versions.get(version_id)

    async def provisions_for_version(self, version_id: UUID):
        return self.provisions[version_id]

    async def chunks_for_version(self, version_id: UUID):
        return self.chunks[version_id]


class _Provider:
    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, *, texts: Sequence[str], dimension: int, timeout_seconds: float):
        self.calls += 1
        return tuple(EmbeddingVector((0.5,) * 4, 4) for _ in texts)

    async def chat(self, **kwargs: object) -> tuple[str, TokenUsage]:
        raise AssertionError("no model chat is used in navigation infrastructure tests")

    async def rerank(self, **kwargs: object) -> tuple[RankedDocument, ...]:
        raise AssertionError("no reranker is used")


class _Recorder:
    async def append(self, record: ModelCallRecord) -> None:
        pass


class _Snapshots:
    def __init__(self) -> None:
        self.saved: list[DatasetSnapshot] = []

    async def find_dataset(self, dataset_name: str) -> DatasetSnapshot | None:
        return None

    async def upsert_dataset(self, snapshot: DatasetSnapshot) -> None:
        self.saved.append(snapshot)


async def test_real_navigation_publish_search_damage_and_legacy_rollback() -> None:
    token = uuid4().hex
    main = f"lawyer_s3_e2e_{token}"
    legacy = f"lawyer_s3_old_{token}"
    alias_name = f"lawyer_s3_alias_{token}"
    for value in (main, legacy, alias_name):
        if re.fullmatch(r"lawyer_s3_(e2e|old|alias)_[a-f0-9]{32}", value) is None:
            raise RuntimeError("refusing to manage an unexpected test index")
    nav_name = navigation_index_name(main)
    search = OpenSearchRestClient(base_url="http://127.0.0.1:9200")
    navigation = OpenSearchNavigationClient(base_url="http://127.0.0.1:9200")
    alias = LegalDatasetAliasService(search)
    source = _Source()
    versions = tuple(source.versions)
    provider = _Provider()
    gateway = ModelGateway(provider, _Recorder())
    snapshots = _Snapshots()
    publisher = LegalDatasetSetPublishService(
        chunks=source, gateway=gateway, search=search, alias=alias, snapshot=snapshots,
        navigation=LegalNavigationIndexService(source, navigation),
        dataset_parser_version="synthetic-v1",
    )
    dataset = LegalDatasetSearchService(
        hybrid=LegalHybridSearchService(gateway, search), alias=alias,
        navigation=LegalNavigationSearchService(navigation),
    )
    try:
        result = await publisher.publish_set(
            version_ids=versions, index_name=main, alias=alias_name,
            model_ref="synthetic", dimension=4,
        )
        assert result.indexed_documents == 2
        assert await search.resolve_alias(alias_name) == main
        assert await navigation.navigation_schema(main) == 1
        assert snapshots.saved[0].manifest["navigation_index"] == nav_name
        assert snapshots.saved[0].manifest["navigation_documents"] == 4

        provider.calls = 0
        hits = await dataset.search_dataset(
            alias=alias_name, query="合成甲法的共同义务", model_ref="synthetic", dimension=4,
        )
        assert hits and {hit.version_id for hit in hits} == {versions[0]}
        assert provider.calls == 1
        hits = await dataset.search_dataset(
            alias=alias_name, query="共同义务", model_ref="synthetic", dimension=4,
        )
        assert {hit.version_id for hit in hits} == set(versions)

        await search.delete_index(nav_name)
        provider.calls = 0
        with pytest.raises(OpenSearchError):
            await dataset.search_dataset(
                alias=alias_name, query="合成甲法", model_ref="synthetic", dimension=4,
            )
        assert provider.calls == 0
        hits = await dataset.search_dataset(
            alias=alias_name, query="共同义务", model_ref="synthetic", dimension=4,
            version_id=versions[1],
        )
        assert hits and {hit.version_id for hit in hits} == {versions[1]}

        # A legacy physical index has no navigation marker and remains searchable.
        await search.ensure_index(legacy, vector_dimension=4)
        documents = []
        for version_id in versions:
            chunk = source.chunks[version_id][0]
            document = chunk_document(
                chunk_id=chunk.id, provision_id=chunk.provision_id, version_id=version_id,
                content=chunk.content, parser_version="synthetic-v1",
            )
            document["content_vector"] = [0.5] * 4
            documents.append(document)
        await search.replace_documents(legacy, tuple(documents), parser_version="synthetic-v1")
        await alias.publish_dataset(alias_name, legacy)
        hits = await dataset.search_dataset(
            alias=alias_name, query="共同义务", model_ref="synthetic", dimension=4,
        )
        assert {hit.version_id for hit in hits} == set(versions)
    finally:
        await search.drop_alias(alias_name)
        for index_name in (nav_name, main, legacy):
            await search.delete_index(index_name)
