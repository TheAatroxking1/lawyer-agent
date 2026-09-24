"""Versioned, bounded wire contracts shared by MCP and the review workflow."""

from __future__ import annotations

import math
from datetime import date
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[\w.-]+$")]
ShortText = Annotated[str, Field(min_length=1, max_length=4000)]


class WireModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        revalidate_instances="always",
    )


class ReviewScope(WireModel):
    """Created by trusted application authentication, never by the model."""

    tenant_id: UUID
    actor_id: UUID
    run_id: UUID
    document_version_id: Identifier


class VersionInput(WireModel):
    document_version_id: Identifier


class StructureInput(VersionInput):
    cursor: Identifier | None = None


class ReadBlocksInput(VersionInput):
    block_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=20)]


class RegionInput(VersionInput):
    region_id: Identifier


class JobInput(WireModel):
    job_id: Identifier


class SearchInput(WireModel):
    query: Annotated[str, Field(min_length=1, max_length=2000)]
    region: Annotated[str, Field(min_length=1, max_length=120)]
    as_of: date
    limit: Annotated[int, Field(strict=True, ge=1, le=10)] = 5


class EvidenceInput(WireModel):
    evidence_id: Identifier


class Anchor(WireModel):
    anchor_id: Identifier
    page: Annotated[int, Field(strict=True, ge=1)]
    start: Annotated[int, Field(strict=True, ge=0)]
    end: Annotated[int, Field(strict=True, ge=1)]
    # Unrotated page coordinates; never guessed by the LLM.
    quad: tuple[float, float, float, float, float, float, float, float]

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end <= self.start:
            raise ValueError("invalid anchor range")
        if any(not math.isfinite(v) for v in self.quad):
            raise ValueError("invalid coordinates")
        return self


class TablePosition(WireModel):
    """Stable structural location for one PDF table cell."""

    table_id: Identifier
    row: Annotated[int, Field(strict=True, ge=0)]
    column: Annotated[int, Field(strict=True, ge=0)]


class DocumentBlock(WireModel):
    block_id: Identifier
    kind: Literal["text", "heading", "table", "image"]
    text: Annotated[str, Field(max_length=32000)]
    anchors: Annotated[tuple[Anchor, ...], Field(max_length=2000)]
    quality: Literal["verified", "needs_review"]
    table_position: TablePosition | None = None

    @model_validator(mode="after")
    def contained(self) -> Self:
        if any(anchor.end > len(self.text) for anchor in self.anchors):
            raise ValueError("anchor outside text")
        return self


class StructureResult(VersionInput):
    block_ids: Annotated[tuple[Identifier, ...], Field(max_length=200)]
    next_cursor: Identifier | None = None
    recognition_complete: bool


class BlocksResult(VersionInput):
    blocks: Annotated[tuple[DocumentBlock, ...], Field(max_length=20)]
    truncated: bool = False


class JobResult(VersionInput):
    job_id: Identifier
    status: Literal["queued", "running", "ready", "failed"]
    result_block_ids: Annotated[tuple[Identifier, ...], Field(max_length=20)] = ()


class LegalEvidence(WireModel):
    evidence_id: Identifier
    title: ShortText
    text: Annotated[str, Field(min_length=1, max_length=32000)]
    source_ref: ShortText
    version: Identifier
    jurisdiction: ShortText
    effective_from: date | None
    effective_until: date | None = None
    validity: Literal["effective", "historical", "unknown", "draft"]
    is_complete: bool


class SearchResult(WireModel):
    items: Annotated[tuple[LegalEvidence, ...], Field(max_length=10)]


class LegalPassageCitation(WireModel):
    passage_id: Identifier
    document_id: Identifier
    quote: ShortText


class IssueHighlight(WireModel):
    """One verified text span rendered with its containing native PDF anchor."""

    block_id: Identifier
    anchor_id: Identifier
    start: Annotated[int, Field(strict=True, ge=0)]
    end: Annotated[int, Field(strict=True, ge=1)]

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end <= self.start:
            raise ValueError("invalid highlight range")
        return self


