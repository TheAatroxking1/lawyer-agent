"""Legal hybrid search orchestration (BM25 + dense k-NN -> RRF).

Hides the mechanics of "embed the query, run two OpenSearch queries, fuse by
reciprocal rank" behind one call. Consumers receive an ordered tuple of
``LegalSearchHit`` and never see provider/search internals. This is retrieval
only: evidence gating and parent-provision restore happen in later slices.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from lawyer_agent.application.model_gateway import (
    ModelGateway,
    ModelInputInvalid,
)
from lawyer_agent.application.retrieval_fusion import reciprocal_rank_fusion
from lawyer_agent.domain.legal_search import LegalSearchHit
from lawyer_agent.infrastructure.search.opensearch import OpenSearchRestClient


class _IndexedChunkScopePort(Protocol):
    """Optional guard: chunk rows exist for a version before searching."""

    async def version_is_indexed(self, version_id: UUID) -> bool: ...


class LegalHybridSearchService:
    """Runs BM25 and dense k-NN over the legal index, then fuses results."""

    def __init__(
        self,
        gateway: ModelGateway,
        search: OpenSearchRestClient,
        chunks: _IndexedChunkScopePort | None = None,
    ) -> None:
        if not hasattr(gateway, "embed"):
            raise ValueError("hybrid search requires a model gateway")
        if not hasattr(search, "search_bm25"):
            raise ValueError("hybrid search requires an OpenSearch client")
        self._gateway = gateway
        self._search = search
        self._chunks = chunks

    async def search(
        self,
        *,
        query: str,
        model_ref: str,
        dimension: int,
        index_name: str,
        version_id: UUID | None = None,
        limit: int = 20,
        bm25_size: int = 100,
        knn_size: int = 100,
    ) -> tuple[LegalSearchHit, ...]:
        if not isinstance(query, str) or not query.strip():
            raise ModelInputInvalid("search query must be non-empty text")
        for value, name in (
            (limit, "limit"),
            (bm25_size, "bm25_size"),
            (knn_size, "knn_size"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ModelInputInvalid(f"{name} must be a positive integer")
        if not isinstance(index_name, str) or not index_name.strip():
            raise ModelInputInvalid("index_name must be non-empty text")
        if self._chunks is not None and version_id is not None:
            if not await self._chunks.version_is_indexed(version_id):
                return ()

        (query_vector,) = await self._gateway.embed(
            model_ref=model_ref,
            texts=(query.strip(),),
            dimension=dimension,
        )
        bm25_hits = await self._search.search_bm25(
            index_name,
            query=query.strip(),
            limit=bm25_size,
            version_id=version_id,
        )
        knn_hits = await self._search.search_knn(
            index_name,
            query_vector=query_vector.values,
            limit=knn_size,
            version_id=version_id,
        )
        fused = reciprocal_rank_fusion(bm25_hits, knn_hits)
        return fused[:limit]
