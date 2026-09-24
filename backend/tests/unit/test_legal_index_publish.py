from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

import pytest

from lawyer_agent.application.legal_index_publish import (
    LegalDatasetIndexPublishService,
    LegalDatasetPublishError,
)
from lawyer_agent.application.legal_navigation_index import NavigationBuildResult
from lawyer_agent.domain.legal_corpus import (
    DatasetSnapshot,
    DatasetState,
)
from lawyer_agent.domain.legal_navigation import navigation_index_name
from lawyer_agent.infrastructure.search.opensearch import OpenSearchError

VERSION = UUID("01a06ae2-6200-7000-8000-0000000000c3")
_FIXED_NOW = datetime(2026, 9, 10, 4, 0, tzinfo=UTC)


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


class _Recorder:
    def __init__(self) -> None:
        self.index_calls: list[dict[str, object]] = []
        self.publish_calls: list[tuple[str, str]] = []
        self.events: list[str] = []
        self.navigation_calls: list[tuple[tuple[UUID, ...], str, str]] = []


class _Navigation:
    def __init__(self, recorder: _Recorder, *, fail: bool = False) -> None:
        self.recorder = recorder
        self.fail = fail

    async def build(
        self, *, version_ids: tuple[UUID, ...], main_index_name: str, parser_version: str
    ) -> NavigationBuildResult:
        self.recorder.events.append("navigation")
        self.recorder.navigation_calls.append((version_ids, main_index_name, parser_version))
        if self.fail:
            raise OpenSearchError("navigation verification failed")
        return NavigationBuildResult(navigation_index_name(main_index_name), 2)


class _Indexer:
    def __init__(
        self, recorder: _Recorder, indexed: int = 3, *, fail_write: bool = False
    ) -> None:
        self._recorder = recorder
        self._indexed = indexed
        self._fail_write = fail_write

    async def index_version(
        self,
        *,
        version_id: UUID,
        index_name: str,
        model_ref: str,
        dimension: int,
        batch_size: int,
    ) -> int:
        self._recorder.events.append("main")
        self._recorder.index_calls.append(
            {
                "version_id": version_id,
                "index_name": index_name,
                "model_ref": model_ref,
                "dimension": dimension,
                "batch_size": batch_size,
            }
        )
        if self._fail_write:
            raise OpenSearchError("OpenSearch bulk write was not fully acknowledged")
        return self._indexed


class _Alias:
    def __init__(self, recorder: _Recorder, previous: str | None = "idx_2020") -> None:
        self._recorder = recorder
        self._previous = previous

    async def publish_dataset(
        self, alias: str, index_name: str
    ) -> str | None:
        self._recorder.events.append("alias")
        self._recorder.publish_calls.append((alias, index_name))
        return self._previous


def _service(recorder: _Recorder, *, indexed: int = 3) -> LegalDatasetIndexPublishService:
    return LegalDatasetIndexPublishService(
        navigation=_Navigation(recorder),
        indexer=_Indexer(recorder, indexed=indexed),
        alias=_Alias(recorder),
    )


async def test_publish_indexes_then_points_alias() -> None:
    recorder = _Recorder()
    service = _service(recorder)
    result = await service.publish_version(
        version_id=VERSION,
        index_name="legal_idx_v2",
        alias="dataset_v1",
        model_ref="bge-small-zh",
        dimension=512,
    )
    assert result.index_name == "legal_idx_v2"
    assert result.indexed_documents == 3
    assert result.previous_target == "idx_2020"
    assert len(recorder.index_calls) == 1
    assert recorder.index_calls[0]["version_id"] == VERSION
    assert recorder.index_calls[0]["model_ref"] == "bge-small-zh"
    assert recorder.index_calls[0]["dimension"] == 512
    assert recorder.publish_calls == [("dataset_v1", "legal_idx_v2")]


async def test_publish_passes_batch_size_through() -> None:
    recorder = _Recorder()
    service = _service(recorder)
    await service.publish_version(
        version_id=VERSION,
        index_name="legal_idx_v2",
        alias="dataset_v1",
        model_ref="m",
        dimension=8,
        batch_size=16,
    )
    assert recorder.index_calls[0]["batch_size"] == 16


async def test_publish_refuses_zero_indexed_documents() -> None:
    recorder = _Recorder()
    service = _service(recorder, indexed=0)
    with pytest.raises(LegalDatasetPublishError, match="no documents"):
        await service.publish_version(
            version_id=VERSION,
            index_name="legal_idx_v2",
            alias="dataset_v1",
            model_ref="m",
            dimension=8,
        )
    assert recorder.publish_calls == []  # alias must not be repointed


