from __future__ import annotations

import base64
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Header, Response
from pydantic import Field, field_validator

from lawyer_agent.api.dependencies import (
    Services,
    TenantActorDependency,
)
from lawyer_agent.api.errors import ApiProblem
from lawyer_agent.api.v1.tenants import StrictModel
from lawyer_agent.application.matter_document_api import (
    MatterDocumentError,
    MatterDocumentHttpService,
    MatterDocumentInvalidRequest,
    MatterDocumentNotFound,
)
from lawyer_agent.domain.matter_documents import (
    DocumentVersion,
    Matter,
    MatterKind,
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
    version: int


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
