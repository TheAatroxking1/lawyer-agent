from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import (
    DatasetSnapshot,
    DatasetState,
    LoadBatch,
    LoadStatus,
)
from lawyer_agent.infrastructure.persistence.models.legal_corpus import (
    LegalDatasetSnapshotModel,
    LegalLoadBatchModel,
    LegalQualityIssueModel,
)

_LOAD_STATUS_MAP = {
    "inventoried": LoadStatus.INVENTORIED,
    "parsing": LoadStatus.PARSING,
    "completed": LoadStatus.COMPLETED,
    "failed": LoadStatus.FAILED,
}
_DATASET_STATE_MAP = {
    "pending": DatasetState.PENDING,
    "published": DatasetState.PUBLISHED,
    "superseded": DatasetState.SUPERSEDED,
    "rejected": DatasetState.REJECTED,
}


def _load_batch(model: LegalLoadBatchModel) -> LoadBatch:
    return LoadBatch(
        id=model.id,
        batch_no=model.batch_no,
        source_ref=model.source_ref,
        file_sha256=bytes(model.file_sha256),
        parser_version=model.parser_version,
        status=_LOAD_STATUS_MAP[model.status],
        item_counts=model.item_counts_json,
        started_at=_aware(model.started_at),
        completed_at=_aware_optional(model.completed_at),
        error_message=model.error_message,
    )


def _dataset(model: LegalDatasetSnapshotModel) -> DatasetSnapshot:
    return DatasetSnapshot(
        id=model.id,
        dataset_name=model.dataset_name,
        parser_version=model.parser_version,
        state=_DATASET_STATE_MAP[model.state],
        manifest=model.manifest_json,
        quality_metrics=model.quality_metrics_json,
        released_at=_aware_optional(model.released_at),
    )


class SqlAlchemyLegalCorpusInventoryRepository:
    """Load batches and dataset snapshots."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_batch_by_sha256(self, file_sha256: bytes) -> LoadBatch | None:
        model = await self._session.scalar(
            select(LegalLoadBatchModel).where(
                LegalLoadBatchModel.file_sha256 == file_sha256
            )
        )
        return None if model is None else _load_batch(model)

    async def create_load_batch(self, batch: LoadBatch) -> None:
        self._session.add(_batch_model(batch))
        await self._session.flush()

    async def find_dataset(self, dataset_name: str) -> DatasetSnapshot | None:
        model = await self._session.scalar(
            select(LegalDatasetSnapshotModel).where(
                LegalDatasetSnapshotModel.dataset_name == dataset_name
            )
        )
        return None if model is None else _dataset(model)

    async def upsert_dataset(self, snapshot: DatasetSnapshot) -> None:
        model = await self._session.scalar(
            select(LegalDatasetSnapshotModel).where(
                LegalDatasetSnapshotModel.dataset_name == snapshot.dataset_name
            )
        )
        if model is None:
            self._session.add(_dataset_model(snapshot))
        else:
            model.parser_version = snapshot.parser_version
            model.state = snapshot.state.value
            model.manifest_json = snapshot.manifest
            model.quality_metrics_json = snapshot.quality_metrics
            model.released_at = _naive_optional(snapshot.released_at)
        await self._session.flush()

    async def find_batch(self, batch_id: UUID) -> LoadBatch | None:
        model = await self._session.scalar(
            select(LegalLoadBatchModel).where(LegalLoadBatchModel.id == batch_id)
        )
        return None if model is None else _load_batch(model)

    async def complete_batch(
        self,
        batch_id: UUID,
        *,
        item_counts: dict[str, int],
        now: datetime,
    ) -> bool:
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(LegalLoadBatchModel)
                .where(
                    LegalLoadBatchModel.id == batch_id,
                    LegalLoadBatchModel.status == "inventoried",
                )
                .values(
                    status="completed",
                    item_counts_json=item_counts,
                    completed_at=_naive_optional(now),
                )
            ),
        )
        return result.rowcount == 1

    async def replace_quality_issues(
        self,
        batch_id: UUID,
        file_sha256: bytes,
        issues: tuple[str, ...],
    ) -> None:
        """Atomically replace a batch's quality issue rows (idempotent)."""
        if not isinstance(issues, tuple):
            raise ValueError("quality issues must be a tuple of text")
        if file_sha256 is None or len(file_sha256) != 32:
            raise ValueError("quality issue file sha256 must be 32 bytes")
        await self._session.execute(
            delete(LegalQualityIssueModel).where(
                LegalQualityIssueModel.batch_id == batch_id
            )
        )
        for issue in issues:
            if not isinstance(issue, str) or not issue:
                continue
            issue_type = issue.split(":", 1)[0].strip() or "quality_issue"
            self._session.add(
                LegalQualityIssueModel(
                    id=new_uuid7(),
                    batch_id=batch_id,
                    file_sha256=file_sha256,
                    issue_type=issue_type[:64],
                    message=issue[:2048],
                )
            )
        await self._session.flush()


def _batch_model(batch: LoadBatch) -> LegalLoadBatchModel:
    return LegalLoadBatchModel(
        id=batch.id,
        batch_no=batch.batch_no,
        source_ref=batch.source_ref,
        file_sha256=batch.file_sha256,
        parser_version=batch.parser_version,
        status=batch.status.value,
        item_counts_json=batch.item_counts,
        started_at=_naive_optional(batch.started_at),
        completed_at=_naive_optional(batch.completed_at),
        error_message=batch.error_message,
    )


def _dataset_model(snapshot: DatasetSnapshot) -> LegalDatasetSnapshotModel:
    return LegalDatasetSnapshotModel(
        id=snapshot.id,
        dataset_name=snapshot.dataset_name,
        parser_version=snapshot.parser_version,
        state=snapshot.state.value,
        manifest_json=snapshot.manifest,
        quality_metrics_json=snapshot.quality_metrics,
        released_at=_naive_optional(snapshot.released_at),
    )


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    from datetime import UTC

    return value.replace(tzinfo=UTC)


def _aware_optional(value: datetime | None) -> datetime | None:
    return _aware(value)


def _naive_optional(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)
