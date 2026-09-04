"""OpenSearch REST client for the legal corpus index (BM25-first, no SDK).

Uses ``httpx.AsyncClient`` so the whole request/response surface can be driven
through an injected transport (tests use MockTransport; no live OpenSearch is
required). Documents carry only identifiers + derived text, never tenant data.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import httpx

from lawyer_agent.domain.legal_search import LegalSearchHit

DEFAULT_OPENSEARCH_URL = "http://127.0.0.1:9200"
_BULK_HEADER = "application/x-ndjson"


class OpenSearchError(RuntimeError):
    """Stable wrapper for OpenSearch request failures."""

    code: str = "opensearch_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class OpenSearchUnavailable(OpenSearchError):
    code = "opensearch_unavailable"


def chunk_document(
    *,
    chunk_id: UUID,
    provision_id: UUID,
    version_id: UUID,
    content: str,
    parser_version: str,
) -> dict[str, Any]:
    """Derived index document; content is already-sourced chunk text."""
    return {
        "chunk_id": str(chunk_id),
        "provision_id": str(provision_id),
        "version_id": str(version_id),
        "content": content,
        "parser_version": parser_version,
    }


def parse_search_hit(raw: dict[str, Any]) -> LegalSearchHit:
    """Parse one OpenSearch hit; ids must be present as UUID strings."""
    try:
        source = raw["_source"]
        score = float(raw.get("_score") or 0.0)
        return LegalSearchHit(
            chunk_id=UUID(source["chunk_id"]),
            provision_id=UUID(source["provision_id"]),
            version_id=UUID(source["version_id"]),
            score=score,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise OpenSearchError(f"malformed search hit: {exc}") from exc


class OpenSearchRestClient:
    """Thin REST client: index ensure, idempotent replace, BM25 search."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_OPENSEARCH_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {"base_url": self._base_url, "timeout": 20.0}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    async def ensure_index(
        self, index_name: str, *, vector_dimension: int | None = None
    ) -> None:
        properties: dict[str, Any] = {
            "chunk_id": {"type": "keyword"},
            "provision_id": {"type": "keyword"},
            "version_id": {"type": "keyword"},
            "parser_version": {"type": "keyword"},
            "content": {"type": "text"},
        }
        settings: dict[str, Any] = {
            "number_of_shards": 1,
            "number_of_replicas": 0,
        }
        if vector_dimension is not None:
            properties["content_vector"] = {
                "type": "knn_vector",
                "dimension": vector_dimension,
                "method": {"name": "hnsw", "space_type": "l2", "engine": "lucene"},
            }
            settings["index"] = {"knn": True}
        body = {"settings": settings, "mappings": {"properties": properties}}
        async with self._client() as client:
            response = await client.put(f"/{index_name}", json=body)
            if response.status_code not in (200, 201):
                await self._raise(response)

    async def delete_index(self, index_name: str) -> None:
        """Remove a test/derived index; 404 is treated as already-absent."""
        async with self._client() as client:
            response = await client.delete(f"/{index_name}")
            if response.status_code not in (200, 404):
                await self._raise(response)

    async def replace_documents(
        self,
        index_name: str,
        documents: tuple[dict[str, Any], ...],
        *,
        parser_version: str,
    ) -> None:
        """Delete-by-query the old parser version rows, then bulk upsert.

        Derived and idempotent: replacing a version rebuilds only its rows.
        """
        lines: list[str] = []
        for document in documents:
            chunk_id = document["chunk_id"]
            action = {"index": {"_index": index_name, "_id": chunk_id}}
            lines.append(json.dumps(action, ensure_ascii=False))
            lines.append(json.dumps(document, ensure_ascii=False))
        payload = ("\n".join(lines) + "\n").encode("utf-8")
        headers = {"Content-Type": _BULK_HEADER}
        async with self._client() as client:
            delete_body = {
                "query": {"term": {"parser_version": parser_version}}
            }
            await client.post(
                f"/{index_name}/_delete_by_query?refresh=true", json=delete_body
            )
            if not payload.strip():
                return
            response = await client.post(
                f"/{index_name}/_bulk?refresh=true",
                content=payload,
                headers=headers,
            )
            if response.status_code not in (200, 201):
                await self._raise(response)

    async def search_bm25(
        self,
        index_name: str,
        *,
        query: str,
        limit: int,
        version_id: UUID | None = None,
    ) -> tuple[LegalSearchHit, ...]:
        body: dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [{"match": {"content": query}}],
                }
            },
            "size": limit,
        }
        if version_id is not None:
            body["query"]["bool"]["filter"] = [
                {"term": {"version_id": str(version_id)}}
            ]
        async with self._client() as client:
            response = await client.post(f"/{index_name}/_search", json=body)
            if response.status_code != 200:
                await self._raise(response)
        hits = response.json().get("hits", {}).get("hits", [])
        parsed: list[LegalSearchHit] = []
        for hit in hits:
            parsed.append(parse_search_hit(hit))
        parsed.sort(key=lambda item: item.score, reverse=True)
        return tuple(parsed)

    async def search_knn(
        self,
        index_name: str,
        *,
        query_vector: tuple[float, ...],
        limit: int,
        version_id: UUID | None = None,
    ) -> tuple[LegalSearchHit, ...]:
        """k-NN search over the dense ``content_vector`` field."""
        if not isinstance(query_vector, tuple) or not query_vector:
            raise ValueError("knn query vector must be a non-empty tuple of floats")
        if any(
            not isinstance(value, float) or not _finite(value)
            for value in query_vector
        ):
            raise ValueError("knn query vector values must be finite floats")
        knn_query: dict[str, Any] = {"vector": list(query_vector), "k": limit}
        if version_id is not None:
            knn_query["filter"] = {"term": {"version_id": str(version_id)}}
        body: dict[str, Any] = {
            "query": {"knn": {"content_vector": knn_query}},
            "size": limit,
        }
        async with self._client() as client:
            response = await client.post(f"/{index_name}/_search", json=body)
            if response.status_code != 200:
                await self._raise(response)
        hits = response.json().get("hits", {}).get("hits", [])
        parsed: list[LegalSearchHit] = []
        for hit in hits:
            parsed.append(parse_search_hit(hit))
        parsed.sort(key=lambda item: item.score, reverse=True)
        return tuple(parsed)

    async def _raise(self, response: httpx.Response) -> None:
        detail = response.text[:512] if response.text else f"HTTP {response.status_code}"
        raise OpenSearchError(f"OpenSearch request failed ({response.status_code}): {detail}")


def _finite(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))
