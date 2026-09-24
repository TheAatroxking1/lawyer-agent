from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import httpx
import pytest

from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_navigation import NavigationDocument, NavigationDocumentKind
from lawyer_agent.infrastructure.search.opensearch import OpenSearchError

INDEX = "lawyer-nav-" + "a" * 32
MAIN = "lawyer_main_test"
DELETE = {"timed_out": False, "version_conflicts": 0, "failures": []}


def document() -> NavigationDocument:
    return NavigationDocument(
        "a" * 64,
        new_uuid7(),
        NavigationDocumentKind.INSTRUMENT,
        "合成合同法",
        "合成合同法",
        "nav-v1",
    )


def client_for(*bodies: Any):
    from lawyer_agent.infrastructure.search import legal_navigation

    requests: list[httpx.Request] = []
    pending = list(bodies)

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = pending.pop(0)
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, json=body)

    return legal_navigation.OpenSearchNavigationClient(
        base_url="http://navigation:9200", transport=httpx.MockTransport(handle)
    ), requests


async def test_append_never_deletes_and_checks_each_receipt():
    doc = document()
    client, requests = client_for(
        {"errors": False, "items": [{"index": {"_id": doc.navigation_id, "status": 201}}]},
    )
    await client.append_navigation_documents(INDEX, (doc,), parser_version="nav-v1")
    assert len(requests) == 1
    assert requests[0].url.path == f"/{INDEX}/_bulk"
    client, _ = client_for({"errors": False, "items": []})
    with pytest.raises(OpenSearchError):
        await client.append_navigation_documents(INDEX, (doc,), parser_version="nav-v1")


async def test_append_splits_bulk_under_eight_mib_and_preserves_all_ids():
    from lawyer_agent.infrastructure.search.legal_navigation import OpenSearchNavigationClient
    seen = []
    def handle(request):
        assert request.url.path == f"/{INDEX}/_bulk"
        assert len(request.content) <= 8 * 1024 * 1024
        lines = request.content.splitlines()
        ids = [json.loads(line)["index"]["_id"] for line in lines[::2]]
        seen.append(ids)
        return httpx.Response(200, json={"errors": False, "items": [
            {"index": {"_id": identifier, "status": 201}} for identifier in ids]})
    client = OpenSearchNavigationClient(transport=httpx.MockTransport(handle))
    docs = tuple(replace(document(), navigation_id=f"{number:064x}",
                         content="x" * (3 * 1024 * 1024)) for number in range(3))
    await client.append_navigation_documents(INDEX, docs, parser_version="nav-v1")
    assert len(seen) == 2
    assert [value for batch in seen for value in batch] == [doc.navigation_id for doc in docs]


async def test_append_oversized_single_document_rejected_before_any_http():
    client, requests = client_for()
    oversized = replace(document(), navigation_id="b" * 64, content="x" * (8 * 1024 * 1024))
    with pytest.raises((ValueError, OpenSearchError)):
        await client.append_navigation_documents(INDEX, (document(), oversized),
                                                 parser_version="nav-v1")
    assert requests == []


async def test_mapping_and_bulk_are_typed_utf8_and_acknowledged() -> None:
    doc = document()
    client, requests = client_for(
        {"acknowledged": True},
        DELETE,
        {"errors": False, "items": [{"index": {"_id": doc.navigation_id, "status": 201}}]},
    )
    await client.ensure_navigation_index(INDEX)
    await client.replace_navigation_documents(INDEX, (doc,), parser_version="nav-v1")
    mapping = json.loads(requests[0].content)["mappings"]
    assert mapping["dynamic"] == "strict"
    assert mapping["properties"] == {
        "navigation_id": {"type": "keyword"},
        "version_id": {"type": "keyword"},
        "kind": {"type": "keyword"},
        "locator": {"type": "keyword"},
        "content": {"type": "text"},
        "parser_version": {"type": "keyword"},
    }
    assert requests[0].url.host == "navigation"
    lines = requests[2].content.decode("utf-8").splitlines()
    assert json.loads(lines[0]) == {"index": {"_index": INDEX, "_id": doc.navigation_id}}
    assert json.loads(lines[1]) == {
        "navigation_id": doc.navigation_id,
        "version_id": str(doc.version_id),
        "kind": "instrument",
        "locator": doc.locator,
        "content": doc.content,
        "parser_version": "nav-v1",
    }
    assert json.loads(requests[1].content) == {"query": {"term": {"parser_version": "nav-v1"}}}


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"errors": True, "items": []},
        {"errors": False, "items": []},
        {"errors": False, "items": [{"index": {"_id": "wrong", "status": 201}}]},
        {
            "errors": False,
            "items": [{"index": {"_id": "a" * 64, "status": 500, "error": "私密正文"}}],
        },
    ],
)
async def test_bulk_rejects_incomplete_item_receipts(body: Any) -> None:
    client, _ = client_for(DELETE, body)
    with pytest.raises(OpenSearchError) as exc:
        await client.replace_navigation_documents(INDEX, (document(),), parser_version="nav-v1")
    assert "私密正文" not in str(exc.value)


