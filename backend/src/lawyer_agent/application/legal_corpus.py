"""Legal corpus inventory: fingerprinting, manifest building, batch records."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import DatasetSnapshot, LoadBatch, LoadStatus


@dataclass(frozen=True, slots=True)
class CorpusFile:
    source_ref: str
    payload: bytes


@dataclass(frozen=True, slots=True)
class InventoryEntry:
    source_ref: str
    file_sha256: bytes
    duplicate_of: str | None = None


@dataclass(frozen=True, slots=True)
class InventoryResult:
    batch: LoadBatch
    manifest: dict[str, Any]
    quality_metrics: dict[str, Any]


class LegalCorpusInventoryPort(Protocol):
    async def find_batch_by_sha256(self, file_sha256: bytes) -> LoadBatch | None: ...

    async def create_load_batch(self, batch: LoadBatch) -> None: ...


class LegalDatasetPort(Protocol):
    async def find_dataset(self, dataset_name: str) -> DatasetSnapshot | None: ...

    async def upsert_dataset(self, snapshot: DatasetSnapshot) -> None: ...


def _sha256(payload: bytes) -> bytes:
    if not isinstance(payload, bytes):
        raise ValueError("payload must be bytes")
    return hashlib.sha256(payload).digest()


class LegalCorpusInventoryService:
    """One-shot inventory: hash each source, detect duplicates, write a batch."""

    def __init__(
        self,
        repository: LegalCorpusInventoryPort,
        dataset_store: LegalDatasetPort,
        parser_version: str,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(parser_version, str) or not parser_version:
            raise ValueError("parser version must be non-empty text")
        self._repository = repository
        self._dataset_store = dataset_store
        self._parser_version = parser_version
        self._now = now if now is not None else lambda: datetime.now(UTC)

    async def inventory(self, files: tuple[CorpusFile, ...]) -> InventoryResult:
        if not isinstance(files, tuple) or not files:
            raise ValueError("at least one corpus file is required")
        seen: dict[bytes, str] = {}
        entries: list[InventoryEntry] = []
        for corpus_file in files:
            if not isinstance(corpus_file, CorpusFile):
                raise ValueError("corpus files must be strongly typed")
            digest = _sha256(corpus_file.payload)
            prior = seen.get(digest)
            seen[digest] = corpus_file.source_ref
            entries.append(
                InventoryEntry(
                    source_ref=corpus_file.source_ref,
                    file_sha256=digest,
                    duplicate_of=prior,
                )
            )
        counts = {
            "files": len(entries),
            "unique": sum(1 for entry in entries if entry.duplicate_of is None),
            "duplicates": sum(1 for entry in entries if entry.duplicate_of is not None),
        }
        now = self._now()
        batch_no = f"B{now:%Y%m%d%H%M%S}{secrets.token_hex(2).upper()}"
        batch = LoadBatch(
            id=new_uuid7(),
            batch_no=batch_no,
            source_ref=entries[0].source_ref,
            file_sha256=entries[0].file_sha256,
            parser_version=self._parser_version,
            status=LoadStatus.INVENTORIED,
            item_counts=counts,
            started_at=now,
        )
        await self._repository.create_load_batch(batch)
        if batch.started_at is None:
            raise RuntimeError("inventoried batch must have a started_at timestamp")
        manifest = {
            "batch_no": batch.batch_no,
            "parser_version": self._parser_version,
            "files": [entry.source_ref for entry in entries],
            "sha256": [entry.file_sha256.hex() for entry in entries],
            "duplicate_of": [entry.duplicate_of for entry in entries],
            "inventoried_at": batch.started_at.isoformat(),        }
        metrics = {"counts": counts, "quality": "inventoried"}
        return InventoryResult(batch=batch, manifest=manifest, quality_metrics=metrics)
