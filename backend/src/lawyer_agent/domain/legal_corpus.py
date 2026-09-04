"""Legal corpus domain: stable instruments, versioned texts, provisions, chunks.

Domain only: no ORM, no document tooling. Product is mainland-China law only.
Unknown effective status must never be presented as current.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any, Final
from uuid import UUID

from lawyer_agent.domain.common import require_uuid7

DATASET_V1: Final[str] = "dataset_v1"
MAX_TEXT_BYTES: Final[int] = 2 * 1024 * 1024


class LegalVersionStatus(StrEnum):
    CURRENT = "current"
    REPEALED = "repealed"
    STATUS_UNKNOWN = "status_unknown"
    HISTORICAL = "historical"
    DRAFT = "draft"


class ProvisionLevel(StrEnum):
    PART = "part"  # 编
    CHAPTER = "chapter"  # 章
    SECTION = "section"  # 节
    ARTICLE = "article"  # 条
    PARAGRAPH = "paragraph"  # 款
    ITEM = "item"  # 项
    SUB_ITEM = "sub_item"  # 目


class ChunkType(StrEnum):
    PROVISION = "provision"
    SUB_ITEM = "sub_item"
    TABLE = "table"
    ATTACHMENT = "attachment"


class ChunkQuality(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    FAILED = "failed"


class DatasetState(StrEnum):
    PENDING = "pending"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


class LoadStatus(StrEnum):
    INVENTORIED = "inventoried"
    PARSING = "parsing"
    COMPLETED = "completed"
    FAILED = "failed"


def _require_utc(value: object, name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be timezone-aware")


def content_sha256(content: str) -> bytes:
    if not isinstance(content, str):
        raise ValueError("content must be text")
    return sha256(content.encode("utf-8")).digest()


def _require_nonempty(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")


@dataclass(frozen=True, slots=True)
class LegalInstrument:
    """Stable identity of one statute; never auto-merged from lookalikes."""

    id: UUID
    title: str
    issuing_authority: str
    jurisdiction: str
    region_code: str | None = None

    def __post_init__(self) -> None:
        require_uuid7(self.id, field="instrument id")
        _require_nonempty(self.title, "instrument title")
        _require_nonempty(self.issuing_authority, "issuing authority")
        _require_nonempty(self.jurisdiction, "jurisdiction")


@dataclass(frozen=True, slots=True)
class LegalVersion:
    """One published/repealed version of a statute."""

    id: UUID
    instrument_id: UUID
    version_label: str
    status: LegalVersionStatus
    published_on: date | None
    effective_on: date | None
    repealed_on: date | None
    law_number: str | None = None
    content_hash: bytes | None = None
    source_ref: str | None = None
    dataset_version: str | None = None
    parser_version: str | None = None

    def __post_init__(self) -> None:
        require_uuid7(self.id, field="version id")
        require_uuid7(self.instrument_id, field="version instrument id")
        _require_nonempty(self.version_label, "version label")
        if not isinstance(self.status, LegalVersionStatus):
            raise ValueError("version status must be strongly typed")
        if self.law_number is not None:
            _require_nonempty(self.law_number, "law number")
        if self.content_hash is not None and len(self.content_hash) != 32:
            raise ValueError("version content hash must be 32 bytes")


@dataclass(frozen=True, slots=True)
class Provision:
    """One full article (条) with its structural path inside a version."""

    id: UUID
    version_id: UUID
    provision_no: str
    level: ProvisionLevel
    structure_path: tuple[str, ...]
    title: str | None
    full_text: str
    content_hash: bytes
    char_start: int
    char_end: int

    def __post_init__(self) -> None:
        require_uuid7(self.id, field="provision id")
        require_uuid7(self.version_id, field="provision version id")
        _require_nonempty(self.provision_no, "provision number")
        if not isinstance(self.level, ProvisionLevel):
            raise ValueError("provision level must be strongly typed")
        if not isinstance(self.structure_path, tuple):
            raise ValueError("provision structure path must be a tuple")
        _require_nonempty(self.full_text, "provision full text")
        if len(self.full_text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise ValueError("provision full text exceeds the size limit")
        if self.content_hash != content_sha256(self.full_text):
            raise ValueError("provision content hash mismatch")
        if isinstance(self.char_start, bool) or not isinstance(self.char_start, int):
            raise ValueError("provision char_start must be an integer")
        if not 0 <= self.char_start < self.char_end:
            raise ValueError("provision character range is invalid")


@dataclass(frozen=True, slots=True)
class LegalChunk:
    """Retrieval unit tied to a Provision (parent) or a child of it."""

    id: UUID
    version_id: UUID
    provision_id: UUID
    chunk_type: ChunkType
    quality: ChunkQuality
    content: str
    content_hash: bytes
    parent_chunk_id: UUID | None = None
    parser_version: str | None = None

    def __post_init__(self) -> None:
        require_uuid7(self.id, field="chunk id")
        require_uuid7(self.version_id, field="chunk version id")
        require_uuid7(self.provision_id, field="chunk provision id")
        if self.parent_chunk_id is not None:
            require_uuid7(self.parent_chunk_id, field="chunk parent id")
        if not isinstance(self.chunk_type, ChunkType):
            raise ValueError("chunk type must be strongly typed")
        if not isinstance(self.quality, ChunkQuality):
            raise ValueError("chunk quality must be strongly typed")
        if self.content_hash != content_sha256(self.content):
            raise ValueError("chunk content hash mismatch")


@dataclass(frozen=True, slots=True)
class DatasetSnapshot:
    id: UUID
    dataset_name: str
    parser_version: str
    state: DatasetState
    manifest: dict[str, Any]
    quality_metrics: dict[str, Any]
    released_at: datetime | None = None

    def __post_init__(self) -> None:
        require_uuid7(self.id, field="dataset snapshot id")
        _require_nonempty(self.dataset_name, "dataset name")
        _require_nonempty(self.parser_version, "dataset parser version")
        if not isinstance(self.state, DatasetState):
            raise ValueError("dataset state must be strongly typed")
        if not isinstance(self.manifest, dict) or not isinstance(
            self.quality_metrics, dict
        ):
            raise ValueError("dataset manifest and metrics must be mappings")
        if self.released_at is not None:
            _require_utc(self.released_at, "dataset released_at")


@dataclass(frozen=True, slots=True)
class LoadBatch:
    id: UUID
    batch_no: str
    source_ref: str
    file_sha256: bytes
    parser_version: str
    status: LoadStatus
    item_counts: dict[str, int] = None  # type: ignore[assignment]
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        require_uuid7(self.id, field="load batch id")
        _require_nonempty(self.batch_no, "batch number")
        _require_nonempty(self.source_ref, "batch source ref")
        if self.file_sha256 is None or len(self.file_sha256) != 32:
            raise ValueError("load batch file hash must be 32 bytes")
        if not isinstance(self.status, LoadStatus):
            raise ValueError("load batch status must be strongly typed")
        if self.item_counts is None:
            raise ValueError("load batch item counts must be a mapping")
        for timestamp, name in (
            (self.started_at, "load batch started_at"),
            (self.completed_at, "load batch completed_at"),
        ):
            if timestamp is not None:
                _require_utc(timestamp, name)


@dataclass(frozen=True, slots=True)
class QualityIssue:
    id: UUID
    batch_id: UUID
    file_sha256: bytes
    issue_type: str
    message: str

    def __post_init__(self) -> None:
        require_uuid7(self.id, field="quality issue id")
        require_uuid7(self.batch_id, field="quality issue batch id")
        if self.file_sha256 is None or len(self.file_sha256) != 32:
            raise ValueError("quality issue file hash must be 32 bytes")
        _require_nonempty(self.issue_type, "issue type")
        _require_nonempty(self.message, "issue message")
