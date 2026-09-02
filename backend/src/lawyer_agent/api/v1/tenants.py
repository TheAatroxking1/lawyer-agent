from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from lawyer_agent.api.dependencies import (
    AccountSession,
    Audit,
    Services,
    TenantActorDependency,
    require_path_tenant,
)
from lawyer_agent.application.invitations import (
    CreateInvitationCommand,
    InvitationTargetKind,
)
from lawyer_agent.application.tenancy import (
    CreateTenantApplicationCommand,
    MemberPageQuery,
    RevokeMemberCommand,
    StrongETag,
    TenantType,
    UpdateMemberCommand,
    UpdateTenantCommand,
)
from lawyer_agent.domain.identity import IdentityKind, normalize_identifier
from lawyer_agent.domain.tenancy import MembershipStatus

router = APIRouter(prefix="/tenants", tags=["tenants"])


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TenantResponse(StrictModel):
    id: UUID
    name: str
    tenant_type: str
    status: str
    review_status: str
    version: int


class MembershipResponse(StrictModel):
    id: UUID
    tenant_id: UUID
    user_id: UUID
    department_id: UUID | None
    member_type: str
    status: str
    valid_from: datetime
    valid_until: datetime | None
    version: int
    role_ids: tuple[UUID, ...] = ()


class TenantApplicationResponse(StrictModel):
    tenant: TenantResponse
    owner_membership: MembershipResponse
    replayed: bool


class MemberListResponse(StrictModel):
    items: tuple[MembershipResponse, ...]
    next_cursor: str | None


class InvitationResponse(StrictModel):
    invitation_id: UUID
    tenant_id: UUID
    expires_at: datetime
    status: str
    replayed: bool


class CreateTenantRequest(StrictModel):
    name: str = Field(min_length=1, max_length=255)
    tenant_type: Literal["law_firm", "enterprise", "university"]

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("tenant name must not be blank")
        return value


class UpdateTenantRequest(StrictModel):
    name: str = Field(min_length=1, max_length=255)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("tenant name must not be blank")
        return value


class UpdateMemberRequest(StrictModel):
    status: Literal["active", "suspended"] | None = None
    role_ids: tuple[UUID, ...] | None = None
    change_department: bool = False
    department_id: UUID | None = None


