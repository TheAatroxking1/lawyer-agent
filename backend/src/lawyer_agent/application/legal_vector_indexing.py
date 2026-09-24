"""Legal corpus vector indexing orchestration (provider-agnostic).

Reads a version's chunks from the corpus chunk store, embeds them in batches
through the Model Gateway, then builds/updates an OpenSearch k-NN index for
that version. The concrete embedding provider is injected behind the gateway,
so this service can be fully unit-tested offline and later run with real
weights unchanged.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from lawyer_agent.application.legal_index_chunks import select_index_leaves
from lawyer_agent.application.model_gateway import ModelGateway
from lawyer_agent.domain.legal_corpus import LegalChunk
from lawyer_agent.infrastructure.search.opensearch import (
    OpenSearchRestClient,
    chunk_document,
)


class CorpusChunkReadPort(Protocol):
    async def chunks_for_version(
        self, version_id: UUID
    ) -> tuple[LegalChunk, ...]: ...


class LegalVectorIndexingService:
    """Batch-embed a version's chunks and index them as k-NN documents."""

    def __init__(
        self,
        chunks: CorpusChunkReadPort,
        gateway: ModelGateway,
        search: OpenSearchRestClient,
    ) -> None:
        if not hasattr(chunks, "chunks_for_version"):
            raise ValueError("vector indexing requires a chunk read port")
        self._chunks = chunks
        self._gateway = gateway
        self._search = search

    async def index_version(
        self,
        *,
        version_id: UUID,
        index_name: str,
        model_ref: str,
        dimension: int,
        batch_size: int = 64,
    ) -> int:
        if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
            raise ValueError("batch_size must be a positive integer")
        all_chunks = select_index_leaves(
            await self._chunks.chunks_for_version(version_id)
        )
        if not all_chunks:
            return 0
        await self._search.ensure_index(index_name, vector_dimension=dimension)
        documents: list[dict[str, object]] = []
        for start in range(0, len(all_chunks), batch_size):
            batch = all_chunks[start : start + batch_size]
            vectors = await self._gateway.embed(
                model_ref=model_ref,
                texts=[chunk.content for chunk in batch],
                dimension=dimension,
            )
            for chunk, vector in zip(batch, vectors, strict=True):
                document = chunk_document(
                    chunk_id=chunk.id,
                    provision_id=chunk.provision_id,
                    version_id=chunk.version_id,
                    content=chunk.content,
                    parser_version=chunk.parser_version or "",
                )
                document["content_vector"] = list(vector.values)
                documents.append(document)
        parser_version = all_chunks[0].parser_version or ""
        await self._search.replace_documents(
            index_name, tuple(documents), parser_version=parser_version
        )
        return len(documents)
