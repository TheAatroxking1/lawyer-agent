from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

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
    ChatMessage,
    EmbeddingVector,
    ModelCallRecord,
    RankedDocument,
    TokenUsage,
)


def _chunk(*, content: str) -> LegalChunk:
    return LegalChunk(
        id=new_uuid7(),
        version_id=new_uuid7(),
        provision_id=new_uuid7(),
        chunk_type=ChunkType.PROVISION,
        quality=ChunkQuality.OK,
        content=content,
        content_hash=content_sha256(content),
        parser_version="docx-zip-v1",
    )


class FakeChunkStore:
    def __init__(self, chunks: tuple[LegalChunk, ...]) -> None:
        self.chunks = chunks

    async def chunks_for_version(
        self, version_id: UUID
    ) -> tuple[LegalChunk, ...]:
        return self.chunks


class MemoryRecorder:
    def __init__(self) -> None:
        self.records: list[ModelCallRecord] = []

    async def append(self, record: ModelCallRecord) -> None:
        self.records.append(record)


class DeterministicProvider:
    async def embed(
        self,
        *,
        texts: Sequence[str],
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
        documents: Sequence[str],
        timeout_seconds: float,
    ) -> tuple[RankedDocument, ...]:
        raise AssertionError("not used")

    async def chat(
        self,
        *,
        messages: Sequence[ChatMessage],
        timeout_seconds: float,
    ) -> tuple[str, TokenUsage]:
        raise AssertionError("not used")


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._mode = "create"

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("_bulk"):
            self._mode = "bulk"
            return httpx.Response(200, json={"errors": False}, request=request)
        if request.url.path.endswith("_delete_by_query"):
            self._mode = "delete"
            return httpx.Response(200, json={}, request=request)
        if request.method == "PUT":
            return httpx.Response(200, json={}, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)


def _service(chunks: tuple[LegalChunk, ...], transport: RecordingTransport):
    gateway = ModelGateway(
        DeterministicProvider(),
        MemoryRecorder(),
        limits=CallLimits(timeout_seconds=5.0),
    )
    search = OpenSearchRestClientForTest(transport)
    return (
        LegalVectorIndexingService(FakeChunkStore(chunks), gateway, search),
        search,
    )


class OpenSearchRestClientForTest:
    """Minimal double matching the OpenSearchRestClient surface used here."""

    def __init__(self, transport: RecordingTransport) -> None:
        self.transport = transport
        self.ensure_calls: list[tuple[str, int | None]] = []
        self.replace_calls: list[tuple[str, int]] = []

    async def ensure_index(
        self, index_name: str, *, vector_dimension: int | None = None
    ) -> None:
        self.ensure_calls.append((index_name, vector_dimension))

    async def replace_documents(
        self,
        index_name: str,
        documents: tuple[dict[str, object], ...],
        *,
        parser_version: str,
    ) -> None:
        self.replace_calls.append((index_name, len(documents)))
        self._documents = documents

    @property
    def documents(self) -> tuple[dict[str, object], ...]:
        return self._documents  # type: ignore[attr-defined]


async def test_index_version_empty_chunks_is_noop() -> None:
    transport = RecordingTransport()
    service, search = _service((), transport)
    count = await service.index_version(
        version_id=new_uuid7(),
        index_name="legal_corpus_v1",
        model_ref="embed-m",
        dimension=4,
    )
    assert count == 0
    assert search.ensure_calls == []
    assert search.replace_calls == []


async def test_index_version_batches_embeds_and_indexes_vectors() -> None:
    chunk_a = _chunk(content="第一条 内容甲。")
    chunk_b = _chunk(content="第二条 内容乙。")
    chunk_c = _chunk(content="第三条 内容丙。")
    transport = RecordingTransport()
    service, search = _service((chunk_a, chunk_b, chunk_c), transport)
    count = await service.index_version(
        version_id=new_uuid7(),
        index_name="legal_corpus_v1",
        model_ref="embed-m",
        dimension=4,
        batch_size=2,
    )
    assert count == 3
    assert search.ensure_calls == [("legal_corpus_v1", 4)]
    assert search.replace_calls == [("legal_corpus_v1", 3)]
    documents = search.documents
    assert all("content_vector" in document for document in documents)
    vectors = [document["content_vector"] for document in documents]
    assert all(isinstance(vector, list) and len(vector) == 4 for vector in vectors)
    assert documents[0]["content"] == "第一条 内容甲。"


async def test_index_version_rejects_bad_batch_size() -> None:
    transport = RecordingTransport()
    service, search = _service((_chunk(content="内容"),), transport)
    with pytest.raises(ValueError):
        await service.index_version(
            version_id=new_uuid7(),
            index_name="i",
            model_ref="m",
            dimension=4,
            batch_size=0,
        )
