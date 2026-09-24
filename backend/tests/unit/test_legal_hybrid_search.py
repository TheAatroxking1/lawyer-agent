from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID, uuid4

import httpx
import pytest

from lawyer_agent.application.legal_hybrid_search import LegalHybridSearchService
from lawyer_agent.application.model_gateway import (
    ModelGateway,
    ModelInputInvalid,
)
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_search import LegalSearchHit
from lawyer_agent.domain.model_gateway import (
    CallLimits,
    EmbeddingVector,
    ModelCallRecord,
    RankedDocument,
    TokenUsage,
)
from lawyer_agent.infrastructure.search.opensearch import OpenSearchRestClient

VERSION = UUID("01a06ae2-6200-7000-8000-0000000000c3")
A = new_uuid7()
B = new_uuid7()
PA = new_uuid7()
PB = new_uuid7()


def _hit(chunk_id, provision_id, score: float) -> LegalSearchHit:
    return LegalSearchHit(
        chunk_id=chunk_id, provision_id=provision_id, version_id=VERSION, score=score
    )


class _Recorder:
    def __init__(self) -> None:
        self.records: list[ModelCallRecord] = []

    async def append(self, record: ModelCallRecord) -> None:
        self.records.append(record)


class _Provider:
    async def embed(
        self,
        *,
        texts: Sequence[str],
        dimension: int,
        timeout_seconds: float,
    ) -> tuple[EmbeddingVector, ...]:
        def vec(text: str) -> EmbeddingVector:
            seed = sum((i + 1) * ord(ch) for i, ch in enumerate(text))
            return EmbeddingVector(
                values=tuple(
                    float((seed * (p + 1)) % 31) / 31.0 for p in range(dimension)
                ),
                dimension=dimension,
            )

        return tuple(vec(t) for t in texts)

    async def rerank(self, **kwargs: object) -> tuple[RankedDocument, ...]:
        raise AssertionError("rerank not used")

    async def chat(self, **kwargs: object) -> tuple[str, TokenUsage]:
        raise AssertionError("chat not used")


class _Transport(httpx.AsyncBaseTransport):
    def __init__(self, hit_sets: list[list[dict[str, object]]]) -> None:
        self._hit_sets = list(hit_sets)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self._hit_sets:
            hits: list[dict[str, object]] = []
        else:
            hits = self._hit_sets.pop(0)
        return httpx.Response(200, json={"hits": {"hits": hits}}, request=request)


def _hit_raw(chunk_id, provision_id, score: float) -> dict[str, object]:
    return {
        "_score": score,
        "_source": {
            "chunk_id": str(chunk_id),
            "provision_id": str(provision_id),
            "version_id": str(VERSION),
        },
    }


def _json(request: httpx.Request) -> dict[str, object]:
    import json

    return json.loads(request.content.decode("utf-8"))


def _service(transport: _Transport) -> LegalHybridSearchService:
    gateway = ModelGateway(
        _Provider(),
        _Recorder(),
        limits=CallLimits(timeout_seconds=5.0),
    )
    search = OpenSearchRestClient(base_url="http://os:9200", transport=transport)
    return LegalHybridSearchService(gateway, search)


async def test_search_runs_bm25_and_knn_then_rrf_orders() -> None:
    bm25 = [_hit_raw(A, PA, 9.0), _hit_raw(B, PB, 4.0)]
    knn = [_hit_raw(B, PB, 8.0), _hit_raw(A, PA, 3.0)]
    transport = _Transport([bm25, knn])
    service = _service(transport)
    hits = await service.search(
        query="违约金",
        model_ref="bge-small-zh",
        dimension=8,
        index_name="legal_corpus_v1",
        version_id=VERSION,
        limit=10,
    )
    # Both requests were issued against the index.
    assert [r.url.path for r in transport.requests] == [
        "/legal_corpus_v1/_search",
        "/legal_corpus_v1/_search",
    ]
    bm25_body = _json(transport.requests[0])
    assert bm25_body["query"]["bool"]["must"][0]["match"]["content"] == "违约金"
    assert bm25_body["query"]["bool"]["filter"][0]["term"]["version_id"] == str(VERSION)
    knn_body = _json(transport.requests[1])
    knn_query = knn_body["query"]["knn"]["content_vector"]
    assert knn_query["vector"] and len(knn_query["vector"]) == 8
    assert knn_query["filter"]["term"]["version_id"] == str(VERSION)
    # RRF: A is rank1 in bm25 and rank2 in knn; B rank2 bm25 and rank1 knn;
    # equal fused scores -> stable id tie-break keeps exactly both.
    assert len(hits) == 2
    assert {str(hit.chunk_id) for hit in hits} == {str(A), str(B)}


