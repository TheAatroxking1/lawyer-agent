from __future__ import annotations

from typing import Protocol
from uuid import UUID

import pytest

from lawyer_agent.application.evidence import EvidenceBundle, EvidenceItem
from lawyer_agent.application.legal_dataset_evidence import (
    LegalDatasetEvidenceService,
)
from lawyer_agent.application.legal_dataset_search import LegalDatasetNotPublished
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import LegalVersionStatus
from lawyer_agent.domain.legal_search import LegalSearchHit

VERSION = UUID("01a06ae2-6200-7000-8000-0000000000c3")
PROVISION = UUID("01a06ae2-6400-7000-8000-0000000000c5")


class _SearchPort(Protocol):
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


class _AssemblyPort(Protocol):
    async def assemble(
        self, *, hits: object
    ) -> EvidenceBundle | None: ...


class _Recorder:
    def __init__(self) -> None:
        self.search_calls: list[dict[str, object]] = []
        self.assemble_calls: list[tuple[object, ...]] = []


class _Search:
    def __init__(self, recorder: _Recorder, hits: tuple[LegalSearchHit, ...]) -> None:
        self._recorder = recorder
        self._hits = hits

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
    ) -> tuple[LegalSearchHit, ...]:
        self._recorder.search_calls.append(
            {
                "alias": alias,
                "query": query,
                "model_ref": model_ref,
                "dimension": dimension,
                "version_id": version_id,
                "limit": limit,
                "bm25_size": bm25_size,
                "knn_size": knn_size,
            }
        )
        return self._hits


class _Assembly:
    def __init__(self, recorder: _Recorder, bundle: EvidenceBundle | None) -> None:
        self._recorder = recorder
        self._bundle = bundle

    async def assemble(self, *, hits: object) -> EvidenceBundle | None:
        self._recorder.assemble_calls.append(tuple(hits))
        return self._bundle


def _item() -> EvidenceItem:
    return EvidenceItem(
        evidence_id=PROVISION,
        instrument_title="中华人民共和国民法典",
        version_id=VERSION,
        version_label="2020 公布版",
        status=LegalVersionStatus.CURRENT,
        published_on=None,
        effective_on=None,
        repealed_on=None,
        provision_no="第一条",
        provision_text="第一条 为了保护民事权益，制定本法。",
        source_ref="object://corpus/civil-code.docx",
        dataset_version="dataset_v1",
        authorized=True,
    )


def _hit() -> LegalSearchHit:
    return LegalSearchHit(
        chunk_id=new_uuid7(),
        provision_id=PROVISION,
        version_id=VERSION,
        score=1.0,
    )


def _bundle() -> EvidenceBundle:
    return EvidenceBundle(items=(_item(),))


_UNSET = object()


def _service(
    recorder: _Recorder,
    *,
    hits: tuple[LegalSearchHit, ...] = (_hit(),),
    bundle: EvidenceBundle | None | object = _UNSET,
) -> LegalDatasetEvidenceService:
    assembly_bundle: EvidenceBundle | None
    if bundle is _UNSET:
        assembly_bundle = _bundle()
    else:
        assembly_bundle = bundle  # type: ignore[assignment]
    return LegalDatasetEvidenceService(
        dataset_search=_Search(recorder, hits),
        evidence_assembly=_Assembly(recorder, assembly_bundle),
    )


async def test_search_evidence_delegates_and_returns_bundle() -> None:
    recorder = _Recorder()
    service = _service(recorder)
    result = await service.search_evidence(
        alias="dataset_v1",
        query="违约金",
        model_ref="bge-small-zh",
        dimension=512,
        version_id=VERSION,
        limit=5,
        bm25_size=50,
        knn_size=40,
    )
    assert result is not None
    assert result.items[0].evidence_id == PROVISION
    assert len(recorder.search_calls) == 1
    call = recorder.search_calls[0]
    assert call["alias"] == "dataset_v1"
    assert call["query"] == "违约金"
    assert call["model_ref"] == "bge-small-zh"
    assert call["dimension"] == 512
    assert call["version_id"] == VERSION
    assert call["limit"] == 5
    assert call["bm25_size"] == 50
    assert call["knn_size"] == 40
    assert len(recorder.assemble_calls) == 1
    passed_hits = recorder.assemble_calls[0]
    assert len(passed_hits) == 1
    assert passed_hits[0].provision_id == PROVISION
    assert passed_hits[0].version_id == VERSION


async def test_search_evidence_no_hits_returns_none_without_assembly() -> None:
    recorder = _Recorder()
    service = _service(recorder, hits=())
    result = await service.search_evidence(
        alias="dataset_v1",
        query="无此内容",
        model_ref="m",
        dimension=8,
    )
    assert result is None
    assert recorder.assemble_calls == []


async def test_search_evidence_unpublished_alias_propagates() -> None:
    class UnpublishedSearch:
        async def search_dataset(self, **kwargs: object) -> tuple[LegalSearchHit, ...]:
            raise LegalDatasetNotPublished("dataset not published")

    service = LegalDatasetEvidenceService(
        dataset_search=UnpublishedSearch(),  # type: ignore[arg-type]
        evidence_assembly=_Assembly(_Recorder(), _bundle()),
    )
    with pytest.raises(LegalDatasetNotPublished):
        await service.search_evidence(
            alias="dataset_v1", query="违约金", model_ref="m", dimension=8
        )


async def test_search_evidence_assembly_none_returns_none() -> None:
    recorder = _Recorder()
    service = _service(recorder, hits=(_hit(),), bundle=None)
    result = await service.search_evidence(
        alias="dataset_v1", query="违约金", model_ref="m", dimension=8
    )
    assert result is None
    assert len(recorder.assemble_calls) == 1
    assert len(recorder.assemble_calls[0]) == 1


async def test_search_evidence_validation() -> None:
    recorder = _Recorder()
    service = _service(recorder)
    with pytest.raises(ValueError, match="alias"):
        await service.search_evidence(
            alias=" ", query="违约金", model_ref="m", dimension=8
        )
    with pytest.raises(ValueError, match="query"):
        await service.search_evidence(
            alias="dataset_v1", query="  ", model_ref="m", dimension=8
        )
    assert recorder.search_calls == []
