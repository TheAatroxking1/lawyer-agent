from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from lawyer_agent.application.legal_corpus_read import (
    LegalCorpusDatasetSnapshotNotFound,
    LegalCorpusInstrumentCursorInvalid,
    LegalCorpusInvalidRequest,
    LegalCorpusLoadBatchCursorInvalid,
    LegalCorpusLoadBatchNotFound,
    LegalCorpusQueryService,
)
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import (
    DatasetSnapshot,
    DatasetState,
    LegalInstrument,
    LoadBatch,
    LoadStatus,
)

_TITLE = "中华人民共和国民法典"
_AUTHORITY = "全国人民代表大会"
_JURISDICTION = "national"


class _FakeCorpus:
    def __init__(self, owner: _FakeUow) -> None:
        self._owner = owner

    async def list_instruments(
        self,
        *,
        limit: int,
        before_id: UUID | None = None,
        title: str | None = None,
        issuing_authority: str | None = None,
        jurisdiction: str | None = None,
        region_code: str | None = None,
    ) -> tuple[LegalInstrument, ...]:
        owner = self._owner
        if owner.fail_cursor:
            from lawyer_agent.infrastructure.persistence.repositories.legal_corpus import (
                LegalCorpusInstrumentListCursorInvalid,
            )

            raise LegalCorpusInstrumentListCursorInvalid("cursor missing")
        owner.calls.append(
            {
                "limit": limit,
                "before_id": before_id,
                "title": title,
                "issuing_authority": issuing_authority,
                "jurisdiction": jurisdiction,
                "region_code": region_code,
            }
        )
        if owner.empty:
            return ()
        return (
            LegalInstrument(
                id=new_uuid7(),
                title=_TITLE,
                issuing_authority=_AUTHORITY,
                jurisdiction=_JURISDICTION,
            ),
        )

    async def dataset_snapshots(self) -> tuple[DatasetSnapshot, ...]:
        return self._owner.snapshots

    async def dataset_snapshot_by_name(
        self, dataset_name: str
    ) -> DatasetSnapshot | None:
        for snapshot in self._owner.snapshots:
            if snapshot.dataset_name == dataset_name:
                return snapshot
        return None

    async def load_batches(
        self,
        *,
        limit: int,
        before_id: UUID | None = None,
    ) -> tuple[LoadBatch, ...]:
        if self._owner.fail_batch_cursor:
            from lawyer_agent.infrastructure.persistence.repositories.legal_corpus import (
                LegalCorpusLoadBatchCursorInvalid,
            )

            raise LegalCorpusLoadBatchCursorInvalid("batch cursor missing")
        return self._owner.batches

    async def load_batch_by_id(self, batch_id: UUID) -> LoadBatch | None:
        for batch in self._owner.batches:
            if batch.id == batch_id:
                return batch
        return None


class _FakeUow:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.fail_cursor = False
        self.fail_batch_cursor = False
        self.empty = False
        self.snapshots: tuple[DatasetSnapshot, ...] = ()
        self.batches: tuple[LoadBatch, ...] = ()
        self.corpus = _FakeCorpus(self)

    async def __aenter__(self) -> _FakeUow:
        return self

    async def __aexit__(self, *exc: object) -> None:
        del exc


def _snapshot(name: str = "dataset_v1") -> DatasetSnapshot:
    return DatasetSnapshot(
        id=new_uuid7(),
        dataset_name=name,
        parser_version="docx-v1",
        state=DatasetState.PUBLISHED,
        manifest={"files": 1},
        quality_metrics={"article_count": 10, "coverage": 1.0},
        released_at=datetime(2026, 5, 1, tzinfo=UTC),
    )


def _service(uow: _FakeUow) -> LegalCorpusQueryService:
    return LegalCorpusQueryService(lambda: uow)


async def test_datasets_returns_all_snapshots_from_uow() -> None:
    uow = _FakeUow()
    first = _snapshot("dataset_v1")
    second = _snapshot("dataset_v2")
    uow.snapshots = (first, second)
    rows = await _service(uow).datasets()
    assert rows == (first, second)


async def test_dataset_returns_snapshot_by_name() -> None:
    uow = _FakeUow()
    target = _snapshot("dataset_v1")
    uow.snapshots = (_snapshot("dataset_v2"), target)
    loaded = await _service(uow).dataset(dataset_name="dataset_v1")
    assert loaded == target


async def test_dataset_missing_maps_to_not_found() -> None:
    uow = _FakeUow()
    uow.snapshots = (_snapshot("dataset_v2"),)
    with pytest.raises(LegalCorpusDatasetSnapshotNotFound):
        await _service(uow).dataset(dataset_name="dataset_v1")


