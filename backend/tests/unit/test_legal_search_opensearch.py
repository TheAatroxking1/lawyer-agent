from __future__ import annotations

import httpx
import pytest

from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_search import LegalSearchHit
from lawyer_agent.infrastructure.search.opensearch import (
    OpenSearchError,
    OpenSearchRestClient,
    chunk_document,
    parse_search_hit,
)


class RecordingTransport(httpx.AsyncBaseTransport):
    """Capture requests and return canned OpenSearch-like responses."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self._responses:
            return httpx.Response(200, json={"ok": True}, request=request)
        return self._responses.pop(0)


def _client(responses: list[httpx.Response]) -> tuple[OpenSearchRestClient, RecordingTransport]:
    transport = RecordingTransport(responses)
    return OpenSearchRestClient(base_url="http://opensearch:9200", transport=transport), transport


async def test_chunk_document_mapping_carries_only_identifiers_and_text() -> None:
    chunk_id = new_uuid7()
    provision_id = new_uuid7()
    version_id = new_uuid7()
    document = chunk_document(
        chunk_id=chunk_id,
        provision_id=provision_id,
        version_id=version_id,
        content="第一条 内容。",
        parser_version="docx-zip-v1",
    )
    assert document["chunk_id"] == str(chunk_id)
    assert document["version_id"] == str(version_id)
    assert document["content"] == "第一条 内容。"
    assert set(document) == {
        "chunk_id",
        "provision_id",
        "version_id",
        "content",
        "parser_version",
    }


async def test_ensure_index_put_mapping() -> None:
    client, transport = _client([httpx.Response(200, json={"acknowledged": True})])
    await client.ensure_index("legal_corpus_v1")
    request = transport.requests[0]
    assert request.method == "PUT"
    assert request.url.path == "/legal_corpus_v1"
    payload = _json_body(request)
    assert payload["mappings"]["properties"]["content"]["type"] == "text"
    assert payload["mappings"]["properties"]["version_id"]["type"] == "keyword"


async def test_replace_documents_bulk_delete_then_upsert() -> None:
    chunk_id = new_uuid7()
    client, transport = _client(
        [
            httpx.Response(
                200,
                json={"timed_out": False, "version_conflicts": 0, "failures": []},
            ),
            httpx.Response(
                200,
                json={
                    "errors": False,
                    "items": [
                        {"index": {"_id": str(chunk_id), "status": 201}}
                    ],
                },
            ),
        ]
    )
    version_id = new_uuid7()
    doc = chunk_document(
        chunk_id=chunk_id,
        provision_id=new_uuid7(),
        version_id=version_id,
        content="第一条 内容。",
        parser_version="docx-zip-v1",
    )
    await client.replace_documents(
        "legal_corpus_v1", (doc,), parser_version="docx-zip-v1"
    )
    assert [request.url.path for request in transport.requests] == [
        "/legal_corpus_v1/_delete_by_query",
        "/legal_corpus_v1/_bulk",
    ]
    delete_body = _json_body(transport.requests[0])
    assert delete_body["query"]["term"]["parser_version"] == "docx-zip-v1"
    raw = transport.requests[1].content.decode("utf-8")
    assert f'"{doc["chunk_id"]}"' in raw
    assert '"_index": "legal_corpus_v1"' in raw


async def test_replace_documents_empty_batch_validates_delete_without_bulk() -> None:
    client, transport = _client(
        [
            httpx.Response(
                200,
                json={"timed_out": False, "version_conflicts": 0, "failures": []},
            )
        ]
    )
    await client.replace_documents(
        "legal_corpus_v1", (), parser_version="docx-zip-v1"
    )
    assert [request.url.path for request in transport.requests] == [
        "/legal_corpus_v1/_delete_by_query"
    ]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, text="第一条 私密正文"),
        httpx.Response(
            200,
            json={"timed_out": True, "version_conflicts": 0, "failures": []},
        ),
        httpx.Response(
            200,
            json={"timed_out": False, "version_conflicts": 1, "failures": []},
        ),
        httpx.Response(
            200,
            json={
                "timed_out": False,
                "version_conflicts": 0,
                "failures": [{"cause": "第一条 私密正文"}],
            },
        ),
    ],
)
async def test_replace_documents_rejects_delete_failures_without_bulk_or_payload(
    response: httpx.Response,
) -> None:
    client, transport = _client([response])
    document = chunk_document(
        chunk_id=new_uuid7(),
        provision_id=new_uuid7(),
        version_id=new_uuid7(),
        content="第一条 私密正文",
        parser_version="docx-zip-v1",
    )
    with pytest.raises(OpenSearchError) as captured:
        await client.replace_documents(
            "legal_corpus_v1", (document,), parser_version="docx-zip-v1"
        )
    assert "私密正文" not in str(captured.value)
    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    "bulk_body",
    [
        {"errors": True, "items": []},
        {"errors": False, "items": []},
        {"errors": False, "items": "malformed"},
        {"errors": False, "items": [{"index": {"_id": "wrong", "status": 201}}]},
        {"errors": False, "items": [{"index": {"_id": "placeholder", "status": 500}}]},
    ],
)
async def test_replace_documents_rejects_unacknowledged_bulk_items_without_payload(
    bulk_body: dict[str, object],
) -> None:
    chunk_id = new_uuid7()
    item = bulk_body.get("items")
    if (
        isinstance(item, list)
        and item
        and isinstance(item[0], dict)
        and isinstance(item[0].get("index"), dict)
        and item[0]["index"].get("_id") == "placeholder"
    ):
        item[0]["index"]["_id"] = str(chunk_id)
        item[0]["index"]["error"] = {"reason": "第一条 私密正文"}
    client, _ = _client(
        [
            httpx.Response(
                200,
                json={"timed_out": False, "version_conflicts": 0, "failures": []},
            ),
            httpx.Response(200, json=bulk_body),
        ]
    )
    document = chunk_document(
        chunk_id=chunk_id,
        provision_id=new_uuid7(),
        version_id=new_uuid7(),
        content="第一条 私密正文",
        parser_version="docx-zip-v1",
    )
    with pytest.raises(OpenSearchError) as captured:
        await client.replace_documents(
            "legal_corpus_v1", (document,), parser_version="docx-zip-v1"
        )
    assert "私密正文" not in str(captured.value)


async def test_replace_documents_rejects_bulk_http_failure_without_payload() -> None:
    client, _ = _client(
        [
            httpx.Response(
                200,
                json={"timed_out": False, "version_conflicts": 0, "failures": []},
            ),
            httpx.Response(503, text="第一条 私密正文"),
        ]
    )
    document = chunk_document(
        chunk_id=new_uuid7(),
        provision_id=new_uuid7(),
        version_id=new_uuid7(),
        content="第一条 私密正文",
        parser_version="docx-zip-v1",
    )
    with pytest.raises(OpenSearchError) as captured:
        await client.replace_documents(
            "legal_corpus_v1", (document,), parser_version="docx-zip-v1"
        )
    assert "私密正文" not in str(captured.value)


async def test_search_bm25_builds_match_query_and_parses_hits() -> None:
    chunk_id = new_uuid7()
    provision_id = new_uuid7()
    version_id = new_uuid7()
    response_body = {
        "hits": {
            "hits": [
                {
                    "_score": 1.7,
                    "_source": {
                        "chunk_id": str(chunk_id),
                        "provision_id": str(provision_id),
                        "version_id": str(version_id),
                    },
                }
            ]
        }
    }
    client, transport = _client([httpx.Response(200, json=response_body)])
    hits = await client.search_bm25(
        "legal_corpus_v1", query="违约金", limit=5, version_id=version_id
    )
    search_body = _json_body(transport.requests[0])
    assert search_body["query"]["bool"]["must"][0]["match"]["content"] == "违约金"
    assert search_body["query"]["bool"]["filter"][0]["term"]["version_id"] == str(version_id)
    assert search_body["size"] == 5
    assert hits == (
        LegalSearchHit(
            chunk_id=chunk_id,
            provision_id=provision_id,
            version_id=version_id,
            score=1.7,
        ),
    )


async def test_search_errors_map_to_stable_exception() -> None:
    client, _ = _client([httpx.Response(503, text="cluster busy")])
    with pytest.raises(OpenSearchError):
        await client.search_bm25("legal_corpus_v1", query="x", limit=5)


def test_parse_search_hit_rejects_malformed_source() -> None:
    with pytest.raises(OpenSearchError):
        parse_search_hit({"_score": 1.0, "_source": {"content": "no ids"}})


def _json_body(request: httpx.Request) -> dict[str, object]:
    import json as _json

    return _json.loads(request.content.decode("utf-8"))


async def test_bm25_and_knn_share_multi_version_terms_scope() -> None:
    versions = (new_uuid7(), new_uuid7())
    client, transport = _client([httpx.Response(200, json={"hits": {"hits": []}})] * 2)
    await client.search_bm25("legal_corpus_v1", query="范围", limit=5, version_ids=versions)
    await client.search_knn("legal_corpus_v1", query_vector=(1.0, 0.0), limit=5,
                            version_ids=versions)
    bm25 = _json_body(transport.requests[0])
    knn = _json_body(transport.requests[1])
    expected = {"terms": {"version_id": [str(value) for value in versions]}}
    assert bm25["query"]["bool"]["filter"] == [expected]
    assert knn["query"]["knn"]["content_vector"]["filter"] == expected


@pytest.mark.parametrize("scope", ["empty", "duplicate", "list", "invalid", "conflict"])
async def test_invalid_multi_version_scope_rejected_before_request(scope: str) -> None:
    version = new_uuid7()
    values = {"empty": (), "duplicate": (version, version), "list": [version],
              "invalid": ("invalid",), "conflict": (version,)}[scope]
    client, transport = _client([])
    for method, arguments in (
        (client.search_bm25, {"query": "范围"}),
        (client.search_knn, {"query_vector": (1.0, 0.0)}),
    ):
        with pytest.raises(ValueError):
            await method("legal_corpus_v1", limit=5, version_ids=values,
                         version_id=version if scope == "conflict" else None, **arguments)
    assert transport.requests == []


@pytest.mark.parametrize("filtered", [False, True])
async def test_legacy_bm25_and_knn_query_json_unchanged(filtered: bool) -> None:
    version = new_uuid7() if filtered else None
    client, transport = _client([httpx.Response(200, json={"hits": {"hits": []}})] * 2)
    await client.search_bm25("legal_corpus_v1", query="范围", limit=5, version_id=version)
    await client.search_knn("legal_corpus_v1", query_vector=(1.0, 0.0), limit=5,
                            version_id=version)
    expected_bool = {"must": [{"match": {"content": "范围"}}]}
    expected_knn = {"vector": [1.0, 0.0], "k": 5}
    if version is not None:
        expected_bool["filter"] = [{"term": {"version_id": str(version)}}]
        expected_knn["filter"] = {"term": {"version_id": str(version)}}
    assert _json_body(transport.requests[0]) == {"query": {"bool": expected_bool}, "size": 5}
    assert _json_body(transport.requests[1]) == {
        "query": {"knn": {"content_vector": expected_knn}}, "size": 5}
