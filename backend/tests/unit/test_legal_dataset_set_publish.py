from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4
from weakref import ref

import pytest

from lawyer_agent.application.legal_dataset_set_publish import (
    DatasetSetPublishResult,
    LegalDatasetSetPublishError,
    LegalDatasetSetPublishService,
)
from lawyer_agent.application.legal_navigation_index import NavigationBuildResult
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import (
    ChunkQuality,
    ChunkType,
    DatasetSnapshot,
    DatasetState,
    LegalChunk,
    content_sha256,
)
from lawyer_agent.domain.legal_navigation import navigation_index_name
from lawyer_agent.infrastructure.search.opensearch import OpenSearchError

_FIXED_NOW = datetime(2026, 9, 10, 6, 0, tzinfo=UTC)
DIM = 4

V1 = new_uuid7()
V2 = new_uuid7()


async def test_build_set_returns_pending_candidate_without_publication() -> None:
    search, alias, snapshot = _FakeSearch(), _FakeAlias(), _FakeSnapshot()
    service = _service(_FakeChunks({V1: (_chunk(version_id=V1, text="甲"),)}),
                       search, alias, snapshot)
    candidate = await service.build_set(
        version_ids=(V1,), index_name="candidate_1", alias="dataset_v1",
        model_ref="m", dimension=DIM,
    )
    assert candidate.state is DatasetState.PENDING
    assert candidate.released_at is None
    assert candidate.manifest["version_ids"] == [str(V1)]
    assert candidate.manifest["navigation_documents"] == 4
    assert len(search.append_calls) == 1
    assert alias.calls == []
    assert snapshot.stored == []


@pytest.mark.parametrize("versions, index, alias", [
    ((V1, V1), "candidate_1", "dataset_v1"),
    ((str(V1),), "candidate_1", "dataset_v1"),
    ((uuid4(),), "candidate_1", "dataset_v1"),
    ((V1,), "dataset_v1", "dataset_v1"),
    ((V1,), "candidate_1", "a" * 65),
])
async def test_build_rejects_invalid_scope_before_external_calls(versions, index, alias):
    search, alias_port, gateway = _FakeSearch(), _FakeAlias(), _FakeGateway()
    service = _service(_FakeChunks({V1: (_chunk(version_id=V1, text="甲"),)}),
                       search, alias_port, gateway=gateway)
    with pytest.raises(LegalDatasetSetPublishError):
        await service.build_set(version_ids=versions, index_name=index, alias=alias,
                                model_ref="m", dimension=DIM)
    assert gateway.calls == []
    assert search.ensure_calls == []


async def test_build_rejects_wrong_version_chunks_before_embedding():
    search, alias, gateway = _FakeSearch(), _FakeAlias(), _FakeGateway()
    service = _service(_FakeChunks({V1: (_chunk(version_id=V2, text="wrong scope"),)}),
                       search, alias, gateway=gateway)
    with pytest.raises(LegalDatasetSetPublishError):
        await service.build_set(version_ids=(V1,), index_name="fresh", alias="laws",
                                model_ref="m", dimension=DIM)
    assert gateway.calls == []
    assert search.ensure_calls == []


async def test_build_rejects_uuid4_before_reading_chunks():
    class Chunks:
        async def chunks_for_version(self, version_id):
            pytest.fail("invalid identifier reached repository")
    service = _service(Chunks(), _FakeSearch(), _FakeAlias())
    with pytest.raises(LegalDatasetSetPublishError):
        await service.build_set(version_ids=(uuid4(),), index_name="fresh", alias="laws",
                                model_ref="m", dimension=DIM)


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
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(
        self,
        *,
        model_ref: str,
        texts: list[str],
        dimension: int,
    ) -> tuple:
        self.calls.append(texts)
        return tuple(
            _Vector(tuple(float((index + position) % 7) for position in range(dimension)))
            for index in range(len(texts))
        )


class _Vector:
    def __init__(self, values: tuple[float, ...]) -> None:
        self.values = values


