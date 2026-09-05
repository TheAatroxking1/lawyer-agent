"""Multi-version legal dataset publish orchestration (non-model glue).

spec 6.6 for *curated law sets*: build one fresh OpenSearch index whose documents
come from several legal versions (one index = one dataset alias), then atomically
re-point the dataset alias. Every indexed document keeps its chunk/version/
provision ids, so retrieval assembles authoritative evidence from MySQL per hit —
no single-law assumption. Never publishes an empty index under an alias.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import DatasetSnapshot, DatasetState, LegalChunk

_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,254}$")


class LegalDatasetSetPublishError(ValueError):
    """Stable error for multi-version dataset publish orchestration."""


@dataclass(frozen=True, slots=True)
class DatasetSetPublishResult:
    index_name: str
    indexed_documents: int
    previous_target: str | None
    version_count: int


class _ChunksPort(Protocol):
    async def chunks_for_version(
        self, version_id: UUID
    ) -> tuple[LegalChunk, ...]: ...


class _Embedded(Protocol):
    values: tuple[float, ...]


class _EmbedGatewayPort(Protocol):
    async def embed(
        self,
        *,
        model_ref: str,
        texts: list[str],
        dimension: int,
    ) -> tuple[_Embedded, ...]: ...


class _SearchPort(Protocol):
    async def ensure_index(
        self, index_name: str, vector_dimension: int
    ) -> None: ...

    async def replace_documents(
        self,
        index_name: str,
        documents: tuple[dict[str, object], ...],
        parser_version: str,
    ) -> None: ...


class _AliasPort(Protocol):
    async def publish_dataset(
        self, alias: str, index_name: str
    ) -> str | None: ...


class DatasetSnapshotWritePort(Protocol):
    async def find_dataset(self, dataset_name: str) -> DatasetSnapshot | None: ...

    async def upsert_dataset(self, snapshot: DatasetSnapshot) -> None: ...


class LegalDatasetSetPublishService:
    """Embeds several legal versions into one index and points the alias at it.

    Documents are accumulated from every requested version and written with a
    single ``replace_documents`` (fresh index; delete-by-parser is a no-op).
    Recording a PUBLISHED dataset snapshot is optional, mirroring the single
    version publisher.
    """

    def __init__(
        self,
        chunks: _ChunksPort,
        gateway: _EmbedGatewayPort,
        search: _SearchPort,
        alias: _AliasPort,
        snapshot: DatasetSnapshotWritePort | None = None,
        *,
        dataset_parser_version: str = "docx-v1",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not hasattr(chunks, "chunks_for_version"):
            raise ValueError("dataset set publish requires a chunk read port")
        if not hasattr(gateway, "embed"):
            raise ValueError("dataset set publish requires an embed gateway")
        if not hasattr(search, "ensure_index") or not hasattr(
            search, "replace_documents"
        ):
            raise ValueError("dataset set publish requires an OpenSearch client")
        if not hasattr(alias, "publish_dataset"):
            raise ValueError("dataset set publish requires an alias service")
        if not isinstance(dataset_parser_version, str) or not dataset_parser_version.strip():
            raise ValueError("dataset parser version must be non-empty text")
        self._chunks = chunks
        self._gateway = gateway
        self._search = search
        self._alias = alias
        self._snapshot = snapshot
        self._parser_version = dataset_parser_version
        self._now = now if now is not None else lambda: datetime.now(UTC)

    async def publish_set(
        self,
        *,
        version_ids: tuple[UUID, ...],
        index_name: str,
        alias: str,
        model_ref: str,
        dimension: int,
        batch_size: int = 64,
    ) -> DatasetSetPublishResult:
        _require_name(index_name, "index_name")
        _require_name(alias, "alias")
        if not isinstance(version_ids, tuple) or not version_ids:
            raise LegalDatasetSetPublishError(
                "version_ids must be a non-empty tuple of UUIDs"
            )
        if (
            isinstance(dimension, bool)
            or not isinstance(dimension, int)
            or dimension <= 0
        ):
            raise LegalDatasetSetPublishError("dimension must be a positive integer")
        if (
            isinstance(batch_size, bool)
            or not isinstance(batch_size, int)
            or batch_size < 1
        ):
            raise LegalDatasetSetPublishError("batch_size must be a positive integer")
        if not isinstance(model_ref, str) or not model_ref.strip():
            raise LegalDatasetSetPublishError("model_ref must be non-empty text")

        documents: list[dict[str, object]] = []
        for version_id in version_ids:
            chunks = await self._chunks.chunks_for_version(version_id)
            if not chunks:
                raise LegalDatasetSetPublishError(
                    f"version {version_id} has no chunks; refusing a partial set"
                )
            documents.extend(
                await self._embed_version(
                    chunks=chunks,
                    model_ref=model_ref,
                    dimension=dimension,
                    batch_size=batch_size,
                )
            )
        if not documents:
            raise LegalDatasetSetPublishError(
                "no documents indexed; refusing to publish an empty dataset "
                "under the alias"
            )
        await self._search.ensure_index(index_name, vector_dimension=dimension)
        await self._search.replace_documents(
            index_name,
            tuple(documents),
            parser_version=self._parser_version,
        )
        previous = await self._alias.publish_dataset(alias, index_name)
        if self._snapshot is not None:
            await self._record_snapshot(
                alias=alias,
                index_name=index_name,
                version_ids=version_ids,
                model_ref=model_ref,
                dimension=dimension,
                indexed_documents=len(documents),
            )
        return DatasetSetPublishResult(
            index_name=index_name,
            indexed_documents=len(documents),
            previous_target=previous,
            version_count=len(version_ids),
        )

    async def _embed_version(
        self,
        *,
        chunks: tuple[LegalChunk, ...],
        model_ref: str,
        dimension: int,
        batch_size: int,
    ) -> list[dict[str, object]]:
        from lawyer_agent.infrastructure.search.opensearch import chunk_document

        documents: list[dict[str, object]] = []
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start : start + batch_size]
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
        return documents

    async def _record_snapshot(
        self,
        *,
        alias: str,
        index_name: str,
        version_ids: tuple[UUID, ...],
        model_ref: str,
        dimension: int,
        indexed_documents: int,
    ) -> None:
        assert self._snapshot is not None
        existing = await self._snapshot.find_dataset(alias)
        snapshot = DatasetSnapshot(
            id=existing.id if existing is not None else new_uuid7(),
            dataset_name=alias,
            parser_version=self._parser_version,
            state=DatasetState.PUBLISHED,
            manifest={
                "index_name": index_name,
                "alias": alias,
                "version_ids": [str(version_id) for version_id in version_ids],
                "version_count": len(version_ids),
                "model_ref": model_ref,
                "dimension": dimension,
                "indexed_documents": indexed_documents,
            },
            quality_metrics={
                "indexed_documents": indexed_documents,
                "dimension": dimension,
            },
            released_at=self._now(),
        )
        await self._snapshot.upsert_dataset(snapshot)


def _require_name(value: str, field: str) -> None:
    if not isinstance(value, str) or _NAME.fullmatch(value) is None:
        raise LegalDatasetSetPublishError(
            f"{field} must be 1-255 chars of [a-z0-9_.-] starting alphanumeric"
        )
