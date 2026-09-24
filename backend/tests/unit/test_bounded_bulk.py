import json

import httpx
import pytest

from lawyer_agent.infrastructure.search import bounded_bulk
from lawyer_agent.infrastructure.search.opensearch import OpenSearchError, OpenSearchRestClient


def document(identifier="one", content="合成正文"):
    return {"chunk_id": identifier, "content": content, "parser_version": "parser-v1"}


def bulk_ack(request):
    lines = request.content.splitlines()
    ids = [json.loads(line)["index"]["_id"] for line in lines[::2]]
    return httpx.Response(200, json={
        "errors": False, "items": [{"index": {"_id": key, "status": 201}} for key in ids],
    })


def test_helper_splits_utf8_bytes_at_document_boundaries(monkeypatch):
    docs = (document("one"), document("two"))
    single = next(bounded_bulk.bounded_bulk_payloads("index", docs[:1], id_field="chunk_id"))
    monkeypatch.setattr(bounded_bulk, "MAX_BULK_BYTES", len(single.content))
    payloads = list(bounded_bulk.bounded_bulk_payloads("index", docs, id_field="chunk_id"))
    assert [item.document_ids for item in payloads] == [("one",), ("two",)]
    assert all(len(item.content) <= len(single.content) for item in payloads)
    assert all(len(item.content.splitlines()) == 2 for item in payloads)
    assert json.loads(payloads[1].content.splitlines()[1])["content"] == "合成正文"


def test_helper_validates_tail_before_returning_first_payload(monkeypatch):
    monkeypatch.setattr(bounded_bulk, "MAX_BULK_BYTES", 250)
    with pytest.raises(bounded_bulk.BoundedBulkError):
        bounded_bulk.bounded_bulk_payloads(
            "index", (document(), document("tail", "x" * 251)), id_field="chunk_id",
        )


def test_actual_eight_mib_limit_includes_action_source_and_newlines():
    empty = next(bounded_bulk.bounded_bulk_payloads(
        "index", (document(content=""),), id_field="chunk_id",
    ))
    content_length = 8 * 1024 * 1024 - len(empty.content)
    exact = next(bounded_bulk.bounded_bulk_payloads(
        "index", (document(content="x" * content_length),), id_field="chunk_id",
    ))
    assert len(exact.content) == 8 * 1024 * 1024
    with pytest.raises(bounded_bulk.BoundedBulkError):
        bounded_bulk.bounded_bulk_payloads(
            "index", (document(content="x" * (content_length + 1)),), id_field="chunk_id",
        )


@pytest.mark.parametrize("docs", [
    [document()], (document(), document()), ({"content": "missing ID"},),
    (document(content=float("nan")),), tuple(document(str(i)) for i in range(257)),
])
def test_invalid_batch_rejected_before_any_payload(docs):
    with pytest.raises(bounded_bulk.BoundedBulkError):
        bounded_bulk.bounded_bulk_payloads("index", docs, id_field="chunk_id")


async def test_append_splits_and_never_deletes_earlier_batches(monkeypatch):
    monkeypatch.setattr(bounded_bulk, "MAX_BULK_BYTES", 200)
    requests = []

    def handle(request):
        requests.append(request)
        return bulk_ack(request)

    client = OpenSearchRestClient(transport=httpx.MockTransport(handle))
    await client.append_documents("index", (document("one"), document("two")),
                                  parser_version="parser-v1")
    await client.append_documents("index", (document("last"),), parser_version="parser-v1")
    assert len(requests) == 3
    assert all(request.url.path == "/index/_bulk" for request in requests)
    assert all(request.url.params["refresh"] == "true" for request in requests)
    assert all(len(request.content) <= 200 for request in requests)


async def test_append_invalid_tail_never_sends_request(monkeypatch):
    monkeypatch.setattr(bounded_bulk, "MAX_BULK_BYTES", 250)
    requests = []
    client = OpenSearchRestClient(transport=httpx.MockTransport(lambda req: requests.append(req)))
    with pytest.raises(OpenSearchError):
        await client.append_documents("index", (document(), document("tail", "x" * 251)),
                                      parser_version="parser-v1")
    assert not requests


async def test_empty_append_is_a_noop():
    requests = []
    client = OpenSearchRestClient(transport=httpx.MockTransport(lambda req: requests.append(req)))
    await client.append_documents("index", (), parser_version="parser-v1")
    assert not requests


async def test_append_rejects_parser_mismatch_before_request():
    requests = []
    client = OpenSearchRestClient(transport=httpx.MockTransport(lambda req: requests.append(req)))
    with pytest.raises(OpenSearchError):
        await client.append_documents("index", (document(),), parser_version="other")
    assert not requests


async def test_append_stops_after_first_unacknowledged_split(monkeypatch):
    monkeypatch.setattr(bounded_bulk, "MAX_BULK_BYTES", 200)
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"errors": True, "items": []})

    client = OpenSearchRestClient(transport=httpx.MockTransport(handle))
    with pytest.raises(OpenSearchError):
        await client.append_documents("index", (document("one"), document("two")),
                                      parser_version="parser-v1")
    assert len(requests) == 1


@pytest.mark.parametrize("body", [
    {}, {"acknowledged": False}, {"acknowledged": 1},
    {"acknowledged": True, "error": "SECRET"},
    {"acknowledged": True, "errors": True},
])
async def test_index_creation_requires_explicit_success(body):
    client = OpenSearchRestClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=body),
    ))
    with pytest.raises(OpenSearchError) as captured:
        await client.ensure_index("new-index")
    assert "SECRET" not in str(captured.value)


async def test_existing_index_is_rejected_without_reusing_it():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(400, json={"error": "resource_already_exists_exception SECRET"})

    with pytest.raises(OpenSearchError) as captured:
        await OpenSearchRestClient(transport=httpx.MockTransport(handle)).ensure_index("index")
    assert "SECRET" not in str(captured.value)
    assert len(requests) == 1 and requests[0].method == "PUT"


@pytest.mark.parametrize("body", [
    {"count": True}, {"count": -1}, {"count": 1.5}, {"count": 1},
    {"count": 1, "_shards": {"total": 1, "successful": 0, "failed": 1}},
    {"count": 1, "_shards": {"total": 2, "successful": 1, "failed": 0}},
    {"count": 1, "_shards": {"total": 1, "successful": 1, "failed": False}},
])
async def test_count_rejects_invalid_or_partial_responses(body):
    client = OpenSearchRestClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=body),
    ))
    with pytest.raises(OpenSearchError):
        await client.count_documents("index")


@pytest.mark.parametrize("count", [0, 42])
async def test_count_accepts_only_complete_nonnegative_integer(count):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={
            "count": count, "_shards": {"total": 2, "successful": 2, "failed": 0},
        })

    assert await OpenSearchRestClient(transport=httpx.MockTransport(handle)).count_documents(
        "index",
    ) == count
    assert len(requests) == 1 and requests[0].url.path == "/index/_count"


async def test_count_http_failure_does_not_expose_provider_body():
    client = OpenSearchRestClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(503, text="SECRET provider error"),
    ))
    with pytest.raises(OpenSearchError) as captured:
        await client.count_documents("index")
    assert "SECRET" not in str(captured.value)