class CreateInvitationRequest(StrictModel):
    target_kind: Literal["phone", "email"]
    target: str = Field(min_length=1, max_length=512)
    role_ids: tuple[UUID, ...]
    expires_at: datetime

    @field_validator("target")
    @classmethod
    def validate_target(cls, value: str, info: ValidationInfo) -> str:
        kind = info.data.get("target_kind")
        if isinstance(kind, str):
            normalize_identifier(IdentityKind(kind), value)
        return value

    @field_validator("role_ids")
    @classmethod
    def validate_role_ids(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if not value or len(set(value)) != len(value):
            raise ValueError("invitation role_ids must be non-empty and unique")
        return value

    @field_validator("expires_at")
    @classmethod
    def validate_expires_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("invitation expires_at must be UTC-aware")
        return value


def _tenant(value: Any) -> TenantResponse:
    return TenantResponse(
        id=value.id,
        name=value.name,
        tenant_type=value.tenant_type,
        status=value.status,
        review_status=value.review_status,
        version=value.version,
    )


def _membership(value: Any, role_ids: tuple[UUID, ...] = ()) -> MembershipResponse:
    return MembershipResponse(
        id=value.id,
        tenant_id=value.tenant_id,
        user_id=value.user_id,
        department_id=value.department_id,
        member_type=value.member_type,
        status=value.status,
        valid_from=value.valid_from,
        valid_until=value.valid_until,
        version=value.version,
        role_ids=role_ids,
    )


@router.post("", response_model=TenantApplicationResponse, status_code=201)
async def create_tenant(
    body: CreateTenantRequest,
    current: AccountSession,
    services: Services,
    audit: Audit,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> TenantApplicationResponse:
    result = await services.tenancy.create_application(
        CreateTenantApplicationCommand(
            actor_user_id=current.user_id,
            name=body.name,
            tenant_type=TenantType(body.tenant_type),
            idempotency_key=idempotency_key,
            audit_context=audit,
        )
    )
    return TenantApplicationResponse(
        tenant=_tenant(result.tenant),
        owner_membership=_membership(result.owner_membership),
        replayed=result.replayed,
    )


@router.get("/{tenant_id}", response_model=TenantResponse)
async def get_tenant(
    tenant_id: UUID,
    actor: TenantActorDependency,
    services: Services,
    response: Response,
) -> TenantResponse:
    require_path_tenant(tenant_id, actor)
    tenant = await services.tenancy.get(actor)
    response.headers["ETag"] = StrongETag.format(tenant.version)
    return _tenant(tenant)


@router.patch("/{tenant_id}", response_model=TenantResponse)
async def update_tenant(
    tenant_id: UUID,
    body: UpdateTenantRequest,
    actor: TenantActorDependency,
    services: Services,
    audit: Audit,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> TenantResponse:
    require_path_tenant(tenant_id, actor)
    result = await services.tenancy.update(
        actor,
        UpdateTenantCommand(
            name=body.name,
            expected_version=StrongETag.parse(if_match).version,
            idempotency_key=idempotency_key,
            audit_context=audit,
        ),
    )
    response.headers["ETag"] = StrongETag.format(result.tenant.version)
    return _tenant(result.tenant)


@router.get("/{tenant_id}/members", response_model=MemberListResponse)
async def list_members(
    tenant_id: UUID,
    actor: TenantActorDependency,
    services: Services,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
) -> MemberListResponse:
    require_path_tenant(tenant_id, actor)
    page = await services.tenancy.list_members(actor, MemberPageQuery(limit, cursor))
    return MemberListResponse(
        items=tuple(
            _membership(item.membership, item.role_ids)
            for item in page.items
        ),
        next_cursor=page.next_cursor,
    )


@router.post(
    "/{tenant_id}/invitations",
    response_model=InvitationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_invitation(
    tenant_id: UUID,
    body: CreateInvitationRequest,
    actor: TenantActorDependency,
    services: Services,
    audit: Audit,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> InvitationResponse:
    require_path_tenant(tenant_id, actor)
    services.invitation_delivery.require()
    result = await services.invitations.create(
        actor,
        CreateInvitationCommand(
            target_kind=InvitationTargetKind(body.target_kind),
            target=body.target,
            role_ids=tuple(sorted(body.role_ids, key=str)),
            expires_at=body.expires_at,
            idempotency_key=idempotency_key,
            audit_context=audit,
        ),
    )
    return InvitationResponse.model_validate(result, from_attributes=True)


@router.patch("/{tenant_id}/members/{membership_id}", response_model=MembershipResponse)
async def update_member(
    tenant_id: UUID,
    membership_id: UUID,
    body: UpdateMemberRequest,
    actor: TenantActorDependency,
    services: Services,
    audit: Audit,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> MembershipResponse:
    require_path_tenant(tenant_id, actor)
    result = await services.tenancy.update_member(
        actor,
        UpdateMemberCommand(
            membership_id=membership_id,
            expected_version=StrongETag.parse(if_match).version,
            idempotency_key=idempotency_key,
            audit_context=audit,
            status=None if body.status is None else MembershipStatus(body.status),
            role_ids=(
                None
                if body.role_ids is None
                else tuple(sorted(body.role_ids, key=str))
            ),
            change_department=body.change_department,
            department_id=body.department_id,
        ),
    )
    response.headers["ETag"] = StrongETag.format(result.membership.version)
    return _membership(result.membership, result.role_ids)


@router.delete(
    "/{tenant_id}/members/{membership_id}",
    response_model=MembershipResponse,
)
async def delete_member(
    tenant_id: UUID,
    membership_id: UUID,
    actor: TenantActorDependency,
    services: Services,
    audit: Audit,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> MembershipResponse:
    require_path_tenant(tenant_id, actor)
    result = await services.tenancy.revoke_member(
        actor,
        RevokeMemberCommand(
            membership_id=membership_id,
            expected_version=StrongETag.parse(if_match).version,
            idempotency_key=idempotency_key,
            audit_context=audit,
        ),
    )
    response.headers["ETag"] = StrongETag.format(result.membership.version)
    return _membership(result.membership, result.role_ids)
