from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID

from fastapi import APIRouter, Request, Response
from pydantic import Field, field_validator

from lawyer_agent.api.dependencies import (
    Services,
    TenantActorDependency,
)
from lawyer_agent.api.errors import ApiProblem
from lawyer_agent.api.v1.tenants import StrictModel
from lawyer_agent.application.rule_check_api import (
    RuleCheckConflict,
    RuleCheckDocumentNotFound,
    RuleCheckError,
    RuleCheckHttpService,
    RuleCheckInvalidRequest,
    RuleCheckIssueNotFound,
    RuleCheckUnavailable,
)
from lawyer_agent.application.rules import ProvisionInput
from lawyer_agent.domain.rule_pack import RiskIssue, RiskIssueStatus

router = APIRouter(
    prefix="/tenants/{tenant_id}",
    tags=["rule-checks"],
)

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class ProvisionInputBody(StrictModel):
    provision_no: str = Field(min_length=1)
    text: str = Field(min_length=1)

    @field_validator("provision_no", "text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class RiskCheckRunBody(StrictModel):
    provisions: list[ProvisionInputBody] = Field(min_length=1)


class DispositionBody(StrictModel):
    status: Literal["accepted", "rejected", "modified"]
    reason: str = Field(min_length=1)

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("disposition reason must not be blank")
        return value


class RiskIssueSummary(StrictModel):
    id: UUID
    provision_no: str
    matched_text: str
    risk_level: str
    status: str
    evidence_level: str
    disposition_reason: str | None = None


def _require_rule_check_service(services: Services) -> RuleCheckHttpService:
    value = getattr(services, "rule_check_http", None)
    if value is None:
        raise ApiProblem(
            503, RuleCheckUnavailable.code, RuleCheckUnavailable.title
        )
    return cast(RuleCheckHttpService, value)


def _map_error(exc: RuleCheckError) -> ApiProblem:
    return ApiProblem(exc.status, exc.code, exc.title)


@router.post(
    "/documents/{document_id}/risk-checks",
    response_model=list[RiskIssueSummary],
    status_code=200,
)
async def run_risk_checks(
    tenant_id: UUID,
    document_id: UUID,
    body: RiskCheckRunBody,
    actor: TenantActorDependency,
    services: Services,
) -> list[RiskIssueSummary]:
    if tenant_id != actor.context.tenant_id:
        raise ApiProblem(404, "tenant_resource_not_found", "Resource not found")
    service = _require_rule_check_service(services)
    provisions = tuple(
        ProvisionInput(provision_no=item.provision_no, text=item.text)
        for item in body.provisions
    )
    try:
        created = await service.run_checks(
            context=actor.context,
            document_id=document_id,
            provisions=provisions,
        )
    except RuleCheckDocumentNotFound as exc:
        raise _map_error(exc) from None
    except RuleCheckUnavailable as exc:
        raise _map_error(exc) from None
    except RuleCheckInvalidRequest as exc:
        raise _map_error(exc) from None
    return [_summary(issue) for issue in created]


@router.get(
    "/documents/{document_id}/risk-issues",
    response_model=list[RiskIssueSummary],
    status_code=200,
)
async def list_risk_issues(
    tenant_id: UUID,
    document_id: UUID,
    actor: TenantActorDependency,
    services: Services,
) -> list[RiskIssueSummary]:
    if tenant_id != actor.context.tenant_id:
        raise ApiProblem(404, "tenant_resource_not_found", "Resource not found")
    service = _require_rule_check_service(services)
    try:
        issues = await service.list_issues(
            context=actor.context,
            document_id=document_id,
        )
    except RuleCheckDocumentNotFound as exc:
        raise _map_error(exc) from None
    return [_summary(issue) for issue in issues]


@router.post(
    "/risk-issues/{issue_id}/disposition",
    response_model=RiskIssueSummary,
    status_code=200,
)
async def dispose_risk_issue(
    tenant_id: UUID,
    issue_id: UUID,
    body: DispositionBody,
    actor: TenantActorDependency,
    services: Services,
    request: Request,
) -> RiskIssueSummary:
    if tenant_id != actor.context.tenant_id:
        raise ApiProblem(404, "tenant_resource_not_found", "Resource not found")
    service = _require_rule_check_service(services)
    try:
        issue = await service.dispose(
            context=actor.context,
            issue_id=issue_id,
            status=RiskIssueStatus(body.status),
            reason=body.reason,
            now=datetime.now(UTC),
            trace_id=getattr(request.state, "trace_id", None),
        )
    except (RuleCheckIssueNotFound, RuleCheckDocumentNotFound) as exc:
        raise _map_error(exc) from None
    except (RuleCheckConflict, RuleCheckInvalidRequest) as exc:
        raise _map_error(exc) from None
    return _summary(issue)


@router.get("/documents/{document_id}/report.docx", status_code=200)
async def export_rule_check_report(
    tenant_id: UUID,
    document_id: UUID,
    actor: TenantActorDependency,
    services: Services,
) -> Response:
    if tenant_id != actor.context.tenant_id:
        raise ApiProblem(404, "tenant_resource_not_found", "Resource not found")
    service = _require_rule_check_service(services)
    try:
        payload = await service.export_report(
            context=actor.context,
            document_id=document_id,
            now=datetime.now(UTC),
        )
    except RuleCheckDocumentNotFound as exc:
        raise _map_error(exc) from None
    return Response(
        content=payload,
        media_type=_DOCX_MIME,
        headers={
            "Content-Disposition": 'attachment; filename="rule-check-report.docx"'
        },
    )


def _summary(issue: RiskIssue) -> RiskIssueSummary:
    return RiskIssueSummary(
        id=issue.id,
        provision_no=issue.provision_no,
        matched_text=issue.matched_text,
        risk_level=issue.risk_level.value,
        status=issue.status.value,
        evidence_level=issue.evidence_level,
        disposition_reason=issue.disposition_reason,
    )
