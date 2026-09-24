"""Validate legal chunk parent graphs and select retrieval leaves."""

from __future__ import annotations

from uuid import UUID

from lawyer_agent.domain.legal_corpus import ChunkQuality, LegalChunk


class LegalIndexChunkError(ValueError):
    """The chunk graph is unsafe to publish into a retrieval index."""


def select_index_leaves(chunks: tuple[LegalChunk, ...]) -> tuple[LegalChunk, ...]:
    """Return chunks with no children after validating the complete parent graph."""
    by_id: dict[UUID, LegalChunk] = {}
    parent_ids: set[UUID] = set()
    for chunk in chunks:
        if chunk.id in by_id:
            raise LegalIndexChunkError(f"duplicate chunk id: {chunk.id}")
        if chunk.quality is ChunkQuality.FAILED:
            raise LegalIndexChunkError(f"failed chunk cannot be indexed: {chunk.id}")
        by_id[chunk.id] = chunk

    for chunk in chunks:
        parent_id = chunk.parent_chunk_id
        if parent_id is None:
            continue
        if parent_id == chunk.id:
            raise LegalIndexChunkError(f"chunk cannot parent itself: {chunk.id}")
        parent = by_id.get(parent_id)
        if parent is None:
            raise LegalIndexChunkError(
                f"chunk {chunk.id} references missing parent {parent_id}"
            )
        if parent.version_id != chunk.version_id:
            raise LegalIndexChunkError("parent and child belong to different versions")
        if parent.provision_id != chunk.provision_id:
            raise LegalIndexChunkError("parent and child belong to different provisions")
        parent_ids.add(parent_id)

    _reject_cycles(by_id)
    return tuple(chunk for chunk in chunks if chunk.id not in parent_ids)


def _reject_cycles(by_id: dict[UUID, LegalChunk]) -> None:
    validated: set[UUID] = set()
    for start_id in by_id:
        if start_id in validated:
            continue
        path: list[UUID] = []
        positions: dict[UUID, int] = {}
        current_id: UUID | None = start_id
        while current_id is not None and current_id not in validated:
            if current_id in positions:
                raise LegalIndexChunkError("chunk parent graph contains a cycle")
            positions[current_id] = len(path)
            path.append(current_id)
            current_id = by_id[current_id].parent_chunk_id
        validated.update(path)
