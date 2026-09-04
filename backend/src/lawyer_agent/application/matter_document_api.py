"""HTTP-facing Matter/Document orchestration (non-model).

Wraps the tenant Matter repository and the document upload service behind a
unit of work so HTTP endpoints only handle tenant actors, commands and
projections. Every read/write stays inside the tenant context; there is no
un-scoped ``get_by_id``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol, cast
from uuid import UUID

from lawyer_agent.application.documents import (
    DocumentCreateCommand,
    DocumentStorePort,
)
from lawyer_agent.application.idempotency import (
    IdempotencyFingerprintPayload,
    IdempotencyRepositoryPort,
    IdempotencyRequest,
    IdempotencyReservation,
    IdempotencyResultReference,
    IdempotencyScope,
    IdempotencyScopeType,
    IdempotencyService,
)
from lawyer_agent.domain.common import require_uuid7
from lawyer_agent.domain.matter_documents import (
    DocumentHeader,
    DocumentVersion,
    Matter,
    MatterKind,
)
from lawyer_agent.domain.tenancy import TenantContext

_MATTER_CREATE_OPERATION = "matter.create"
_MATTER_CREATE_ROUTE = "/api/v1/tenants/{tenant_id}/matters"
_MATTER_RESULT_TYPE = "matter.matter"


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
    idempotency: IdempotencyRepositoryPort

    async def __aenter__(self) -> MatterDocumentUnitOfWorkPort: ...

    async def __aexit__(self, *exc: object) -> None: ...


class MatterDocumentHttpService:
    """Composition facade used by the tenant HTTP endpoints."""

    def __init__(
        self,
        uow_factory: Callable[[], object],
        idempotency: IdempotencyService | None = None,
    ) -> None:
        if not callable(uow_factory):
            raise ValueError("matter document http service requires a unit of work factory")
        self._uow_factory = uow_factory
        self._idempotency = idempotency

    async def create_matter(
        self,
        *,
        context: TenantContext,
        title: str,
        kind: MatterKind,
        description: str | None,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> Matter:
        user_id, membership_id = _actor_ids(context)
        effective_now = now or datetime.now(UTC)
        async with cast(MatterDocumentUnitOfWorkPort, self._uow_factory()) as uow:
            try:
                if idempotency_key and self._idempotency is not None:
                    reservation = await self._reserve_matter_create(
                        uow,
                        tenant_id=context.tenant_id,
                        membership_id=membership_id,
                        title=title,
                        kind=kind,
                        description=description,
                        idempotency_key=idempotency_key,
                        now=effective_now,
                    )
                    if reservation.replay is not None:
                        matter = await uow.matters.get_matter(
                            context, reservation.replay.result_id
                        )
                        if matter is None:
                            raise MatterDocumentConflict
                        return matter
                    created = await uow.matters.create_matter(
                        tenant_id=context.tenant_id,
                        title=title,
                        kind=kind,
                        created_by_user_id=user_id,
                        created_by_membership_id=membership_id,
                        description=description,
                    )
                    await self._idempotency.complete(
                        uow.idempotency,
                        reservation,
                        IdempotencyResultReference(
                            _MATTER_RESULT_TYPE, created.id
                        ),
                        now=effective_now,
                    )
                    return created
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

    async def _reserve_matter_create(
        self,
        uow: MatterDocumentUnitOfWorkPort,
        *,
        tenant_id: UUID,
        membership_id: UUID,
        title: str,
        kind: MatterKind,
        description: str | None,
        idempotency_key: str,
        now: datetime,
    ) -> IdempotencyReservation:
        assert self._idempotency is not None
        return await self._idempotency.reserve(
            uow.idempotency,
            scope=IdempotencyScope(
                IdempotencyScopeType.MEMBERSHIP,
                membership_id,
                tenant_id=tenant_id,
            ),
            operation=_MATTER_CREATE_OPERATION,
            request=IdempotencyRequest(
                key=idempotency_key,
                method="POST",
                canonical_route=_MATTER_CREATE_ROUTE,
                body=IdempotencyFingerprintPayload(
                    values={
                        "title": title,
                        "kind": kind.value,
                        "description": description,
                    },
                    business_paths=frozenset(
                        {("title",), ("kind",), ("description",)}
                    ),
                ),
            ),
            now=now,
        )

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
