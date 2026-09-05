"""Legal dataset index publish orchestration (non-model glue).

spec 6.6: build a fresh OpenSearch index for a dataset, then atomically
re-point the dataset alias so readers get the new current index while older
indexes remain for history and fast rollback. This service composes the
vector indexing service (version -> index) with the dataset alias service
(alias -> index). It never moves data, never writes MySQL and never publishes
an empty index under an alias.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import DatasetSnapshot, DatasetState

_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,254}$")


class LegalDatasetPublishError(ValueError):
    """Stable error for dataset index publish orchestration."""


@dataclass(frozen=True, slots=True)
class DatasetPublishResult:
    index_name: str
    indexed_documents: int
    previous_target: str | None


class _IndexerPort(Protocol):
    async def index_version(
        self,
        *,
        version_id: UUID,
        index_name: str,
        model_ref: str,
        dimension: int,
        batch_size: int,
    ) -> int: ...


class _AliasPort(Protocol):
    async def publish_dataset(
        self, alias: str, index_name: str
    ) -> str | None: ...


class DatasetSnapshotWritePort(Protocol):
    async def find_dataset(self, dataset_name: str) -> DatasetSnapshot | None: ...

    async def upsert_dataset(self, snapshot: DatasetSnapshot) -> None: ...


class LegalDatasetIndexPublishService:
    """Indexes a version and points the dataset alias at the new index.

    Optionally records a PUBLISHED dataset snapshot row after a successful
    alias switch (see spec 6.6: a human-approved release becomes the new
    dataset version that the read/inventory layer can list). Recording is
    purely additive: callers that construct the service without a snapshot
    port keep the exact legacy behaviour.
    """

    def __init__(
        self,
        indexer: _IndexerPort,
        alias: _AliasPort,
        snapshot: DatasetSnapshotWritePort | None = None,
        *,
        dataset_parser_version: str = "docx-zip-v1",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not hasattr(indexer, "index_version"):
            raise ValueError("dataset publish requires a vector indexing service")
        if not hasattr(alias, "publish_dataset"):
            raise ValueError("dataset publish requires an alias service")
        if not isinstance(dataset_parser_version, str) or not dataset_parser_version.strip():
            raise ValueError("dataset parser version must be non-empty text")
        self._indexer = indexer
        self._alias = alias
        self._snapshot = snapshot
        self._parser_version = dataset_parser_version
        self._now = now if now is not None else lambda: datetime.now(UTC)

    async def publish_version(
        self,
        *,
        version_id: UUID,
        index_name: str,
        alias: str,
        model_ref: str,
        dimension: int,
        batch_size: int = 64,
    ) -> DatasetPublishResult:
        _require_name(index_name, "index_name")
        _require_name(alias, "alias")
        if (
            not isinstance(dimension, int)
            or isinstance(dimension, bool)
            or dimension <= 0
        ):
            raise LegalDatasetPublishError("dimension must be a positive integer")
        if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
            raise LegalDatasetPublishError("batch_size must be a positive integer")
        if not isinstance(model_ref, str) or not model_ref.strip():
            raise LegalDatasetPublishError("model_ref must be non-empty text")

        indexed = await self._indexer.index_version(
            version_id=version_id,
            index_name=index_name,
            model_ref=model_ref,
            dimension=dimension,
            batch_size=batch_size,
        )
        if indexed == 0:
            raise LegalDatasetPublishError(
                "no documents indexed; refusing to publish an empty dataset "
                "under the alias"
            )
        previous = await self._alias.publish_dataset(alias, index_name)
        if self._snapshot is not None:
            await self._record_snapshot(
                alias=alias,
                index_name=index_name,
                version_id=version_id,
                model_ref=model_ref,
                dimension=dimension,
                indexed_documents=indexed,
            )
        return DatasetPublishResult(
            index_name=index_name,
            indexed_documents=indexed,
            previous_target=previous,
        )

    async def _record_snapshot(
        self,
        *,
        alias: str,
        index_name: str,
        version_id: UUID,
        model_ref: str,
        dimension: int,
        indexed_documents: int,
    ) -> None:
        assert self._snapshot is not None
        existing = await self._snapshot.find_dataset(alias)
        snapshot = DatasetSnapshot(
            id=existing.id if existing is not None else new_uuid7(),
            dataset_name=alias,
            parser_version=self._parser_version,
            state=DatasetState.PUBLISHED,
            manifest={
                "index_name": index_name,
                "alias": alias,
                "version_id": str(version_id),
                "model_ref": model_ref,
                "dimension": dimension,
                "indexed_documents": indexed_documents,
            },
            quality_metrics={
                "indexed_documents": indexed_documents,
                "dimension": dimension,
            },
            released_at=self._now(),
        )
        await self._snapshot.upsert_dataset(snapshot)


def _require_name(value: str, field: str) -> None:
    if not isinstance(value, str) or _NAME.fullmatch(value) is None:
        raise LegalDatasetPublishError(
            f"{field} must be 1-255 chars of [a-z0-9_.-] starting alphanumeric"
        )
