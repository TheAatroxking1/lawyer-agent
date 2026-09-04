from __future__ import annotations

from typing import Protocol
from uuid import UUID

import pytest

from lawyer_agent.application.legal_dataset_search import (
    LegalDatasetNotPublished,
    LegalDatasetSearchService,
)
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_search import LegalSearchHit

VERSION = UUID("01a06ae2-6200-7000-8000-0000000000c3")


class _HybridPort(Protocol):
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
    ) -> tuple[LegalSearchHit, ...]: ...


class _AliasPort(Protocol):
    async def active_dataset_index(self, alias: str) -> str | None: ...


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []


class _Hybrid:
    def __init__(self, recorder: _Recorder) -> None:
        self._recorder = recorder

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
    ) -> tuple[LegalSearchHit, ...]:
        self._recorder.calls.append(
            {
                "query": query,
                "model_ref": model_ref,
                "dimension": dimension,
                "index_name": index_name,
                "version_id": version_id,
                "limit": limit,
                "bm25_size": bm25_size,
                "knn_size": knn_size,
            }
        )
        return (
            LegalSearchHit(
                chunk_id=new_uuid7(),
                provision_id=new_uuid7(),
                version_id=VERSION,
                score=1.0,
            ),
        )


class _Alias:
    def __init__(self, target: str | None) -> None:
        self._target = target

    async def active_dataset_index(self, alias: str) -> str | None:
        if alias != "dataset_v1":
            raise ValueError(f"unknown alias: {alias}")
        return self._target


def _service(target: str | None) -> tuple[LegalDatasetSearchService, _Recorder]:
    recorder = _Recorder()
    return (
        LegalDatasetSearchService(
            hybrid=_Hybrid(recorder),
            alias=_Alias(target),
        ),
        recorder,
    )


async def test_search_dataset_resolves_alias_and_delegates() -> None:
    service, recorder = _service("legal_idx_2024")
    hits = await service.search_dataset(
        alias="dataset_v1",
        query="违约金",
        model_ref="bge-small-zh",
        dimension=512,
        version_id=VERSION,
        limit=5,
        bm25_size=50,
        knn_size=40,
    )
    assert hits
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["index_name"] == "legal_idx_2024"
    assert call["query"] == "违约金"
    assert call["model_ref"] == "bge-small-zh"
    assert call["dimension"] == 512
    assert call["version_id"] == VERSION
    assert call["limit"] == 5
    assert call["bm25_size"] == 50
    assert call["knn_size"] == 40


async def test_search_dataset_without_version() -> None:
    service, recorder = _service("legal_idx_2024")
    await service.search_dataset(
        alias="dataset_v1",
        query="违约金",
        model_ref="m",
        dimension=8,
    )
    call = recorder.calls[0]
    assert call["version_id"] is None
    assert call["limit"] == 20


async def test_search_dataset_refuses_unpublished_alias() -> None:
    service, _ = _service(None)
    with pytest.raises(LegalDatasetNotPublished, match="not published"):
        await service.search_dataset(
            alias="dataset_v1",
            query="违约金",
            model_ref="m",
            dimension=8,
        )


async def test_search_dataset_alias_error_propagates() -> None:
    service, recorder = _service("legal_idx_2024")
    with pytest.raises(ValueError, match="unknown alias"):
        await service.search_dataset(
            alias="dataset_v2",
            query="违约金",
            model_ref="m",
            dimension=8,
        )
    assert recorder.calls == []


async def test_search_dataset_returns_empty_when_hybrid_has_no_hits() -> None:
    class EmptyHybrid:
        async def search(self, **kwargs: object) -> tuple[LegalSearchHit, ...]:
            return ()

    service = LegalDatasetSearchService(
        hybrid=EmptyHybrid(),  # type: ignore[arg-type]
        alias=_Alias("legal_idx_2024"),
    )
    hits = await service.search_dataset(
        alias="dataset_v1", query="无此内容", model_ref="m", dimension=8
    )
    assert hits == ()
