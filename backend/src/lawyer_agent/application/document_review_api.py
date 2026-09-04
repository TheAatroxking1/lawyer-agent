"""HTTP-facing Document Review orchestration (non-model).

Applies a human review decision to a specific uploaded document version inside
the tenant context. Every read/write stays tenant-scoped.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol, cast
from uuid import UUID

from lawyer_agent.application.audit import new_tenant_user_audit_event
from lawyer_agent.application.idempotency import (
    IdempotencyFingerprintPayload,
    IdempotencyRepositoryPort,
    IdempotencyRequest,
    IdempotencyReservation,
    IdempotencyResultReference,
    IdempotencyScope,
    IdempotencyScopeType,
    IdempotencyService,
)
from lawyer_agent.domain.common import require_uuid7
from lawyer_agent.domain.document_review import (
    InvalidReviewDecision,
    InvalidReviewTransition,
    ReviewDecision,
)
from lawyer_agent.domain.matter_documents import DocumentVersion
from lawyer_agent.domain.tenancy import TenantContext

_REVIEW_OPERATION = "document.review"
_REVIEW_ROUTE = (
    "/api/v1/tenants/{tenant_id}/documents/{document_id}/versions/{version_no}/review"
)
_REVIEW_RESULT_TYPE = "document_version"


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

    async def find_version(
        self,
        tenant_id: UUID,
        document_id: UUID,
        version_no: int,
    ) -> DocumentVersion | None: ...


class DocumentReviewUnitOfWorkPort(Protocol):
    documents: DocumentReviewStorePort
    idempotency: IdempotencyRepositoryPort

    async def __aenter__(self) -> DocumentReviewUnitOfWorkPort: ...

    async def __aexit__(self, *exc: object) -> None: ...


class DocumentReviewHttpService:
    """Composition facade used by the tenant HTTP endpoint."""

    def __init__(
        self,
        uow_factory: Callable[[], object],
        idempotency: IdempotencyService | None = None,
    ) -> None:
        if not callable(uow_factory):
            raise ValueError("document review http service requires a unit of work factory")
        if idempotency is not None and not isinstance(idempotency, IdempotencyService):
            raise ValueError("document review service requires an idempotency service")
        self._uow_factory = uow_factory
        self._idempotency = idempotency

    async def get_version(
        self,
        *,
        context: TenantContext,
        document_id: UUID,
        version_no: int,
    ) -> DocumentVersion:
        """Read one tenant document version's upload/review state (read-only)."""
        require_uuid7(document_id, field="document_id")
        if isinstance(version_no, bool) or not isinstance(version_no, int) or version_no < 1:
            raise DocumentReviewInvalidRequest
        async with cast(DocumentReviewUnitOfWorkPort, self._uow_factory()) as uow:
            version = await uow.documents.find_version(
                context.tenant_id, document_id, version_no
            )
            if version is None:
                raise DocumentReviewNotFound
            return version

    async def apply_review(
        self,
        *,
        context: TenantContext,
        document_id: UUID,
        version_no: int,
        decision: ReviewDecision,
        reason: str | None,
        idempotency_key: str | None = None,
        trace_id: str | None = None,
        now: datetime | None = None,
    ) -> DocumentVersion:
        require_uuid7(document_id, field="document_id")
        membership_id = context.membership_id
        effective_now = now or datetime.now(UTC)
        reservation = None
        try:
            async with cast(DocumentReviewUnitOfWorkPort, self._uow_factory()) as uow:
                if idempotency_key and self._idempotency is not None:
                    reservation = await self._reserve_review(
                        uow,
                        context=context,
                        document_id=document_id,
                        version_no=version_no,
                        decision=decision,
                        reason=reason,
                        idempotency_key=idempotency_key,
                        now=effective_now,
                    )
                    if reservation.is_replay:
                        assert reservation.replay is not None
                        version = await uow.documents.find_version(
                            context.tenant_id, document_id, version_no
                        )
                        if version is None:
                            raise DocumentReviewConflict
                        return version
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
                if reservation is not None:
                    assert self._idempotency is not None
                    assert membership_id is not None
                    await self._idempotency.complete(
                        uow.idempotency,
                        reservation,
                        IdempotencyResultReference(
                            _REVIEW_RESULT_TYPE, version.id
                        ),
                        now=effective_now,
                    )
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
        except DocumentReviewNotFound as exc:
            await _append_rejected_audit(
                self._uow_factory,
                context=context,
                action="document.review",
                result="denied",
                reason_code=exc.code,
                target_type="document",
                target_id=document_id,
                trace_id=trace_id,
            )
            raise
        except DocumentReviewConflict as exc:
            await _append_rejected_audit(
                self._uow_factory,
                context=context,
                action="document.review",
                result="denied",
                reason_code=exc.code,
                target_type="document",
                target_id=document_id,
                trace_id=trace_id,
            )
            raise
        except DocumentReviewInvalidRequest as exc:
            await _append_rejected_audit(
                self._uow_factory,
                context=context,
                action="document.review",
                result="failure",
                reason_code=exc.code,
                target_type="document",
                target_id=document_id,
                trace_id=trace_id,
            )
            raise

    async def _reserve_review(
        self,
        uow: DocumentReviewUnitOfWorkPort,
        *,
        context: TenantContext,
        document_id: UUID,
        version_no: int,
        decision: ReviewDecision,
        reason: str | None,
        idempotency_key: str,
        now: datetime,
    ) -> IdempotencyReservation:
        assert self._idempotency is not None
        membership_id = context.membership_id
        if membership_id is None:
            raise DocumentReviewConflict
        return await self._idempotency.reserve(
            uow.idempotency,
            scope=IdempotencyScope(
                IdempotencyScopeType.MEMBERSHIP,
                membership_id,
                tenant_id=context.tenant_id,
            ),
            operation=_REVIEW_OPERATION,
            request=IdempotencyRequest(
                key=idempotency_key,
                method="POST",
                canonical_route=_REVIEW_ROUTE,
                body=IdempotencyFingerprintPayload(
                    values={
                        "document_id": str(document_id),
                        "version_no": version_no,
                        "decision": decision.value,
                        "reason": reason,
                    },
                    business_paths=frozenset(
                        {
                            ("document_id",),
                            ("version_no",),
                            ("decision",),
                            ("reason",),
                        }
                    ),
                ),
            ),
            now=now,
        )


async def _append_rejected_audit(
    uow_factory: Callable[[], object],
    *,
    context: TenantContext,
    action: str,
    result: str,
    reason_code: str,
    target_type: str,
    target_id: UUID,
    trace_id: str | None,
) -> None:
    """Record a rejected/failed review attempt in its own committed transaction.

    The failing business UoW has already rolled back when an error handler calls
    this, so the audit append opens a fresh short-lived UoW whose clean exit
    commits only the audit row.
    """
    async with cast(DocumentReviewUnitOfWorkPort, uow_factory()) as uow:
        await _append_audit(
            uow,
            context=context,
            action=action,
            reason_code=reason_code,
            target_type=target_type,
            target_id=target_id,
            trace_id=trace_id,
            result=result,
        )


async def _append_audit(
    uow: object,
    *,
    context: TenantContext,
    action: str,
    reason_code: str,
    target_type: str,
    target_id: UUID,
    trace_id: str | None,
    result: str = "success",
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
        result=result,
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
