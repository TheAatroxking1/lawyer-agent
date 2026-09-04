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
_VERSION_OLD = UUID("01a06ae2-6100-7000-8000-0000000000c2")
_VERSION_NEW = UUID("01a06ae2-6200-7000-8000-0000000000c3")


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
    """Real chunk repository reads behind one short session."""

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
            for version_id, label, text_body in (
                (
                    _VERSION_OLD,
                    "2020版",
                    "第一条 逾期交付按日租金百分之三支付违约金。",
                ),
                (
                    _VERSION_NEW,
                    "2024版",
                    "第一条 逾期交付按日租金百分之五支付违约金。",
                ),
            ):
                provision_id = new_uuid7()
                await connection.execute(
                    text(
                        "INSERT INTO legal_versions "
                        "(id,instrument_id,version_label,status,published_on,"
                        "effective_on,content_hash) "
                        "VALUES (:id,:inst,:label,'current',"
                        "'2020-01-01','2020-03-01',:h)"
                    ),
                    {
                        "id": version_id.bytes,
                        "inst": _INSTRUMENT.bytes,
                        "label": label,
                        "h": content_sha256(text_body),
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
                        "ver": version_id.bytes,
                        "text": text_body,
                        "hash": content_sha256(text_body),
                        "len": len(text_body),
                    },
                )
    finally:
        await engine.dispose()


async def _ensure_chunks(mysql_url: URL, version_id: UUID) -> None:
    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            corpus = SqlAlchemyLegalCorpusRepository(session)
            chunks = SqlAlchemyLegalCorpusChunkRepository(session)
            provisions = await corpus.provisions_for_version(version_id)
            derived = derive_chunks(
                version_id=version_id,
                provisions=provisions,
                parser_version="docx-zip-v1",
            )
            await chunks.replace_chunks_for_version(version_id, derived)
    finally:
        await engine.dispose()


def _gateway() -> ModelGateway:
    return ModelGateway(
        _DeterministicProvider(),
        _Recorder(),
        limits=CallLimits(timeout_seconds=5.0),
    )


async def _run_scenario(
    mysql_url: URL,
    alias: str,
    index_a: str,
    index_b: str,
) -> None:
    os_client = OpenSearchRestClient(base_url="http://127.0.0.1:9200")
    indexer = LegalVectorIndexingService(
        chunks=_ChunkStore(mysql_url),
        gateway=_gateway(),
        search=os_client,
    )
    publisher = LegalDatasetIndexPublishService(
        indexer=indexer,
        alias=LegalDatasetAliasService(os_client),
    )
    alias_service = LegalDatasetAliasService(os_client)
    try:
        first = await publisher.publish_version(
            version_id=_VERSION_OLD,
            index_name=index_a,
            alias=alias,
            model_ref="synthetic-v1",
            dimension=8,
        )
        assert first.indexed_documents == 1
        assert first.previous_target is None
        assert await alias_service.active_dataset_index(alias) == index_a

        second = await publisher.publish_version(
            version_id=_VERSION_NEW,
            index_name=index_b,
            alias=alias,
            model_ref="synthetic-v1",
            dimension=8,
        )
        assert second.indexed_documents == 1
        assert second.previous_target == index_a
        assert await alias_service.active_dataset_index(alias) == index_b
        assert await os_client.index_exists(index_a)  # history index retained
    finally:
        await os_client.drop_alias(alias)
        await os_client.delete_index(index_a)
        await os_client.delete_index(index_b)


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


def test_legal_index_publish_over_real_mysql_and_opensearch(mysql_url: URL) -> None:
    if not asyncio.run(_opensearch_available()):
        pytest.skip("test OpenSearch unavailable")
    database_name = f"lawyer_test_{uuid4().hex}"
    if not re.match(r"^lawyer_test_[a-f0-9]{32}$", database_name):
        raise RuntimeError("refusing to manage an unexpected database name")
    alias = f"lawyer_publish_alias_{uuid4().hex}"
    index_a = f"lawyer_publish_a_{uuid4().hex}"
    index_b = f"lawyer_publish_b_{uuid4().hex}"
    name_pattern = re.compile(r"^lawyer_(publish_alias|publish_[ab])_[a-f0-9]{32}$")
    if not all(name_pattern.fullmatch(name) for name in (alias, index_a, index_b)):
        raise RuntimeError("refusing to manage an unexpected index/alias name")

    isolated_url = mysql_url.set(database=database_name)
    asyncio.run(_create_database(mysql_url, database_name))
    try:
        command.upgrade(_alembic_config(isolated_url), "head")
        asyncio.run(_seed_corpus(isolated_url))
        asyncio.run(_ensure_chunks(isolated_url, _VERSION_OLD))
        asyncio.run(_ensure_chunks(isolated_url, _VERSION_NEW))
        asyncio.run(_run_scenario(isolated_url, alias, index_a, index_b))
    finally:
        asyncio.run(_drop_database(mysql_url, database_name))