async def test_empty_replace_checks_delete_receipt() -> None:
    client, requests = client_for(DELETE)
    await client.replace_navigation_documents(INDEX, (), parser_version="nav-v1")
    assert len(requests) == 1
    client, _ = client_for({"timed_out": True, "failures": [], "version_conflicts": 0})
    with pytest.raises(OpenSearchError):
        await client.replace_navigation_documents(INDEX, (), parser_version="nav-v1")


@pytest.mark.parametrize(
    "documents, parser",
    [
        (({"content": "私密正文"},), "nav-v1"),
        ((), " "),
        ((document(),), "wrong"),
    ],
)
async def test_invalid_batch_rejected_before_delete(documents: Any, parser: str) -> None:
    client, requests = client_for()
    with pytest.raises(ValueError):
        await client.replace_navigation_documents(INDEX, documents, parser_version=parser)
    assert requests == []


async def test_duplicate_ids_rejected_before_delete() -> None:
    doc = document()
    client, requests = client_for()
    with pytest.raises(ValueError):
        await client.replace_navigation_documents(INDEX, (doc, doc), parser_version="nav-v1")
    assert requests == []


@pytest.mark.parametrize("body", [{}, {"count": True}, {"count": -1}, {"count": "1"}, []])
async def test_count_rejects_malformed_response(body: Any) -> None:
    client, _ = client_for(body)
    with pytest.raises(OpenSearchError):
        await client.count_documents(INDEX)


async def test_count_returns_integer() -> None:
    client, requests = client_for({"count": 2, "_shards": {
        "total": 1, "successful": 1, "failed": 0,
    }})
    assert await client.count_documents(INDEX) == 2
    assert requests[0].url.path == f"/{INDEX}/_count"


@pytest.mark.parametrize("extra", [{}, {"_shards": {"failed": 0}},
    {"_shards": {"total": 2, "successful": 1, "failed": 0}}])
async def test_navigation_count_requires_all_shards_acknowledged(extra):
    client, _ = client_for({"count": 2, **extra})
    with pytest.raises(OpenSearchError):
        await client.count_documents(INDEX)


async def test_marker_preserves_other_mapping_meta() -> None:
    client, requests = client_for(
        {MAIN: {"mappings": {"_meta": {"existing": {"dimension": 2}}}}},
        {"acknowledged": True},
        {MAIN: {"mappings": {"_meta": {"legal_navigation_schema": 1}}}},
    )
    await client.mark_navigation_ready(MAIN)
    assert json.loads(requests[1].content) == {
        "_meta": {"existing": {"dimension": 2}, "legal_navigation_schema": 1}
    }
    assert await client.navigation_schema(MAIN) == 1


async def test_absent_marker_is_legacy() -> None:
    client, _ = client_for({MAIN: {"mappings": {"properties": {}}}})
    assert await client.navigation_schema(MAIN) is None


@pytest.mark.parametrize("schema", [None, True, "1", 2, 0, -1])
async def test_unknown_schema_is_not_legacy_and_cannot_be_overwritten(schema: Any) -> None:
    mapping = {MAIN: {"mappings": {"_meta": {"legal_navigation_schema": schema}}}}
    for operation in ("navigation_schema", "mark_navigation_ready"):
        client, requests = client_for(mapping)
        with pytest.raises(OpenSearchError):
            await getattr(client, operation)(MAIN)
        assert len(requests) == 1


@pytest.mark.parametrize(
    "body", [{}, [], {"other": {"mappings": {}}}, {MAIN: {"mappings": {"_meta": []}}}]
)
async def test_malformed_mapping_rejected(body: Any) -> None:
    client, _ = client_for(body)
    with pytest.raises(OpenSearchError):
        await client.navigation_schema(MAIN)