async def test_publish_first_time_returns_none_previous() -> None:
    recorder = _Recorder()
    alias = _Alias(recorder, previous=None)
    service = LegalDatasetIndexPublishService(
        navigation=_Navigation(recorder),
        indexer=_Indexer(recorder), alias=alias
    )
    result = await service.publish_version(
        version_id=VERSION,
        index_name="legal_idx_v1",
        alias="dataset_v1",
        model_ref="m",
        dimension=8,
    )
    assert result.previous_target is None


async def test_publish_validates_names() -> None:
    recorder = _Recorder()
    service = _service(recorder)
    with pytest.raises(LegalDatasetPublishError, match="index"):
        await service.publish_version(
            version_id=VERSION,
            index_name="Bad Index!",
            alias="dataset_v1",
            model_ref="m",
            dimension=8,
        )
    with pytest.raises(LegalDatasetPublishError, match="alias"):
        await service.publish_version(
            version_id=VERSION,
            index_name="legal_idx_v2",
            alias="dataset v1",
            model_ref="m",
            dimension=8,
        )
    assert recorder.index_calls == [] and recorder.publish_calls == []


async def test_publish_repeat_same_version_is_safe() -> None:
    recorder = _Recorder()
    service = _service(recorder)
    first = await service.publish_version(
        version_id=VERSION,
        index_name="legal_idx_v2",
        alias="dataset_v1",
        model_ref="m",
        dimension=8,
    )
    second = await service.publish_version(
        version_id=VERSION,
        index_name="legal_idx_v2",
        alias="dataset_v1",
        model_ref="m",
        dimension=8,
    )
    assert first.indexed_documents == 3
    assert second.indexed_documents == 3
    assert len(recorder.index_calls) == 2
    assert len(recorder.publish_calls) == 2


class _SnapshotRecorder:
    def __init__(self) -> None:
        self.writes: list[DatasetSnapshot] = []
        self._stored: dict[str, DatasetSnapshot] = {}

    async def find_dataset(self, dataset_name: str) -> DatasetSnapshot | None:
        return self._stored.get(dataset_name)

    async def upsert_dataset(self, snapshot: DatasetSnapshot) -> None:
        self._stored[snapshot.dataset_name] = snapshot
        self.writes.append(snapshot)


def _service_with_snapshot(
    recorder: _Recorder,
    snapshot: _SnapshotRecorder,
    *,
    previous: str | None = "idx_2020",
) -> LegalDatasetIndexPublishService:
    return LegalDatasetIndexPublishService(
        navigation=_Navigation(recorder),
        indexer=_Indexer(recorder),
        alias=_Alias(recorder, previous=previous),
        snapshot=snapshot,
        now=lambda: _FIXED_NOW,
    )


async def test_publish_records_published_snapshot_when_snapshot_port_given() -> None:
    recorder = _Recorder()
    snapshot = _SnapshotRecorder()
    service = _service_with_snapshot(recorder, snapshot)
    result = await service.publish_version(
        version_id=VERSION,
        index_name="legal_idx_v2",
        alias="dataset_v1",
        model_ref="bge-small-zh",
        dimension=512,
    )
    assert result.indexed_documents == 3
    assert len(snapshot.writes) == 1
    recorded = snapshot.writes[0]
    assert recorded.dataset_name == "dataset_v1"
    assert recorded.state is DatasetState.PUBLISHED
    assert recorded.parser_version == "docx-zip-v1"
    assert recorded.released_at == _FIXED_NOW
    assert recorded.manifest == {
        "index_name": "legal_idx_v2",
        "alias": "dataset_v1",
        "version_id": str(VERSION),
        "model_ref": "bge-small-zh",
        "dimension": 512,
        "indexed_documents": 3,
        "navigation_index": navigation_index_name("legal_idx_v2"),
        "navigation_schema_version": 1,
        "navigation_documents": 2,
    }
    assert recorded.quality_metrics == {
        "indexed_documents": 3, "dimension": 512, "navigation_documents": 2
    }


async def test_publish_reuses_existing_snapshot_row_on_repeat() -> None:
    recorder = _Recorder()
    snapshot = _SnapshotRecorder()
    service = _service_with_snapshot(recorder, snapshot)
    first = await service.publish_version(
        version_id=VERSION,
        index_name="legal_idx_v1",
        alias="dataset_v1",
        model_ref="m",
        dimension=8,
    )
    first_row = snapshot.writes[0]
    second = await service.publish_version(
        version_id=VERSION,
        index_name="legal_idx_v2",
        alias="dataset_v1",
        model_ref="m",
        dimension=8,
    )
    assert first.indexed_documents == 3
    assert second.indexed_documents == 3
    assert len(snapshot.writes) == 2
    assert snapshot.writes[1].id == first_row.id
    assert snapshot.writes[1].manifest["index_name"] == "legal_idx_v2"


