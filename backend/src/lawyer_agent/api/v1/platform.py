from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query
from pydantic import BaseModel, ConfigDict, Field

from lawyer_agent.api.dependencies import Audit, PlatformActorDependency, Services
from lawyer_agent.application.identity import AuditContext
from lawyer_agent.application.platform import (
    PlatformActor,
    ReviewDecision,
    ReviewTenantApplicationCommand,
)
from lawyer_agent.domain.sessions import StepUpGrant

router = APIRouter(prefix="/platform/tenant-applications", tags=["platform"])


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReviewRequest(StrictModel):
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")


class PlatformApplicationResponse(StrictModel):
    tenant_id: UUID
    name: str
    tenant_type: str
    status: str
    review_status: str
    created_at: datetime
    version: int


class PlatformApplicationListResponse(StrictModel):
    items: tuple[PlatformApplicationResponse, ...]


class PlatformReviewResponse(StrictModel):
    application: PlatformApplicationResponse
    replayed: bool


@router.get("", response_model=PlatformApplicationListResponse)
async def list_applications(
    actor: PlatformActorDependency,
    services: Services,
    audit: Audit,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> PlatformApplicationListResponse:
    items = await services.platform.list(actor, audit_context=audit, limit=limit)
    return PlatformApplicationListResponse(
        items=tuple(
            PlatformApplicationResponse.model_validate(item, from_attributes=True)
            for item in items
        )
    )


async def _review(
    *,
    tenant_id: UUID,
    decision: Literal["approve", "reject"],
    body: ReviewRequest,
    actor: PlatformActor,
    services: Services,
    audit: AuditContext,
    idempotency_key: str,
    step_up_grant: str,
) -> PlatformReviewResponse:
    command = ReviewTenantApplicationCommand(
        tenant_id=tenant_id,
        decision=ReviewDecision(decision),
        reason_code=body.reason_code,
        step_up_grant=StepUpGrant(step_up_grant),
        idempotency_key=idempotency_key,
        audit_context=audit,
    )
    method = services.platform.approve if decision == "approve" else services.platform.reject
    result = await method(actor, command)
    return PlatformReviewResponse(
        application=PlatformApplicationResponse.model_validate(
            result.application,
            from_attributes=True,
        ),
        replayed=result.replayed,
    )


@router.post("/{tenant_id}/approve", response_model=PlatformReviewResponse)
async def approve(
    tenant_id: UUID,
    body: ReviewRequest,
    actor: PlatformActorDependency,
    services: Services,
    audit: Audit,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    step_up_grant: Annotated[str, Header(alias="X-Step-Up-Grant")],
) -> PlatformReviewResponse:
    return await _review(
        tenant_id=tenant_id,
        decision="approve",
        body=body,
        actor=actor,
        services=services,
        audit=audit,
        idempotency_key=idempotency_key,
        step_up_grant=step_up_grant,
    )


@router.post("/{tenant_id}/reject", response_model=PlatformReviewResponse)
async def reject(
    tenant_id: UUID,
    body: ReviewRequest,
    actor: PlatformActorDependency,
    services: Services,
    audit: Audit,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    step_up_grant: Annotated[str, Header(alias="X-Step-Up-Grant")],
) -> PlatformReviewResponse:
    return await _review(
        tenant_id=tenant_id,
        decision="reject",
        body=body,
        actor=actor,
        services=services,
        audit=audit,
        idempotency_key=idempotency_key,
        step_up_grant=step_up_grant,
    )
