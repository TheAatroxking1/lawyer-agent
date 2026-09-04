"""HTTP-facing Matter/Document orchestration (non-model).

Wraps the tenant Matter repository and the document upload service behind a
unit of work so HTTP endpoints only handle tenant actors, commands and
projections. Every read/write stays inside the tenant context; there is no
un-scoped ``get_by_id``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, cast
from uuid import UUID

from lawyer_agent.application.documents import (
    DocumentCreateCommand,
    DocumentStorePort,
)
from lawyer_agent.domain.common import require_uuid7
from lawyer_agent.domain.matter_documents import (
    DocumentHeader,
    DocumentVersion,
    Matter,
    MatterKind,
)
from lawyer_agent.domain.tenancy import TenantContext


class MatterDocumentError(Exception):
    """Base class carrying a stable Problem Details code."""

    status: int = 500
    code: str = "matter_document_error"
    title: str = "Matter or document operation failed"


class MatterDocumentNotFound(MatterDocumentError):
    status = 404
    code = "matter_document_not_found"
    title = "Matter not found for this tenant"


class MatterDocumentConflict(MatterDocumentError):
    status = 409
    code = "matter_document_conflict"
    title = "Matter or document request conflicts with current state"


class MatterDocumentInvalidRequest(MatterDocumentError):
    status = 422
    code = "matter_document_invalid_request"
    title = "Matter or document request is invalid"


class MatterStorePort(Protocol):
    async def get_matter(
        self, context: TenantContext, matter_id: UUID
    ) -> Matter | None: ...

    async def create_matter(
        self,
        *,
        tenant_id: UUID,
        title: str,
        kind: MatterKind,
        created_by_user_id: UUID,
        created_by_membership_id: UUID,
        description: str | None = None,
    ) -> Matter: ...


class DocumentHeaderStorePort(Protocol):
    async def headers_for_matter(
        self, tenant_id: UUID, matter_id: UUID
    ) -> tuple[DocumentHeader, ...]: ...


class _DocumentStoreForUow(DocumentStorePort, DocumentHeaderStorePort, Protocol):
    pass


class MatterDocumentUnitOfWorkPort(Protocol):
    matters: MatterStorePort
    documents: _DocumentStoreForUow
    upload: object

    async def __aenter__(self) -> MatterDocumentUnitOfWorkPort: ...

    async def __aexit__(self, *exc: object) -> None: ...


class MatterDocumentHttpService:
    """Composition facade used by the tenant HTTP endpoints."""

    def __init__(self, uow_factory: Callable[[], object]) -> None:
        if not callable(uow_factory):
            raise ValueError("matter document http service requires a unit of work factory")
        self._uow_factory = uow_factory

    async def create_matter(
        self,
        *,
        context: TenantContext,
        title: str,
        kind: MatterKind,
        description: str | None,
    ) -> Matter:
        user_id, membership_id = _actor_ids(context)
        async with cast(MatterDocumentUnitOfWorkPort, self._uow_factory()) as uow:
            try:
                matter = await uow.matters.create_matter(
                    tenant_id=context.tenant_id,
                    title=title,
                    kind=kind,
                    created_by_user_id=user_id,
                    created_by_membership_id=membership_id,
                    description=description,
                )
            except ValueError as exc:
                raise MatterDocumentInvalidRequest from exc
            return matter

    async def get_matter(
        self, *, context: TenantContext, matter_id: UUID
    ) -> Matter:
        require_uuid7(matter_id, field="matter_id")
        async with cast(MatterDocumentUnitOfWorkPort, self._uow_factory()) as uow:
            matter = await uow.matters.get_matter(context, matter_id)
            if matter is None:
                raise MatterDocumentNotFound
            return matter

    async def register_document(
        self,
        *,
        context: TenantContext,
        matter_id: UUID,
        file_name: str,
        mime_type: str,
        payload: bytes,
    ) -> DocumentVersion:
        require_uuid7(matter_id, field="matter_id")
        if not isinstance(payload, bytes) or not payload:
            raise MatterDocumentInvalidRequest
        user_id, membership_id = _actor_ids(context)
        async with cast(MatterDocumentUnitOfWorkPort, self._uow_factory()) as uow:
            await self._require_matter(uow, context, matter_id)
            try:
                version = await _complete_upload(
                    uow, DocumentCreateCommand(
                        tenant_id=context.tenant_id,
                        matter_id=matter_id,
                        file_name=file_name,
                        mime_type=mime_type,
                        payload=payload,
                        created_by_user_id=user_id,
                        created_by_membership_id=membership_id,
                    )
                )
            except ValueError as exc:
                raise MatterDocumentInvalidRequest from exc
            return version

    async def list_documents(
        self, *, context: TenantContext, matter_id: UUID
    ) -> tuple[DocumentHeader, ...]:
        require_uuid7(matter_id, field="matter_id")
        async with cast(MatterDocumentUnitOfWorkPort, self._uow_factory()) as uow:
            await self._require_matter(uow, context, matter_id)
            return tuple(
                await uow.documents.headers_for_matter(context.tenant_id, matter_id)
            )

    async def _require_matter(
        self, uow: MatterDocumentUnitOfWorkPort, context: TenantContext, matter_id: UUID
    ) -> None:
        matter = await uow.matters.get_matter(context, matter_id)
        if matter is None:
            raise MatterDocumentNotFound


async def _complete_upload(
    uow: MatterDocumentUnitOfWorkPort, command: DocumentCreateCommand
) -> DocumentVersion:
    upload = uow.upload
    complete = getattr(upload, "complete", None)
    if complete is None:
        raise MatterDocumentConflict
    version = await complete(command)
    if not isinstance(version, DocumentVersion):
        raise MatterDocumentConflict
    return version


def _actor_ids(context: TenantContext) -> tuple[UUID, UUID]:
    user_id = context.membership_user_id
    membership_id = context.membership_id
    if user_id is None or membership_id is None:
        raise MatterDocumentConflict
    require_uuid7(user_id, field="actor user_id")
    require_uuid7(membership_id, field="actor membership_id")
    return user_id, membership_id
