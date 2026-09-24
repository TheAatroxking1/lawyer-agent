"""Multi-version legal dataset publish orchestration (non-model glue).

spec 6.6 for *curated law sets*: build one fresh OpenSearch index whose documents
come from several legal versions (one index = one dataset alias), then atomically
re-point the dataset alias. Every indexed document keeps its chunk/version/
provision ids, so retrieval assembles authoritative evidence from MySQL per hit —
no single-law assumption. Never publishes an empty index under an alias.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from typing import Protocol
from uuid import UUID

from lawyer_agent.application.legal_index_chunks import select_index_leaves
from lawyer_agent.application.legal_index_publish import NavigationBuildPort
from lawyer_agent.application.legal_navigation_index import NavigationBuildResult
from lawyer_agent.domain.common import is_uuid7, new_uuid7
from lawyer_agent.domain.legal_corpus import DatasetSnapshot, DatasetState, LegalChunk

_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,254}$")
MAX_EMBED_BATCH_SIZE = 256


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
    @property
    def values(self) -> tuple[float, ...]: ...


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
        self, index_name: str, *, vector_dimension: int
    ) -> None: ...

    async def append_documents(
        self,
        index_name: str,
        documents: tuple[dict[str, object], ...],
        *,
        parser_version: str,
    ) -> None: ...

    async def count_documents(self, index_name: str) -> int: ...


class _AliasPort(Protocol):
    async def publish_dataset(
        self, alias: str, index_name: str
    ) -> str | None: ...


class DatasetSnapshotWritePort(Protocol):
    async def find_dataset(self, dataset_name: str) -> DatasetSnapshot | None: ...

    async def upsert_dataset(self, snapshot: DatasetSnapshot) -> None: ...


class LegalDatasetSetPublishService:
    """Embeds several legal versions into one index and points the alias at it.

    Preflight holds one version graph, then each embedding batch is appended
    immediately to a fresh index. Production callers supply a repeatable-read
    source view and publish the returned candidate through the durable journal.
    """

    def __init__(
        self,
        chunks: _ChunksPort,
        gateway: _EmbedGatewayPort,
        search: _SearchPort,
        alias: _AliasPort,
        snapshot: DatasetSnapshotWritePort | None = None,
        *,
        navigation: NavigationBuildPort,
        dataset_parser_version: str = "docx-v1",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not hasattr(chunks, "chunks_for_version"):
            raise ValueError("dataset set publish requires a chunk read port")
        if not hasattr(gateway, "embed"):
            raise ValueError("dataset set publish requires an embed gateway")
        if not hasattr(search, "ensure_index") or not hasattr(
            search, "append_documents"
        ) or not hasattr(search, "count_documents"):
            raise ValueError("dataset set publish requires an OpenSearch client")
        if not hasattr(alias, "publish_dataset"):
            raise ValueError("dataset set publish requires an alias service")
        if not hasattr(navigation, "build"):
            raise ValueError("dataset set publish requires a navigation builder")
        if (not isinstance(dataset_parser_version, str)
            or not dataset_parser_version.strip() or len(dataset_parser_version) > 64):
            raise ValueError("dataset parser version must be non-empty text")
        self._chunks = chunks
        self._gateway = gateway
        self._search = search
        self._alias = alias
        self._snapshot = snapshot
        self._navigation = navigation
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
        candidate = await self.build_set(
            version_ids=version_ids, index_name=index_name, alias=alias,
            model_ref=model_ref, dimension=dimension, batch_size=batch_size,
        )
        previous = await self._alias.publish_dataset(alias, index_name)
        if self._snapshot is not None:
            existing = await self._snapshot.find_dataset(alias)
            await self._snapshot.upsert_dataset(replace(
                candidate, id=existing.id if existing else candidate.id,
                state=DatasetState.PUBLISHED, released_at=self._now(),
            ))
        return DatasetSetPublishResult(
            index_name=index_name,
            indexed_documents=int(candidate.quality_metrics["indexed_documents"]),
            previous_target=previous, version_count=len(version_ids),
        )

    async def build_set(
        self, *, version_ids: tuple[UUID, ...], index_name: str, alias: str,
        model_ref: str, dimension: int, batch_size: int = 64,
    ) -> DatasetSnapshot:
        """Build mechanically checked indexes; legal quality review remains separate."""
        _require_name(index_name, "index_name")
        _require_name(alias, "alias")
        if len(alias) > 64 or index_name == alias:
            raise LegalDatasetSetPublishError("alias must fit 64 chars and differ from index")
        if (not isinstance(version_ids, tuple) or not version_ids
            or any(not is_uuid7(value) for value in version_ids)
            or len(set(version_ids)) != len(version_ids)):
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
            or not 1 <= batch_size <= MAX_EMBED_BATCH_SIZE
        ):
            raise LegalDatasetSetPublishError("batch_size must be an integer from 1 to 256")
        if not isinstance(model_ref, str) or not model_ref.strip():
            raise LegalDatasetSetPublishError("model_ref must be non-empty text")

        expected: dict[UUID, tuple[str, int]] = {}
        for version_id in version_ids:
            leaves, digest = await self._selected_version(version_id)
            expected[version_id] = (digest, len(leaves))
            del leaves

        expected_count = sum(count for _, count in expected.values())
        await self._search.ensure_index(index_name, vector_dimension=dimension)
        indexed = 0
        for version_id in version_ids:
            leaves, digest = await self._selected_version(version_id)
            if (digest, len(leaves)) != expected[version_id]:
                raise LegalDatasetSetPublishError("source changed after dataset preflight")
            for start in range(0, len(leaves), batch_size):
                documents = await self._embed_batch(
                    chunks=leaves[start:start + batch_size],
                    model_ref=model_ref,
                    dimension=dimension,
                )
                await self._search.append_documents(
                    index_name, documents, parser_version=self._parser_version,
                )
                indexed += len(documents)
                del documents
            del leaves
        actual_count = await self._search.count_documents(index_name)
        if (type(actual_count) is not int or actual_count <= 0
            or actual_count != indexed or indexed != expected_count):
            raise LegalDatasetSetPublishError("dataset document count verification failed")
        navigation = await self._navigation.build(
            version_ids=version_ids,
            main_index_name=index_name,
            parser_version=self._parser_version,
        )
        return self._candidate_snapshot(
                alias=alias,
                index_name=index_name,
                version_ids=version_ids,
                model_ref=model_ref,
                dimension=dimension,
                indexed_documents=indexed,
                navigation=navigation,
        )

    async def _selected_version(self, version_id: UUID) -> tuple[tuple[LegalChunk, ...], str]:
        chunks = await self._chunks.chunks_for_version(version_id)
        if any(chunk.version_id != version_id for chunk in chunks):
            raise LegalDatasetSetPublishError("chunk version does not match requested scope")
        if not chunks:
            raise LegalDatasetSetPublishError(
                f"version {version_id} has no chunks; refusing a partial set"
            )
        leaves = select_index_leaves(chunks)
        digest = sha256()
        for chunk in sorted(chunks, key=lambda item: item.id):
            fact = json.dumps([
                str(chunk.id), str(chunk.version_id), str(chunk.provision_id),
                str(chunk.parent_chunk_id) if chunk.parent_chunk_id else None,
                chunk.chunk_type.value, chunk.quality.value, chunk.parser_version,
                chunk.content_hash.hex(), sha256(chunk.content.encode("utf-8")).hexdigest(),
                chunk.parent_relative_char_start, chunk.parent_relative_char_end,
            ], ensure_ascii=True, separators=(",", ":")).encode("ascii")
            digest.update(len(fact).to_bytes(8, "big"))
            digest.update(fact)
        return leaves, digest.hexdigest()

    async def _embed_batch(
        self,
        *,
        chunks: tuple[LegalChunk, ...],
        model_ref: str,
        dimension: int,
    ) -> tuple[dict[str, object], ...]:
        from lawyer_agent.infrastructure.search.opensearch import chunk_document

        documents: list[dict[str, object]] = []
        vectors = await self._gateway.embed(
            model_ref=model_ref, texts=[chunk.content for chunk in chunks], dimension=dimension,
        )
        for chunk, vector in zip(chunks, vectors, strict=True):
            document = chunk_document(
                chunk_id=chunk.id,
                provision_id=chunk.provision_id,
                version_id=chunk.version_id,
                content=chunk.content,
                parser_version=chunk.parser_version or "",
            )
            document["content_vector"] = list(vector.values)
            documents.append(document)
        return tuple(documents)

    def _candidate_snapshot(
        self,
        *,
        alias: str,
        index_name: str,
        version_ids: tuple[UUID, ...],
        model_ref: str,
        dimension: int,
        indexed_documents: int,
        navigation: NavigationBuildResult,
    ) -> DatasetSnapshot:
        return DatasetSnapshot(
            id=new_uuid7(),
            dataset_name=alias,
            parser_version=self._parser_version,
            state=DatasetState.PENDING,
            manifest={
                "index_name": index_name,
                "alias": alias,
                "version_ids": [str(version_id) for version_id in version_ids],
                "version_count": len(version_ids),
                "model_ref": model_ref,
                "dimension": dimension,
                "indexed_documents": indexed_documents,
                "navigation_index": navigation.index_name,
                "navigation_schema_version": 1,
                "navigation_documents": navigation.indexed_documents,
            },
            quality_metrics={
                "indexed_documents": indexed_documents,
                "navigation_documents": navigation.indexed_documents,
                "dimension": dimension,
            },
            released_at=None,
        )


def _require_name(value: str, field: str) -> None:
    if not isinstance(value, str) or _NAME.fullmatch(value) is None:
        raise LegalDatasetSetPublishError(
            f"{field} must be 1-255 chars of [a-z0-9_.-] starting alphanumeric"
        )
