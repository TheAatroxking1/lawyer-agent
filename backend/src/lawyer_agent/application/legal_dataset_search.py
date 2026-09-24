"""Legal dataset search: resolve the dataset alias, then hybrid-search it.

Callers of the hybrid search today must name a physical OpenSearch index
(tests used throw-away names). This service hides that: you ask for the
current ``dataset_v1`` and the service resolves the dataset alias to its
active index before delegating to the hybrid BM25+Dense->RRF search. An alias
that is not published is a hard error (never faked as an empty result), so
retrieval layers never silently search a missing dataset.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from lawyer_agent.domain.legal_search import LegalSearchHit


class LegalDatasetNotPublished(ValueError):
    """The dataset alias does not point at any index yet."""


class _HybridSearchPort(Protocol):
    async def search(
        self,
        *,
        query: str,
        model_ref: str,
        dimension: int,
        index_name: str,
        version_id: UUID | None,
        limit: int,
        bm25_size: int,
        knn_size: int,
        version_ids: tuple[UUID, ...] | None = None,
    ) -> tuple[LegalSearchHit, ...]: ...


class _DatasetAliasPort(Protocol):
    async def active_dataset_index(self, alias: str) -> str | None: ...


class _NavigationSearchPort(Protocol):
    async def candidate_versions(
        self, *, main_index_name: str, query: str,
    ) -> tuple[UUID, ...] | None: ...


class LegalDatasetSearchService:
    """Searches the current dataset by resolving its stable alias."""

    def __init__(
        self,
        hybrid: _HybridSearchPort,
        alias: _DatasetAliasPort,
        navigation: _NavigationSearchPort,
    ) -> None:
        if not hasattr(hybrid, "search"):
            raise ValueError("dataset search requires a hybrid search service")
        if not hasattr(alias, "active_dataset_index"):
            raise ValueError("dataset search requires an alias service")
        self._hybrid = hybrid
        self._alias = alias
        self._navigation = navigation

    async def search_dataset(
        self,
        *,
        alias: str,
        query: str,
        model_ref: str,
        dimension: int,
        version_id: UUID | None = None,
        limit: int = 20,
        bm25_size: int = 100,
        knn_size: int = 100,
    ) -> tuple[LegalSearchHit, ...]:
        if not isinstance(alias, str) or not alias.strip():
            raise ValueError("dataset alias must be non-empty text")
        index_name = await self._alias.active_dataset_index(alias)
        if index_name is None:
            raise LegalDatasetNotPublished(
                f"dataset {alias} is not published under any index"
            )
        if version_id is None:
            candidates = await self._navigation.candidate_versions(
                main_index_name=index_name, query=query,
            )
            if candidates is not None:
                return await self._hybrid.search(
                    query=query, model_ref=model_ref, dimension=dimension,
                    index_name=index_name, version_id=None, version_ids=candidates,
                    limit=limit, bm25_size=bm25_size, knn_size=knn_size,
                )
        return await self._hybrid.search(
            query=query,
            model_ref=model_ref,
            dimension=dimension,
            index_name=index_name,
            version_id=version_id,
            limit=limit,
            bm25_size=bm25_size,
            knn_size=knn_size,
        )