async def test_dataset_rejects_blank_or_overlong_name() -> None:
    service = _service(_FakeUow())
    with pytest.raises(LegalCorpusInvalidRequest):
        await service.dataset(dataset_name="   ")
    with pytest.raises(LegalCorpusInvalidRequest):
        await service.dataset(dataset_name="x" * 65)
    with pytest.raises(LegalCorpusInvalidRequest):
        await service.dataset(dataset_name="bad name!")


def _batch(overrides: dict[str, object] | None = None) -> LoadBatch:
    started = datetime(2026, 1, 1, tzinfo=UTC)
    values: dict[str, object] = {
        "id": new_uuid7(),
        "batch_no": "B20260101000000AB",
        "source_ref": "object://corpus/a.docx",
        "file_sha256": bytes(32),
        "parser_version": "docx-v1",
        "status": LoadStatus.COMPLETED,
        "item_counts": {"files": 1, "unique": 1, "duplicates": 0},
        "started_at": started,
        "completed_at": started,
        "error_message": None,
    }
    if overrides:
        values.update(overrides)
    return LoadBatch(**values)


async def test_load_batches_returns_all_from_uow() -> None:
    uow = _FakeUow()
    first = _batch()
    second = _batch()
    uow.batches = (first, second)
    rows = await _service(uow).load_batches(limit=10)
    assert rows == (first, second)


async def test_load_batch_returns_by_id() -> None:
    uow = _FakeUow()
    target = _batch()
    uow.batches = (_batch(), target)
    loaded = await _service(uow).load_batch(batch_id=target.id)
    assert loaded == target


async def test_load_batch_missing_maps_to_not_found() -> None:
    uow = _FakeUow()
    uow.batches = (_batch(),)
    with pytest.raises(LegalCorpusLoadBatchNotFound):
        await _service(uow).load_batch(batch_id=new_uuid7())


async def test_load_batches_rejects_invalid_limit_and_cursor() -> None:
    service = _service(_FakeUow())
    with pytest.raises(LegalCorpusInvalidRequest):
        await service.load_batches(limit=0)
    with pytest.raises(LegalCorpusInvalidRequest):
        await service.load_batches(limit=101)
    with pytest.raises(LegalCorpusInvalidRequest):
        await service.load_batches(limit=5, before_id=UUID(int=0))  # not uuid7


async def test_load_batches_maps_cursor_missing_to_stable_error() -> None:
    uow = _FakeUow()
    uow.fail_batch_cursor = True
    with pytest.raises(LegalCorpusLoadBatchCursorInvalid):
        await _service(uow).load_batches(limit=5, before_id=new_uuid7())


async def test_instruments_passes_validated_filters_to_uow() -> None:
    uow = _FakeUow()
    cursor = new_uuid7()
    rows = await _service(uow).instruments(
        limit=5,
        before_id=cursor,
        title="  中华人民共和国民法典  ",
        issuing_authority="  全国人大  ",
        jurisdiction="  national ",
        region_code=None,
    )
    assert len(rows) == 1
    assert uow.calls == [
        {
            "limit": 5,
            "before_id": cursor,
            "title": "中华人民共和国民法典",
            "issuing_authority": "全国人大",
            "jurisdiction": "national",
            "region_code": None,
        }
    ]


async def test_instruments_rejects_blank_or_overlong_filters() -> None:
    service = _service(_FakeUow())
    with pytest.raises(LegalCorpusInvalidRequest):
        await service.instruments(limit=5, title="   ")
    with pytest.raises(LegalCorpusInvalidRequest):
        await service.instruments(limit=5, issuing_authority="x" * 257)
    with pytest.raises(LegalCorpusInvalidRequest):
        await service.instruments(limit=5, jurisdiction="x" * 65)
    with pytest.raises(LegalCorpusInvalidRequest):
        await service.instruments(limit=5, region_code="x" * 17)


async def test_instruments_rejects_invalid_limit_and_cursor() -> None:
    service = _service(_FakeUow())
    with pytest.raises(LegalCorpusInvalidRequest):
        await service.instruments(limit=0)
    with pytest.raises(LegalCorpusInvalidRequest):
        await service.instruments(limit=101)
    with pytest.raises(LegalCorpusInvalidRequest):
        await service.instruments(limit=5, before_id=UUID(int=0))  # not uuid7


async def test_instruments_maps_cursor_missing_to_stable_error() -> None:
    uow = _FakeUow()
    uow.fail_cursor = True
    with pytest.raises(LegalCorpusInstrumentCursorInvalid):
        await _service(uow).instruments(limit=5, before_id=new_uuid7())


async def test_instruments_empty_result_is_returned() -> None:
    uow = _FakeUow()
    uow.empty = True
    rows = await _service(uow).instruments(limit=5)
    assert rows == ()