async def test_publish_without_snapshot_port_writes_nothing() -> None:
    recorder = _Recorder()
    service = _service(recorder)  # legacy constructor: no snapshot port
    await service.publish_version(
        version_id=VERSION,
        index_name="legal_idx_v2",
        alias="dataset_v1",
        model_ref="m",
        dimension=8,
    )
    assert len(recorder.index_calls) == 1
    assert recorder.publish_calls == [("dataset_v1", "legal_idx_v2")]


async def test_publish_zero_indexed_never_writes_snapshot() -> None:
    recorder = _Recorder()
    snapshot = _SnapshotRecorder()
    service = LegalDatasetIndexPublishService(
        navigation=_Navigation(recorder),
        indexer=_Indexer(recorder, indexed=0),
        alias=_Alias(recorder),
        snapshot=snapshot,
        now=lambda: _FIXED_NOW,
    )
    with pytest.raises(LegalDatasetPublishError, match="no documents"):
        await service.publish_version(
            version_id=VERSION,
            index_name="legal_idx_v2",
            alias="dataset_v1",
            model_ref="m",
            dimension=8,
        )
    assert snapshot.writes == []
    assert recorder.publish_calls == []


async def test_publish_name_validation_never_writes_snapshot() -> None:
    recorder = _Recorder()
    snapshot = _SnapshotRecorder()
    service = _service_with_snapshot(recorder, snapshot)
    with pytest.raises(LegalDatasetPublishError, match="index"):
        await service.publish_version(
            version_id=VERSION,
            index_name="Bad Index!",
            alias="dataset_v1",
            model_ref="m",
            dimension=8,
        )
    with pytest.raises(LegalDatasetPublishError, match="alias"):
        await service.publish_version(
            version_id=VERSION,
            index_name="legal_idx_v2",
            alias="dataset v1",
            model_ref="m",
            dimension=8,
        )
    assert snapshot.writes == []


async def test_snapshot_row_uses_new_uuid7_when_none_stored() -> None:
    recorder = _Recorder()
    snapshot = _SnapshotRecorder()
    service = _service_with_snapshot(recorder, snapshot)
    await service.publish_version(
        version_id=VERSION,
        index_name="legal_idx_v1",
        alias="dataset_v1",
        model_ref="m",
        dimension=8,
    )
    recorded = snapshot.writes[0]
    assert recorded.id != VERSION
    # uuid7 carries a version nibble of 7 in the third group.
    assert recorded.id.version == 7


async def test_publish_write_failure_never_switches_alias_or_records_snapshot() -> None:
    recorder = _Recorder()
    snapshot = _SnapshotRecorder()
    service = LegalDatasetIndexPublishService(
        navigation=_Navigation(recorder),
        indexer=_Indexer(recorder, fail_write=True),
        alias=_Alias(recorder),
        snapshot=snapshot,
    )
    with pytest.raises(OpenSearchError, match="not fully acknowledged"):
        await service.publish_version(
            version_id=VERSION,
            index_name="legal_idx_failed",
            alias="dataset_v1",
            model_ref="m",
            dimension=8,
        )
    assert recorder.publish_calls == []
    assert snapshot.writes == []


async def test_navigation_failure_blocks_alias_and_snapshot() -> None:
    recorder = _Recorder()
    snapshot = _SnapshotRecorder()
    service = LegalDatasetIndexPublishService(
        _Indexer(recorder), _Alias(recorder), snapshot,
        navigation=_Navigation(recorder, fail=True),
    )
    with pytest.raises(OpenSearchError, match="navigation verification"):
        await service.publish_version(
            version_id=VERSION, index_name="legal_idx_v2", alias="dataset_v1",
            model_ref="m", dimension=8,
        )
    assert recorder.events == ["main", "navigation"]
    assert recorder.publish_calls == []
    assert snapshot.writes == []


async def test_navigation_precedes_alias_and_snapshot_with_exact_build_scope() -> None:
    recorder = _Recorder()

    class Snapshot(_SnapshotRecorder):
        async def upsert_dataset(self, snapshot: DatasetSnapshot) -> None:
            recorder.events.append("snapshot")
            await super().upsert_dataset(snapshot)

    service = _service_with_snapshot(recorder, Snapshot())
    await service.publish_version(
        version_id=VERSION, index_name="legal_idx_v2", alias="dataset_v1",
        model_ref="m", dimension=8,
    )
    assert recorder.events == ["main", "navigation", "alias", "snapshot"]
    assert recorder.navigation_calls == [((VERSION,), "legal_idx_v2", "docx-zip-v1")]
