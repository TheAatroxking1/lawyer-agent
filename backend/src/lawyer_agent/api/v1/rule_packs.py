from __future__ import annotations

from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Header, Request, Response
from pydantic import Field, field_validator

from lawyer_agent.api.dependencies import (
    Services,
    TenantActorDependency,
)
from lawyer_agent.api.errors import ApiProblem
from lawyer_agent.api.v1.tenants import StrictModel
from lawyer_agent.application.rule_pack_admin_api import (
    RulePackAdminConflict,
    RulePackAdminError,
    RulePackAdminHttpService,
    RulePackAdminInvalidRequest,
    RulePackAdminNotFound,
)
from lawyer_agent.domain.rule_pack import (
    RiskLevel,
    RulePack,
    RulePackRule,
    RuleTriggerKind,
)

router = APIRouter(
    prefix="/tenants/{tenant_id}",
    tags=["rule-pack-admin"],
)


class CreateRulePackBody(StrictModel):
    name: str = Field(min_length=1, max_length=256)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be blank")
        return value


class RulePackSummary(StrictModel):
    id: UUID
    name: str
    version: int
    active: bool


class RulePackRuleBody(StrictModel):
    trigger_kind: Literal["clause_type", "risk_phrase"]
    label: str = Field(min_length=1)
    pattern: str = Field(min_length=1)
    risk_level: Literal["low", "medium", "high"]
    suggestion: str = Field(min_length=1)

    @field_validator("label", "suggestion", "pattern")
    @classmethod
    def _text_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class RuleSummary(StrictModel):
    id: UUID
    trigger_kind: str
    label: str
    risk_level: str
    enabled: bool


class PatchRuleBody(StrictModel):
    enabled: bool


def _require_service(services: Services) -> RulePackAdminHttpService:
    value = getattr(services, "rule_pack_admin_http", None)
    if value is None:
        raise ApiProblem(503, RulePackAdminError.code, RulePackAdminError.title)
    return cast(RulePackAdminHttpService, value)


def _map_error(exc: RulePackAdminError) -> ApiProblem:
    return ApiProblem(exc.status, exc.code, exc.title)


@router.post("/rule-packs", response_model=RulePackSummary, status_code=201)
async def create_rule_pack(
    tenant_id: UUID,
    body: CreateRulePackBody,
    actor: TenantActorDependency,
    services: Services,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> RulePackSummary:
    if tenant_id != actor.context.tenant_id:
        raise ApiProblem(404, "tenant_resource_not_found", "Resource not found")
    service = _require_service(services)
    try:
        pack = await service.create_pack(
            context=actor.context,
            name=body.name.strip(),
            idempotency_key=idempotency_key,
        )
    except RulePackAdminInvalidRequest as exc:
        raise _map_error(exc) from None
    return _pack_summary(pack)


@router.get("/rule-packs", response_model=list[RulePackSummary])
async def list_rule_packs(
    tenant_id: UUID,
    actor: TenantActorDependency,
    services: Services,
) -> list[RulePackSummary]:
    if tenant_id != actor.context.tenant_id:
        raise ApiProblem(404, "tenant_resource_not_found", "Resource not found")
    service = _require_service(services)
    packs = await service.list_packs(context=actor.context)
    return [_pack_summary(pack) for pack in packs]


@router.post(
    "/rule-packs/{pack_id}/rules",
    response_model=RuleSummary,
    status_code=201,
)
async def add_rule(
    tenant_id: UUID,
    pack_id: UUID,
    body: RulePackRuleBody,
    actor: TenantActorDependency,
    services: Services,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> RuleSummary:
    if tenant_id != actor.context.tenant_id:
        raise ApiProblem(404, "tenant_resource_not_found", "Resource not found")
    service = _require_service(services)
    try:
        rule = await service.add_rule(
            context=actor.context,
            pack_id=pack_id,
            trigger_kind=RuleTriggerKind(body.trigger_kind),
            label=body.label.strip(),
            pattern=body.pattern,
            risk_level=RiskLevel(body.risk_level),
            suggestion=body.suggestion.strip(),
            idempotency_key=idempotency_key,
        )
    except (RulePackAdminNotFound, RulePackAdminInvalidRequest) as exc:
        raise _map_error(exc) from None
    return _rule_summary(rule)


@router.patch(
    "/rule-packs/{pack_id}/rules/{rule_id}",
    response_model=None,
)
async def set_rule_enabled(
    tenant_id: UUID,
    pack_id: UUID,
    rule_id: UUID,
    body: PatchRuleBody,
    actor: TenantActorDependency,
    services: Services,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Response:
    if tenant_id != actor.context.tenant_id:
        raise ApiProblem(404, "tenant_resource_not_found", "Resource not found")
    service = _require_service(services)
    try:
        await service.set_rule_enabled(
            context=actor.context,
            pack_id=pack_id,
            rule_id=rule_id,
            enabled=body.enabled,
            idempotency_key=idempotency_key,
        )
    except (RulePackAdminNotFound, RulePackAdminInvalidRequest) as exc:
        raise _map_error(exc) from None
    response.status_code = 204
    return response


@router.post(
    "/rule-packs/{pack_id}/activate",
    response_model=RulePackSummary,
)
async def activate_rule_pack(
    tenant_id: UUID,
    pack_id: UUID,
    actor: TenantActorDependency,
    services: Services,
    request: Request,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> RulePackSummary:
    if tenant_id != actor.context.tenant_id:
        raise ApiProblem(404, "tenant_resource_not_found", "Resource not found")
    service = _require_service(services)
    try:
        pack = await service.activate_pack(
            context=actor.context,
            pack_id=pack_id,
            idempotency_key=idempotency_key,
            trace_id=getattr(request.state, "trace_id", None),
        )
    except (RulePackAdminNotFound, RulePackAdminConflict) as exc:
        raise _map_error(exc) from None
    return _pack_summary(pack)


def _pack_summary(pack: RulePack) -> RulePackSummary:
    return RulePackSummary(
        id=pack.id,
        name=pack.name,
        version=pack.version,
        active=bool(pack.active),
    )


def _rule_summary(rule: RulePackRule) -> RuleSummary:
    return RuleSummary(
        id=rule.id,
        trigger_kind=rule.trigger_kind.value,
        label=rule.label,
        risk_level=rule.risk_level.value,
        enabled=bool(rule.enabled),
    )