async def test_search_parses_only_typed_hits_in_rank_order() -> None:
    version = new_uuid7()
    client, requests = client_for(
        {
            "hits": {
                "hits": [
                    {
                        "_score": 3.5,
                        "_source": {"version_id": str(version), "locator": "合成合同法"},
                    }
                ]
            }
        }
    )
    hits = await client.search_navigation(INDEX, query="合成合同法", limit=50)
    assert hits[0].version_id == version
    assert hits[0].locator == "合成合同法"
    assert hits[0].score == 3.5
    assert json.loads(requests[0].content)["query"] == {"match": {"content": "合成合同法"}}


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"hits": {"hits": "bad"}},
        {"hits": {"hits": [None]}},
        {"hits": {"hits": [{"_score": 1, "_source": {"version_id": "私密正文"}}]}},
        {
            "hits": {
                "hits": [
                    {
                        "_score": "NaN",
                        "_source": {"version_id": str(new_uuid7()), "locator": "标题"},
                    }
                ]
            }
        },
        {"hits": {"hits": [{"_source": {"version_id": str(new_uuid7()), "locator": "标题"}}]}},
    ],
)
async def test_malformed_search_hits_fail_without_body(body: Any) -> None:
    client, _ = client_for(body)
    with pytest.raises(OpenSearchError) as exc:
        await client.search_navigation(INDEX, query="标题", limit=5)
    assert "私密正文" not in str(exc.value)


@pytest.mark.parametrize(
    "operation",
    ["ensure_navigation_index", "count_documents", "navigation_schema", "mark_navigation_ready"],
)
async def test_http_errors_do_not_expose_body(operation: str) -> None:
    client, _ = client_for(httpx.Response(503, text="私密正文"))
    with pytest.raises(OpenSearchError) as exc:
        index = MAIN if "schema" in operation or "ready" in operation else INDEX
        await getattr(client, operation)(index)
    assert "私密正文" not in str(exc.value)


@pytest.mark.parametrize("extra", [{"timed_out": True}, {"_shards": {"failed": 1}}])
async def test_partial_search_fails_instead_of_narrowing_candidates(extra: Any) -> None:
    client, _ = client_for({"hits": {"hits": []}, **extra})
    with pytest.raises(OpenSearchError):
        await client.search_navigation(INDEX, query="标题", limit=5)


async def test_failed_count_shard_is_not_a_complete_count() -> None:
    client, _ = client_for({"count": 2, "_shards": {"failed": 1}})
    with pytest.raises(OpenSearchError):
        await client.count_documents(INDEX)


async def test_network_error_is_wrapped_without_payload() -> None:
    from lawyer_agent.infrastructure.search.legal_navigation import OpenSearchNavigationClient

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("私密正文", request=request)

    client = OpenSearchNavigationClient(transport=httpx.MockTransport(fail))
    with pytest.raises(OpenSearchError) as exc:
        await client.count_documents(INDEX)
    assert "私密正文" not in str(exc.value)


@pytest.mark.parametrize("ack", [
    {}, {"acknowledged": False}, {"acknowledged": 1},
    {"acknowledged": True, "error": "private payload"},
    {"acknowledged": True, "error": None},
    {"acknowledged": True, "errors": True},
    {"acknowledged": True, "errors": 0},
])
@pytest.mark.parametrize("operation", ["index", "marker"])
async def test_index_and_marker_need_explicit_acknowledgement(ack: Any, operation: str) -> None:
    client, _ = client_for(ack) if operation == "index" else client_for(
        {MAIN: {"mappings": {}}}, ack,
    )
    with pytest.raises(OpenSearchError) as exc:
        if operation == "index":
            await client.ensure_navigation_index(INDEX)
        else:
            await client.mark_navigation_ready(MAIN)
    assert "private payload" not in str(exc.value)


@pytest.mark.parametrize("index", ["_all", "lawyer-nav-*", "../other", MAIN])
async def test_sidecar_name_validation_before_network(index: str) -> None:
    client, requests = client_for()
    for operation in (client.ensure_navigation_index, client.count_documents):
        with pytest.raises(ValueError):
            await operation(index)
    with pytest.raises(ValueError):
        await client.replace_navigation_documents(index, (), parser_version="nav-v1")
    with pytest.raises(ValueError):
        await client.search_navigation(index, query="标题", limit=5)
    assert requests == []


@pytest.mark.parametrize("index", ["_all", "main*", "../other"])
async def test_main_mapping_name_validation_before_network(index: str) -> None:
    client, requests = client_for()
    for operation in (client.mark_navigation_ready, client.navigation_schema):
        with pytest.raises(ValueError):
            await operation(index)
    assert requests == []


@pytest.mark.parametrize("query, limit", [(" ", 5), ("标题", 0), ("标题", True)])
async def test_search_input_validation_before_network(query: str, limit: Any) -> None:
    client, requests = client_for()
    with pytest.raises(ValueError):
        await client.search_navigation(INDEX, query=query, limit=limit)
    assert requests == []