async def test_search_empty_results_returns_empty() -> None:
    transport = _Transport([[], []])
    service = _service(transport)
    hits = await service.search(
        query="无此内容", model_ref="m", dimension=4, index_name="idx"
    )
    assert hits == ()


async def test_multiple_versions_share_one_embedding_and_identical_scope() -> None:
    other_version = new_uuid7()
    recorder = _Recorder()
    transport = _Transport([[_hit_raw(A, PA, 2.0)], [_hit_raw(A, PA, 1.0)]])
    service = LegalHybridSearchService(
        ModelGateway(_Provider(), recorder),
        OpenSearchRestClient(base_url="http://os:9200", transport=transport),
    )
    hits = await service.search(
        query="示例法第一章", model_ref="m", dimension=4, index_name="idx",
        version_ids=(VERSION, other_version),
    )
    assert len(hits) == 1 and hits[0].chunk_id == A
    assert len(recorder.records) == 1
    bm25, knn = [_json(request) for request in transport.requests]
    expected = {"terms": {"version_id": [str(VERSION), str(other_version)]}}
    assert bm25["query"]["bool"]["filter"] == [expected]
    assert knn["query"]["knn"]["content_vector"]["filter"] == expected


@pytest.mark.parametrize("scope", [(), (VERSION, VERSION), (uuid4(),), [VERSION]])
async def test_invalid_multi_version_scope_is_rejected_before_embedding(scope: object) -> None:
    recorder = _Recorder()
    transport = _Transport([])
    service = LegalHybridSearchService(
        ModelGateway(_Provider(), recorder), OpenSearchRestClient(transport=transport),
    )
    with pytest.raises(ModelInputInvalid):
        await service.search(
            query="示例法", model_ref="m", dimension=4, index_name="idx", version_ids=scope,
        )
    assert recorder.records == [] and transport.requests == []


async def test_single_and_multi_version_scope_are_mutually_exclusive() -> None:
    transport = _Transport([])
    with pytest.raises(ModelInputInvalid, match="mutually exclusive"):
        await _service(transport).search(
            query="示例法", model_ref="m", dimension=4, index_name="idx",
            version_id=VERSION, version_ids=(VERSION,),
        )
    assert transport.requests == []


async def test_search_truncates_to_limit() -> None:
    bm25 = [_hit_raw(A, PA, 5.0)]
    knn = []
    transport = _Transport([bm25, knn])
    service = _service(transport)
    hits = await service.search(
        query="限缩", model_ref="m", dimension=4, index_name="idx", limit=1
    )
    assert len(hits) == 1


async def test_search_rejects_invalid_inputs() -> None:
    transport = _Transport([])
    service = _service(transport)
    with pytest.raises(ModelInputInvalid):
        await service.search(query="  ", model_ref="m", dimension=4, index_name="i")
    with pytest.raises(ModelInputInvalid):
        await service.search(query="x", model_ref="m", dimension=4, index_name="i", limit=0)
    with pytest.raises(ModelInputInvalid):
        await service.search(query="x", model_ref="m", dimension=4, index_name="i", bm25_size=0)
    with pytest.raises(ModelInputInvalid):
        await service.search(query="x", model_ref="m", dimension=4, index_name="  ")


class _ChunkScope:
    def __init__(self, indexed: bool) -> None:
        self._indexed = indexed

    async def version_is_indexed(self, version_id: UUID) -> bool:
        return self._indexed


async def test_search_skips_when_version_not_indexed() -> None:
    transport = _Transport([])
    service = LegalHybridSearchService(
        ModelGateway(_Provider(), _Recorder()),
        OpenSearchRestClient(transport=transport),
        chunks=_ChunkScope(indexed=False),
    )
    hits = await service.search(
        query="违约金",
        model_ref="m",
        dimension=4,
        index_name="idx",
        version_id=VERSION,
    )
    assert hits == ()
    assert transport.requests == []
