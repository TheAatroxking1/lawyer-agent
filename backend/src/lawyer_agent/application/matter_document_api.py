"""HTTP-facing Matter/Document orchestration (non-model).

Wraps the tenant Matter repository and the document upload service behind a
unit of work so HTTP endpoints only handle tenant actors, commands and
projections. Every read/write stays inside the tenant context; there is no
un-scoped ``get_by_id``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from typing import Protocol, cast
from uuid import UUID

from lawyer_agent.application.audit import new_tenant_user_audit_event
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
    MAX_MATTER_TITLE_CHARS,
    DocumentHeader,
    DocumentVersion,
    Matter,
    MatterKind,
    MatterParty,
    MatterStatus,
    PartyConflictCheck,
)
from lawyer_agent.domain.tenancy import TenantContext

_MATTER_CREATE_OPERATION = "matter.create"
_MATTER_CREATE_ROUTE = "/api/v1/tenants/{tenant_id}/matters"
_MATTER_RESULT_TYPE = "matter.matter"
_DOCUMENT_REGISTER_OPERATION = "document.register"
_DOCUMENT_REGISTER_ROUTE = (
    "/api/v1/tenants/{tenant_id}/matters/{matter_id}/documents"
)
_DOCUMENT_RESULT_TYPE = "document.document_version"
_PARTY_ADD_OPERATION = "matter.party.add"
_PARTY_ADD_ROUTE = "/api/v1/tenants/{tenant_id}/matters/{matter_id}/parties"
_PARTY_UPDATE_OPERATION = "matter.party.update"
_PARTY_UPDATE_ROUTE = (
    "/api/v1/tenants/{tenant_id}/matters/{matter_id}/parties/{party_id}"
)
_PARTY_REMOVE_OPERATION = "matter.party.remove"
_PARTY_REMOVE_ROUTE = _PARTY_UPDATE_ROUTE
_PARTY_RESULT_TYPE = "matter_party"


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

    async def list_matters(
        self,
        context: TenantContext,
        *,
        limit: int,
        before_id: UUID | None = None,
        title: str | None = None,
        status: MatterStatus | None = None,
        kind: MatterKind | None = None,
    ) -> tuple[Matter, ...]: ...

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

    async def add_party(
        self,
        *,
        tenant_id: UUID,
        matter_id: UUID,
        display_name: str,
        kind: str,
    ) -> MatterParty: ...

    async def list_parties(
        self, context: TenantContext, matter_id: UUID
    ) -> tuple[MatterParty, ...]: ...

    async def update_party(
        self,
        *,
        tenant_id: UUID,
        matter_id: UUID,
        party_id: UUID,
        display_name: str | None,
        kind: str | None,
    ) -> MatterParty | None: ...

    async def remove_party(
        self,
        *,
        tenant_id: UUID,
        matter_id: UUID,
        party_id: UUID,
    ) -> bool: ...

    async def count_other_party_matters(
        self,
        *,
        tenant_id: UUID,
        display_name: str,
        kind: str | None,
        exclude_matter_id: UUID | None,
    ) -> int: ...

    async def transition_matter_status(
        self,
        context: TenantContext,
        matter_id: UUID,
        *,
        expected_version: int,
        target_status: MatterStatus,
    ) -> Matter | None: ...

    async def update_matter_metadata(
        self,
        context: TenantContext,
        matter_id: UUID,
        *,
        expected_version: int,
        title: str | None,
        description: str | None,
    ) -> Matter | None: ...

    async def assign_owner(
        self,
        context: TenantContext,
        matter_id: UUID,
        *,
        expected_version: int,
        owner_membership_id: UUID,
    ) -> Matter | None: ...

class DocumentHeaderStorePort(Protocol):
    async def headers_for_matter(
        self, tenant_id: UUID, matter_id: UUID
    ) -> tuple[DocumentHeader, ...]: ...


class _DocumentStoreForUow(DocumentStorePort, DocumentHeaderStorePort, Protocol):
    async def find_version(
        self, tenant_id: UUID, document_id: UUID
    ) -> DocumentVersion | None: ...


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

    async def list_matters(
        self,
        *,
        context: TenantContext,
        limit: int,
        before_id: UUID | None = None,
        title: str | None = None,
        status: str | None = None,
        kind: str | None = None,
    ) -> tuple[Matter, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise MatterDocumentInvalidRequest
        if before_id is not None:
            require_uuid7(before_id, field="before_id")
        stripped_title: str | None = None
        if title is not None:
            if not isinstance(title, str) or not title.strip():
                raise MatterDocumentInvalidRequest
            if len(title.strip()) > MAX_MATTER_TITLE_CHARS:
                raise MatterDocumentInvalidRequest
            stripped_title = title.strip()
        parsed_status: MatterStatus | None = None
        if status is not None:
            try:
                parsed_status = MatterStatus(status)
            except ValueError as exc:
                raise MatterDocumentInvalidRequest from exc
        parsed_kind: MatterKind | None = None
        if kind is not None:
            try:
                parsed_kind = MatterKind(kind)
            except ValueError as exc:
                raise MatterDocumentInvalidRequest from exc
        async with cast(MatterDocumentUnitOfWorkPort, self._uow_factory()) as uow:
            from lawyer_agent.infrastructure.persistence.repositories.matters import (
                MatterListCursorInvalid,
            )

            try:
                return await uow.matters.list_matters(
                    context,
                    limit=limit,
                    before_id=before_id,
                    title=stripped_title,
                    status=parsed_status,
                    kind=parsed_kind,
                )
            except MatterListCursorInvalid as exc:
                raise MatterDocumentNotFound from exc
            except ValueError as exc:
                raise MatterDocumentInvalidRequest from exc

    async def register_document(
        self,
        *,
        context: TenantContext,
        matter_id: UUID,
        file_name: str,
        mime_type: str,
        payload: bytes,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> DocumentVersion:
        require_uuid7(matter_id, field="matter_id")
        if not isinstance(payload, bytes) or not payload:
            raise MatterDocumentInvalidRequest
        user_id, membership_id = _actor_ids(context)
        effective_now = now or datetime.now(UTC)
        async with cast(MatterDocumentUnitOfWorkPort, self._uow_factory()) as uow:
            await self._require_matter(uow, context, matter_id)
            try:
                if idempotency_key and self._idempotency is not None:
                    reservation = await self._reserve_document_register(
                        uow,
                        tenant_id=context.tenant_id,
                        membership_id=membership_id,
                        matter_id=matter_id,
                        file_name=file_name,
                        mime_type=mime_type,
                        payload=payload,
                        idempotency_key=idempotency_key,
                        now=effective_now,
                    )
                    if reservation.replay is not None:
                        document_id = reservation.replay.result_id
                        version = await uow.documents.find_version(
                            context.tenant_id, document_id
                        )
                        if version is None:
                            raise MatterDocumentConflict
                        return version
                    created = await _complete_upload(
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
                    await self._idempotency.complete(
                        uow.idempotency,
                        reservation,
                        IdempotencyResultReference(
                            _DOCUMENT_RESULT_TYPE, created.document_id
                        ),
                        now=effective_now,
                    )
                    return created
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

    async def _reserve_document_register(
        self,
        uow: MatterDocumentUnitOfWorkPort,
        *,
        tenant_id: UUID,
        membership_id: UUID,
        matter_id: UUID,
        file_name: str,
        mime_type: str,
        payload: bytes,
        idempotency_key: str,
        now: datetime,
    ) -> IdempotencyReservation:
        assert self._idempotency is not None
        payload_sha256 = sha256(payload).digest()
        return await self._idempotency.reserve(
            uow.idempotency,
            scope=IdempotencyScope(
                IdempotencyScopeType.MEMBERSHIP,
                membership_id,
                tenant_id=tenant_id,
            ),
            operation=_DOCUMENT_REGISTER_OPERATION,
            request=IdempotencyRequest(
                key=idempotency_key,
                method="POST",
                canonical_route=_DOCUMENT_REGISTER_ROUTE,
                body=IdempotencyFingerprintPayload(
                    values={
                        "file_name": file_name,
                        "mime_type": mime_type,
                        "payload_sha256": payload_sha256.hex(),
                    },
                    business_paths=frozenset(
                        {
                            ("file_name",),
                            ("mime_type",),
                            ("payload_sha256",),
                        }
                    ),
                ),
            ),
            now=now,
        )

    async def list_documents(
        self, *, context: TenantContext, matter_id: UUID
    ) -> tuple[DocumentHeader, ...]:
        require_uuid7(matter_id, field="matter_id")
        async with cast(MatterDocumentUnitOfWorkPort, self._uow_factory()) as uow:
            await self._require_matter(uow, context, matter_id)
            return tuple(
                await uow.documents.headers_for_matter(context.tenant_id, matter_id)
            )

    async def add_party(
        self,
        *,
        context: TenantContext,
        matter_id: UUID,
        display_name: str,
        kind: str,
        idempotency_key: str | None = None,
        now: datetime | None = None,
        trace_id: str | None = None,
    ) -> MatterParty:
        require_uuid7(matter_id, field="matter_id")
        membership_id = context.membership_id
        effective_now = now or datetime.now(UTC)
        reservation = None
        try:
            async with cast(MatterDocumentUnitOfWorkPort, self._uow_factory()) as uow:
                await self._require_matter(uow, context, matter_id)
                if idempotency_key and self._idempotency is not None:
                    if membership_id is None:
                        raise MatterDocumentConflict
                    reservation = await self._reserve_party_add(
                        uow,
                        tenant_id=context.tenant_id,
                        membership_id=membership_id,
                        matter_id=matter_id,
                        display_name=display_name,
                        kind=kind,
                        idempotency_key=idempotency_key,
                        now=effective_now,
                    )
                    if reservation.is_replay:
                        assert reservation.replay is not None
                        party = await _find_party_by_id(
                            uow.matters, context, matter_id, reservation.replay.result_id
                        )
                        if party is None:
                            raise MatterDocumentConflict
                        return party
                try:
                    party = await uow.matters.add_party(
                        tenant_id=context.tenant_id,
                        matter_id=matter_id,
                        display_name=display_name,
                        kind=kind,
                    )
                except ValueError as exc:
                    raise MatterDocumentInvalidRequest from exc
                if reservation is not None:
                    assert self._idempotency is not None
                    await self._idempotency.complete(
                        uow.idempotency,
                        reservation,
                        IdempotencyResultReference(_PARTY_RESULT_TYPE, party.id),
                        now=effective_now,
                    )
                await _append_party_audit(
                    uow,
                    context=context,
                    action="matter.party.add",
                    reason_code="added",
                    target_id=party.id,
                    trace_id=trace_id,
                )
                return party
        except MatterDocumentNotFound as exc:
            await _append_party_rejected_audit(
                self._uow_factory,
                context=context,
                action="matter.party.add",
                result="denied",
                reason_code=exc.code,
                target_type="matter",
                target_id=matter_id,
                trace_id=trace_id,
            )
            raise
        except MatterDocumentInvalidRequest as exc:
            await _append_party_rejected_audit(
                self._uow_factory,
                context=context,
                action="matter.party.add",
                result="failure",
                reason_code=exc.code,
                target_type="matter",
                target_id=matter_id,
                trace_id=trace_id,
            )
            raise

    async def _reserve_party_add(
        self,
        uow: MatterDocumentUnitOfWorkPort,
        *,
        tenant_id: UUID,
        membership_id: UUID,
        matter_id: UUID,
        display_name: str,
        kind: str,
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
            operation=_PARTY_ADD_OPERATION,
            request=IdempotencyRequest(
                key=idempotency_key,
                method="POST",
                canonical_route=_PARTY_ADD_ROUTE,
                body=IdempotencyFingerprintPayload(
                    values={
                        "matter_id": str(matter_id),
                        "display_name": display_name,
                        "kind": kind,
                    },
                    business_paths=frozenset(
                        {("matter_id",), ("display_name",), ("kind",)}
                    ),
                ),
            ),
            now=now,
        )

    async def list_parties(
        self, *, context: TenantContext, matter_id: UUID
    ) -> tuple[MatterParty, ...]:
        require_uuid7(matter_id, field="matter_id")
        async with cast(MatterDocumentUnitOfWorkPort, self._uow_factory()) as uow:
            await self._require_matter(uow, context, matter_id)
            return tuple(await uow.matters.list_parties(context, matter_id))

    async def update_party(
        self,
        *,
        context: TenantContext,
        matter_id: UUID,
        party_id: UUID,
        display_name: str | None,
        kind: str | None,
        idempotency_key: str | None = None,
        now: datetime | None = None,
        trace_id: str | None = None,
    ) -> MatterParty:
        require_uuid7(matter_id, field="matter_id")
        require_uuid7(party_id, field="party_id")
        membership_id = context.membership_id
        effective_now = now or datetime.now(UTC)
        reservation = None
        try:
            async with cast(MatterDocumentUnitOfWorkPort, self._uow_factory()) as uow:
                await self._require_matter(uow, context, matter_id)
                if idempotency_key and self._idempotency is not None:
                    if membership_id is None:
                        raise MatterDocumentConflict
                    reservation = await self._reserve_party_update(
                        uow,
                        tenant_id=context.tenant_id,
                        membership_id=membership_id,
                        matter_id=matter_id,
                        party_id=party_id,
                        display_name=display_name,
                        kind=kind,
                        idempotency_key=idempotency_key,
                        now=effective_now,
                    )
                    if reservation.is_replay:
                        assert reservation.replay is not None
                        party = await _find_party_by_id(
                            uow.matters, context, matter_id, reservation.replay.result_id
                        )
                        if party is None:
                            raise MatterDocumentConflict
                        return party
                try:
                    party = await uow.matters.update_party(
                        tenant_id=context.tenant_id,
                        matter_id=matter_id,
                        party_id=party_id,
                        display_name=display_name,
                        kind=kind,
                    )
                except ValueError as exc:
                    raise MatterDocumentInvalidRequest from exc
                if party is None:
                    raise MatterDocumentNotFound
                if reservation is not None:
                    assert self._idempotency is not None
                    await self._idempotency.complete(
                        uow.idempotency,
                        reservation,
                        IdempotencyResultReference(_PARTY_RESULT_TYPE, party.id),
                        now=effective_now,
                    )
                await _append_party_audit(
                    uow,
                    context=context,
                    action="matter.party.update",
                    reason_code="updated",
                    target_id=party.id,
                    trace_id=trace_id,
                )
                return party
        except MatterDocumentNotFound as exc:
            await _append_party_rejected_audit(
                self._uow_factory,
                context=context,
                action="matter.party.update",
                result="denied",
                reason_code=exc.code,
                target_type="matter_party",
                target_id=party_id,
                trace_id=trace_id,
            )
            raise
        except MatterDocumentInvalidRequest as exc:
            await _append_party_rejected_audit(
                self._uow_factory,
                context=context,
                action="matter.party.update",
                result="failure",
                reason_code=exc.code,
                target_type="matter_party",
                target_id=party_id,
                trace_id=trace_id,
            )
            raise

    async def _reserve_party_update(
        self,
        uow: MatterDocumentUnitOfWorkPort,
        *,
        tenant_id: UUID,
        membership_id: UUID,
        matter_id: UUID,
        party_id: UUID,
        display_name: str | None,
        kind: str | None,
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
            operation=_PARTY_UPDATE_OPERATION,
            request=IdempotencyRequest(
                key=idempotency_key,
                method="PATCH",
                canonical_route=_PARTY_UPDATE_ROUTE,
                body=IdempotencyFingerprintPayload(
                    values={
                        "matter_id": str(matter_id),
                        "party_id": str(party_id),
                        "display_name": display_name,
                        "kind": kind,
                    },
                    business_paths=frozenset(
                        {
                            ("matter_id",),
                            ("party_id",),
                            ("display_name",),
                            ("kind",),
                        }
                    ),
                ),
            ),
            now=now,
        )

    async def remove_party(
        self,
        *,
        context: TenantContext,
        matter_id: UUID,
        party_id: UUID,
        idempotency_key: str | None = None,
        now: datetime | None = None,
        trace_id: str | None = None,
    ) -> None:
        require_uuid7(matter_id, field="matter_id")
        require_uuid7(party_id, field="party_id")
        membership_id = context.membership_id
        effective_now = now or datetime.now(UTC)
        reservation = None
        try:
            async with cast(MatterDocumentUnitOfWorkPort, self._uow_factory()) as uow:
                await self._require_matter(uow, context, matter_id)
                if idempotency_key and self._idempotency is not None:
                    if membership_id is None:
                        raise MatterDocumentConflict
                    reservation = await self._reserve_party_remove(
                        uow,
                        tenant_id=context.tenant_id,
                        membership_id=membership_id,
                        matter_id=matter_id,
                        party_id=party_id,
                        idempotency_key=idempotency_key,
                        now=effective_now,
                    )
                    if reservation.is_replay:
                        return
                if not await uow.matters.remove_party(
                    tenant_id=context.tenant_id,
                    matter_id=matter_id,
                    party_id=party_id,
                ):
                    raise MatterDocumentNotFound
                if reservation is not None:
                    assert self._idempotency is not None
                    await self._idempotency.complete(
                        uow.idempotency,
                        reservation,
                        IdempotencyResultReference(_PARTY_RESULT_TYPE, party_id),
                        now=effective_now,
                    )
                await _append_party_audit(
                    uow,
                    context=context,
                    action="matter.party.remove",
                    reason_code="removed",
                    target_id=party_id,
                    trace_id=trace_id,
                )
        except MatterDocumentNotFound as exc:
            await _append_party_rejected_audit(
                self._uow_factory,
                context=context,
                action="matter.party.remove",
                result="denied",
                reason_code=exc.code,
                target_type="matter_party",
                target_id=party_id,
                trace_id=trace_id,
            )
            raise

    async def _reserve_party_remove(
        self,
        uow: MatterDocumentUnitOfWorkPort,
        *,
        tenant_id: UUID,
        membership_id: UUID,
        matter_id: UUID,
        party_id: UUID,
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
            operation=_PARTY_REMOVE_OPERATION,
            request=IdempotencyRequest(
                key=idempotency_key,
                method="DELETE",
                canonical_route=_PARTY_REMOVE_ROUTE,
                body=IdempotencyFingerprintPayload(
                    values={
                        "matter_id": str(matter_id),
                        "party_id": str(party_id),
                    },
                    business_paths=frozenset({("matter_id",), ("party_id",)}),
                ),
            ),
            now=now,
        )

    async def check_party_conflicts(
        self,
        *,
        context: TenantContext,
        display_name: str,
        kind: str | None,
        exclude_matter_id: UUID | None,
    ) -> PartyConflictCheck:
        if not isinstance(display_name, str) or not display_name.strip():
            raise MatterDocumentInvalidRequest
        if kind is not None and (not isinstance(kind, str) or not kind.strip()):
            raise MatterDocumentInvalidRequest
        if exclude_matter_id is not None:
            require_uuid7(exclude_matter_id, field="exclude_matter_id")
        async with cast(MatterDocumentUnitOfWorkPort, self._uow_factory()) as uow:
            try:
                count = await uow.matters.count_other_party_matters(
                    tenant_id=context.tenant_id,
                    display_name=display_name,
                    kind=kind,
                    exclude_matter_id=exclude_matter_id,
                )
            except ValueError as exc:
                raise MatterDocumentInvalidRequest from exc
        return PartyConflictCheck(
            tenant_id=context.tenant_id,
            conflict=count > 0,
            other_matter_count=count,
        )

    async def transition_matter_status(
        self,
        *,
        context: TenantContext,
        matter_id: UUID,
        expected_version: int | None,
        target_status: MatterStatus,
        trace_id: str | None = None,
    ) -> Matter:
        require_uuid7(matter_id, field="matter_id")
        async with cast(MatterDocumentUnitOfWorkPort, self._uow_factory()) as uow:
            existing = await uow.matters.get_matter(context, matter_id)
            if existing is None:
                raise MatterDocumentNotFound
            from lawyer_agent.domain.matter_documents import (
                MatterStatusTransitionInvalid,
            )

            try:
                updated = await uow.matters.transition_matter_status(
                    context,
                    matter_id,
                    expected_version=(
                        existing.version if expected_version is None else expected_version
                    ),
                    target_status=target_status,
                )
            except (ValueError, MatterStatusTransitionInvalid) as exc:
                raise MatterDocumentConflict from exc
            if updated is None:
                raise MatterDocumentConflict
            await _append_matter_audit(
                uow,
                context=context,
                action="matter.status",
                reason_code="changed",
                target_id=matter_id,
                trace_id=trace_id,
            )
            return updated

    async def update_matter_metadata(
        self,
        *,
        context: TenantContext,
        matter_id: UUID,
        expected_version: int | None,
        title: str | None,
        description: str | None,
        trace_id: str | None = None,
    ) -> Matter:
        require_uuid7(matter_id, field="matter_id")
        async with cast(MatterDocumentUnitOfWorkPort, self._uow_factory()) as uow:
            existing = await uow.matters.get_matter(context, matter_id)
            if existing is None:
                raise MatterDocumentNotFound
            from lawyer_agent.domain.matter_documents import validate_matter_metadata

            try:
                validate_matter_metadata(title=title, description=description)
                updated = await uow.matters.update_matter_metadata(
                    context,
                    matter_id,
                    expected_version=(
                        existing.version if expected_version is None else expected_version
                    ),
                    title=title,
                    description=description,
                )
            except ValueError as exc:
                raise MatterDocumentInvalidRequest from exc
            if updated is None:
                raise MatterDocumentConflict
            await _append_matter_audit(
                uow,
                context=context,
                action="matter.update",
                reason_code="changed",
                target_id=matter_id,
                trace_id=trace_id,
            )
            return updated

    async def assign_matter_owner(
        self,
        *,
        context: TenantContext,
        matter_id: UUID,
        expected_version: int | None,
        owner_membership_id: UUID,
        trace_id: str | None = None,
    ) -> Matter:
        require_uuid7(matter_id, field="matter_id")
        require_uuid7(owner_membership_id, field="owner_membership_id")
        async with cast(MatterDocumentUnitOfWorkPort, self._uow_factory()) as uow:
            existing = await uow.matters.get_matter(context, matter_id)
            if existing is None:
                raise MatterDocumentNotFound
            try:
                updated = await uow.matters.assign_owner(
                    context,
                    matter_id,
                    expected_version=(
                        existing.version if expected_version is None else expected_version
                    ),
                    owner_membership_id=owner_membership_id,
                )
            except ValueError as exc:
                raise MatterDocumentInvalidRequest from exc
            if updated is None:
                raise MatterDocumentConflict
            await _append_matter_audit(
                uow,
                context=context,
                action="matter.owner",
                reason_code="assigned",
                target_id=matter_id,
                trace_id=trace_id,
            )
            return updated

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


async def _find_party_by_id(
    matters: MatterStorePort,
    context: TenantContext,
    matter_id: UUID,
    party_id: UUID,
) -> MatterParty | None:
    parties = await matters.list_parties(context, matter_id)
    for party in parties:
        if party.id == party_id:
            return party
    return None


async def _append_matter_audit(
    uow: object,
    *,
    context: TenantContext,
    action: str,
    reason_code: str,
    target_id: UUID,
    trace_id: str | None,
) -> None:
    audit = getattr(uow, "audit", None)
    user_id = context.membership_user_id
    membership_id = context.membership_id
    if audit is None or user_id is None or membership_id is None:
        return
    event = new_tenant_user_audit_event(
        tenant_id=context.tenant_id,
        actor_user_id=user_id,
        actor_membership_id=membership_id,
        action=action,
        reason_code=reason_code,
        trace_id=trace_id or "http",
        target_type="matter",
        target_id=target_id,
    )
    await audit.append_structured(event)


async def _append_party_audit(
    uow: object,
    *,
    context: TenantContext,
    action: str,
    reason_code: str,
    target_id: UUID,
    trace_id: str | None,
) -> None:
    audit = getattr(uow, "audit", None)
    user_id = context.membership_user_id
    membership_id = context.membership_id
    if audit is None or user_id is None or membership_id is None:
        return
    event = new_tenant_user_audit_event(
        tenant_id=context.tenant_id,
        actor_user_id=user_id,
        actor_membership_id=membership_id,
        action=action,
        reason_code=reason_code,
        trace_id=trace_id or "http",
        target_type="matter_party",
        target_id=target_id,
    )
    await audit.append_structured(event)


async def _append_party_rejected_audit(
    uow_factory: Callable[[], object],
    *,
    context: TenantContext,
    action: str,
    result: str,
    reason_code: str,
    target_type: str,
    target_id: UUID,
    trace_id: str | None,
) -> None:
    """Record a rejected/failed party write attempt in its own committed transaction.

    The failing business UoW has already rolled back when an error handler calls
    this, so the audit append opens a fresh short-lived UoW whose clean exit
    commits only the audit row.
    """
    async with cast(MatterDocumentUnitOfWorkPort, uow_factory()) as uow:
        audit = getattr(uow, "audit", None)
        user_id = context.membership_user_id
        membership_id = context.membership_id
        if audit is None or user_id is None or membership_id is None:
            return
        event = new_tenant_user_audit_event(
            tenant_id=context.tenant_id,
            actor_user_id=user_id,
            actor_membership_id=membership_id,
            action=action,
            reason_code=reason_code,
            trace_id=trace_id or "http",
            target_type=target_type,
            target_id=target_id,
            result=result,
        )
        await audit.append_structured(event)


def _actor_ids(context: TenantContext) -> tuple[UUID, UUID]:
    user_id = context.membership_user_id
    membership_id = context.membership_id
    if user_id is None or membership_id is None:
        raise MatterDocumentConflict
    require_uuid7(user_id, field="actor user_id")
    require_uuid7(membership_id, field="actor membership_id")
    return user_id, membership_id
