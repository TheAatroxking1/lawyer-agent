from __future__ import annotations

from dataclasses import replace

import pytest

from lawyer_agent.application.legal_corpus_import import LegalProvisionDraft
from lawyer_agent.application.legal_corpus_replay import (
    LegalCorpusReplayConflict,
    assert_chunk_graph_matches,
    assert_provisions_match,
    persist_or_replay_chunk_graph,
)
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import (
    ChunkQuality,
    ChunkType,
    LegalChunk,
    Provision,
    ProvisionLevel,
    content_sha256,
)


def _provision(text: str = "第一条 甲。") -> Provision:
    return Provision(
        id=new_uuid7(), version_id=new_uuid7(), provision_no="第一条",
        level=ProvisionLevel.ARTICLE, structure_path=("第一章", "第一条"),
        title=None, full_text=text, content_hash=content_sha256(text),
        char_start=0, char_end=len(text),
    )


def _draft(provision: Provision) -> LegalProvisionDraft:
    return LegalProvisionDraft(
        provision_no=provision.provision_no, level=provision.level,
        structure_path=provision.structure_path, title=provision.title,
        full_text=provision.full_text,
    )


def _graph(provision: Provision) -> tuple[LegalChunk, ...]:
    parent = LegalChunk(
        id=new_uuid7(), version_id=provision.version_id, provision_id=provision.id,
        chunk_type=ChunkType.PROVISION, quality=ChunkQuality.OK,
        content=provision.full_text, content_hash=content_sha256(provision.full_text),
        parser_version="parser/hierarchical-v2", parent_relative_char_start=0,
        parent_relative_char_end=len(provision.full_text),
    )
    child = LegalChunk(
        id=new_uuid7(), version_id=provision.version_id, provision_id=provision.id,
        parent_chunk_id=parent.id, chunk_type=ChunkType.PARAGRAPH,
        quality=ChunkQuality.DEGRADED, content="甲。", content_hash=content_sha256("甲。"),
        parser_version="parser/hierarchical-v2", parent_relative_char_start=4,
        parent_relative_char_end=6,
    )
    return parent, child


def _independent_ids(chunks: tuple[LegalChunk, ...]) -> tuple[LegalChunk, ...]:
    id_map = {chunk.id: new_uuid7() for chunk in chunks}
    return tuple(replace(
        chunk, id=id_map[chunk.id],
        parent_chunk_id=id_map.get(chunk.parent_chunk_id),
    ) for chunk in reversed(chunks))


class _ChunkRepo:
    def __init__(self, existing: tuple[LegalChunk, ...]) -> None:
        self.existing = existing
        self.replacements: list[tuple[LegalChunk, ...]] = []

    async def chunks_for_version(self, version_id: object) -> tuple[LegalChunk, ...]:
        return self.existing

    async def replace_chunks_for_version(
        self, version_id: object, chunks: tuple[LegalChunk, ...],
    ) -> None:
        self.replacements.append(chunks)


def test_provisions_match_only_the_complete_derived_command_semantics() -> None:
    provision = _provision()
    assert_provisions_match((_draft(provision),), (provision,))

    mutations = (
        replace(provision, provision_no="第二条"),
        replace(provision, level=ProvisionLevel.SECTION),
        replace(provision, structure_path=("第二章",)),
        replace(provision, title="标题"),
        _provision("第一条 乙。"),
        replace(provision, char_start=1, char_end=len(provision.full_text) + 1),
    )
    for mutated in mutations:
        with pytest.raises(LegalCorpusReplayConflict, match="provisions differ"):
            assert_provisions_match((_draft(provision),), (mutated,))
    with pytest.raises(LegalCorpusReplayConflict, match="provisions differ"):
        assert_provisions_match((_draft(provision),), ())


def test_exact_v4_replay_preserves_edge_whitespace() -> None:
    provision = _provision(" \t第一条 甲。\r\n")

    assert_provisions_match(
        (_draft(provision),), (provision,), parser_version="corpus-docx-v4",
    )

    trimmed = _provision(provision.full_text.strip())
    with pytest.raises(LegalCorpusReplayConflict, match="provisions differ"):
        assert_provisions_match(
            (_draft(provision),), (trimmed,), parser_version="corpus-docx-v4",
        )


@pytest.mark.parametrize("parser_version", [None, "corpus-docx-v3", "corpus-docx-v4-near"])
def test_non_v4_replay_retains_legacy_strip_policy(parser_version: str | None) -> None:
    draft_source = _provision(" \t第一条 甲。\r\n")
    stored = _provision(draft_source.full_text.strip())

    assert_provisions_match(
        (_draft(draft_source),), (stored,), parser_version=parser_version,
    )


def test_chunk_graph_matches_with_independent_ids_and_order() -> None:
    provision = _provision()
    expected = _graph(provision)
    assert_chunk_graph_matches(expected, _independent_ids(expected))


async def test_replay_reads_and_preserves_existing_chunk_rows() -> None:
    provision = _provision()
    expected = _graph(provision)
    existing = _independent_ids(expected)
    repository = _ChunkRepo(existing)
    count = await persist_or_replay_chunk_graph(
        repository, provision.version_id, expected, replayed=True,
    )
    assert count == len(existing)
    assert repository.replacements == []


async def test_first_import_writes_derived_chunk_rows() -> None:
    provision = _provision()
    expected = _graph(provision)
    repository = _ChunkRepo(())
    count = await persist_or_replay_chunk_graph(
        repository, provision.version_id, expected, replayed=False,
    )
    assert count == len(expected)
    assert repository.replacements == [expected]


@pytest.mark.parametrize("field,value", [
    ("content", "乙。"),
    ("chunk_type", ChunkType.ITEM),
    ("quality", ChunkQuality.FAILED),
    ("parser_version", "other-parser"),
    ("parent_relative_char_start", 3),
])
def test_chunk_graph_rejects_changed_node_semantics(field: str, value: object) -> None:
    provision = _provision()
    expected = _graph(provision)
    if field == "content":
        changed = replace(expected[1], content=value, content_hash=content_sha256(str(value)))
    else:
        changed = replace(expected[1], **{field: value})
    with pytest.raises(LegalCorpusReplayConflict, match="chunk graph differs"):
        assert_chunk_graph_matches(expected, (expected[0], changed))


def test_chunk_graph_rejects_provision_parent_count_and_invalid_topology() -> None:
    provision = _provision()
    expected = _graph(provision)
    other_provision = new_uuid7()
    cases = (
        (expected[0], replace(expected[1], provision_id=other_provision)),
        (expected[0], replace(expected[1], parent_chunk_id=None)),
        (expected[0],),
        (*expected, replace(expected[1], id=new_uuid7())),
    )
    for actual in cases:
        with pytest.raises(LegalCorpusReplayConflict, match="chunk graph (differs|invalid)"):
            assert_chunk_graph_matches(expected, actual)

    orphan = (replace(expected[1], parent_chunk_id=new_uuid7()),)
    cycle = (
        replace(expected[0], parent_chunk_id=expected[1].id),
        expected[1],
    )
    for actual in (orphan, cycle):
        with pytest.raises(LegalCorpusReplayConflict, match="chunk graph invalid"):
            assert_chunk_graph_matches(expected, actual)
