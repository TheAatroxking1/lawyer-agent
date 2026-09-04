from __future__ import annotations

import asyncio
import re
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_ai_job_migration import _alembic_config

from alembic import command
from lawyer_agent.application.legal_corpus_chunks import derive_chunks
from lawyer_agent.application.legal_vector_indexing import (
    LegalVectorIndexingService,
)
from lawyer_agent.application.model_gateway import ModelGateway
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import content_sha256
from lawyer_agent.domain.model_gateway import (
    CallLimits,
    ChatMessage,
    EmbeddingVector,
    ModelCallRecord,
    RankedDocument,
    TokenUsage,
)
from lawyer_agent.infrastructure.persistence.repositories.legal_corpus import (
    SqlAlchemyLegalCorpusChunkRepository,
    SqlAlchemyLegalCorpusRepository,
)
from lawyer_agent.infrastructure.search.opensearch import (
    DEFAULT_OPENSEARCH_URL,
    OpenSearchRestClient,
)

pytestmark = [pytest.mark.integration, pytest.mark.mysql]


async def _opensearch_available(base_url: str = DEFAULT_OPENSEARCH_URL) -> bool:
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=2.0) as client:
            response = await client.get("/_cluster/health")
        return response.status_code == 200
    except Exception:  # noqa: BLE001 - availability probe
        return False


def test_legal_corpus_opensearch_end_to_end(mysql_url: URL) -> None:
    base_url = "http://127.0.0.1:9200"
    if not asyncio.run(_opensearch_available(base_url)):
        pytest.skip("test OpenSearch unavailable")
    index_name = f"lawyer_legal_test_{uuid4().hex}"
    if not re.match(r"^lawyer_legal_test_[a-f0-9]{32}$", index_name):
        raise RuntimeError("refusing to manage an unexpected index name")
    database_name = f"lawyer_test_{uuid4().hex}"
    if not re.match(r"^lawyer_test_[a-f0-9]{32}$", database_name):
        raise RuntimeError("refusing to manage an unexpected database name")
    asyncio.run(
        _create_database(mysql_url, database_name)
    )
    isolated_url = mysql_url.set(database=database_name)
    command.upgrade(_alembic_config(isolated_url), "head")
    try:
        asyncio.run(
            _run_e2e(isolated_url, database_name=database_name, index_name=index_name)
        )
    finally:
        asyncio.run(_drop_database(mysql_url, database_name))


async def _run_e2e(
    mysql_url: URL, *, database_name: str, index_name: str
) -> None:
    client = OpenSearchRestClient(base_url="http://127.0.0.1:9200")
    await _seed_corpus(mysql_url)
    await client.ensure_index(index_name)

    # 1. Derive chunks from the seeded version and index them.
    version_id = UUID("01a06ae2-6200-7000-8000-0000000000c3")
    await _index_version(mysql_url, client, index_name, version_id)

    # 2. Wait for refresh then BM25 search.
    async with httpx.AsyncClient(base_url="http://127.0.0.1:9200", timeout=5.0) as http:
        await http.post(f"/{index_name}/_refresh")

    hits = await client.search_bm25(index_name, query="百分之五", limit=5)
    assert len(hits) >= 1
    assert all(hit.version_id == version_id for hit in hits)

    # 3. Version filter narrows to the expected version only.
    other_version = UUID("01a06ae2-6100-7000-8000-0000000000c2")
    filtered = await client.search_bm25(
        index_name, query="百分之五", limit=5, version_id=other_version
    )
    assert filtered == ()

    # 4. Idempotent rebuild: re-running replace keeps document count stable.
    before = await _doc_count("http://127.0.0.1:9200", index_name)
    await _index_version(mysql_url, client, index_name, version_id)
    async with httpx.AsyncClient(base_url="http://127.0.0.1:9200", timeout=5.0) as http:
        await http.post(f"/{index_name}/_refresh")
    after = await _doc_count("http://127.0.0.1:9200", index_name)
    assert before == after
    assert before >= 1
    await client.delete_index(index_name)


async def _index_version(
    mysql_url: URL,
    client: OpenSearchRestClient,
    index_name: str,
    version_id: UUID,
) -> None:
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
            documents = tuple(
                {
                    "chunk_id": str(chunk.id),
                    "provision_id": str(chunk.provision_id),
                    "version_id": str(chunk.version_id),
                    "content": chunk.content,
                    "parser_version": chunk.parser_version or "",
                }
                for chunk in derived
            )
            await client.replace_documents(
                index_name, documents, parser_version="docx-zip-v1"
            )
    finally:
        await engine.dispose()


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
                {"id": UUID("01a06ae2-6000-7000-8000-0000000000c1").bytes},
            )
            for version_id, label, text_body in (
                (
                    UUID("01a06ae2-6100-7000-8000-0000000000c2"),
                    "2020版",
                    "第一条 逾期交付按日租金百分之三支付违约金。",
                ),
                (
                    UUID("01a06ae2-6200-7000-8000-0000000000c3"),
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
                        "inst": UUID("01a06ae2-6000-7000-8000-0000000000c1").bytes,
                        "label": label,
                        "h": content_sha256(text_body),
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO legal_provisions "
                        "(id,version_id,provision_no,level,full_text,content_hash,"
                        "char_start,char_end) "
                        "VALUES (:id,:ver,'第一条','article',:text,:h,0,:end)"
                    ),
                    {
                        "id": provision_id.bytes,
                        "ver": version_id.bytes,
                        "text": text_body,
                        "h": content_sha256(text_body),
                        "end": len(text_body),
                    },
                )
    finally:
        await engine.dispose()


