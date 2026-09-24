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

from lawyer_agent.domain.legal_search import LegalSearchHit, validate_version_scope
from lawyer_agent.infrastructure.search.bounded_bulk import BoundedBulkError, bounded_bulk_payloads

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
                raise OpenSearchError(
                    f"OpenSearch index creation failed with HTTP {response.status_code}"
                )
            acknowledgement = _response_object(response, "index creation")
            if (
                acknowledgement.get("acknowledged") is not True
                or acknowledgement.get("errors", False) is not False
                or "error" in acknowledgement
            ):
                raise OpenSearchError("OpenSearch index creation was not acknowledged")

    async def delete_index(self, index_name: str) -> None:
        """Remove a test/derived index; 404 is treated as already-absent."""
        async with self._client() as client:
            response = await client.delete(f"/{index_name}")
            if response.status_code not in (200, 404):
                await self._raise(response)

    async def index_exists(self, index_name: str) -> bool:
        """Whether a concrete index exists (used before alias pointing)."""
        async with self._client() as client:
            response = await client.head(f"/{index_name}")
            if response.status_code == 200:
                return True
            if response.status_code == 404:
                return False
            await self._raise(response)
            return False  # pragma: no cover - _raise always raises

    async def resolve_alias(self, alias: str) -> str | None:
        """Return the single index the alias points at, or None when absent."""
        async with self._client() as client:
            response = await client.get(f"/_alias/{alias}")
            if response.status_code == 404:
                return None
            if response.status_code != 200:
                raise OpenSearchError(
                    f"OpenSearch alias lookup failed with HTTP {response.status_code}"
                )
        body = _response_object(response, "alias lookup")
        if not body:
            raise OpenSearchError("OpenSearch alias lookup returned no target")
        for target, value in body.items():
            if (
                not isinstance(target, str) or not target
                or not isinstance(value, dict)
                or not isinstance(value.get("aliases"), dict)
                or not isinstance(value["aliases"].get(alias), dict)
            ):
                raise OpenSearchError("OpenSearch alias lookup is malformed")
        targets = set(body)
        if len(targets) > 1:
            raise OpenSearchError(
                f"alias {alias} points at multiple indices: {sorted(targets)}"
            )
        return next(iter(targets), None)

    async def point_alias(self, alias: str, index_name: str) -> str | None:
        """Atomically point the alias at one index; returns the previous target.

        No-op when the alias already points at ``index_name``. Switching is a
        single ``POST /_aliases`` (remove old target + add new target) so a
        reader never observes a dangling alias.
        """
        previous = await self.resolve_alias(alias)
        if previous == index_name:
            return previous
        actions: list[dict[str, Any]] = []
        if previous is not None:
            actions.append(
                {"remove": {"index": previous, "alias": alias}}
            )
        actions.append({"add": {"index": index_name, "alias": alias}})
        async with self._client() as client:
            response = await client.post("/_aliases", json={"actions": actions})
            if response.status_code != 200:
                raise OpenSearchError(
                    f"OpenSearch alias switch failed with HTTP {response.status_code}"
                )
            body = _response_object(response, "alias switch")
            if (
                body.get("acknowledged") is not True
                or body.get("errors", False) is not False
                or "error" in body
            ):
                raise OpenSearchError("OpenSearch alias switch was not fully acknowledged")
        return previous

    async def drop_alias(self, alias: str) -> None:
        """Remove the alias; absent aliases are an idempotent no-op."""
        target = await self.resolve_alias(alias)
        if target is None:
            return
        async with self._client() as client:
            response = await client.delete(f"/{target}/_alias/{alias}")
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
        requested_ids: list[str] = []
        for document in documents:
            chunk_id = document.get("chunk_id")
            if not isinstance(chunk_id, str) or not chunk_id:
                raise OpenSearchError("OpenSearch bulk document id is invalid")
            if chunk_id in requested_ids:
                raise OpenSearchError("OpenSearch bulk document ids must be unique")
            requested_ids.append(chunk_id)
            action = {"index": {"_index": index_name, "_id": chunk_id}}
            lines.append(json.dumps(action, ensure_ascii=False))
            lines.append(json.dumps(document, ensure_ascii=False))
        payload = ("\n".join(lines) + "\n").encode("utf-8")
        headers = {"Content-Type": _BULK_HEADER}
        async with self._client() as client:
            delete_body = {
                "query": {"term": {"parser_version": parser_version}}
            }
            delete_response = await client.post(
                f"/{index_name}/_delete_by_query?refresh=true", json=delete_body
            )
            _validate_delete_acknowledgement(delete_response)
            if not payload.strip():
                return
            response = await client.post(
                f"/{index_name}/_bulk?refresh=true",
                content=payload,
                headers=headers,
            )
            _validate_bulk_acknowledgement(response, tuple(requested_ids))

    async def append_documents(
        self,
        index_name: str,
        documents: tuple[dict[str, Any], ...],
        *,
        parser_version: str,
    ) -> None:
        """Append at most 256 documents without deleting earlier build batches."""
        if (not isinstance(documents, tuple)
                or not isinstance(parser_version, str) or not parser_version.strip()
                or any(
                    not isinstance(document, dict)
                    or document.get("parser_version") != parser_version
                    for document in documents
                )):
            raise OpenSearchError("OpenSearch append batch parser is invalid")
        try:
            payloads = bounded_bulk_payloads(index_name, documents, id_field="chunk_id")
            async with self._client() as client:
                for payload in payloads:
                    response = await client.post(
                        f"/{index_name}/_bulk?refresh=true", content=payload.content,
                        headers={"Content-Type": _BULK_HEADER},
                    )
                    _validate_bulk_acknowledgement(response, payload.document_ids)
        except BoundedBulkError:
            raise OpenSearchError(
                "OpenSearch append batch is invalid or exceeds size limits"
            ) from None

    async def count_documents(self, index_name: str) -> int:
        """Count a fully acknowledged index; partial shard counts cannot release a build."""
        async with self._client() as client:
            response = await client.get(f"/{index_name}/_count")
        if response.status_code != 200:
            raise OpenSearchError(f"OpenSearch count failed with HTTP {response.status_code}")
        body = _response_object(response, "count")
        count, shards = body.get("count"), body.get("_shards")
        if (type(count) is not int or count < 0 or not isinstance(shards, dict)
                or any(
                    type(shards.get(key)) is not int for key in ("total", "successful", "failed")
                )
                or shards["total"] <= 0 or shards["successful"] != shards["total"]
                or shards["failed"] != 0
                or ("failures" in shards and shards["failures"] != [])
                or body.get("timed_out", False) is not False
                or body.get("terminated_early", False) is not False
                or body.get("errors", False) is not False
                or "error" in body):
            raise OpenSearchError("OpenSearch count response is invalid or incomplete")
        return count

    async def search_bm25(
        self,
        index_name: str,
        *,
        query: str,
        limit: int,
        version_id: UUID | None = None,
        version_ids: tuple[UUID, ...] | None = None,
    ) -> tuple[LegalSearchHit, ...]:
        validate_version_scope(version_id, version_ids)
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
        elif version_ids is not None:
            body["query"]["bool"]["filter"] = [
                {"terms": {"version_id": [str(value) for value in version_ids]}}
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
        version_ids: tuple[UUID, ...] | None = None,
    ) -> tuple[LegalSearchHit, ...]:
        """k-NN search over the dense ``content_vector`` field."""
        validate_version_scope(version_id, version_ids)
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
        elif version_ids is not None:
            knn_query["filter"] = {
                "terms": {"version_id": [str(value) for value in version_ids]}
            }
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


