from __future__ import annotations

from typing import Protocol
from uuid import UUID

import pytest

from lawyer_agent.application.legal_index_publish import (
    LegalDatasetIndexPublishService,
    LegalDatasetPublishError,
)

VERSION = UUID("01a06ae2-6200-7000-8000-0000000000c3")


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


class _Indexer:
    def __init__(self, recorder: _Recorder, indexed: int = 3) -> None:
        self._recorder = recorder
        self._indexed = indexed

    async def index_version(
        self,
        *,
        version_id: UUID,
        index_name: str,
        model_ref: str,
        dimension: int,
        batch_size: int,
    ) -> int:
        self._recorder.index_calls.append(
            {
                "version_id": version_id,
                "index_name": index_name,
                "model_ref": model_ref,
                "dimension": dimension,
                "batch_size": batch_size,
            }
        )
        return self._indexed


class _Alias:
    def __init__(self, recorder: _Recorder, previous: str | None = "idx_2020") -> None:
        self._recorder = recorder
        self._previous = previous

    async def publish_dataset(
        self, alias: str, index_name: str
    ) -> str | None:
        self._recorder.publish_calls.append((alias, index_name))
        return self._previous


def _service(recorder: _Recorder, *, indexed: int = 3) -> LegalDatasetIndexPublishService:
    return LegalDatasetIndexPublishService(
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
