from __future__ import annotations

import base64
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response
from pydantic import Field, field_validator, model_validator

from lawyer_agent.api.dependencies import (
    Services,
    TenantActorDependency,
)
from lawyer_agent.api.errors import ApiProblem
from lawyer_agent.api.v1.tenants import StrictModel
from lawyer_agent.application.matter_document_api import (
    MatterDocumentConflict,
    MatterDocumentError,
    MatterDocumentHttpService,
    MatterDocumentInvalidRequest,
    MatterDocumentNotFound,
)
from lawyer_agent.application.tenancy import StrongETag
from lawyer_agent.domain.matter_documents import (
    DocumentVersion,
    Matter,
    MatterKind,
    MatterParty,
    MatterStatus,
)

router = APIRouter(
    prefix="/tenants/{tenant_id}",
    tags=["matter-documents"],
)


class CreateMatterBody(StrictModel):
    title: str = Field(min_length=1)
    kind: Literal[
        "contract_review", "litigation", "legal_advice", "compliance", "other"
    ]
    description: str | None = None

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must not be blank")
        return value


class MatterSummary(StrictModel):
    id: UUID
    title: str
    kind: str
    status: str
    description: str | None = None
    owner_membership_id: UUID | None = None
    version: int


class MatterPage(StrictModel):
    items: list[MatterSummary]
    next_before_id: UUID | None = None