def _response_object(response: httpx.Response, operation: str) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise OpenSearchError(
            f"OpenSearch {operation} acknowledgement is malformed"
        ) from exc
    if not isinstance(body, dict):
        raise OpenSearchError(
            f"OpenSearch {operation} acknowledgement is malformed"
        )
    return body


def _validate_delete_acknowledgement(response: httpx.Response) -> None:
    if response.status_code != 200:
        raise OpenSearchError(
            f"OpenSearch delete-by-query failed with HTTP {response.status_code}"
        )
    body = _response_object(response, "delete-by-query")
    timed_out = body.get("timed_out")
    version_conflicts = body.get("version_conflicts")
    failures = body.get("failures")
    if (
        timed_out is not False
        or isinstance(version_conflicts, bool)
        or not isinstance(version_conflicts, int)
        or version_conflicts != 0
        or not isinstance(failures, list)
        or bool(failures)
    ):
        raise OpenSearchError("OpenSearch delete-by-query was not fully acknowledged")


def _validate_bulk_acknowledgement(
    response: httpx.Response, requested_ids: tuple[str, ...]
) -> None:
    if response.status_code != 200:
        raise OpenSearchError(
            f"OpenSearch bulk write failed with HTTP {response.status_code}"
        )
    body = _response_object(response, "bulk write")
    if body.get("errors") is not False:
        raise OpenSearchError("OpenSearch bulk write was not fully acknowledged")
    items = body.get("items")
    if not isinstance(items, list) or len(items) != len(requested_ids):
        raise OpenSearchError("OpenSearch bulk acknowledgement count mismatch")
    for expected_id, item in zip(requested_ids, items, strict=True):
        if not isinstance(item, dict) or set(item) != {"index"}:
            raise OpenSearchError("OpenSearch bulk item acknowledgement is malformed")
        result = item["index"]
        if not isinstance(result, dict):
            raise OpenSearchError("OpenSearch bulk item acknowledgement is malformed")
        status = result.get("status")
        if (
            result.get("_id") != expected_id
            or isinstance(status, bool)
            or not isinstance(status, int)
            or not 200 <= status < 300
            or "error" in result
        ):
            raise OpenSearchError("OpenSearch bulk item was not acknowledged")
