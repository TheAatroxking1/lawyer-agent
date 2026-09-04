from __future__ import annotations

import asyncio
import re
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_ai_job_migration import _alembic_config

from alembic import command
from lawyer_agent.application.legal_corpus_chunks import derive_chunks
from lawyer_agent.application.legal_dataset_search import (
    LegalDatasetNotPublished,
    LegalDatasetSearchService,
)
from lawyer_agent.application.legal_hybrid_search import LegalHybridSearchService
from lawyer_agent.application.legal_index_alias import LegalDatasetAliasService
from lawyer_agent.application.legal_index_publish import (
    LegalDatasetIndexPublishService,
)
from lawyer_agent.application.legal_vector_indexing import (
    LegalVectorIndexingService,
)
from lawyer_agent.application.model_gateway import ModelGateway
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import content_sha256
from lawyer_agent.domain.model_gateway import (
    CallLimits,
    EmbeddingVector,
    ModelCallRecord,
    RankedDocument,
    TokenUsage,
)
from lawyer_agent.infrastructure.persistence.repositories.legal_corpus import (
    SqlAlchemyLegalCorpusChunkRepository,
    SqlAlchemyLegalCorpusRepository,
)
from lawyer_agent.infrastructure.search.opensearch import OpenSearchRestClient

pytestmark = [pytest.mark.integration, pytest.mark.mysql]

_INSTRUMENT = UUID("01a06ae2-6000-7000-8000-0000000000c1")
_VERSION = UUID("01a06ae2-6200-7000-8000-0000000000c3")
_FULL_TEXT = "承租人逾期支付租金应当支付违约金。"


async def _opensearch_available(base_url: str = "http://127.0.0.1:9200") -> bool:
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=2.0) as client:
            response = await client.get("/_cluster/health")
        return response.status_code == 200
    except Exception:  # noqa: BLE001 - availability probe
        return False


class _DeterministicProvider:
    async def embed(
        self,
        *,
        texts: Any,
        dimension: int,
        timeout_seconds: float,
    ) -> tuple[EmbeddingVector, ...]:
        del timeout_seconds
        vectors: list[EmbeddingVector] = []
        for item in texts:
            seed = sum((i + 1) * ord(ch) for i, ch in enumerate(item))
            values = tuple(
                float((seed * (p + 1)) % 31) / 31.0 for p in range(dimension)
            )
            vectors.append(EmbeddingVector(values=values, dimension=dimension))
        return tuple(vectors)

    async def rerank(self, **kwargs: object) -> tuple[RankedDocument, ...]:
        raise AssertionError("rerank not used")

    async def chat(self, **kwargs: object) -> tuple[str, TokenUsage]:
        raise AssertionError("chat not used")


class _Recorder:
    async def append(self, record: ModelCallRecord) -> None:
        del record


class _ChunkStore:
    def __init__(self, mysql_url: URL) -> None:
        self._mysql_url = mysql_url

    async def chunks_for_version(self, version_id: UUID):
        engine = create_async_engine(self._mysql_url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                repo = SqlAlchemyLegalCorpusChunkRepository(session)
                return await repo.chunks_for_version(version_id)
        finally:
            await engine.dispose()


def _gateway() -> ModelGateway:
    return ModelGateway(
        _DeterministicProvider(),
        _Recorder(),
        limits=CallLimits(timeout_seconds=5.0),
    )


async def _seed_corpus(mysql_url: URL) -> None:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO legal_instruments "
                    "(id,title,issuing_authority,jurisdiction,version) "
                    "VALUES (:id,'示范法','机关','national',1)"
                ),
                {"id": _INSTRUMENT.bytes},
            )
            provision_id = new_uuid7()
            await connection.execute(
                text(
                    "INSERT INTO legal_versions "
                    "(id,instrument_id,version_label,status,published_on,"
                    "effective_on,content_hash) "
                    "VALUES (:id,:inst,'2024版','current',"
                    "'2024-01-01','2024-03-01',:h)"
                ),
                {
                    "id": _VERSION.bytes,
                    "inst": _INSTRUMENT.bytes,
                    "h": content_sha256(_FULL_TEXT),
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO legal_provisions "
                    "(id,version_id,provision_no,level,full_text,content_hash,"
                    "char_start,char_end) "
                    "VALUES (:id,:ver,'第一条','article',:text,:hash,0,:len)"
                ),
                {
                    "id": provision_id.bytes,
                    "ver": _VERSION.bytes,
                    "text": _FULL_TEXT,
                    "hash": content_sha256(_FULL_TEXT),
                    "len": len(_FULL_TEXT),
                },
            )
    finally:
        await engine.dispose()


