"""HTTP-facing Document Review orchestration (non-model).

Applies a human review decision to a specific uploaded document version inside
the tenant context. Every read/write stays tenant-scoped.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, cast
from uuid import UUID

from lawyer_agent.application.audit import new_tenant_user_audit_event
from lawyer_agent.domain.common import require_uuid7
from lawyer_agent.domain.document_review import (
    InvalidReviewDecision,
    InvalidReviewTransition,
    ReviewDecision,
)
from lawyer_agent.domain.matter_documents import DocumentVersion
from lawyer_agent.domain.tenancy import TenantContext


class DocumentReviewError(Exception):
    status: int = 500
    code: str = "document_review_error"
    title: str = "Document review failed"


class DocumentReviewNotFound(DocumentReviewError):
    status = 404
    code = "document_review_not_found"
    title = "Document version not found for this tenant"


class DocumentReviewConflict(DocumentReviewError):
    status = 409
    code = "document_review_conflict"
    title = "Review transition is not allowed from the current state"


class DocumentReviewInvalidRequest(DocumentReviewError):
    status = 422
    code = "document_review_invalid_request"
    title = "Document review request is invalid"


class DocumentReviewStorePort(Protocol):
    async def review_document_version(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        version_no: int,
        decision: ReviewDecision,
        reason: str | None,
    ) -> DocumentVersion: ...


class DocumentReviewUnitOfWorkPort(Protocol):
    documents: DocumentReviewStorePort

    async def __aenter__(self) -> DocumentReviewUnitOfWorkPort: ...

    async def __aexit__(self, *exc: object) -> None: ...


class DocumentReviewHttpService:
    """Composition facade used by the tenant HTTP endpoint."""

    def __init__(self, uow_factory: Callable[[], object]) -> None:
        if not callable(uow_factory):
            raise ValueError("document review http service requires a unit of work factory")
        self._uow_factory = uow_factory

    async def apply_review(
        self,
        *,
        context: TenantContext,
        document_id: UUID,
        version_no: int,
        decision: ReviewDecision,
        reason: str | None,
        trace_id: str | None = None,
    ) -> DocumentVersion:
        require_uuid7(document_id, field="document_id")
        async with cast(DocumentReviewUnitOfWorkPort, self._uow_factory()) as uow:
            try:
                version = await uow.documents.review_document_version(
                    tenant_id=context.tenant_id,
                    document_id=document_id,
                    version_no=version_no,
                    decision=decision,
                    reason=reason,
                )
            except ValueError as exc:
                raise _map_domain_error(exc) from exc
            await _append_audit(
                uow,
                context=context,
                action="document.review",
                reason_code="reviewed",
                target_type="document_version",
                target_id=version.id,
                trace_id=trace_id,
            )
            return version


async def _append_audit(
    uow: object,
    *,
    context: TenantContext,
    action: str,
    reason_code: str,
    target_type: str,
    target_id: UUID,
    trace_id: str | None,
) -> None:
    audit = getattr(uow, "audit", None)
    user_id = context.membership_user_id
    membership_id = context.membership_id
    if audit is None or user_id is None or membership_id is None:
        return
    event = new_tenant_user_audit_event(
        tenant_id=context.tenant_id,
        actor_user_id=user_id,
        actor_membership_id=membership_id,
        action=action,
        reason_code=reason_code,
        trace_id=trace_id or "http",
        target_type=target_type,
        target_id=target_id,
        result="success",
    )
    await audit.append_structured(event)


def _map_domain_error(exc: ValueError) -> DocumentReviewError:
    from lawyer_agent.infrastructure.persistence.repositories.documents import (
        ReviewTargetNotFound,
    )

    if isinstance(exc, ReviewTargetNotFound):
        return DocumentReviewNotFound()
    if isinstance(exc, InvalidReviewTransition):
        return DocumentReviewConflict()
    if isinstance(exc, InvalidReviewDecision):
        return DocumentReviewInvalidRequest()
    return DocumentReviewInvalidRequest()