class _FakeSearch:
    def __init__(self, *, fail_write: bool = False) -> None:
        self.ensure_calls: list[tuple[str, int]] = []
        self.replace_calls: list[tuple[str, int, str]] = []
        self.append_calls: list[tuple[str, int, str]] = []
        self.fail_write = fail_write

    async def ensure_index(self, index_name: str, vector_dimension: int) -> None:
        self.ensure_calls.append((index_name, vector_dimension))

    async def replace_documents(
        self,
        index_name: str,
        documents: tuple[dict[str, object], ...],
        parser_version: str,
    ) -> None:
        self.replace_calls.append((index_name, len(documents), parser_version))
        if self.fail_write:
            raise OpenSearchError("OpenSearch bulk write was not fully acknowledged")

    async def append_documents(self, index_name, documents, *, parser_version) -> None:
        self.append_calls.append((index_name, len(documents), parser_version))
        if self.fail_write:
            raise OpenSearchError("OpenSearch bulk write was not fully acknowledged")

    async def count_documents(self, index_name) -> int:
        return sum(count for name, count, _ in self.append_calls if name == index_name)


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


class _Navigation:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[tuple[UUID, ...], str, str]] = []
        self.fail = fail

    async def build(
        self, *, version_ids: tuple[UUID, ...], main_index_name: str, parser_version: str
    ) -> NavigationBuildResult:
        self.calls.append((version_ids, main_index_name, parser_version))
        if self.fail:
            raise OpenSearchError("navigation verification failed")
        return NavigationBuildResult(navigation_index_name(main_index_name), 4)


