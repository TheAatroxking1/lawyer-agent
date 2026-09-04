from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Response
from pydantic import BaseModel, ConfigDict, Field

from lawyer_agent.api.dependencies import (
    Services,
    TenantActorDependency,
)
from lawyer_agent.api.errors import ApiProblem
from lawyer_agent.application.ai_job_service import (
    AIJobAccessDenied,
    AIJobNotFound,
    AIJobServiceUnavailable,
    CreateAIJobCommand,
)
from lawyer_agent.application.tenancy import TenantActor
from lawyer_agent.domain.ai_jobs import PublicAIJobStatus, to_public_status

router = APIRouter(prefix="/tenants/{tenant_id}/ai-jobs", tags=["ai-jobs"])


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateAIJobBody(StrictModel):
    kind: Literal["synthetic.v1"]
    scenario: Literal["success", "retry_once", "permanent_failure", "wait_for_cancel"]
    work_units: int = Field(ge=1, le=10)


class CreateAIJobResponse(StrictModel):
    job_id: UUID
    status: PublicAIJobStatus


def _require_ai_job_service(services: Services) -> Any:
    value = getattr(services, "ai_jobs", None)
    if value is None:
        raise ApiProblem(
            503, AIJobServiceUnavailable.code, "AI Job handler is unavailable"
        )
    return value


def _require_membership(actor: TenantActor) -> UUID:
    if actor.context.membership_id is None or actor.context.authz_version is None:
        raise ApiProblem(401, "authentication_failed", "Authentication failed")
    return actor.context.membership_id


def _require_authz_version(actor: TenantActor) -> int:
    if actor.context.authz_version is None:
        raise ApiProblem(401, "authentication_failed", "Authentication failed")
    return actor.context.authz_version


@router.post("", status_code=202, response_model=CreateAIJobResponse)
async def create_ai_job(
    tenant_id: UUID,
    body: CreateAIJobBody,
    actor: TenantActorDependency,
    services: Services,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> CreateAIJobResponse:
    if tenant_id != actor.context.tenant_id:
        raise ApiProblem(404, "tenant_resource_not_found", "Resource not found")
    service = _require_ai_job_service(services)
    command = CreateAIJobCommand(
        tenant_id=actor.context.tenant_id,
        membership_id=_require_membership(actor),
        user_id=actor.principal.user_id,
        session_id=actor.principal.session_id,
        auth_version=actor.principal.auth_version,
        authz_version=_require_authz_version(actor),
        idempotency_key=idempotency_key,
        kind=body.kind,
        scenario=body.scenario,
        work_units=body.work_units,
    )
    try:
        accepted = await service.create(command)
    except AIJobAccessDenied as exc:
        raise ApiProblem(403, exc.code, "Action is not allowed") from None
    return CreateAIJobResponse(
        job_id=accepted.job_id,
        status=PublicAIJobStatus.QUEUED,
    )


@router.get("/{job_id}", response_model=CreateAIJobResponse)
async def get_ai_job(
    tenant_id: UUID,
    job_id: UUID,
    actor: TenantActorDependency,
    services: Services,
    response: Response,
) -> CreateAIJobResponse:
    if tenant_id != actor.context.tenant_id:
        raise ApiProblem(404, "tenant_resource_not_found", "Resource not found")
    service = _require_ai_job_service(services)
    try:
        projection = await service.get(actor.context, job_id)
    except AIJobNotFound as exc:
        raise ApiProblem(404, exc.code, "Resource not found") from None
    job = projection.job
    response.headers["ETag"] = f'"{job.version}"'
    return CreateAIJobResponse(
        job_id=job.id,
        status=to_public_status(job.status),
    )


@router.post("/{job_id}/cancel", status_code=200)
async def cancel_ai_job(
    tenant_id: UUID,
    job_id: UUID,
    actor: TenantActorDependency,
    services: Services,
) -> Response:
    if tenant_id != actor.context.tenant_id:
        raise ApiProblem(404, "tenant_resource_not_found", "Resource not found")
    service = _require_ai_job_service(services)
    try:
        await service.cancel(actor.context, job_id)
    except AIJobNotFound as exc:
        raise ApiProblem(404, exc.code, "Resource not found") from None
    return Response(status_code=200)
