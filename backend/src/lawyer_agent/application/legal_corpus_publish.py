"""Quality gate and immutable dataset publishing for the legal corpus."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import DatasetSnapshot, DatasetState, LoadBatch

_ARTICLE_NO = re.compile(r"第([一二三四五六七八九十百千零〇0-9０-９]+)条")


class LegalCorpusPublishPort(Protocol):
    async def find_dataset(self, dataset_name: str) -> DatasetSnapshot | None: ...

    async def upsert_dataset(self, snapshot: DatasetSnapshot) -> None: ...

    async def find_batch(self, batch_id: UUID) -> LoadBatch | None: ...

    async def complete_batch(
        self,
        batch_id: UUID,
        *,
        item_counts: dict[str, int],
        now: datetime,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class QualityReport:
    passed: bool
    metrics: dict[str, Any]
    issues: tuple[str, ...]


def _cn_number(value: str) -> int:
    digits = {
        "零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4,
        "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    }
    if value.isdigit() or all(ch in "０１２３４５６７８９" for ch in value):
        normalized = value.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
        return int(normalized)
    if value in {"十", "10"}:
        return 10
    total = 0
    section = 0
    for ch in value:
        if ch in digits:
            section = digits[ch]
        elif ch == "十":
            total += (section if section else 1) * 10
            section = 0
    return total + section


class LegalCorpusQualityGate:
    """Rejects publishing when coverage or structural quality is unacceptable."""

    def __init__(
        self,
        *,
        min_coverage: float = 0.95,
        parser_version: str = "docx-zip-v1",
    ) -> None:
        if not 0.0 < min_coverage <= 1.0:
            raise ValueError("minimum coverage must be between 0 and 1")
        self._min_coverage = min_coverage
        self._parser_version = parser_version

    @property
    def parser_version(self) -> str:
        return self._parser_version

    def evaluate(
        self,
        *,
        article_count: int,
        article_numbers: tuple[str, ...],
        required_field_missing: tuple[str, ...],
        parse_failures: int,
    ) -> QualityReport:
        issues: list[str] = []
        if article_count <= 0:
            issues.append("no_articles")
        coverage = (
            1.0
            if article_count + parse_failures == 0
            else article_count / (article_count + parse_failures)
        )
        if coverage < self._min_coverage:
            issues.append("coverage_below_threshold")
        if required_field_missing:
            issues.append("missing_required_fields")
        sequence_broken = self._sequence_break(article_numbers)
        if sequence_broken is not None:
            issues.append(f"article_sequence_break:{sequence_broken}")
        metrics = {
            "article_count": article_count,
            "coverage": round(coverage, 4),
            "parse_failures": parse_failures,
            "required_field_missing": list(required_field_missing),
        }
        return QualityReport(passed=not issues, metrics=metrics, issues=tuple(issues))

    def _sequence_break(self, numbers: tuple[str, ...]) -> str | None:
        parsed: list[int | None] = []
        for item in numbers:
            match = _ARTICLE_NO.search(item)
            parsed.append(_cn_number(match.group(1)) if match else None)
        for index, value in enumerate(parsed):
            if value is not None and value != index + 1:
                return numbers[index]
        return None


class LegalCorpusPublishService:
    """Publishes dataset_v1 only after the quality gate passes."""

    def __init__(
        self,
        port: LegalCorpusPublishPort,
        gate: LegalCorpusQualityGate,
        dataset_name: str = "dataset_v1",
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._port = port
        self._gate = gate
        self._dataset_name = dataset_name
        self._now = now if now is not None else lambda: datetime.now(UTC)

    async def publish(
        self,
        *,
        batch_id: UUID,
        article_count: int,
        article_numbers: tuple[str, ...],
        required_field_missing: tuple[str, ...],
        parse_failures: int,
        manifest: dict[str, Any],
    ) -> tuple[DatasetSnapshot, QualityReport]:
        report = self._gate.evaluate(
            article_count=article_count,
            article_numbers=article_numbers,
            required_field_missing=required_field_missing,
            parse_failures=parse_failures,
        )
        if not report.passed:
            snapshot = DatasetSnapshot(
                id=new_uuid7(),
                dataset_name=self._dataset_name,
                parser_version=self._gate.parser_version,
                state=DatasetState.REJECTED,
                manifest=manifest,
                quality_metrics=report.metrics,
            )
            await self._port.upsert_dataset(snapshot)
            return snapshot, report

        now = self._now()
        existing = await self._port.find_dataset(self._dataset_name)
        state = (
            DatasetState.SUPERSEDED
            if existing is not None and existing.state is DatasetState.PUBLISHED
            else DatasetState.PUBLISHED
        )
        published = DatasetSnapshot(
            id=existing.id if existing is not None else new_uuid7(),
            dataset_name=self._dataset_name,
            parser_version=self._gate.parser_version,
            state=state,
            manifest=manifest,
            quality_metrics=report.metrics,
            released_at=now,
        )
        await self._port.upsert_dataset(published)
        if batch_id is not None:
            await self._port.complete_batch(
                batch_id,
                item_counts={"articles": article_count},
                now=now,
            )
        return published, report
