"""Dataset-level search-to-evidence orchestration (non-model).

The retrieval+QA pipeline should call one entry point: ask the current
dataset (by stable alias) a question and receive an authoritative
``EvidenceBundle`` backed by restored provisions — or ``None`` when there is
no evidence (upstream then refuses or narrows). This service hides "resolve
alias -> hybrid search -> restore provisions -> assemble bundle".
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from lawyer_agent.application.evidence import EvidenceBundle
from lawyer_agent.domain.legal_search import LegalSearchHit


class _DatasetSearchPort(Protocol):
    async def search_dataset(
        self,
        *,
        alias: str,
        query: str,
        model_ref: str,
        dimension: int,
        version_id: UUID | None,
        limit: int,
        bm25_size: int,
        knn_size: int,
    ) -> tuple[LegalSearchHit, ...]: ...


class _EvidenceAssemblyPort(Protocol):
    async def assemble(
        self, *, hits: Sequence[LegalSearchHit]
    ) -> EvidenceBundle | None: ...


class LegalDatasetEvidenceService:
    """Searches the current dataset and assembles authoritative evidence."""

    def __init__(
        self,
        dataset_search: _DatasetSearchPort,
        evidence_assembly: _EvidenceAssemblyPort,
    ) -> None:
        if not hasattr(dataset_search, "search_dataset"):
            raise ValueError("dataset evidence requires a dataset search service")
        if not hasattr(evidence_assembly, "assemble"):
            raise ValueError("dataset evidence requires an evidence assembly service")
        self._dataset_search = dataset_search
        self._evidence_assembly = evidence_assembly

    async def search_evidence(
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
    ) -> EvidenceBundle | None:
        if not isinstance(alias, str) or not alias.strip():
            raise ValueError("dataset alias must be non-empty text")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("search query must be non-empty text")
        hits = await self._dataset_search.search_dataset(
            alias=alias,
            query=query,
            model_ref=model_ref,
            dimension=dimension,
            version_id=version_id,
            limit=limit,
            bm25_size=bm25_size,
            knn_size=knn_size,
        )
        if not hits:
            return None
        return await self._evidence_assembly.assemble(hits=hits)
