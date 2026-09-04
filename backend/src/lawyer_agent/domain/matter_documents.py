"""Tenant Matter and versioned Document domain (non-model slice).

A Matter is a tenant-scoped business project folder. A Document holds an
immutable original-object reference plus versioned derived documents. Any
process-time limit or high-risk conclusion requires lawyer confirmation; this
slice stores facts only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from lawyer_agent.domain.common import require_uuid7

MAX_OBJECT_KEY_BYTES = 512
MAX_DOCUMENT_NAME_BYTES = 256


class MatterKind(StrEnum):
    CONTRACT_REVIEW = "contract_review"
    LITIGATION = "litigation"
    LEGAL_ADVICE = "legal_advice"
    COMPLIANCE = "compliance"
    OTHER = "other"


class MatterStatus(StrEnum):
    OPEN = "open"
    ACTIVE = "active"
    CLOSED = "closed"
    ARCHIVED = "archived"


_MATTER_STATUS_TRANSITIONS: frozenset[tuple[MatterStatus, MatterStatus]] = frozenset(
    {
        (MatterStatus.OPEN, MatterStatus.ACTIVE),
        (MatterStatus.OPEN, MatterStatus.CLOSED),
        (MatterStatus.ACTIVE, MatterStatus.CLOSED),
        (MatterStatus.CLOSED, MatterStatus.ARCHIVED),
    }
)


class MatterStatusTransitionInvalid(ValueError):
    def __init__(self, source: MatterStatus, target: MatterStatus) -> None:
        self.source = source
        self.target = target
        super().__init__(f"invalid matter status transition: {source.value} -> {target.value}")


def require_matter_status_transition(
    source: MatterStatus, target: MatterStatus
) -> None:
    """Reject any status transition outside the approved conservative set."""
    if not isinstance(source, MatterStatus) or not isinstance(target, MatterStatus):
        raise ValueError("matter status transition endpoints must be strongly typed")
    if (source, target) not in _MATTER_STATUS_TRANSITIONS:
        raise MatterStatusTransitionInvalid(source, target)


class DocumentKind(StrEnum):
    ORIGINAL = "original"
    DERIVED = "derived"


class DocumentUploadStatus(StrEnum):
    UPLOADED = "uploaded"
    VALIDATING = "validating"
    ACCEPTED = "accepted"
    NEEDS_REVIEW = "needs_review"
    PARSING = "parsing"
    READY = "ready"
    FAILED = "failed"


class ReviewStatus(StrEnum):
    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    CHANGES_REQUESTED = "changes_requested"
    APPROVED = "approved"
    REJECTED = "rejected"


def _require_positive_int(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")


def _require_utc(value: object, name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class Matter:
    id: UUID
    tenant_id: UUID
    title: str
    kind: MatterKind
    status: MatterStatus
    created_by_user_id: UUID
    created_by_membership_id: UUID
    owner_membership_id: UUID | None = None
    description: str | None = None
    version: int = 1

    def __post_init__(self) -> None:
        for value, name in (
            (self.id, "matter id"),
            (self.tenant_id, "matter tenant_id"),
            (self.created_by_user_id, "matter created_by_user_id"),
            (self.created_by_membership_id, "matter created_by_membership_id"),
        ):
            require_uuid7(value, field=name)
        if self.owner_membership_id is not None:
            require_uuid7(self.owner_membership_id, field="matter owner_membership_id")
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("matter title must be non-empty text")
        if not isinstance(self.kind, MatterKind):
            raise ValueError("matter kind must be strongly typed")
        if not isinstance(self.status, MatterStatus):
            raise ValueError("matter status must be strongly typed")
        _require_positive_int(self.version, "matter version")


@dataclass(frozen=True, slots=True)
class MatterParty:
    """Participant in a Matter; parties never leak across tenants."""

    id: UUID
    tenant_id: UUID
    matter_id: UUID
    display_name: str
    kind: str
    version: int = 1

    def __post_init__(self) -> None:
        require_uuid7(self.id, field="party id")
        require_uuid7(self.tenant_id, field="party tenant_id")
        require_uuid7(self.matter_id, field="party matter_id")
        if not isinstance(self.display_name, str) or not self.display_name.strip():
            raise ValueError("party display name must be non-empty text")
        _require_positive_int(self.version, "party version")


@dataclass(frozen=True, slots=True)
class PartyConflictCheck:
    """Conflict-of-interest existence result (never leaks other Matters).

    Only answers whether the party appears in other Matters of the tenant and
    how many; never exposes their identifiers or contents.
    """

    tenant_id: UUID
    conflict: bool
    other_matter_count: int

    def __post_init__(self) -> None:
        require_uuid7(self.tenant_id, field="conflict tenant_id")
        if isinstance(self.other_matter_count, bool) or not isinstance(
            self.other_matter_count, int
        ):
            raise ValueError("conflict matter count must be an integer")
        if self.other_matter_count < 0:
            raise ValueError("conflict matter count must not be negative")
        if self.conflict != (self.other_matter_count > 0):
            raise ValueError("conflict flag must match the other-matter count")


@dataclass(frozen=True, slots=True)
class DocumentHeader:
    """Tenant document header row (stable identity across versions)."""

    id: UUID
    tenant_id: UUID
    matter_id: UUID
    display_name: str
    current_version_no: int
    version: int

    def __post_init__(self) -> None:
        require_uuid7(self.id, field="document id")
        require_uuid7(self.tenant_id, field="document tenant_id")
        require_uuid7(self.matter_id, field="document matter_id")
        if not isinstance(self.display_name, str) or not self.display_name.strip():
            raise ValueError("document display name must be non-empty text")
        if (
            isinstance(self.current_version_no, bool)
            or not isinstance(self.current_version_no, int)
            or self.current_version_no < 0
        ):
            raise ValueError("document current_version_no must be a non-negative integer")
        _require_positive_int(self.version, "document version")


@dataclass(frozen=True, slots=True)
class DocumentVersion:
    id: UUID
    tenant_id: UUID
    document_id: UUID
    version_no: int
    kind: DocumentKind
    object_key: str
    sha256: bytes
    upload_status: DocumentUploadStatus
    file_name: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    parser_version: str | None = None
    parse_error: str | None = None
    review_status: ReviewStatus | None = None
    review_reason: str | None = None
    created_by_user_id: UUID | None = None
    created_by_membership_id: UUID | None = None
    uploaded_at: datetime | None = None

    def __post_init__(self) -> None:
        require_uuid7(self.id, field="document version id")
        require_uuid7(self.tenant_id, field="document version tenant_id")
        require_uuid7(self.document_id, field="document version document_id")
        _require_positive_int(self.version_no, "document version_no")
        if not isinstance(self.kind, DocumentKind):
            raise ValueError("document kind must be strongly typed")
        if not isinstance(self.object_key, str) or not self.object_key:
            raise ValueError("document object key must be non-empty text")
        if len(self.object_key.encode("utf-8")) > MAX_OBJECT_KEY_BYTES:
            raise ValueError("document object key is too long")
        if self.sha256 is None or len(self.sha256) != 32:
            raise ValueError("document sha256 must be 32 bytes")
        if not isinstance(self.upload_status, DocumentUploadStatus):
            raise ValueError("document upload status must be strongly typed")
        if self.size_bytes is not None:
            _require_positive_int(self.size_bytes, "document size_bytes")
        if self.uploaded_at is not None:
            _require_utc(self.uploaded_at, "document uploaded_at")
