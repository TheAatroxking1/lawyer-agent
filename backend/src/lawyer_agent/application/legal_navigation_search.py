"""Conservative, source-locator-based narrowing of legal corpus retrieval."""

from __future__ import annotations

from typing import Protocol
from unicodedata import normalize
from uuid import UUID

from lawyer_agent.domain.legal_navigation import NavigationSearchHit, navigation_index_name


class NavigationSearchPort(Protocol):
    async def navigation_schema(self, main_index_name: str) -> int | None: ...

    async def search_navigation(
        self,
        index_name: str,
        *,
        query: str,
        limit: int,
    ) -> tuple[NavigationSearchHit, ...]: ...


class LegalNavigationSearchService:
    def __init__(self, search: NavigationSearchPort) -> None:
        self._search = search

    async def candidate_versions(
        self,
        *,
        main_index_name: str,
        query: str,
        candidate_limit: int = 5,
        scan_size: int = 50,
    ) -> tuple[UUID, ...] | None:
        index_name = navigation_index_name(main_index_name)
        if not isinstance(query, str) or not _normalize_locator(query):
            raise ValueError("navigation query must be non-empty text")
        for value in (candidate_limit, scan_size):
            if type(value) is not int or value < 1:
                raise ValueError("navigation limits must be positive integers")
        schema = await self._search.navigation_schema(main_index_name)
        if schema is None:
            return None
        if type(schema) is not int or schema != 1:
            raise RuntimeError("unsupported navigation schema")
        hits = await self._search.search_navigation(index_name, query=query, limit=scan_size)
        # A full scan page may omit another equally strong version.
        if len(hits) >= scan_size:
            return None
        normalized_query = _normalize_locator(query)
        versions: list[UUID] = []
        for hit in hits:
            locator = _normalize_locator(hit.locator)
            if locator and locator in normalized_query and hit.version_id not in versions:
                versions.append(hit.version_id)
                if len(versions) > candidate_limit:
                    return None
        return tuple(versions) or None


def _normalize_locator(text: str) -> str:
    return "".join(
        char for char in normalize("NFKC", text) if not char.isspace() and char not in "《》〈〉"
    )
