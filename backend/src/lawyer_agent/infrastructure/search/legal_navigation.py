"""Strict REST boundary for the source-derived legal navigation sidecar."""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID

import httpx

from lawyer_agent.domain.legal_navigation import (
    NavigationDocument,
    NavigationSearchHit,
    navigation_index_name,
)
from lawyer_agent.infrastructure.search.opensearch import (
    DEFAULT_OPENSEARCH_URL,
    OpenSearchError,
    OpenSearchRestClient,
    OpenSearchUnavailable,
    _validate_bulk_acknowledgement,
    _validate_delete_acknowledgement,
)

_MARKER = "legal_navigation_schema"
_SIDECAR = re.compile(r"lawyer-nav-[a-f0-9]{32}")


def _validate_sidecar(index_name: str) -> None:
    if not isinstance(index_name, str) or not _SIDECAR.fullmatch(index_name):
        raise ValueError("navigation index name is invalid")


def _object(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        raise OpenSearchError("OpenSearch navigation response is malformed") from None
    if not isinstance(body, dict):
        raise OpenSearchError("OpenSearch navigation response is malformed")
    return body


def _complete_read(body: dict[str, Any]) -> None:
    if "timed_out" in body and body["timed_out"] is not False:
        raise OpenSearchError("OpenSearch navigation read timed out")
    if "_shards" in body:
        shards = body["_shards"]
        if (
            not isinstance(shards, dict)
            or type(shards.get("failed")) is not int
            or shards["failed"] != 0
        ):
            raise OpenSearchError("OpenSearch navigation read was incomplete")


class OpenSearchNavigationClient:
    def __init__(
        self,
        *,
        base_url: str = DEFAULT_OPENSEARCH_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._rest = OpenSearchRestClient(base_url=base_url, transport=transport)

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            async with self._rest._client() as client:
                response = await client.request(method, path, **kwargs)
        except httpx.HTTPError:
            raise OpenSearchUnavailable("OpenSearch navigation request unavailable") from None
        if response.status_code not in (200, 201):
            raise OpenSearchError(
                f"OpenSearch navigation request failed with HTTP {response.status_code}"
            )
        return response

    async def ensure_navigation_index(self, index_name: str) -> None:
        _validate_sidecar(index_name)
        response = await self._request(
            "PUT",
            f"/{index_name}",
            json={
                "settings": {"number_of_shards": 1, "number_of_replicas": 0},
                "mappings": {
                    "dynamic": "strict",
                    "properties": {
                        "navigation_id": {"type": "keyword"},
                        "version_id": {"type": "keyword"},
                        "kind": {"type": "keyword"},
                        "locator": {"type": "keyword"},
                        "content": {"type": "text"},
                        "parser_version": {"type": "keyword"},
                    },
                },
            },
        )
        acknowledgement = _object(response)
        if (acknowledgement.get("acknowledged") is not True
            or acknowledgement.get("errors", False) is not False or "error" in acknowledgement):
            raise OpenSearchError("OpenSearch navigation index was not acknowledged")

    async def replace_navigation_documents(
        self,
        index_name: str,
        documents: tuple[NavigationDocument, ...],
        *,
        parser_version: str,
    ) -> None:
        _validate_sidecar(index_name)
        if not isinstance(parser_version, str) or not parser_version.strip():
            raise ValueError("navigation parser_version is invalid")
        if not isinstance(documents, tuple):
            raise ValueError("navigation documents must be a tuple")
        ids: list[str] = []
        lines: list[str] = []
        for document in documents:
            if not isinstance(document, NavigationDocument):
                raise ValueError("navigation document must be strongly typed")
            if document.parser_version != parser_version:
                raise ValueError("navigation parser_version mismatch")
            if document.navigation_id in ids:
                raise ValueError("navigation document ids must be unique")
            ids.append(document.navigation_id)
            lines.append(
                json.dumps(
                    {
                        "index": {"_index": index_name, "_id": document.navigation_id},
                    }
                )
            )
            lines.append(
                json.dumps(
                    {
                        "navigation_id": document.navigation_id,
                        "version_id": str(document.version_id),
                        "kind": document.kind.value,
                        "locator": document.locator,
                        "content": document.content,
                        "parser_version": document.parser_version,
                    },
                    ensure_ascii=False,
                )
            )
        response = await self._request(
            "POST",
            f"/{index_name}/_delete_by_query?refresh=true",
            json={"query": {"term": {"parser_version": parser_version}}},
        )
        _validate_delete_acknowledgement(response)
        if not documents:
            return
        response = await self._request(
            "POST",
            f"/{index_name}/_bulk?refresh=true",
            content=("\n".join(lines) + "\n").encode("utf-8"),
            headers={"Content-Type": "application/x-ndjson"},
        )
        _validate_bulk_acknowledgement(response, tuple(ids))

    async def count_documents(self, index_name: str) -> int:
        _validate_sidecar(index_name)
        try:
            return await self._rest.count_documents(index_name)
        except httpx.HTTPError:
            raise OpenSearchUnavailable("OpenSearch navigation request unavailable") from None

    async def append_navigation_documents(
        self, index_name: str, documents: tuple[NavigationDocument, ...], *, parser_version: str,
    ) -> None:
        """Append a bounded batch to a fresh sidecar without deleting earlier batches."""
        from lawyer_agent.infrastructure.search.bounded_bulk import (
            MAX_BULK_DOCUMENTS,
            BoundedBulkError,
            bounded_bulk_payloads,
        )

        _validate_sidecar(index_name)
        if not isinstance(parser_version, str) or not parser_version.strip():
            raise ValueError("navigation parser_version is invalid")
        if not isinstance(documents, tuple) or len(documents) > MAX_BULK_DOCUMENTS:
            raise ValueError("navigation documents must be a bounded tuple")
        for document in documents:
            if not isinstance(document, NavigationDocument):
                raise ValueError("navigation document must be strongly typed")
            if document.parser_version != parser_version:
                raise ValueError("navigation parser_version mismatch")
        mapped = tuple({
            "navigation_id": document.navigation_id, "version_id": str(document.version_id),
            "kind": document.kind.value, "locator": document.locator,
            "content": document.content, "parser_version": document.parser_version,
        } for document in documents)
        try:
            payloads = bounded_bulk_payloads(index_name, mapped, id_field="navigation_id")
            for payload in payloads:
                response = await self._request(
                    "POST", f"/{index_name}/_bulk?refresh=true", content=payload.content,
                    headers={"Content-Type": "application/x-ndjson"},
                )
                _validate_bulk_acknowledgement(response, payload.document_ids)
        except BoundedBulkError:
            raise OpenSearchError("OpenSearch navigation batch is invalid or oversized") from None

    async def _mapping_meta(self, main_index_name: str) -> dict[str, Any]:
        navigation_index_name(main_index_name)
        body = _object(await self._request("GET", f"/{main_index_name}/_mapping"))
        try:
            if set(body) != {main_index_name}:
                raise ValueError
            index = body[main_index_name]
            mappings = index["mappings"]
            if not isinstance(mappings, dict):
                raise ValueError
            meta = mappings.get("_meta", {})
            if not isinstance(meta, dict):
                raise ValueError
            if _MARKER in meta and (type(meta[_MARKER]) is not int or meta[_MARKER] != 1):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise OpenSearchError("OpenSearch navigation mapping or schema is invalid") from None
        return meta

    async def mark_navigation_ready(self, main_index_name: str) -> None:
        meta = await self._mapping_meta(main_index_name)
        response = await self._request(
            "PUT",
            f"/{main_index_name}/_mapping",
            json={"_meta": {**meta, _MARKER: 1}},
        )
        acknowledgement = _object(response)
        if (acknowledgement.get("acknowledged") is not True
            or acknowledgement.get("errors", False) is not False or "error" in acknowledgement):
            raise OpenSearchError("OpenSearch navigation marker was not acknowledged")

    async def navigation_schema(self, main_index_name: str) -> int | None:
        meta = await self._mapping_meta(main_index_name)
        return 1 if _MARKER in meta else None

    async def search_navigation(
        self,
        index_name: str,
        *,
        query: str,
        limit: int,
    ) -> tuple[NavigationSearchHit, ...]:
        _validate_sidecar(index_name)
        if not isinstance(query, str) or not query.strip():
            raise ValueError("navigation query must be non-empty")
        if type(limit) is not int or limit <= 0:
            raise ValueError("navigation limit must be positive")
        body = _object(
            await self._request(
                "POST",
                f"/{index_name}/_search",
                json={
                    "query": {"match": {"content": query}},
                    "size": limit,
                    "_source": ["version_id", "locator"],
                },
            )
        )
        _complete_read(body)
        try:
            raw_hits = body["hits"]["hits"]
            if not isinstance(raw_hits, list):
                raise ValueError
            hits: list[NavigationSearchHit] = []
            for raw in raw_hits:
                source = raw["_source"]
                score = raw["_score"]
                if type(score) not in (int, float):
                    raise ValueError
                hits.append(
                    NavigationSearchHit(
                        version_id=UUID(source["version_id"]),
                        locator=source["locator"],
                        score=float(score),
                    )
                )
        except (AttributeError, KeyError, TypeError, ValueError):
            raise OpenSearchError("OpenSearch navigation search hits are malformed") from None
        return tuple(hits)