async def _doc_count(base_url: str, index_name: str) -> int:
    async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as client:
        response = await client.get(f"/{index_name}/_count")
        response.raise_for_status()
        return int(response.json()["count"])


class _MemoryRecorder:
    def __init__(self) -> None:
        self.records: list[ModelCallRecord] = []

    async def append(self, record: ModelCallRecord) -> None:
        self.records.append(record)


class _DeterministicProvider:
    """Synthetic provider used only to exercise the real k-NN pipeline."""

    async def embed(
        self,
        *,
        texts: tuple[str, ...],
        dimension: int,
        timeout_seconds: float,
    ) -> tuple[EmbeddingVector, ...]:
        def vector_for(text: str) -> EmbeddingVector:
            seed = sum((index + 1) * ord(char) for index, char in enumerate(text))
            values = tuple(
                float((seed * (position + 1)) % 97) / 97.0
                for position in range(dimension)
            )
            return EmbeddingVector(values=values, dimension=dimension)

        return tuple(vector_for(text) for text in texts)

    async def rerank(
        self,
        *,
        query: str,
        documents: tuple[str, ...],
        timeout_seconds: float,
    ) -> tuple[RankedDocument, ...]:
        raise AssertionError("rerank not exercised")

    async def chat(
        self,
        *,
        messages: tuple[ChatMessage, ...],
        timeout_seconds: float,
    ) -> tuple[str, TokenUsage]:
        raise AssertionError("chat not exercised")


def test_legal_corpus_knn_indexing_over_real_mysql_and_opensearch(
    mysql_url: URL,
) -> None:
    if not asyncio.run(_opensearch_available("http://127.0.0.1:9200")):
        pytest.skip("test OpenSearch unavailable")
    index_name = f"lawyer_legal_knn_{uuid4().hex}"
    database_name = f"lawyer_test_{uuid4().hex}"
    if not re.match(r"^lawyer_legal_knn_[a-f0-9]{32}$", index_name):
        raise RuntimeError("refusing to manage an unexpected index name")
    if not re.match(r"^lawyer_test_[a-f0-9]{32}$", database_name):
        raise RuntimeError("refusing to manage an unexpected database name")
    asyncio.run(_create_database(mysql_url, database_name))
    isolated_url = mysql_url.set(database=database_name)
    command.upgrade(_alembic_config(isolated_url), "head")
    client = OpenSearchRestClient(base_url="http://127.0.0.1:9200")
    try:
        asyncio.run(_seed_corpus(isolated_url))
        version_id = UUID("01a06ae2-6200-7000-8000-0000000000c3")

        async def _run() -> None:
            engine = create_async_engine(isolated_url)
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
                    service = LegalVectorIndexingService(
                        chunks,
                        ModelGateway(
                            _DeterministicProvider(),
                            _MemoryRecorder(),
                            limits=CallLimits(timeout_seconds=5.0),
                        ),
                        client,
                    )
                    count = await service.index_version(
                        version_id=version_id,
                        index_name=index_name,
                        model_ref="synthetic-v1",
                        dimension=8,
                    )
                    assert count == 1
            finally:
                await engine.dispose()

        asyncio.run(_run())
        asyncio.run(_refresh_index("http://127.0.0.1:9200", index_name))
        provider = _DeterministicProvider()
        (query_vector,) = asyncio.run(
            provider.embed(
                texts=("第一条 逾期交付按日租金百分之五支付违约金。",),
                dimension=8,
                timeout_seconds=5.0,
            )
        )
        hits = asyncio.run(
            client.search_knn(
                index_name,
                query_vector=query_vector.values,
                limit=3,
                version_id=version_id,
            )
        )
        assert len(hits) == 1
        assert hits[0].version_id == version_id
        # Exact same source text -> nearest neighbour is the indexed chunk.
        assert hits[0].score > 0.0
    finally:
        asyncio.run(client.delete_index(index_name))
        asyncio.run(_drop_database(mysql_url, database_name))


async def _refresh_index(base_url: str, index_name: str) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as http:
        await http.post(f"/{index_name}/_refresh")
