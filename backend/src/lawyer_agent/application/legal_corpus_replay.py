"""Fail-closed semantic identity checks for non-destructive corpus replay."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from lawyer_agent.application.legal_corpus_import import (
    LegalProvisionDraft,
    provision_text_for_parser,
)
from lawyer_agent.domain.legal_corpus import LegalChunk, Provision, content_sha256


class LegalCorpusReplayConflict(ValueError):
    """Existing corpus facts do not exactly match the replay input."""


class LegalCorpusReplayChunkPort(Protocol):
    async def chunks_for_version(self, version_id: UUID) -> tuple[LegalChunk, ...]: ...

    async def replace_chunks_for_version(
        self, version_id: UUID, chunks: tuple[LegalChunk, ...],
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class _ProvisionSemantic:
    provision_no: str
    level: str
    structure_path: tuple[str, ...]
    title: str | None
    full_text: str
    content_hash: bytes
    char_start: int
    char_end: int


type _ChunkSemantic = tuple[
    UUID, UUID, str, str, str, bytes, str | None, int | None, int | None,
]
type _ChunkTree = tuple[_ChunkSemantic, tuple[_ChunkTree, ...]]


def assert_provisions_match(
    drafts: tuple[LegalProvisionDraft, ...], actual: tuple[Provision, ...],
    *,
    parser_version: str | None = None,
) -> None:
    """Compare stored provisions with the exact facts derived from this command."""
    cursor = 0
    expected: list[_ProvisionSemantic] = []
    for draft in drafts:
        text = provision_text_for_parser(draft.full_text, parser_version)
        end = cursor + len(text)
        expected.append(_ProvisionSemantic(
            provision_no=draft.provision_no, level=draft.level.value,
            structure_path=draft.structure_path, title=draft.title,
            full_text=text, content_hash=content_sha256(text),
            char_start=cursor, char_end=end,
        ))
        cursor = end
    observed = [
        _ProvisionSemantic(
            provision_no=item.provision_no, level=item.level.value,
            structure_path=item.structure_path, title=item.title,
            full_text=item.full_text, content_hash=item.content_hash,
            char_start=item.char_start, char_end=item.char_end,
        )
        for item in actual
    ]
    if expected != observed:
        raise LegalCorpusReplayConflict("replay provisions differ from existing version")


def assert_chunk_graph_matches(
    expected: tuple[LegalChunk, ...], actual: tuple[LegalChunk, ...],
) -> None:
    """Compare complete chunk semantics and topology while ignoring chunk UUIDs."""
    try:
        expected_graph = _canonical_graph(expected)
        actual_graph = _canonical_graph(actual)
    except _InvalidChunkGraph as exc:
        raise LegalCorpusReplayConflict(f"replay chunk graph invalid: {exc}") from exc
    if expected_graph != actual_graph:
        raise LegalCorpusReplayConflict("replay chunk graph differs from existing version")


async def persist_or_replay_chunk_graph(
    repository: LegalCorpusReplayChunkPort,
    version_id: UUID,
    expected: tuple[LegalChunk, ...],
    *,
    replayed: bool,
) -> int:
    """Write chunks once, or verify and preserve every existing row on replay."""
    if not replayed:
        await repository.replace_chunks_for_version(version_id, expected)
        return len(expected)
    actual = await repository.chunks_for_version(version_id)
    assert_chunk_graph_matches(expected, actual)
    return len(actual)


class _InvalidChunkGraph(ValueError):
    pass


def _canonical_graph(chunks: tuple[LegalChunk, ...]) -> Counter[_ChunkTree]:
    by_id: dict[UUID, LegalChunk] = {}
    for chunk in chunks:
        if chunk.id in by_id:
            raise _InvalidChunkGraph("duplicate chunk id")
        by_id[chunk.id] = chunk
    children: dict[UUID, list[UUID]] = {chunk_id: [] for chunk_id in by_id}
    roots: list[UUID] = []
    for chunk in chunks:
        if chunk.parent_chunk_id is None:
            roots.append(chunk.id)
            continue
        parent = by_id.get(chunk.parent_chunk_id)
        if parent is None:
            raise _InvalidChunkGraph("orphan parent")
        if parent.version_id != chunk.version_id or parent.provision_id != chunk.provision_id:
            raise _InvalidChunkGraph("parent crosses version or provision")
        children[parent.id].append(chunk.id)

    visiting: set[UUID] = set()
    visited: set[UUID] = set()

    def tree(chunk_id: UUID) -> _ChunkTree:
        if chunk_id in visiting:
            raise _InvalidChunkGraph("cycle")
        visiting.add(chunk_id)
        child_trees = tuple(sorted((tree(item) for item in children[chunk_id]), key=repr))
        if len(set(child_trees)) != len(child_trees):
            raise _InvalidChunkGraph("ambiguous duplicate sibling")
        visiting.remove(chunk_id)
        visited.add(chunk_id)
        chunk = by_id[chunk_id]
        semantic: _ChunkSemantic = (
            chunk.version_id, chunk.provision_id, chunk.chunk_type.value,
            chunk.quality.value, chunk.content, chunk.content_hash,
            chunk.parser_version, chunk.parent_relative_char_start,
            chunk.parent_relative_char_end,
        )
        return semantic, child_trees

    result = Counter(tree(chunk_id) for chunk_id in roots)
    if len(visited) != len(chunks):
        raise _InvalidChunkGraph("cycle or unreachable node")
    if any(count > 1 for count in result.values()):
        raise _InvalidChunkGraph("ambiguous duplicate root")
    return result
