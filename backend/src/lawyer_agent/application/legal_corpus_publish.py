"""Quality gate and immutable dataset publishing for the legal corpus."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import (
    DatasetSnapshot,
    DatasetState,
    LoadBatch,
)
from lawyer_agent.domain.legal_dataset_quality import ArticleNumberingReview

_NUMBER_PATTERN = r"[一二三四五六七八九十百千零〇0-9０-９]+"
_ARTICLE_NO = re.compile(rf"第({_NUMBER_PATTERN})条(?:之({_NUMBER_PATTERN}))?")
_LEGACY_NUMERIC_ARTICLE_NO = re.compile(r"[0-9０-９]+")


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

    async def replace_quality_issues(
        self,
        batch_id: UUID,
        file_sha256: bytes,
        issues: tuple[str, ...],
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class QualityReport:
    passed: bool
    metrics: dict[str, Any]
    issues: tuple[str, ...]


def _cn_number(value: str) -> int | None:
    """Parse positive Arabic or standard Chinese ten/hundred/thousand numerals."""
    normalized = value.translate(str.maketrans("０１２３４５６７８９〇", "0123456789零"))
    if re.fullmatch(r"[0-9]+", normalized):
        try:
            number = int(normalized)
            return number if number > 0 else None
        except ValueError:
            return None
    digits = "零一二三四五六七八九"
    units = {"十": 10, "百": 100, "千": 1000}
    total = 0
    section = 0
    for ch in normalized:
        if ch in digits:
            section = digits.index(ch)
        elif ch in units:
            total += (section if section else 1) * units[ch]
            section = 0
        else:
            return None
    total += section
    if not 1 <= total <= 9999:
        return None
    # Round-trip validation prevents guessing omitted/repeated/out-of-order units.
    canonical = ""
    remaining = total
    zero_needed = False
    for unit, suffix in ((1000, "千"), (100, "百"), (10, "十"), (1, "")):
        digit, remaining = divmod(remaining, unit)
        if digit:
            if zero_needed:
                canonical += "零"
            if not (unit == 10 and digit == 1 and not canonical):
                canonical += digits[digit]
            canonical += suffix
            zero_needed = False
        elif canonical and remaining:
            zero_needed = True
    if canonical == normalized or (10 <= total <= 19 and "一" + canonical == normalized):
        return total
    return None


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
        if article_count != len(article_numbers):
            issues.append("article_count_mismatch")
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

    def _sequence_break(
        self,
        numbers: tuple[str, ...],
        *,
        numbering_review: ArticleNumberingReview | None = None,
    ) -> str | None:
        if numbering_review is not None and len(numbers) != (
            numbering_review.last_article - numbering_review.first_article + 1
        ):
            return "reviewed_interval_count"
        current_article = numbering_review.first_article - 1 if numbering_review else 0
        current_supplement = 0
        for item in numbers:
            match = _ARTICLE_NO.fullmatch(item)
            if match is None:
                # The parser retains bare Arabic/fullwidth numbers for ordinary
                # articles. Accept that existing identity without rewriting it.
                if _LEGACY_NUMERIC_ARTICLE_NO.fullmatch(item) is None:
                    return item
                article = _cn_number(item)
                supplement_token = None
            else:
                article = _cn_number(match.group(1))
                supplement_token = match.group(2)
            if article is None:
                return item
            article_token = match.group(1) if match is not None else item
            if numbering_review is not None:
                normalized_token = article_token.translate(
                    str.maketrans("０１２３４５６７８９", "0123456789")
                )
                if (
                    re.fullmatch(r"[0-9]+", normalized_token)
                    and len(normalized_token) > 1
                    and normalized_token.startswith("0")
                ):
                    return item
            if supplement_token is None:
                if article != current_article + 1:
                    return item
                current_article = article
                current_supplement = 0
            else:
                if numbering_review is not None:
                    return item
                supplement = _cn_number(supplement_token)
                if (article != current_article or supplement is None
                        or supplement != current_supplement + 1):
                    return item
                current_supplement = supplement
        if numbering_review is not None and current_article != numbering_review.last_article:
            return "reviewed_interval_end"
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
            await self._record_failure_issues(batch_id, report.issues)
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

    async def _record_failure_issues(
        self, batch_id: UUID | None, issues: tuple[str, ...]
    ) -> None:
        if batch_id is None:
            return
        batch = await self._port.find_batch(batch_id)
        if batch is None:
            return
        await self._port.replace_quality_issues(
            batch_id,
            batch.file_sha256,
            tuple(issue for issue in issues if issue),
        )