class RegisterDocumentBody(StrictModel):
    file_name: str = Field(min_length=1)
    mime_type: str = Field(min_length=1)
    payload_b64: str = Field(min_length=1)

    @field_validator("file_name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("file name must not be blank")
        return value

    @field_validator("mime_type")
    @classmethod
    def _mime_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("mime type must not be blank")
        return value

    @field_validator("payload_b64")
    @classmethod
    def _payload_decodes(cls, value: str) -> str:
        try:
            decoded = base64.b64decode(value, validate=True)
        except Exception as exc:  # noqa: BLE001 - mapped to validation error
            raise ValueError("payload must be valid base64") from exc
        if not decoded:
            raise ValueError("payload must not be empty")
        return value


class DocumentSummary(StrictModel):
    id: UUID
    document_id: UUID
    version_no: int
    kind: str
    file_name: str | None = None
    upload_status: str
    review_status: str | None = None


class DocumentHeaderSummary(StrictModel):
    id: UUID
    display_name: str
    current_version_no: int


class CreatePartyBody(StrictModel):
    display_name: str = Field(min_length=1, max_length=256)
    kind: str = Field(min_length=1, max_length=64)

    @field_validator("display_name", "kind")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class UpdatePartyBody(StrictModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=256)
    kind: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("display_name", "kind")
    @classmethod
    def _strip_optional(cls, value: str | None) -> str | None:
        if value is not None:
            stripped = value.strip()
            if not stripped:
                raise ValueError("must not be blank")
            return stripped
        return value

    @model_validator(mode="after")
    def _at_least_one_change(self) -> UpdatePartyBody:
        if self.display_name is None and self.kind is None:
            raise ValueError("at least one of display_name or kind is required")
        return self


class PartySummary(StrictModel):
    id: UUID
    matter_id: UUID
    display_name: str
    kind: str
    version: int


class PartyConflictCheckBody(StrictModel):
    display_name: str = Field(min_length=1, max_length=256)
    kind: str | None = Field(default=None, min_length=1, max_length=64)
    exclude_matter_id: UUID | None = None

    @field_validator("display_name", "kind")
    @classmethod
    def _not_blank_optional(cls, value: str | None) -> str | None:
        if value is not None:
            stripped = value.strip()
            if not stripped:
                raise ValueError("must not be blank")
            return stripped
        return value


class PartyConflictCheckResponse(StrictModel):
    conflict: bool
    other_matter_count: int


def _require_service(services: Services) -> MatterDocumentHttpService:
    value = getattr(services, "matter_document_http", None)
    if value is None:
        raise ApiProblem(503, MatterDocumentError.code, MatterDocumentError.title)
    return cast(MatterDocumentHttpService, value)


def _map_error(exc: MatterDocumentError) -> ApiProblem:
    return ApiProblem(exc.status, exc.code, exc.title)


@router.post("/matters", response_model=MatterSummary, status_code=201)
async def create_matter(
    tenant_id: UUID,
    body: CreateMatterBody,
    actor: TenantActorDependency,
    services: Services,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> MatterSummary:
    if tenant_id != actor.context.tenant_id:
        raise ApiProblem(404, "tenant_resource_not_found", "Resource not found")
    service = _require_service(services)
    try:
        matter = await service.create_matter(
            context=actor.context,
            title=body.title,
            kind=MatterKind(body.kind),
            description=body.description,
            idempotency_key=idempotency_key,
        )
    except MatterDocumentInvalidRequest as exc:
        raise _map_error(exc) from None
    return _matter_summary(matter)


class MatterStatusBody(StrictModel):
    status: Literal["open", "active", "closed", "archived"]


class MatterStatusResponse(StrictModel):
    matter: MatterSummary


class AssignMatterOwnerBody(StrictModel):
    owner_membership_id: UUID


class UpdateMatterMetadataBody(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=512)
    description: str | None = Field(default=None, max_length=4000)

    @field_validator("title")
    @classmethod
    def _title_not_blank_optional(cls, value: str | None) -> str | None:
        if value is not None:
            stripped = value.strip()
            if not stripped:
                raise ValueError("title must not be blank")
            return stripped
        return value

    @model_validator(mode="after")
    def _at_least_one_field(self) -> UpdateMatterMetadataBody:
        if self.title is None and self.description is None:
            raise ValueError("at least one of title or description is required")
        return self


@router.patch("/matters/{matter_id}", response_model=MatterStatusResponse, status_code=200)
async def update_matter_metadata(
    tenant_id: UUID,
    matter_id: UUID,
    body: UpdateMatterMetadataBody,
    actor: TenantActorDependency,
    services: Services,
    response: Response,
    request: Request,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> MatterStatusResponse:
    if tenant_id != actor.context.tenant_id:
        raise ApiProblem(404, "tenant_resource_not_found", "Resource not found")
    service = _require_service(services)
    expected_version = None if if_match is None else StrongETag.parse(if_match).version
    try:
        matter = await service.update_matter_metadata(
            context=actor.context,
            matter_id=matter_id,
            expected_version=expected_version,
            title=body.title,
            description=body.description,
            trace_id=getattr(request.state, "trace_id", None),
        )
    except (MatterDocumentNotFound, MatterDocumentInvalidRequest, MatterDocumentConflict) as exc:
        raise _map_error(exc) from None
    response.headers["ETag"] = StrongETag.format(matter.version)
    return MatterStatusResponse(matter=_matter_summary(matter))


@router.put(
    "/matters/{matter_id}/owner",
    response_model=MatterStatusResponse,
    status_code=200,
)
async def assign_matter_owner(
    tenant_id: UUID,
    matter_id: UUID,
    body: AssignMatterOwnerBody,
    actor: TenantActorDependency,
    services: Services,
    response: Response,
    request: Request,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> MatterStatusResponse:
    if tenant_id != actor.context.tenant_id:
        raise ApiProblem(404, "tenant_resource_not_found", "Resource not found")
    service = _require_service(services)
    expected_version = (
        None if if_match is None else StrongETag.parse(if_match).version
    )
    try:
        matter = await service.assign_matter_owner(
            context=actor.context,
            matter_id=matter_id,
            expected_version=expected_version,
            owner_membership_id=body.owner_membership_id,
            trace_id=getattr(request.state, "trace_id", None),
        )
    except (MatterDocumentNotFound, MatterDocumentInvalidRequest, MatterDocumentConflict) as exc:
        raise _map_error(exc) from None
    response.headers["ETag"] = StrongETag.format(matter.version)
    return MatterStatusResponse(matter=_matter_summary(matter))


@router.get("/matters", response_model=MatterPage)
async def list_matters(
    tenant_id: UUID,
    actor: TenantActorDependency,
    services: Services,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    before_id: Annotated[UUID | None, Query(alias="before_id")] = None,
    title: Annotated[str | None, Query(max_length=512)] = None,
    status: Annotated[str | None, Query()] = None,
    kind: Annotated[str | None, Query()] = None,
) -> MatterPage:
    if tenant_id != actor.context.tenant_id:
        raise ApiProblem(404, "tenant_resource_not_found", "Resource not found")
    service = _require_service(services)
    try:
        matters = await service.list_matters(
            context=actor.context,
            limit=limit,
            before_id=before_id,
            title=title,
            status=status,
            kind=kind,
        )
    except (MatterDocumentNotFound, MatterDocumentInvalidRequest) as exc:
        raise _map_error(exc) from None
    items = [_matter_summary(matter) for matter in matters]
    next_cursor = items[-1].id if len(items) == limit else None
    return MatterPage(items=items, next_before_id=next_cursor)


@router.post(
    "/matters/{matter_id}/status",
    response_model=MatterStatusResponse,
    status_code=200,
)
async def transition_matter_status(
    tenant_id: UUID,
    matter_id: UUID,
    body: MatterStatusBody,
    actor: TenantActorDependency,
    services: Services,
    response: Response,
    request: Request,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> MatterStatusResponse:
    if tenant_id != actor.context.tenant_id:
        raise ApiProblem(404, "tenant_resource_not_found", "Resource not found")
    service = _require_service(services)
    expected_version = (
        None if if_match is None else StrongETag.parse(if_match).version
    )
    try:
        matter = await service.transition_matter_status(
            context=actor.context,
            matter_id=matter_id,
            expected_version=expected_version,
            target_status=MatterStatus(body.status),
            trace_id=getattr(request.state, "trace_id", None),
        )
    except (MatterDocumentNotFound, MatterDocumentConflict) as exc:
        raise _map_error(exc) from None
    response.headers["ETag"] = StrongETag.format(matter.version)
    return MatterStatusResponse(matter=_matter_summary(matter))


@router.get("/matters/{matter_id}", response_model=MatterSummary)
async def get_matter(
    tenant_id: UUID,
    matter_id: UUID,
    actor: TenantActorDependency,
    services: Services,
    response: Response,
) -> MatterSummary:
    if tenant_id != actor.context.tenant_id:
        raise ApiProblem(404, "tenant_resource_not_found", "Resource not found")
    service = _require_service(services)
    try:
        matter = await service.get_matter(context=actor.context, matter_id=matter_id)
    except MatterDocumentNotFound as exc:
        raise _map_error(exc) from None
    response.headers["ETag"] = f'"{matter.version}"'
    return _matter_summary(matter)


@router.post(
    "/matters/{matter_id}/documents",
    response_model=DocumentSummary,
    status_code=201,
)
async def register_document(
    tenant_id: UUID,
    matter_id: UUID,
    body: RegisterDocumentBody,
    actor: TenantActorDependency,
    services: Services,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> DocumentSummary:
    if tenant_id != actor.context.tenant_id:
        raise ApiProblem(404, "tenant_resource_not_found", "Resource not found")
    service = _require_service(services)
    try:
        payload = base64.b64decode(body.payload_b64, validate=True)
        version = await service.register_document(
            context=actor.context,
            matter_id=matter_id,
            file_name=body.file_name,
            mime_type=body.mime_type,
            payload=payload,
            idempotency_key=idempotency_key,
        )
    except (MatterDocumentNotFound, MatterDocumentInvalidRequest) as exc:
        raise _map_error(exc) from None
    return _document_summary(version)


@router.get(
    "/matters/{matter_id}/documents",
    response_model=list[DocumentHeaderSummary],
)
async def list_documents(
    tenant_id: UUID,
    matter_id: UUID,
    actor: TenantActorDependency,
    services: Services,
) -> list[DocumentHeaderSummary]:
    if tenant_id != actor.context.tenant_id:
        raise ApiProblem(404, "tenant_resource_not_found", "Resource not found")
    service = _require_service(services)
    try:
        headers = await service.list_documents(
            context=actor.context, matter_id=matter_id
        )
    except MatterDocumentNotFound as exc:
        raise _map_error(exc) from None
    return [
        DocumentHeaderSummary(
            id=header.id,
            display_name=header.display_name,
            current_version_no=header.current_version_no,
        )
        for header in headers
    ]


def _matter_summary(matter: Matter) -> MatterSummary:
    return MatterSummary(
        id=matter.id,
        title=matter.title,
        kind=matter.kind.value,
        status=matter.status.value,
        description=matter.description,
        owner_membership_id=matter.owner_membership_id,
        version=matter.version,
    )


def _document_summary(version: DocumentVersion) -> DocumentSummary:
    return DocumentSummary(
        id=version.id,
        document_id=version.document_id,
        version_no=version.version_no,
        kind=version.kind.value,
        file_name=version.file_name,
        upload_status=version.upload_status.value,
        review_status=version.review_status.value
        if version.review_status is not None
        else None,
    )


@router.post(
    "/matters/{matter_id}/parties",
    response_model=PartySummary,
    status_code=201,
)
async def add_matter_party(
    tenant_id: UUID,
    matter_id: UUID,
    body: CreatePartyBody,
    actor: TenantActorDependency,
    services: Services,
    request: Request,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> PartySummary:
    if tenant_id != actor.context.tenant_id:
        raise ApiProblem(404, "tenant_resource_not_found", "Resource not found")
    service = _require_service(services)
    try:
        party = await service.add_party(
            context=actor.context,
            matter_id=matter_id,
            display_name=body.display_name.strip(),
            kind=body.kind.strip(),
            idempotency_key=idempotency_key,
            trace_id=getattr(request.state, "trace_id", None),
        )
    except (MatterDocumentNotFound, MatterDocumentInvalidRequest) as exc:
        raise _map_error(exc) from None
    return _party_summary(party)


@router.get(
    "/matters/{matter_id}/parties",
    response_model=list[PartySummary],
)
async def list_matter_parties(
    tenant_id: UUID,
    matter_id: UUID,
    actor: TenantActorDependency,
    services: Services,
) -> list[PartySummary]:
    if tenant_id != actor.context.tenant_id:
        raise ApiProblem(404, "tenant_resource_not_found", "Resource not found")
    service = _require_service(services)
    try:
        parties = await service.list_parties(
            context=actor.context, matter_id=matter_id
        )
    except MatterDocumentNotFound as exc:
        raise _map_error(exc) from None
    return [_party_summary(party) for party in parties]


@router.post(
    "/matter-party-conflict-checks",
    response_model=PartyConflictCheckResponse,
    status_code=200,
)
async def check_matter_party_conflicts(
    tenant_id: UUID,
    body: PartyConflictCheckBody,
    actor: TenantActorDependency,
    services: Services,
) -> PartyConflictCheckResponse:
    if tenant_id != actor.context.tenant_id:
        raise ApiProblem(404, "tenant_resource_not_found", "Resource not found")
    service = _require_service(services)
    try:
        result = await service.check_party_conflicts(
            context=actor.context,
            display_name=body.display_name,
            kind=body.kind,
            exclude_matter_id=body.exclude_matter_id,
        )
    except MatterDocumentInvalidRequest as exc:
        raise _map_error(exc) from None
    return PartyConflictCheckResponse(
        conflict=result.conflict,
        other_matter_count=result.other_matter_count,
    )


@router.patch(
    "/matters/{matter_id}/parties/{party_id}",
    response_model=PartySummary,
    status_code=200,
)
async def update_matter_party(
    tenant_id: UUID,
    matter_id: UUID,
    party_id: UUID,
    body: UpdatePartyBody,
    actor: TenantActorDependency,
    services: Services,
    request: Request,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> PartySummary:
    if tenant_id != actor.context.tenant_id:
        raise ApiProblem(404, "tenant_resource_not_found", "Resource not found")
    service = _require_service(services)
    try:
        party = await service.update_party(
            context=actor.context,
            matter_id=matter_id,
            party_id=party_id,
            display_name=body.display_name,
            kind=body.kind,
            idempotency_key=idempotency_key,
            trace_id=getattr(request.state, "trace_id", None),
        )
    except (MatterDocumentNotFound, MatterDocumentInvalidRequest) as exc:
        raise _map_error(exc) from None
    return _party_summary(party)


@router.delete(
    "/matters/{matter_id}/parties/{party_id}",
    status_code=204,
)
async def remove_matter_party(
    tenant_id: UUID,
    matter_id: UUID,
    party_id: UUID,
    actor: TenantActorDependency,
    services: Services,
    response: Response,
    request: Request,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Response:
    if tenant_id != actor.context.tenant_id:
        raise ApiProblem(404, "tenant_resource_not_found", "Resource not found")
    service = _require_service(services)
    try:
        await service.remove_party(
            context=actor.context,
            matter_id=matter_id,
            party_id=party_id,
            idempotency_key=idempotency_key,
            trace_id=getattr(request.state, "trace_id", None),
        )
    except MatterDocumentNotFound as exc:
        raise _map_error(exc) from None
    response.status_code = 204
    return response


def _party_summary(party: MatterParty) -> PartySummary:
    return PartySummary(
        id=party.id,
        matter_id=party.matter_id,
        display_name=party.display_name,
        kind=party.kind,
        version=party.version,
    )
