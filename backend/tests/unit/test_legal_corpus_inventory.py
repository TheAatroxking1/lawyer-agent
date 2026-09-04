from __future__ import annotations

from datetime import UTC, datetime

import pytest

from lawyer_agent.application.legal_corpus import (
    CorpusFile,
    LegalCorpusInventoryService,
)
from lawyer_agent.domain.legal_corpus import DatasetSnapshot, LoadBatch, LoadStatus

_FIXED_NOW = datetime(2026, 9, 4, 3, 0, tzinfo=UTC)


class _FakeInventory:
    def __init__(self) -> None:
        self.batches: list[LoadBatch] = []

    async def find_batch_by_sha256(self, file_sha256: bytes) -> LoadBatch | None:
        return next(
            (batch for batch in self.batches if batch.file_sha256 == file_sha256), None
        )

    async def create_load_batch(self, batch: LoadBatch) -> None:
        self.batches.append(batch)


class _FakeDataset:
    def __init__(self) -> None:
        self.snapshots: dict[str, DatasetSnapshot] = {}

    async def find_dataset(self, dataset_name: str) -> DatasetSnapshot | None:
        return self.snapshots.get(dataset_name)

    async def upsert_dataset(self, snapshot: DatasetSnapshot) -> None:
        self.snapshots[snapshot.dataset_name] = snapshot


def _service() -> LegalCorpusInventoryService:
    return LegalCorpusInventoryService(
        _FakeInventory(),
        _FakeDataset(),
        "docx-zip-v1",
        now=lambda: _FIXED_NOW,
    )


def test_inventory_counts_duplicates_and_builds_manifest() -> None:
    service = _service()
    payload_a = "第一条 内容A。".encode()
    payload_b = "第二条 内容B。".encode()
    result = __import__("asyncio").run(
        service.inventory(
            (
                CorpusFile("object://corpus/a.docx", payload_a),
                CorpusFile("object://corpus/b.docx", payload_b),
                CorpusFile("object://corpus/a-copy.docx", payload_a),
            )
        )
    )
    assert result.batch.status is LoadStatus.INVENTORIED
    assert result.quality_metrics["counts"]["files"] == 3
    assert result.quality_metrics["counts"]["unique"] == 2
    assert result.quality_metrics["counts"]["duplicates"] == 1
    assert result.manifest["duplicate_of"][2] == "object://corpus/a.docx"
    assert len(result.manifest["sha256"]) == 3


def test_inventory_rejects_empty_batch() -> None:
    service = _service()
    with pytest.raises(ValueError, match="at least one corpus file"):
        __import__("asyncio").run(service.inventory(()))