async def _ensure_chunks(mysql_url: URL) -> None:
    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            corpus = SqlAlchemyLegalCorpusRepository(session)
            chunks = SqlAlchemyLegalCorpusChunkRepository(session)
            provisions = await corpus.provisions_for_version(_VERSION)
            derived = derive_chunks(
                version_id=_VERSION,
                provisions=provisions,
                parser_version="docx-zip-v1",
            )
            await chunks.replace_chunks_for_version(_VERSION, derived)
    finally:
        await engine.dispose()


async def _run_scenario(mysql_url: URL, alias: str, index_name: str) -> None:
    os_client = OpenSearchRestClient(base_url="http://127.0.0.1:9200")
    alias_service = LegalDatasetAliasService(os_client)
    indexer = LegalVectorIndexingService(
        chunks=_ChunkStore(mysql_url),
        gateway=_gateway(),
        search=os_client,
    )
    publisher = LegalDatasetIndexPublishService(indexer=indexer, alias=alias_service)
    hybrid = LegalHybridSearchService(
        gateway=_gateway(),
        search=os_client,
    )
    searcher = LegalDatasetSearchService(hybrid=hybrid, alias=alias_service)
    try:
        # No alias yet -> dataset is not published.
        try:
            await searcher.search_dataset(
                alias=alias,
                query="逾期支付租金",
                model_ref="synthetic-v1",
                dimension=8,
            )
        except LegalDatasetNotPublished:
            pass
        else:
            raise AssertionError("expected LegalDatasetNotPublished")

        await publisher.publish_version(
            version_id=_VERSION,
            index_name=index_name,
            alias=alias,
            model_ref="synthetic-v1",
            dimension=8,
        )
        hits = await searcher.search_dataset(
            alias=alias,
            query="逾期支付租金违约金",
            model_ref="synthetic-v1",
            dimension=8,
            limit=5,
        )
        assert hits
        assert all(hit.version_id == _VERSION for hit in hits)
    finally:
        await os_client.drop_alias(alias)
        await os_client.delete_index(index_name)


async def _create_database(mysql_url: URL, database_name: str) -> None:
    engine = create_async_engine(mysql_url.set(database="mysql"))
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f"CREATE DATABASE `{database_name}`"))
    finally:
        await engine.dispose()


async def _drop_database(mysql_url: URL, database_name: str) -> None:
    engine = create_async_engine(mysql_url.set(database="mysql"))
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f"DROP DATABASE `{database_name}`"))
    finally:
        await engine.dispose()


def test_legal_dataset_search_over_real_mysql_and_opensearch(mysql_url: URL) -> None:
    if not asyncio.run(_opensearch_available()):
        pytest.skip("test OpenSearch unavailable")
    database_name = f"lawyer_test_{uuid4().hex}"
    if not re.match(r"^lawyer_test_[a-f0-9]{32}$", database_name):
        raise RuntimeError("refusing to manage an unexpected database name")
    alias = f"lawyer_ds_alias_{uuid4().hex}"
    index_name = f"lawyer_ds_idx_{uuid4().hex}"
    if not re.match(r"^lawyer_ds_(alias|idx)_[a-f0-9]{32}$", alias) or not re.match(
        r"^lawyer_ds_(alias|idx)_[a-f0-9]{32}$", index_name
    ):
        raise RuntimeError("refusing to manage an unexpected index/alias name")

    isolated_url = mysql_url.set(database=database_name)
    asyncio.run(_create_database(mysql_url, database_name))
    try:
        command.upgrade(_alembic_config(isolated_url), "head")
        asyncio.run(_seed_corpus(isolated_url))
        asyncio.run(_ensure_chunks(isolated_url))
        asyncio.run(_run_scenario(isolated_url, alias, index_name))
    finally:
        asyncio.run(_drop_database(mysql_url, database_name))
