from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest

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


def _evaluate_numbers(numbers: tuple[str, ...], count: int | None = None):
    return LegalCorpusQualityGate().evaluate(
        article_count=len(numbers) if count is None else count,
        article_numbers=numbers, required_field_missing=(), parse_failures=0,
    )


@pytest.mark.parametrize("fullwidth", [False, True])
def test_real_parser_numeric_article_identities_pass_sequence_gate(fullwidth):
    from lawyer_agent.infrastructure.documents.loader import ParsedDocument, ParsedParagraph
    from lawyer_agent.infrastructure.documents.parsers import LegalStructureParser

    labels = tuple(str(number) for number in range(1, 261))
    if fullwidth:
        labels = tuple(label.translate(str.maketrans("0123456789", "０１２３４５６７８９"))
                       for label in labels)
    document = ParsedDocument(
        paragraphs=tuple(ParsedParagraph(f"第{label}条 合成条文。") for label in labels),
        source_ref="file:///synthetic.docx",
    )
    parsed = LegalStructureParser().parse(document)
    identities = tuple(article.provision_no for article in parsed.articles)
    assert identities == labels  # Existing persisted identities must remain unchanged.
    assert _evaluate_numbers(identities).passed


def test_real_parser_preserves_numeric_and_supplement_sequence_contract():
    from lawyer_agent.infrastructure.documents.loader import ParsedDocument, ParsedParagraph
    from lawyer_agent.infrastructure.documents.parsers import LegalStructureParser

    parsed = LegalStructureParser().parse(ParsedDocument(
        paragraphs=tuple(ParsedParagraph(text) for text in (
            "第1条 合成正文。", "第1条之一 合成增补。", "第1条之二 合成增补。",
            "第２条 合成正文。", "第三条 合成正文。",
        )), source_ref="file:///synthetic.docx",
    ))
    identities = tuple(article.provision_no for article in parsed.articles)
    assert identities == ("1", "第1条之一", "第1条之二", "２", "第三条")
    assert _evaluate_numbers(identities).passed


@pytest.mark.parametrize("numbers", [
    ("2",), ("1", "3"), ("1", "1"), ("1", "2", "1"),
    ("1", "第1条"), ("１", "1"), ("0",), ("００",),
    ("1正文",), ("正文1",), (" 1",), ("1\n",), ("1条",), ("1一",),
    ("一",), ("1", "1之一"), ("1", "第1条之二"),
])
def test_legacy_numeric_identity_does_not_relax_sequence_or_text_validation(numbers):
    assert not _evaluate_numbers(numbers).passed


@pytest.mark.parametrize("boundary,marker", [
    (10, "第十条"), (10, "第一十条"), (11, "第一十一条"),
    (99, "第九十九条"), (100, "第一百条"), (101, "第一百零一条"),
    (101, "第一百〇一条"), (110, "第一百一十条"),
    (999, "第九百九十九条"), (1000, "第一千条"),
    (1001, "第一千零一条"), (1010, "第一千零一十条"),
    (1100, "第一千一百条"), (9999, "第九千九百九十九条"),
    (100, "第１００条"),
])
def test_gate_accepts_standard_hundreds_thousands_and_fullwidth(boundary, marker):
    numbers = tuple(f"第{number}条" for number in range(1, boundary)) + (marker,)
    assert _evaluate_numbers(numbers).passed


@pytest.mark.parametrize("numbers", [
    ("第一条", "第一条之一", "第一条之二", "第二条"),
    ("第１条", "第１条之１", "第1条之２", "第二条", "第二条之一"),
    ("第01条", "第02条"),
    ("第０１条", "第０２条"),
    ("第01条", "第01条之01", "第02条"),
    tuple(["第一条"] + [f"第一条之{number}" for number in range(1, 10)]
          + ["第一条之十", "第一条之十一", "第二条"]),
])
def test_gate_accepts_contiguous_supplementary_articles(numbers):
    assert _evaluate_numbers(numbers).passed


@pytest.mark.parametrize("numbers,bad", [
    (("第一条", "第一条", "第二条"), "第一条"),
    (("第一条", "第二条", "第一条"), "第一条"),
    (("第一条", "第三条"), "第三条"),
    (("第二条",), "第二条"),
    (("第一条之一", "第二条"), "第一条之一"),
    (("第一条", "第一条之二"), "第一条之二"),
    (("第一条", "第一条之一", "第一条之一"), "第一条之一"),
    (("第一条", "第二条之一"), "第二条之一"),
    (("第一条", "第二条", "第一条之一"), "第一条之一"),
    (("第一条", "第一条之零"), "第一条之零"),
    (("第一条", "第2条之"), "第2条之"),
    (("unknown",), "unknown"), (("",), ""),
    (("前言第一条",), "前言第一条"), (("第一条正文",), "第一条正文"),
    (("第一条\n",), "第一条\n"), ((" 第1条",), " 第1条"),
    (("第百一条",), "第百一条"), (("第零一条",), "第零一条"),
    (("第一条", "第01条"), "第01条"),
    (("第0条",), "第0条"), (("第00条",), "第00条"),
    (("第００条",), "第００条"), (("第一一条",), "第一一条"),
    (("第1一条",), "第1一条"), (("第零条",), "第零条"),
])
def test_gate_rejects_unrecognized_malformed_or_noncontiguous_markers(numbers, bad):
    report = _evaluate_numbers(numbers)
    assert not report.passed
    assert f"article_sequence_break:{bad}" in report.issues


@pytest.mark.parametrize("count", [1, 3])
def test_gate_rejects_count_different_from_number_list(count):
    report = _evaluate_numbers(("第一条", "第二条"), count)
    assert not report.passed
    assert "article_count_mismatch" in report.issues


@pytest.mark.parametrize("marker,boundary", [
    ("第一百一条", 101), ("第一百零十条", 110),
    ("第一千一条", 1001), ("第一千零百条", 1100),
    ("第一百百条", 100), ("第一百零零一条", 101),
])
def test_gate_does_not_guess_nonstandard_chinese_number_forms(marker, boundary):
    numbers = tuple(f"第{number}条" for number in range(1, boundary)) + (marker,)
    report = _evaluate_numbers(numbers)
    assert not report.passed
    assert f"article_sequence_break:{marker}" in report.issues


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
