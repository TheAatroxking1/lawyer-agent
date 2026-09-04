from __future__ import annotations

from datetime import datetime
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Query

from lawyer_agent.api.dependencies import (
    Services,
    TenantActorDependency,
)
from lawyer_agent.api.errors import ApiProblem
from lawyer_agent.api.v1.tenants import StrictModel
from lawyer_agent.application.audit_query_api import (
    AuditQueryError,
    AuditQueryHttpService,
    AuditQueryInvalidRequest,
)
from lawyer_agent.infrastructure.persistence.repositories.audit import TenantAuditRow

router = APIRouter(
    prefix="/tenants/{tenant_id}",
    tags=["tenant-audit"],
)


class AuditEventSummary(StrictModel):
    id: UUID
    actor_kind: str | None = None
    action: str
    result: str
    reason_code: str
    target_type: str | None = None
    target_id: UUID | None = None
    trace_id: str
    occurred_at: datetime


class AuditPage(StrictModel):
    events: list[AuditEventSummary]
    next_before_id: UUID | None = None


def _require_service(services: Services) -> AuditQueryHttpService:
    value = getattr(services, "audit_query_http", None)
    if value is None:
        raise ApiProblem(503, AuditQueryError.code, AuditQueryError.title)
    return cast(AuditQueryHttpService, value)


def _map_error(exc: AuditQueryError) -> ApiProblem:
    return ApiProblem(exc.status, exc.code, exc.title)


@router.get("/audit", response_model=AuditPage)
async def list_tenant_audit(
    tenant_id: UUID,
    actor: TenantActorDependency,
    services: Services,
    action: Annotated[str | None, Query(alias="action")] = None,
    target_type: Annotated[str | None, Query(alias="target_type")] = None,
    trace_id: Annotated[str | None, Query(alias="trace_id", max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    before_id: Annotated[UUID | None, Query(alias="before_id")] = None,
) -> AuditPage:
    if tenant_id != actor.context.tenant_id:
        raise ApiProblem(404, "tenant_resource_not_found", "Resource not found")
    service = _require_service(services)
    try:
        rows = await service.list_audit(
            context=actor.context,
            action_prefix=action,
            target_type=target_type,
            trace_id=trace_id,
            limit=limit,
            before_id=before_id,
        )
    except AuditQueryInvalidRequest as exc:
        raise _map_error(exc) from None
    events = [_summary(row) for row in rows]
    next_cursor = rows[-1].id if len(rows) == limit else None
    return AuditPage(events=events, next_before_id=next_cursor)


def _summary(row: TenantAuditRow) -> AuditEventSummary:
    return AuditEventSummary(
        id=row.id,
        actor_kind=row.actor_kind,
        action=row.action,
        result=row.result,
        reason_code=row.reason_code,
        target_type=row.target_type,
        target_id=row.target_id,
        trace_id=row.trace_id,
        occurred_at=row.occurred_at,
    )
