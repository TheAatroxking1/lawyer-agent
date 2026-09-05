from __future__ import annotations

from datetime import UTC, datetime

import pytest

from lawyer_agent.application.legal_dataset_set_publish import (
    DatasetSetPublishResult,
    LegalDatasetSetPublishError,
    LegalDatasetSetPublishService,
)
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import (
    ChunkQuality,
    ChunkType,
    DatasetSnapshot,
    DatasetState,
    LegalChunk,
    content_sha256,
)

_FIXED_NOW = datetime(2026, 9, 10, 6, 0, tzinfo=UTC)
DIM = 4

V1 = new_uuid7()
V2 = new_uuid7()


def _chunk(*, version_id: object, text: str) -> LegalChunk:
    return LegalChunk(
        id=new_uuid7(),
        version_id=version_id,
        provision_id=new_uuid7(),
        chunk_type=ChunkType.PROVISION,
        quality=ChunkQuality.OK,
        content=text,
        content_hash=content_sha256(text),
        parent_chunk_id=None,
        parser_version="docx-v1",
    )


class _FakeChunks:
    def __init__(self, per_version: dict[object, tuple[LegalChunk, ...]]) -> None:
        self._per = per_version

    async def chunks_for_version(self, version_id: object) -> tuple[LegalChunk, ...]:
        return self._per.get(version_id, ())


class _FakeGateway:
    async def embed(
        self,
        *,
        model_ref: str,
        texts: list[str],
        dimension: int,
    ) -> tuple:
        return tuple(
            _Vector(tuple(float((index + position) % 7) for position in range(dimension)))
            for index in range(len(texts))
        )


class _Vector:
    def __init__(self, values: tuple[float, ...]) -> None:
        self.values = values


class _FakeSearch:
    def __init__(self) -> None:
        self.ensure_calls: list[tuple[str, int]] = []
        self.replace_calls: list[tuple[str, int, str]] = []

    async def ensure_index(self, index_name: str, vector_dimension: int) -> None:
        self.ensure_calls.append((index_name, vector_dimension))

    async def replace_documents(
        self,
        index_name: str,
        documents: tuple[dict[str, object], ...],
        parser_version: str,
    ) -> None:
        self.replace_calls.append((index_name, len(documents), parser_version))


class _FakeAlias:
    def __init__(self, previous: str | None = None) -> None:
        self.previous = previous
        self.calls: list[tuple[str, str]] = []

    async def publish_dataset(
        self, alias: str, index_name: str
    ) -> str | None:
        self.calls.append((alias, index_name))
        return self.previous


class _FakeSnapshot:
    def __init__(self) -> None:
        self.stored: list[DatasetSnapshot] = []

    async def find_dataset(self, dataset_name: str) -> DatasetSnapshot | None:
        del dataset_name
        return None

    async def upsert_dataset(self, snapshot: DatasetSnapshot) -> None:
        self.stored.append(snapshot)


def _service(
    chunks: _FakeChunks,
    search: _FakeSearch,
    alias: _FakeAlias,
    snapshot: _FakeSnapshot | None = None,
) -> LegalDatasetSetPublishService:
    return LegalDatasetSetPublishService(
        chunks=chunks,
        gateway=_FakeGateway(),
        search=search,
        alias=alias,
        snapshot=snapshot,
        dataset_parser_version="docx-v1",
        now=lambda: _FIXED_NOW,
    )


async def test_publish_set_merges_two_versions_into_one_index() -> None:
    chunks = _FakeChunks(
        {
            V1: (_chunk(version_id=V1, text="甲法第一条"),),
            V2: (
                _chunk(version_id=V2, text="乙法第一条"),
                _chunk(version_id=V2, text="乙法第二条"),
            ),
        }
    )
    search = _FakeSearch()
    alias = _FakeAlias(previous="old-index")
    snapshot = _FakeSnapshot()
    service = _service(chunks, search, alias, snapshot)
    result = await service.publish_set(
        version_ids=(V1, V2),
        index_name="lawyer_set_1",
        alias="dataset_v1",
        model_ref="yuan",
        dimension=DIM,
        batch_size=2,
    )
    assert isinstance(result, DatasetSetPublishResult)
    assert result.index_name == "lawyer_set_1"
    assert result.indexed_documents == 3
    assert result.previous_target == "old-index"
    assert result.version_count == 2
    assert search.ensure_calls == [("lawyer_set_1", DIM)]
    assert search.replace_calls == [("lawyer_set_1", 3, "docx-v1")]
    assert alias.calls == [("dataset_v1", "lawyer_set_1")]
    assert len(snapshot.stored) == 1
    stored = snapshot.stored[0]
    assert stored.dataset_name == "dataset_v1"
    assert stored.state is DatasetState.PUBLISHED
    assert stored.manifest["version_ids"] == [str(V1), str(V2)]
    assert stored.manifest["version_count"] == 2
    assert stored.manifest["indexed_documents"] == 3
    assert stored.quality_metrics["indexed_documents"] == 3


async def test_publish_set_rejects_empty_version_without_touching_search() -> None:
    chunks = _FakeChunks({V1: ()})
    search = _FakeSearch()
    alias = _FakeAlias()
    service = _service(chunks, search, alias)
    with pytest.raises(LegalDatasetSetPublishError, match="no chunks"):
        await service.publish_set(
            version_ids=(V1,),
            index_name="lawyer_set_1",
            alias="dataset_v1",
            model_ref="yuan",
            dimension=DIM,
        )
    assert search.ensure_calls == []
    assert search.replace_calls == []
    assert alias.calls == []


async def test_publish_set_requires_a_version_set_and_validates_names() -> None:
    chunks = _FakeChunks({V1: (_chunk(version_id=V1, text="甲法第一条"),)})
    search = _FakeSearch()
    alias = _FakeAlias()
    service = _service(chunks, search, alias)
    with pytest.raises(LegalDatasetSetPublishError, match="version_ids"):
        await service.publish_set(
            version_ids=(),
            index_name="lawyer_set_1",
            alias="dataset_v1",
            model_ref="yuan",
            dimension=DIM,
        )
    with pytest.raises(LegalDatasetSetPublishError, match="dimension"):
        await service.publish_set(
            version_ids=(V1,),
            index_name="lawyer_set_1",
            alias="dataset_v1",
            model_ref="yuan",
            dimension=0,
        )
    with pytest.raises(LegalDatasetSetPublishError, match="index_name"):
        await service.publish_set(
            version_ids=(V1,),
            index_name="Bad Name",
            alias="dataset_v1",
            model_ref="yuan",
            dimension=DIM,
        )


async def test_publish_set_without_snapshot_keeps_legacy_behaviour() -> None:
    chunks = _FakeChunks({V1: (_chunk(version_id=V1, text="甲法第一条"),)})
    search = _FakeSearch()
    alias = _FakeAlias()
    service = _service(chunks, search, alias, snapshot=None)
    result = await service.publish_set(
        version_ids=(V1,),
        index_name="lawyer_set_1",
        alias="dataset_v1",
        model_ref="yuan",
        dimension=DIM,
    )
    assert result.indexed_documents == 1
    assert alias.calls == [("dataset_v1", "lawyer_set_1")]