def _service(
    chunks: _FakeChunks,
    search: _FakeSearch,
    alias: _FakeAlias,
    snapshot: _FakeSnapshot | None = None,
    gateway: _FakeGateway | None = None,
    navigation: _Navigation | None = None,
) -> LegalDatasetSetPublishService:
    return LegalDatasetSetPublishService(
        chunks=chunks,
        gateway=gateway or _FakeGateway(),
        search=search,
        alias=alias,
        snapshot=snapshot,
        navigation=navigation or _Navigation(),
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
    assert search.replace_calls == []
    assert search.append_calls == [("lawyer_set_1", 1, "docx-v1"),
                                   ("lawyer_set_1", 2, "docx-v1")]
    assert alias.calls == [("dataset_v1", "lawyer_set_1")]
    assert len(snapshot.stored) == 1
    stored = snapshot.stored[0]
    assert stored.dataset_name == "dataset_v1"
    assert stored.state is DatasetState.PUBLISHED
    assert stored.manifest["version_ids"] == [str(V1), str(V2)]
    assert stored.manifest["version_count"] == 2
    assert stored.manifest["indexed_documents"] == 3
    assert stored.quality_metrics["indexed_documents"] == 3
    assert stored.manifest["navigation_index"] == navigation_index_name("lawyer_set_1")
    assert stored.manifest["navigation_schema_version"] == 1
    assert stored.manifest["navigation_documents"] == 4
    assert stored.quality_metrics["navigation_documents"] == 4


async def test_set_navigation_failure_blocks_alias_and_snapshot() -> None:
    chunks = _FakeChunks({V1: (_chunk(version_id=V1, text="甲法第一条"),)})
    search, alias, snapshot = _FakeSearch(), _FakeAlias(), _FakeSnapshot()
    navigation = _Navigation(fail=True)
    service = _service(chunks, search, alias, snapshot, navigation=navigation)
    with pytest.raises(OpenSearchError, match="navigation verification"):
        await service.publish_set(
            version_ids=(V1,), index_name="lawyer_set_1", alias="dataset_v1",
            model_ref="m", dimension=DIM,
        )
    assert len(search.append_calls) == 1
    assert alias.calls == []
    assert snapshot.stored == []


async def test_set_navigation_build_has_exact_scope_before_alias_and_snapshot() -> None:
    events: list[str] = []

    class Search(_FakeSearch):
        async def append_documents(self, *args, **kwargs) -> None:
            await super().append_documents(*args, **kwargs)
            events.append("main")

    class Navigation(_Navigation):
        async def build(self, **kwargs) -> NavigationBuildResult:
            result = await super().build(**kwargs)
            events.append("navigation")
            return result

    class Alias(_FakeAlias):
        async def publish_dataset(self, alias: str, index_name: str) -> str | None:
            events.append("alias")
            return await super().publish_dataset(alias, index_name)

    class Snapshot(_FakeSnapshot):
        async def upsert_dataset(self, snapshot: DatasetSnapshot) -> None:
            events.append("snapshot")
            await super().upsert_dataset(snapshot)

    navigation = Navigation()
    service = _service(
        _FakeChunks({V1: (_chunk(version_id=V1, text="甲"),),
                     V2: (_chunk(version_id=V2, text="乙"),)}),
        Search(), Alias(), Snapshot(), navigation=navigation,
    )
    await service.publish_set(
        version_ids=(V1, V2), index_name="lawyer_set_1", alias="dataset_v1",
        model_ref="m", dimension=DIM,
    )
    assert events == ["main", "main", "navigation", "alias", "snapshot"]
    assert navigation.calls == [((V1, V2), "lawyer_set_1", "docx-v1")]


async def test_publish_set_prevalidates_all_versions_and_indexes_only_leaves() -> None:
    parent = _chunk(version_id=V1, text="甲法第一条全文")
    child = replace(
        _chunk(version_id=V1, text="甲法第一款"),
        provision_id=parent.provision_id,
        parent_chunk_id=parent.id,
    )
    broken = replace(
        _chunk(version_id=V2, text="乙法悬空子块"),
        parent_chunk_id=new_uuid7(),
    )
    chunks = _FakeChunks({V1: (parent, child), V2: (broken,)})
    gateway = _FakeGateway()
    search = _FakeSearch()
    alias = _FakeAlias()
    service = _service(chunks, search, alias, gateway=gateway)
    with pytest.raises(ValueError):
        await service.publish_set(
            version_ids=(V1, V2),
            index_name="lawyer_set_1",
            alias="dataset_v1",
            model_ref="yuan",
            dimension=DIM,
        )
    assert gateway.calls == []
    assert search.ensure_calls == []
    assert alias.calls == []


async def test_publish_set_counts_only_leaf_documents_in_snapshot() -> None:
    parent = _chunk(version_id=V1, text="甲法第一条全文")
    child = replace(
        _chunk(version_id=V1, text="甲法第一款"),
        provision_id=parent.provision_id,
        parent_chunk_id=parent.id,
    )
    gateway = _FakeGateway()
    snapshot = _FakeSnapshot()
    search = _FakeSearch()
    service = _service(
        _FakeChunks({V1: (parent, child)}),
        search,
        _FakeAlias(),
        snapshot,
        gateway,
    )
    result = await service.publish_set(
        version_ids=(V1,),
        index_name="lawyer_set_1",
        alias="dataset_v1",
        model_ref="yuan",
        dimension=DIM,
    )
    assert gateway.calls == [["甲法第一款"]]
    assert result.indexed_documents == 1
    assert snapshot.stored[0].manifest["indexed_documents"] == 1


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


async def test_publish_set_write_failure_never_switches_alias_or_records_snapshot() -> None:
    chunks = _FakeChunks({V1: (_chunk(version_id=V1, text="甲法第一条"),)})
    search = _FakeSearch(fail_write=True)
    alias = _FakeAlias()
    snapshot = _FakeSnapshot()
    service = _service(chunks, search, alias, snapshot)

    with pytest.raises(OpenSearchError, match="not fully acknowledged"):
        await service.publish_set(
            version_ids=(V1,),
            index_name="lawyer_set_failed",
            alias="dataset_v1",
            model_ref="test-model",
            dimension=DIM,
        )
    assert alias.calls == []
    assert snapshot.stored == []


async def test_set_writes_each_embedding_batch_before_requesting_the_next() -> None:
    search, alias = _FakeSearch(), _FakeAlias()

    class Gateway(_FakeGateway):
        async def embed(self, **kwargs):
            assert len(search.append_calls) == len(self.calls)
            return await super().embed(**kwargs)

    chunks = _FakeChunks({V1: tuple(_chunk(version_id=V1, text=str(i)) for i in range(5))})
    service = _service(chunks, search, alias, gateway=Gateway())
    result = await service.build_set(
        version_ids=(V1,), index_name="fresh", alias="laws", model_ref="m",
        dimension=DIM, batch_size=2,
    )
    assert [count for _, count, _ in search.append_calls] == [2, 2, 1]
    assert search.replace_calls == []
    assert result.manifest["indexed_documents"] == 5


@pytest.mark.parametrize("count", [0, 1, 3, True, 2.0])
async def test_set_count_mismatch_blocks_navigation_and_publication(count) -> None:
    class Search(_FakeSearch):
        async def count_documents(self, index_name):
            return count

    chunks = _FakeChunks({V1: tuple(_chunk(version_id=V1, text=str(i)) for i in range(2))})
    alias, snapshot, navigation = _FakeAlias(), _FakeSnapshot(), _Navigation()
    service = _service(chunks, Search(), alias, snapshot, navigation=navigation)
    with pytest.raises(LegalDatasetSetPublishError, match="count"):
        await service.publish_set(version_ids=(V1,), index_name="fresh", alias="laws",
                                  model_ref="m", dimension=DIM)
    assert navigation.calls == alias.calls == snapshot.stored == []


async def test_set_rejects_source_change_on_second_read_before_embedding() -> None:
    original = _chunk(version_id=V1, text="original")

    class Chunks:
        reads = 0

        async def chunks_for_version(self, version_id):
            self.reads += 1
            return (original if self.reads == 1 else replace(
                original, content="changed", content_hash=content_sha256("changed"),
            ),)

    gateway, alias = _FakeGateway(), _FakeAlias()
    service = _service(Chunks(), _FakeSearch(), alias, gateway=gateway)
    with pytest.raises(LegalDatasetSetPublishError, match="changed"):
        await service.build_set(version_ids=(V1,), index_name="fresh", alias="laws",
                                model_ref="m", dimension=DIM)
    assert gateway.calls == alias.calls == []


async def test_set_rejects_unbounded_batch_before_reading_or_external_calls() -> None:
    class Chunks:
        async def chunks_for_version(self, version_id):
            pytest.fail("invalid batch size reached source")

    service = _service(Chunks(), _FakeSearch(), _FakeAlias())
    with pytest.raises(LegalDatasetSetPublishError, match="batch_size"):
        await service.build_set(version_ids=(V1,), index_name="fresh", alias="laws",
                                model_ref="m", dimension=DIM, batch_size=257)


async def test_set_releases_previous_version_chunks_before_reading_next() -> None:
    class TrackedChunk(LegalChunk):
        pass

    versions = tuple(new_uuid7() for _ in range(30))
    identifiers = {version: (new_uuid7(), new_uuid7()) for version in versions}

    class Chunks:
        previous = None
        reads = 0

        async def chunks_for_version(self, version_id):
            assert self.previous is None or self.previous() is None
            chunk_id, provision_id = identifiers[version_id]
            chunk = TrackedChunk(
                id=chunk_id, version_id=version_id, provision_id=provision_id,
                chunk_type=ChunkType.PROVISION, quality=ChunkQuality.OK,
                content="合成正文", content_hash=content_sha256("合成正文"),
                parent_chunk_id=None, parser_version="docx-v1",
            )
            self.previous = ref(chunk)
            self.reads += 1
            return (chunk,)

    source = Chunks()
    service = _service(source, _FakeSearch(), _FakeAlias())
    result = await service.build_set(version_ids=versions, index_name="fresh", alias="laws",
                                      model_ref="m", dimension=DIM, batch_size=2)
    assert source.reads == 60
    assert result.manifest["indexed_documents"] == 30


async def test_late_append_failure_stops_later_model_batches_and_publication() -> None:
    class Search(_FakeSearch):
        async def append_documents(self, *args, **kwargs):
            await super().append_documents(*args, **kwargs)
            if len(self.append_calls) == 2:
                raise OpenSearchError("late batch failed")

    source = _FakeChunks({V1: tuple(_chunk(version_id=V1, text=str(i)) for i in range(5))})
    gateway, alias, snapshot, navigation = (
        _FakeGateway(), _FakeAlias(), _FakeSnapshot(), _Navigation(),
    )
    service = _service(source, Search(), alias, snapshot, gateway, navigation)
    with pytest.raises(OpenSearchError, match="late batch"):
        await service.publish_set(version_ids=(V1,), index_name="fresh", alias="laws",
                                  model_ref="m", dimension=DIM, batch_size=2)
    assert len(gateway.calls) == 2
    assert navigation.calls == alias.calls == snapshot.stored == []
