"""Tenant HTTP-facing contracts; no FastAPI, SDK or database dependency."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Annotated, Literal, Protocol, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from lawyer_agent.application.contract_review.contracts import (
    DocumentBlock,
    DocumentVerificationReport,
    Identifier,
    RiskIssueDraft,
    WireModel,
    WithheldReviewIssue,
)
from lawyer_agent.application.tenancy import TenantActor

from .research import NO_EVIDENCE_MESSAGE

DRAFT_MESSAGE = "AI风险草稿，引用匹配不等于法律结论成立；法规效力、适用性及建议须由律师或法务复核"
_PageValue = Annotated[float, Field(gt=0, le=20000, allow_inf_nan=False)]
PageDimension = tuple[_PageValue, _PageValue]


class CreateReviewInput(WireModel):
    file_name: Annotated[str, Field(min_length=1, max_length=255)]
    instruction: Annotated[str, Field(max_length=4000)] = ""
    as_of: date | None = None

    @field_validator("file_name")
    @classmethod
    def pdf_name(cls, value: str) -> str:
        if (
            not value.lower().endswith(".pdf") or "/" in value or "\\" in value
            or len(value.encode("utf-8")) > 255 or any(ord(ch) < 32 for ch in value)
        ):
            raise ValueError("a PDF filename is required")
        return value


class EvidenceDocumentLabel(WireModel):
    document_id: Identifier
    title: Annotated[str, Field(min_length=1, max_length=4000)]


class McpToolCatalogItem(WireModel):
    """Safe MCP discovery projection; never contains a provider schema."""

    name: Annotated[str, Field(min_length=1, max_length=120)]
    capability: Annotated[str, Field(min_length=1, max_length=120)]
    description: Annotated[str, Field(min_length=1, max_length=500)]
    status: Literal["discovered", "authorized"]


class ContractRunSummary(WireModel):
    id: UUID
    file_name: str
    status: str
    created_at: datetime
    updated_at: datetime


class ContractRunPage(WireModel):
    items: tuple[ContractRunSummary, ...]
    next_cursor: str | None = None


class ContractRunView(WireModel):
    id: UUID
    status: Literal[
        "awaiting_upload", "uploaded", "running", "draft", "no_evidence", "failed", "cancelled",
    ]
    file_name: str
    document_version_id: str | None = None
    page_dimensions: tuple[tuple[float, float], ...] = ()
    blocks: tuple[DocumentBlock, ...] = ()
    issues: tuple[RiskIssueDraft, ...] = ()
    evidence_documents: Annotated[tuple[EvidenceDocumentLabel, ...], Field(max_length=7)] = ()
    mcp_tools: Annotated[tuple[McpToolCatalogItem, ...], Field(max_length=32)] = ()
    is_demo: bool = False
    withheld_issues: tuple[WithheldReviewIssue, ...] = ()
    failure_code: str | None = None
    verification_report: DocumentVerificationReport = DocumentVerificationReport()
    message: str | None = None


class ContractReviewFinal(WireModel):
    """Bounded public result; internal/provider messages never cross HTTP."""

    id: UUID
    status: Literal["draft", "no_evidence"]
    file_name: Annotated[str, Field(min_length=1, max_length=255)]
    document_version_id: Identifier | None = None
    page_dimensions: Annotated[tuple[PageDimension, ...], Field(max_length=30)] = ()
    blocks: Annotated[tuple[DocumentBlock, ...], Field(max_length=2000)] = ()
    issues: Annotated[tuple[RiskIssueDraft, ...], Field(max_length=100)] = ()
    evidence_documents: Annotated[tuple[EvidenceDocumentLabel, ...], Field(max_length=7)] = ()
    mcp_tools: Annotated[tuple[McpToolCatalogItem, ...], Field(max_length=32)] = ()
    is_demo: bool = False
    withheld_issues: Annotated[tuple[WithheldReviewIssue, ...], Field(max_length=100)] = ()
    verification_report: DocumentVerificationReport = DocumentVerificationReport()
    message: Annotated[str, Field(min_length=1, max_length=64)]

    @model_validator(mode="after")
    def fixed_message_for_state(self) -> Self:
        expected = (
            NO_EVIDENCE_MESSAGE if self.status == "no_evidence" else DRAFT_MESSAGE
        )
        if self.message != expected:
            raise ValueError("final message must be derived from status")
        return self

    @classmethod
    def from_run(cls, run: ContractRunView) -> ContractReviewFinal:
        if run.status not in {"draft", "no_evidence"}:
            raise ValueError("contract run is not publishable")
        status: Literal["draft", "no_evidence"] = (
            "draft" if run.status == "draft" else "no_evidence"
        )
        return cls(
            id=run.id,
            status=status,
            file_name=run.file_name,
            document_version_id=run.document_version_id,
            page_dimensions=run.page_dimensions,
            blocks=run.blocks,
            issues=run.issues,
            evidence_documents=run.evidence_documents,
            mcp_tools=run.mcp_tools,
            is_demo=run.is_demo,
            withheld_issues=run.withheld_issues,
            verification_report=run.verification_report,
            message=(
                NO_EVIDENCE_MESSAGE
                if run.status == "no_evidence"
                else DRAFT_MESSAGE
            ),
        )


class ContractReviewWeb(Protocol):
    async def list_owned(self, actor: TenantActor, *, limit: int = 30,
                         cursor: str | None = None) -> ContractRunPage: ...

    async def create(
        self, actor: TenantActor, body: CreateReviewInput, idempotency_key: str,
    ) -> ContractRunView: ...

    async def upload(
        self, actor: TenantActor, run_id: UUID, payload: bytes,
    ) -> ContractRunView: ...

    async def get(self, actor: TenantActor, run_id: UUID) -> ContractRunView: ...

    async def document(self, actor: TenantActor, run_id: UUID) -> bytes: ...

    async def run(
        self, actor: TenantActor, run_id: UUID, on_progress: Callable[[str], None],
    ) -> ContractRunView: ...

    async def cancel(self, actor: TenantActor, run_id: UUID) -> None: ...
