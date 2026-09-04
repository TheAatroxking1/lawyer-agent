from __future__ import annotations

import asyncio
import os
import re
from uuid import UUID, uuid4

import httpx
import pytest

from lawyer_agent.application.legal_vector_indexing import (
    LegalVectorIndexingService,
)
from lawyer_agent.application.model_gateway import ModelGateway
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import (
    ChunkQuality,
    ChunkType,
    LegalChunk,
    content_sha256,
)
from lawyer_agent.domain.model_gateway import (
    CallLimits,
    ModelCallRecord,
)
from lawyer_agent.infrastructure.providers.embedding import (
    LocalSentenceTransformerEmbeddingProvider,
)
from lawyer_agent.infrastructure.search.opensearch import OpenSearchRestClient

pytestmark = [pytest.mark.integration]

_MODEL = os.getenv("LAWYER_TEST_EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")


class _FakeChunkStore:
    def __init__(self, chunks: tuple[LegalChunk, ...]) -> None:
        self._chunks = chunks

    async def chunks_for_version(
        self, version_id: UUID
    ) -> tuple[LegalChunk, ...]:
        return self._chunks


class _MemoryRecorder:
    def __init__(self) -> None:
        self.records: list[ModelCallRecord] = []

    async def append(self, record: ModelCallRecord) -> None:
        self.records.append(record)


_VERSION_ID = UUID("01a06ae2-6200-7000-8000-0000000000c3")


def _chunk(content: str) -> LegalChunk:
    return LegalChunk(
        id=new_uuid7(),
        version_id=_VERSION_ID,
        provision_id=new_uuid7(),
        chunk_type=ChunkType.PROVISION,
        quality=ChunkQuality.OK,
        content=content,
        content_hash=content_sha256(content),
        parser_version="docx-zip-v1",
    )


async def _os_available() -> bool:
    try:
        async with httpx.AsyncClient(base_url="http://127.0.0.1:9200", timeout=2.0) as client:
            response = await client.get("/_cluster/health")
        return response.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def _offline_env() -> None:
    """Run strictly from the local HF cache to avoid network-probe hangs.

    An uncached model then fails fast and the caller skips the test instead of
    blocking on a hub connectivity probe.
    """
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")


async def _model_loads() -> bool:
    _offline_env()
    try:
        provider = LocalSentenceTransformerEmbeddingProvider(model_name_or_path=_MODEL)
        (vector,) = await provider.embed(texts=("测试",), dimension=512, timeout_seconds=120.0)
        return len(vector.values) > 0
    except Exception:  # noqa: BLE001
        return False


def test_real_embedding_knn_over_opensearch() -> None:
    """Real CPU embedding -> gateway -> OS k-NN nearest-neighbour retrieval."""
    if not asyncio.run(_os_available()):
        pytest.skip("test OpenSearch unavailable")
    if not asyncio.run(_model_loads()):
        pytest.skip("embedding model unavailable (download or network)")

    _offline_env()
    provider = LocalSentenceTransformerEmbeddingProvider(model_name_or_path=_MODEL)
    gateway = ModelGateway(
        provider,
        _MemoryRecorder(),
        limits=CallLimits(timeout_seconds=300.0),
    )
    # Determine model dimension by encoding once.
    probe = asyncio.run(
        provider.embed(texts=("维探测",), dimension=512, timeout_seconds=120.0)
    )
    dimension = len(probe[0].values)
    assert dimension == 512

    a = _chunk("承租人逾期支付租金，出租人有权按日收取违约金。")
    b = _chunk("租赁合同中的抵押条款涉及不动产担保登记。")
    version_id = _VERSION_ID
    chunks = (a, b)
    store = _FakeChunkStore(chunks)
    index_name = f"lawyer_real_embed_{uuid4().hex}"
    if not re.match(r"^lawyer_real_embed_[a-f0-9]{32}$", index_name):
        raise RuntimeError("refusing to manage an unexpected index name")
    search = OpenSearchRestClient(base_url="http://127.0.0.1:9200")
    try:
        service = LegalVectorIndexingService(store, gateway, search)
        count = asyncio.run(
            service.index_version(
                version_id=version_id,
                index_name=index_name,
                model_ref=_MODEL,
                dimension=dimension,
                batch_size=8,
            )
        )
        assert count == 2
        # Refresh then query with text semantically equal to chunk a.
        asyncio.run(_refresh(index_name))
        (query_vector,) = asyncio.run(
            provider.embed(
                texts=("逾期支付租金应当支付违约金。",),
                dimension=dimension,
                timeout_seconds=120.0,
            )
        )
        hits = asyncio.run(
            search.search_knn(
                index_name,
                query_vector=query_vector.values,
                limit=3,
                version_id=version_id,
            )
        )
        assert len(hits) >= 1
        assert hits[0].chunk_id == a.id
    finally:
        asyncio.run(search.delete_index(index_name))


async def _refresh(index_name: str) -> None:
    async with httpx.AsyncClient(base_url="http://127.0.0.1:9200", timeout=5.0) as client:
        await client.post(f"/{index_name}/_refresh")
