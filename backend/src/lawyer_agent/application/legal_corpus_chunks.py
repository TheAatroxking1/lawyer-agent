"""Legal corpus chunk derivation (non-model, retrieval feed).

The corpus ``legal_chunks`` schema and domain exist; this module turns parsed
Provision rows into the deterministic retrieval chunks that later
OpenSearch/Embedding indexing consumes. Chunking never joins text across
provisions: each full article becomes exactly one PROVISION chunk. Child
sub-item/table/attachment chunks are future work once the parser produces them.
"""

from __future__ import annotations

from uuid import UUID

from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import (
    ChunkQuality,
    ChunkType,
    LegalChunk,
    Provision,
    content_sha256,
)


def derive_chunks(
    *,
    version_id: UUID,
    provisions: tuple[Provision, ...],
    parser_version: str,
) -> tuple[LegalChunk, ...]:
    """One PROVISION chunk per article, ordered by source character offset."""
    if not isinstance(provisions, tuple):
        raise ValueError("provisions must be a tuple of typed provisions")
    if not isinstance(parser_version, str) or not parser_version.strip():
        raise ValueError("parser version must be non-empty text")
    if any(not isinstance(provision, Provision) for provision in provisions):
        raise ValueError("provisions must contain typed Provision rows")
    ordered = sorted(provisions, key=lambda item: (item.char_start, item.provision_no))
    chunks: list[LegalChunk] = []
    for provision in ordered:
        chunks.append(
            LegalChunk(
                id=new_uuid7(),
                version_id=version_id,
                provision_id=provision.id,
                chunk_type=ChunkType.PROVISION,
                quality=ChunkQuality.OK,
                content=provision.full_text,
                content_hash=content_sha256(provision.full_text),
                parent_chunk_id=None,
                parser_version=parser_version.strip(),
            )
        )
    return tuple(chunks)
