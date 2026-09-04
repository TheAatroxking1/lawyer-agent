from __future__ import annotations

from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Header, Request, Response
from pydantic import Field

from lawyer_agent.api.dependencies import (
    Services,
    TenantActorDependency,
)
from lawyer_agent.api.errors import ApiProblem
from lawyer_agent.api.v1.tenants import StrictModel
from lawyer_agent.application.document_review_api import (
    DocumentReviewConflict,
    DocumentReviewError,
    DocumentReviewHttpService,
    DocumentReviewInvalidRequest,
    DocumentReviewNotFound,
)
from lawyer_agent.domain.document_review import ReviewDecision

router = APIRouter(
    prefix="/tenants/{tenant_id}",
    tags=["document-reviews"],
)


class DocumentReviewBody(StrictModel):
    decision: Literal["submit", "approve", "reject", "request_changes"]
    reason: str | None = Field(default=None, max_length=2000)


class DocumentReviewResponse(StrictModel):
    document_id: UUID
    version_no: int
    review_status: str | None
    review_reason: str | None = None


class DocumentVersionResponse(StrictModel):
    """White-list read projection of one tenant document version."""

    document_id: UUID
    version_no: int
    kind: str
    file_name: str | None = None
    upload_status: str
    review_status: str | None = None
    review_reason: str | None = None


def _require_service(services: Services) -> DocumentReviewHttpService:
    value = getattr(services, "document_review_http", None)
    if value is None:
        raise ApiProblem(503, DocumentReviewError.code, DocumentReviewError.title)
    return cast(DocumentReviewHttpService, value)


def _map_error(exc: DocumentReviewError) -> ApiProblem:
    return ApiProblem(exc.status, exc.code, exc.title)


@router.post(
    "/documents/{document_id}/versions/{version_no}/review",
    response_model=DocumentReviewResponse,
)
async def apply_document_review(
    tenant_id: UUID,
    document_id: UUID,
    version_no: int,
    body: DocumentReviewBody,
    actor: TenantActorDependency,
    services: Services,
    response: Response,
    request: Request,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> DocumentReviewResponse:
    if tenant_id != actor.context.tenant_id:
        raise ApiProblem(404, "tenant_resource_not_found", "Resource not found")
    service = _require_service(services)
    try:
        version = await service.apply_review(
            context=actor.context,
            document_id=document_id,
            version_no=version_no,
            decision=ReviewDecision(body.decision),
            reason=body.reason,
            idempotency_key=idempotency_key,
            trace_id=getattr(request.state, "trace_id", None),
        )
    except DocumentReviewNotFound as exc:
        raise _map_error(exc) from None
    except (DocumentReviewConflict, DocumentReviewInvalidRequest) as exc:
        raise _map_error(exc) from None
    response.headers["ETag"] = f'"{version.version_no}"'
    return DocumentReviewResponse(
        document_id=version.document_id,
        version_no=version.version_no,
        review_status=version.review_status.value
        if version.review_status is not None
        else None,
        review_reason=version.review_reason,
    )


@router.get(
    "/documents/{document_id}/versions/{version_no}",
    response_model=DocumentVersionResponse,
)
async def get_document_version(
    tenant_id: UUID,
    document_id: UUID,
    version_no: int,
    actor: TenantActorDependency,
    services: Services,
    response: Response,
) -> DocumentVersionResponse:
    if tenant_id != actor.context.tenant_id:
        raise ApiProblem(404, "tenant_resource_not_found", "Resource not found")
    service = _require_service(services)
    try:
        version = await service.get_version(
            context=actor.context,
            document_id=document_id,
            version_no=version_no,
        )
    except (DocumentReviewNotFound, DocumentReviewInvalidRequest) as exc:
        raise _map_error(exc) from None
    response.headers["ETag"] = f'"{version.version_no}"'
    return DocumentVersionResponse(
        document_id=version.document_id,
        version_no=version.version_no,
        kind=version.kind.value,
        file_name=version.file_name,
        upload_status=version.upload_status.value,
        review_status=version.review_status.value
        if version.review_status is not None
        else None,
        review_reason=version.review_reason,
    )
