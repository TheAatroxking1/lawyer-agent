from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from lawyer_agent.application.legal_navigation_index import LegalNavigationIndexService
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import (
    LegalInstrument,
    LegalVersion,
    LegalVersionStatus,
    Provision,
    ProvisionLevel,
    content_sha256,
)
from lawyer_agent.domain.legal_navigation import NavigationDocument, navigation_index_name


class Source:
    def __init__(self) -> None:
        self.instrument = LegalInstrument(new_uuid7(), "合成民法典", "合成机关", "CN")
        self.version = LegalVersion(
            new_uuid7(),
            self.instrument.id,
            "v1",
            LegalVersionStatus.STATUS_UNKNOWN,
            None,
            None,
            None,
        )
        self.provisions = (self.provision(("第一编 总则", "第一章 基本规定", "第一节 范围")),)
        self.versions = {self.version.id: (self.version, self.instrument)}
        self.reads: list[UUID] = []

    def provision(self, path: tuple[str, ...]) -> Provision:
        return Provision(
            new_uuid7(),
            self.version.id,
            "第一条",
            ProvisionLevel.ARTICLE,
            path,
            None,
            "合成条文",
            content_sha256("合成条文"),
            0,
            4,
        )

    async def version_with_instrument(self, version_id: UUID):
        self.reads.append(version_id)
        return self.versions.get(version_id)

    async def provisions_for_version(self, version_id: UUID):
        return self.provisions


class Search:
    def __init__(self, *, count: object = None, fail: str | None = None) -> None:
        self.events: list[str] = []
        self.documents: tuple[NavigationDocument, ...] = ()
        self.count = count
        self.fail = fail

    def record(self, event: str) -> None:
        self.events.append(event)
        if self.fail == event:
            raise RuntimeError("synthetic service failure")

    async def ensure_navigation_index(self, index_name: str) -> None:
        self.record("ensure")

    async def replace_navigation_documents(self, index_name, documents, *, parser_version):
        pytest.fail("new navigation builds must not delete earlier batches")

    async def append_navigation_documents(self, index_name, documents, *, parser_version):
        self.record("append")
        assert len(documents) <= 256
        self.documents += documents
        assert all(doc.parser_version == parser_version for doc in documents)

    async def count_documents(self, index_name: str):
        self.record("count")
        return len(self.documents) if self.count is None else self.count

    async def mark_navigation_ready(self, main_index_name: str) -> None:
        assert main_index_name == "main-a"
        self.record("marker")


async def build(source, search, **kwargs):
    params = dict(
        version_ids=(source.version.id,), main_index_name="main-a", parser_version="nav-v1"
    )
    params.update(kwargs)
    return await LegalNavigationIndexService(source, search).build(**params)


async def test_build_deduplicates_prefixes_and_preserves_full_path_identity() -> None:
    source, search = Source(), Search()
    source.provisions += (
        source.provision(("第一编 总则", "第一章 基本规定", "第二节 其它")),
        source.provision(("第二编 物权", "第一章 基本规定")),
        source.provision(("未知节点",)),
    )
    result = await build(source, search)
    assert result.index_name == navigation_index_name("main-a")
    assert result.indexed_documents == 5
    assert search.events == ["ensure", "append", "count", "marker"]
    assert [doc.locator for doc in search.documents] == [
        "合成民法典",
        "第一编 总则",
        "第一章 基本规定",
        "第二编 物权",
        "第一章 基本规定",
    ]
    assert "合成民法典" in search.documents[2].content
    assert "第一编 总则" in search.documents[2].content
    assert "第一章 基本规定" in search.documents[2].content
    assert len({doc.navigation_id for doc in search.documents}) == 5
    again = Search()
    source.provisions = tuple(reversed(source.provisions))
    await build(source, again, parser_version="nav-v2")
    assert {doc.navigation_id for doc in again.documents} == {
        doc.navigation_id for doc in search.documents
    }


async def test_build_preflights_later_missing_version_before_any_write() -> None:
    source, search = Source(), Search()
    missing = new_uuid7()
    with pytest.raises(ValueError):
        await build(source, search, version_ids=(source.version.id, missing))
    assert source.reads == [source.version.id, missing]
    assert search.events == []


