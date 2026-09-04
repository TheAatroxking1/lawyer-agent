from __future__ import annotations

import httpx
import pytest

from lawyer_agent.application.retrieval_fusion import reciprocal_rank_fusion
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_search import LegalSearchHit
from lawyer_agent.infrastructure.search.opensearch import (
    OpenSearchError,
    OpenSearchRestClient,
)

CHUNK_A = new_uuid7()
CHUNK_B = new_uuid7()
CHUNK_C = new_uuid7()
PROVISION_A = new_uuid7()
PROVISION_B = new_uuid7()
PROVISION_C = new_uuid7()
VERSION = new_uuid7()


def _hit(chunk_id, provision_id, score: float) -> LegalSearchHit:
    return LegalSearchHit(
        chunk_id=chunk_id,
        provision_id=provision_id,
        version_id=VERSION,
        score=score,
    )


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self._responses:
            return httpx.Response(200, json={"ok": True}, request=request)
        return self._responses.pop(0)


def _json_body(request: httpx.Request) -> dict[str, object]:
    import json as _json

    return _json.loads(request.content.decode("utf-8"))


async def test_ensure_index_with_vector_dimension_adds_knn_mapping() -> None:
    transport = RecordingTransport([httpx.Response(200, json={})])
    client = OpenSearchRestClient(transport=transport)
    await client.ensure_index("legal_corpus_v2", vector_dimension=1792)
    payload = _json_body(transport.requests[0])
    props = payload["mappings"]["properties"]
    assert props["content_vector"] == {"type": "knn_vector", "dimension": 1792}


async def test_ensure_index_without_vector_dimension_keeps_text_only() -> None:
    transport = RecordingTransport([httpx.Response(200, json={})])
    client = OpenSearchRestClient(transport=transport)
    await client.ensure_index("legal_corpus_v1")
    props = _json_body(transport.requests[0])["mappings"]["properties"]
    assert "content_vector" not in props


def _hit_response(chunk_id, provision_id, score: float) -> dict[str, object]:
    return {
        "_score": score,
        "_source": {
            "chunk_id": str(chunk_id),
            "provision_id": str(provision_id),
            "version_id": str(VERSION),
        },
    }


async def test_search_knn_builds_query_and_parses_hits() -> None:
    response_body = {"hits": {"hits": [_hit_response(CHUNK_A, PROVISION_A, 0.9)]}}
    transport = RecordingTransport([httpx.Response(200, json=response_body)])
    client = OpenSearchRestClient(transport=transport)
    hits = await client.search_knn(
        "legal_corpus_v2",
        query_vector=(0.1, 0.2, 0.3),
        limit=5,
        version_id=VERSION,
    )
    payload = _json_body(transport.requests[0])
    assert payload["query"]["bool"]["must"][0]["knn"]["field"] == "content_vector"
    assert payload["query"]["bool"]["must"][0]["knn"]["query_vector"] == [0.1, 0.2, 0.3]
    assert payload["query"]["bool"]["must"][0]["knn"]["k"] == 5
    assert payload["query"]["bool"]["filter"][0]["term"]["version_id"] == str(VERSION)
    assert hits == (_hit(CHUNK_A, PROVISION_A, 0.9),)


async def test_search_knn_without_filter_uses_top_level_knn() -> None:
    response_body = {"hits": {"hits": []}}
    transport = RecordingTransport([httpx.Response(200, json=response_body)])
    client = OpenSearchRestClient(transport=transport)
    await client.search_knn("idx", query_vector=(0.5,), limit=3)
    payload = _json_body(transport.requests[0])
    assert payload["query"]["knn"]["field"] == "content_vector"
    assert "bool" not in payload["query"]


async def test_search_knn_rejects_bad_vector_and_maps_http_error() -> None:
    client = OpenSearchRestClient(transport=RecordingTransport([]))
    with pytest.raises(ValueError):
        await client.search_knn("idx", query_vector=(), limit=3)
    with pytest.raises(ValueError):
        await client.search_knn("idx", query_vector=(float("nan"),), limit=3)

    transport = RecordingTransport([httpx.Response(503, text="down")])
    failing = OpenSearchRestClient(transport=transport)
    with pytest.raises(OpenSearchError):
        await failing.search_knn("idx", query_vector=(1.0,), limit=3)


def test_rrf_fuses_disjoint_lists_by_rank() -> None:
    bm25 = (_hit(CHUNK_A, PROVISION_A, 5.0), _hit(CHUNK_B, PROVISION_B, 3.0))
    dense = (_hit(CHUNK_C, PROVISION_C, 2.0),)
    fused = reciprocal_rank_fusion(bm25, dense, k=60)
    assert len(fused) == 3
    by_chunk = {str(hit.chunk_id): hit.score for hit in fused}
    # A ranks 1st in BM25 (1/61); C ranks 1st in dense (1/61); B ranks 2nd (1/62).
    assert by_chunk[str(CHUNK_A)] == pytest.approx(1 / 61)
    assert by_chunk[str(CHUNK_B)] == pytest.approx(1 / 62)
    assert by_chunk[str(CHUNK_C)] == pytest.approx(1 / 61)
    scores = [hit.score for hit in fused]
    assert scores[0] == pytest.approx(1 / 61)  # top group first
    assert scores[-1] == pytest.approx(1 / 62)  # B last
    assert len({hit.chunk_id for hit in fused}) == 3


def test_rrf_accumulates_overlap_and_deduplicates() -> None:
    bm25 = (_hit(CHUNK_A, PROVISION_A, 5.0), _hit(CHUNK_B, PROVISION_B, 3.0))
    dense = (_hit(CHUNK_B, PROVISION_B, 2.0), _hit(CHUNK_A, PROVISION_A, 1.0))
    fused = reciprocal_rank_fusion(bm25, dense, k=10)
    assert len(fused) == 2
    by_chunk = {str(hit.chunk_id): hit.score for hit in fused}
    # A: rank 1 in bm25 (1/11) + rank 2 in dense (1/12); B: the mirror image.
    assert by_chunk[str(CHUNK_A)] == pytest.approx(1 / 11 + 1 / 12)
    assert by_chunk[str(CHUNK_B)] == pytest.approx(1 / 12 + 1 / 11)
    assert len({hit.chunk_id for hit in fused}) == 2


def test_rrf_empty_inputs_and_validation() -> None:
    assert reciprocal_rank_fusion((), ()) == ()
    fused = reciprocal_rank_fusion((_hit(CHUNK_A, PROVISION_A, 1.0),), ())
    assert len(fused) == 1
    with pytest.raises(ValueError):
        reciprocal_rank_fusion((), (), k=0)
    with pytest.raises(ValueError):
        reciprocal_rank_fusion(("bad",), ())  # type: ignore[arg-type]
