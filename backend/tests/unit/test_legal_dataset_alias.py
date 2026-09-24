from __future__ import annotations

import httpx
import pytest

from lawyer_agent.application.legal_index_alias import (
    LegalDatasetAliasService,
    LegalIndexAliasError,
)
from lawyer_agent.infrastructure.search.opensearch import (
    OpenSearchError,
    OpenSearchRestClient,
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


def _json(request: httpx.Request) -> dict[str, object]:
    import json

    return json.loads(request.content.decode("utf-8"))


def _client(responses: list[httpx.Response]) -> tuple[OpenSearchRestClient, RecordingTransport]:
    transport = RecordingTransport(responses)
    return OpenSearchRestClient(base_url="http://os:9200", transport=transport), transport


async def test_resolve_alias_returns_none_on_missing() -> None:
    client, transport = _client([httpx.Response(404, json={})])
    assert await client.resolve_alias("dataset_v1") is None
    assert transport.requests[0].method == "GET"
    assert transport.requests[0].url.path == "/_alias/dataset_v1"


async def test_resolve_alias_parses_single_target() -> None:
    client, transport = _client(
        [
            httpx.Response(
                200,
                json={"legal_idx_2024": {"aliases": {"dataset_v1": {}}}},
            )
        ]
    )
    assert await client.resolve_alias("dataset_v1") == "legal_idx_2024"


async def test_resolve_alias_rejects_multiple_targets() -> None:
    client, _ = _client(
        [
            httpx.Response(
                200,
                json={
                    "legal_idx_a": {"aliases": {"dataset_v1": {}}},
                    "legal_idx_b": {"aliases": {"dataset_v1": {}}},
                },
            )
        ]
    )
    with pytest.raises(OpenSearchError, match="multiple"):
        await client.resolve_alias("dataset_v1")


async def test_point_alias_switches_atomically_and_returns_previous() -> None:
    client, transport = _client(
        [
            httpx.Response(
                200,
                json={"legal_idx_2020": {"aliases": {"dataset_v1": {}}}},
            ),
            httpx.Response(200, json={"acknowledged": True}),
        ]
    )
    previous = await client.point_alias("dataset_v1", "legal_idx_2024")
    assert previous == "legal_idx_2020"
    switch = transport.requests[1]
    assert switch.method == "POST"
    assert switch.url.path == "/_aliases"
    body = _json(switch)
    actions = body["actions"]
    assert actions == [
        {"remove": {"index": "legal_idx_2020", "alias": "dataset_v1"}},
        {"add": {"index": "legal_idx_2024", "alias": "dataset_v1"}},
    ]


async def test_point_alias_is_noop_when_already_pointing() -> None:
    client, transport = _client(
        [
            httpx.Response(
                200,
                json={"legal_idx_2024": {"aliases": {"dataset_v1": {}}}},
            )
        ]
    )
    previous = await client.point_alias("dataset_v1", "legal_idx_2024")
    assert previous == "legal_idx_2024"
    assert len(transport.requests) == 1  # resolve only, no switch call


async def test_point_alias_first_time_adds_without_remove() -> None:
    client, transport = _client(
        [
            httpx.Response(404, json={}),
            httpx.Response(200, json={"acknowledged": True}),
        ]
    )
    previous = await client.point_alias("dataset_v1", "legal_idx_2024")
    assert previous is None
    body = _json(transport.requests[1])
    assert body["actions"] == [
        {"add": {"index": "legal_idx_2024", "alias": "dataset_v1"}}
    ]


async def test_drop_alias_is_idempotent() -> None:
    client, transport = _client(
        [
            httpx.Response(
                200,
                json={"legal_idx_2024": {"aliases": {"dataset_v1": {}}}},
            ),
            httpx.Response(200, json={"acknowledged": True}),
        ]
    )
    await client.drop_alias("dataset_v1")
    drop = transport.requests[1]
    assert drop.method == "DELETE"
    assert drop.url.path == "/legal_idx_2024/_alias/dataset_v1"


async def test_drop_alias_absent_is_noop() -> None:
    client, transport = _client([httpx.Response(404, json={})])
    await client.drop_alias("dataset_v1")
    assert len(transport.requests) == 1


async def test_service_validates_names() -> None:
    client, _ = _client([])
    service = LegalDatasetAliasService(client)
    with pytest.raises(LegalIndexAliasError, match="alias"):
        await service.publish_dataset("Bad Alias", "legal_idx")
    with pytest.raises(LegalIndexAliasError, match="index"):
        await service.publish_dataset("dataset_v1", "Legal_Idx!")


async def test_service_refuses_missing_target_index() -> None:
    client, _ = _client([httpx.Response(404, json={})])
    service = LegalDatasetAliasService(client)
    with pytest.raises(LegalIndexAliasError, match="does not exist"):
        await service.publish_dataset("dataset_v1", "legal_idx_2024")


async def test_service_publishes_dataset_alias() -> None:
    client, _ = _client(
        [
            httpx.Response(200, json={}),  # index exists HEAD
            httpx.Response(404, json={}),  # no current alias
            httpx.Response(200, json={"acknowledged": True}),
        ]
    )
    service = LegalDatasetAliasService(client)
    previous = await service.publish_dataset("dataset_v1", "legal_idx_2024")
    assert previous is None


async def test_service_resolves_active_index() -> None:
    client, _ = _client(
        [
            httpx.Response(
                200,
                json={"legal_idx_2024": {"aliases": {"dataset_v1": {}}}},
            )
        ]
    )
    service = LegalDatasetAliasService(client)
    assert await service.active_dataset_index("dataset_v1") == "legal_idx_2024"


@pytest.mark.parametrize("body", [
    {}, {"acknowledged": False}, {"acknowledged": 1}, {"acknowledged": "true"},
    {"acknowledged": True, "errors": True}, {"acknowledged": True, "errors": 0},
    {"acknowledged": True, "error": "private response body"}, [], None,
])
async def test_alias_switch_requires_explicit_complete_acknowledgement(body: object) -> None:
    client, transport = _client([
        httpx.Response(404), httpx.Response(200, json=body),
    ])
    with pytest.raises(OpenSearchError, match="alias") as caught:
        await client.point_alias("dataset_v1", "legal_idx_2024")
    assert "private response body" not in str(caught.value)
    assert len(transport.requests) == 2


@pytest.mark.parametrize("body", [
    {}, [], None, {"legal_idx_2024": {}},
    {"legal_idx_2024": {"aliases": {"another_alias": {}}}},
    {"legal_idx_2024": {"aliases": {"dataset_v1": None}}},
])
async def test_alias_lookup_rejects_malformed_success_instead_of_assuming_absence(
    body: object,
) -> None:
    client, transport = _client([httpx.Response(200, json=body)])
    with pytest.raises(OpenSearchError, match="alias"):
        await client.point_alias("dataset_v1", "legal_idx_2024")
    assert len(transport.requests) == 1


async def test_alias_switch_http_failure_does_not_echo_response_body() -> None:
    client, _ = _client([
        httpx.Response(404), httpx.Response(503, text="private response body"),
    ])
    with pytest.raises(OpenSearchError) as caught:
        await client.point_alias("dataset_v1", "legal_idx_2024")
    assert "private response body" not in str(caught.value)


async def test_alias_lookup_http_failure_does_not_echo_response_body() -> None:
    client, _ = _client([httpx.Response(503, text="private response body")])
    with pytest.raises(OpenSearchError) as caught:
        await client.resolve_alias("dataset_v1")
    assert "private response body" not in str(caught.value)