class RiskIssueDraft(WireModel):
    block_id: Identifier
    anchor_id: Identifier | None
    start: Annotated[int, Field(strict=True, ge=0)]
    end: Annotated[int, Field(strict=True, ge=1)]
    quote: ShortText
    category: Literal["wording", "commercial", "legal"]
    severity: Literal["low", "medium", "high"]
    problem: ShortText
    suggestion: ShortText
    evidence_ids: Annotated[tuple[Identifier, ...], Field(max_length=10)] = ()
    evidence_passages: Annotated[tuple[LegalPassageCitation, ...], Field(max_length=7)] = ()
    highlights: Annotated[tuple[IssueHighlight, ...], Field(max_length=200)] = ()
    highlights_complete: bool = True

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end <= self.start:
            raise ValueError("invalid issue range")
        if self.category == "legal" and not self.evidence_ids:
            raise ValueError("legal claims require evidence")
        if any(item.document_id not in self.evidence_ids for item in self.evidence_passages):
            raise ValueError("passage outside issue evidence")
        if self.highlights:
            first = self.highlights[0]
            if (self.block_id, self.anchor_id, self.start, self.end) != (
                first.block_id, first.anchor_id, first.start, first.end,
            ):
                raise ValueError("legacy anchor must match first highlight")
            anchor_ids = [item.anchor_id for item in self.highlights]
            if len(anchor_ids) != len(set(anchor_ids)):
                raise ValueError("duplicate issue highlight")
        elif self.anchor_id is None:
            return self
        return self


class ReviewCandidate(WireModel):
    reviewed_block_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=2000)]
    issues: Annotated[tuple[RiskIssueDraft, ...], Field(max_length=100)]


class WithheldReviewIssue(WireModel):
    index: Annotated[int, Field(strict=True, ge=1, le=100)]
    page: Annotated[int, Field(strict=True, ge=1, le=30)]
    reason: Literal["unverified_citation"]


class ReviewResult(VersionInput):
    run_id: UUID
    status: Literal["draft"] = "draft"
    issues: tuple[RiskIssueDraft, ...]
    withheld_issues: Annotated[tuple[WithheldReviewIssue, ...], Field(max_length=100)] = ()


class ReviewRequest(WireModel):
    instruction: Annotated[str, Field(min_length=1, max_length=4000)]
    standpoint: Annotated[str, Field(min_length=1, max_length=200)] = "中立检查"
    as_of: date
    region: Annotated[str, Field(min_length=1, max_length=120)] = "中国大陆"


class RunLimits(WireModel):
    max_steps: Annotated[int, Field(strict=True, ge=1, le=100)] = 12
    max_tool_calls: Annotated[int, Field(strict=True, ge=0, le=100)] = 16
    max_response_bytes: Annotated[int, Field(strict=True, ge=64, le=1048576)] = 65536
    max_context_bytes: Annotated[int, Field(strict=True, ge=1024, le=2097152)] = 262144
    timeout_seconds: Annotated[float, Field(gt=0, le=600, allow_inf_nan=False)] = 120


VerificationReason = Literal[
    "missing_native_coordinates", "unreadable", "missing_text", "garbled_text",
    "text_mismatch", "unconfirmed_coverage", "visual_read_failed",
]


class PageVerificationIssue(WireModel):
    """Metadata only: no model observations, quotes, or document instructions."""

    page: Annotated[int, Field(strict=True, ge=1, le=30)]
    reasons: Annotated[tuple[VerificationReason, ...], Field(min_length=1, max_length=7)]


class DocumentVerificationReport(WireModel):
    pages: Annotated[tuple[PageVerificationIssue, ...], Field(max_length=30)] = ()


class RetrievalCallReceipt(WireModel):
    """No query, source text, endpoint, or vendor payload is persisted here."""

    status: Literal["started", "success", "error"]
    collection: Literal["lawyer_windows_v2_20260920"]
    region: Annotated[str, Field(max_length=10)] = ""
    release_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    embedding_tokens: Annotated[int, Field(strict=True, ge=0)] | None = None
    rerank_tokens: Annotated[int, Field(strict=True, ge=0)] | None = None
    dense_count: Annotated[int, Field(strict=True, ge=0, le=100)] | None = None
    bm25_count: Annotated[int, Field(strict=True, ge=0, le=100)] | None = None
    rrf_count: Annotated[int, Field(strict=True, ge=0, le=100)] | None = None
    hit_count: Annotated[int, Field(strict=True, ge=0, le=7)] | None = None


class ReviewError(Exception):
    """Only a stable code crosses the boundary; provider payloads never do."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class DocumentVerificationError(ReviewError):
    def __init__(self, report: DocumentVerificationReport) -> None:
        self.report = DocumentVerificationReport.model_validate(report)
        super().__init__("document_incomplete")
