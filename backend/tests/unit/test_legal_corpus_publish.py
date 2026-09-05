from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from lawyer_agent.application.legal_corpus_publish import (
    LegalCorpusPublishService,
    LegalCorpusQualityGate,
)
from lawyer_agent.domain.legal_corpus import (
    DatasetSnapshot,
    DatasetState,
    LoadBatch,
    LoadStatus,
)

_FIXED_NOW = datetime(2026, 9, 4, 4, 0, tzinfo=UTC)
_BATCH_ID = UUID("01a06ae2-7100-7000-8000-000000000001")
_FILE_SHA = bytes(32)


def _batch() -> LoadBatch:
    return LoadBatch(
        id=_BATCH_ID,
        batch_no="B20260101000001AA",
        source_ref="object://corpus/a.docx",
        file_sha256=_FILE_SHA,
        parser_version="docx-v1",
        status=LoadStatus.INVENTORIED,
        item_counts={"files": 1, "unique": 1, "duplicates": 0},
        started_at=_FIXED_NOW,
    )


class _FakePort:
    def __init__(self) -> None:
        self.datasets: dict[str, DatasetSnapshot] = {}
        self.completed: list[UUID] = []
        self.issue_writes: list[tuple[UUID, bytes, tuple[str, ...]]] = []
        self.batch: LoadBatch | None = _batch()

    async def find_dataset(self, dataset_name: str) -> DatasetSnapshot | None:
        return self.datasets.get(dataset_name)

    async def upsert_dataset(self, snapshot: DatasetSnapshot) -> None:
        self.datasets[snapshot.dataset_name] = snapshot

    async def find_batch(self, batch_id: UUID) -> LoadBatch | None:
        del batch_id
        return self.batch

    async def complete_batch(
        self, batch_id: UUID, *, item_counts: dict[str, int], now: datetime
    ) -> bool:
        del item_counts, now
        self.completed.append(batch_id)
        return True

    async def replace_quality_issues(
        self,
        batch_id: UUID,
        file_sha256: bytes,
        issues: tuple[str, ...],
    ) -> None:
        self.issue_writes.append((batch_id, file_sha256, issues))


def _service() -> tuple[LegalCorpusPublishService, _FakePort]:
    port = _FakePort()
    return (
        LegalCorpusPublishService(
            port,
            LegalCorpusQualityGate(),
            now=lambda: _FIXED_NOW,
        ),
        port,
    )


def test_gate_rejects_missing_required_fields() -> None:
    gate = LegalCorpusQualityGate()
    report = gate.evaluate(
        article_count=5,
        article_numbers=("第一条", "第二条", "第三条", "第四条", "第五条"),
        required_field_missing=("effective_on",),
        parse_failures=0,
    )
    assert not report.passed
    assert "missing_required_fields" in report.issues


def test_gate_rejects_sequence_break() -> None:
    gate = LegalCorpusQualityGate()
    report = gate.evaluate(
        article_count=3,
        article_numbers=("第一条", "第三条", "第四条"),
        required_field_missing=(),
        parse_failures=0,
    )
    assert not report.passed
    assert any("sequence_break" in issue for issue in report.issues)


def test_publish_rejected_when_gate_fails() -> None:
    service, port = _service()
    snapshot, report = asyncio.run(
        service.publish(
            batch_id=_BATCH_ID,
            article_count=2,
            article_numbers=("第一条", "第三条"),
            required_field_missing=("effective_on",),
            parse_failures=0,
            manifest={"batch_no": "B1"},
        )
    )
    assert snapshot.state is DatasetState.REJECTED
    assert not report.passed
    assert not port.completed
    # Every gate issue is recorded against the batch (idempotent replace).
    assert len(port.issue_writes) == 1
    batch_id, file_sha256, issues = port.issue_writes[0]
    assert batch_id == _BATCH_ID
    assert file_sha256 == _FILE_SHA
    assert any("article_sequence_break" in issue for issue in issues)
    assert any("missing_required_fields" in issue for issue in issues)


def test_publish_published_when_gate_passes() -> None:
    service, port = _service()
    snapshot, report = asyncio.run(
        service.publish(
            batch_id=_BATCH_ID,
            article_count=3,
            article_numbers=("第一条", "第二条", "第三条"),
            required_field_missing=(),
            parse_failures=0,
            manifest={"batch_no": "B1"},
        )
    )
    assert snapshot.state is DatasetState.PUBLISHED
    assert report.passed
    assert port.completed == [_BATCH_ID]
    # No issues are recorded on the success path.
    assert port.issue_writes == []


def test_publish_failure_without_batch_writes_no_issues() -> None:
    service, port = _service()
    port.batch = None
    snapshot, report = asyncio.run(
        service.publish(
            batch_id=_BATCH_ID,
            article_count=2,
            article_numbers=("第一条", "第三条"),
            required_field_missing=(),
            parse_failures=0,
            manifest={"batch_no": "B1"},
        )
    )
    assert snapshot.state is DatasetState.REJECTED
    assert not report.passed
    assert port.issue_writes == []