async def test_build_keeps_same_title_in_distinct_versions() -> None:
    first, second, search = Source(), Source(), Search()

    class MultipleSources:
        async def version_with_instrument(self, version_id: UUID):
            source = first if version_id == first.version.id else second
            return await source.version_with_instrument(version_id)

        async def provisions_for_version(self, version_id: UUID):
            source = first if version_id == first.version.id else second
            return await source.provisions_for_version(version_id)

    result = await LegalNavigationIndexService(MultipleSources(), search).build(
        version_ids=(first.version.id, second.version.id),
        main_index_name="main-a",
        parser_version="nav-v1",
    )
    assert result.indexed_documents == 6
    assert len({document.navigation_id for document in search.documents}) == 6
    assert {document.version_id for document in search.documents} == {
        first.version.id,
        second.version.id,
    }


async def test_source_failure_propagates_before_any_write() -> None:
    class UnavailableSource(Source):
        async def provisions_for_version(self, version_id: UUID):
            raise RuntimeError("synthetic source unavailable")

    search = Search()
    with pytest.raises(RuntimeError, match="synthetic source unavailable"):
        await build(UnavailableSource(), search)
    assert search.events == []


@pytest.mark.parametrize(
    "damage",
    ["wrong_version", "wrong_instrument", "wrong_provision", "empty", "blank_node", "nontext_node"],
)
async def test_build_rejects_invalid_source_before_writes(damage: str) -> None:
    source, search = Source(), Search()
    if damage == "wrong_version":
        source.versions[source.version.id] = (
            replace(source.version, id=new_uuid7()),
            source.instrument,
        )
    elif damage == "wrong_instrument":
        source.versions[source.version.id] = (
            source.version,
            replace(source.instrument, id=new_uuid7()),
        )
    elif damage == "wrong_provision":
        source.provisions = (replace(source.provisions[0], version_id=new_uuid7()),)
    elif damage == "empty":
        source.provisions = ()
    else:
        node = " " if damage == "blank_node" else 3
        source.provisions = (replace(source.provisions[0], structure_path=(node,)),)
    with pytest.raises(ValueError):
        await build(source, search)
    assert search.events == []


@pytest.mark.parametrize(
    "params",
    [
        {"version_ids": ()},
        {"version_ids": [new_uuid7()]},
        {"version_ids": (uuid4(),)},
        {"parser_version": ""},
        {"main_index_name": "a*"},
    ],
)
async def test_build_rejects_invalid_arguments_before_source_reads(params) -> None:
    source, search = Source(), Search()
    with pytest.raises(ValueError):
        await build(source, search, **params)
    assert source.reads == []
    assert search.events == []


async def test_build_rejects_duplicate_versions() -> None:
    source, search = Source(), Search()
    with pytest.raises(ValueError):
        await build(source, search, version_ids=(source.version.id, source.version.id))
    assert source.reads == []


@pytest.mark.parametrize("count", [0, 2, 4, True, 3.0, "3", -1])
async def test_count_must_exactly_confirm_all_documents(count) -> None:
    source, search = Source(), Search(count=count)
    with pytest.raises(RuntimeError):
        await build(source, search)
    assert search.events == ["ensure", "append", "count"]


@pytest.mark.parametrize("stage", ["ensure", "append", "count"])
async def test_build_does_not_mark_ready_after_service_failure(stage: str) -> None:
    search = Search(fail=stage)
    with pytest.raises(RuntimeError, match="synthetic service failure"):
        await build(Source(), search)
    assert "marker" not in search.events


async def test_navigation_batches_large_version_without_replacing_prior_documents():
    class CountingSearch(Search):
        def __init__(self):
            super().__init__()
            self.written = 0
        async def append_navigation_documents(self, index_name, documents, *, parser_version):
            assert 1 <= len(documents) <= 256
            self.record("append")
            self.written += len(documents)
        async def count_documents(self, index_name):
            self.record("count")
            return self.written
    source, search = Source(), CountingSearch()
    source.provisions = tuple(source.provision((f"第{number}章 合成",)) for number in range(600))
    result = await build(source, search)
    assert result.indexed_documents == 601
    assert search.events == ["ensure", "append", "append", "append", "count", "marker"]
    assert source.reads == [source.version.id, source.version.id]


@pytest.mark.parametrize("change", ["title", "parser", "provision"])
async def test_navigation_reread_drift_blocks_append_and_marker(change):
    class ChangingSource(Source):
        async def version_with_instrument(self, version_id):
            if self.reads:
                if change == "title":
                    self.versions[version_id] = (self.version,
                        replace(self.instrument, title="changed title"))
                elif change == "parser":
                    self.versions[version_id] = (
                        replace(self.version, parser_version="changed"), self.instrument)
                else:
                    self.provisions = (replace(self.provisions[0], id=new_uuid7()),)
            return await super().version_with_instrument(version_id)
    search = Search()
    with pytest.raises(ValueError, match="changed"):
        await build(ChangingSource(), search)
    assert search.events == ["ensure"]
