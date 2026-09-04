from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lawyer_agent.domain.common import new_uuid7, require_uuid7
from lawyer_agent.domain.matter_documents import (
    DocumentKind,
    DocumentUploadStatus,
    Matter,
    MatterKind,
    MatterParty,
    MatterStatus,
    ReviewStatus,
)
from lawyer_agent.domain.tenancy import TenantContext
from lawyer_agent.infrastructure.persistence.models.matter_documents import (
    TenantMatterModel,
    TenantMatterPartyModel,
)

_MATTER_KIND_MAP = {
    "contract_review": MatterKind.CONTRACT_REVIEW,
    "litigation": MatterKind.LITIGATION,
    "legal_advice": MatterKind.LEGAL_ADVICE,
    "compliance": MatterKind.COMPLIANCE,
    "other": MatterKind.OTHER,
}
_MATTER_STATUS_MAP = {
    "open": MatterStatus.OPEN,
    "active": MatterStatus.ACTIVE,
    "closed": MatterStatus.CLOSED,
    "archived": MatterStatus.ARCHIVED,
}
_KIND_MAP = {"original": DocumentKind.ORIGINAL, "derived": DocumentKind.DERIVED}
_UPLOAD_STATUS_MAP = {
    "uploaded": DocumentUploadStatus.UPLOADED,
    "validating": DocumentUploadStatus.VALIDATING,
    "accepted": DocumentUploadStatus.ACCEPTED,
    "needs_review": DocumentUploadStatus.NEEDS_REVIEW,
    "parsing": DocumentUploadStatus.PARSING,
    "ready": DocumentUploadStatus.READY,
    "failed": DocumentUploadStatus.FAILED,
}
_REVIEW_STATUS_MAP = {
    "draft": ReviewStatus.DRAFT,
    "pending_review": ReviewStatus.PENDING_REVIEW,
    "changes_requested": ReviewStatus.CHANGES_REQUESTED,
    "approved": ReviewStatus.APPROVED,
    "rejected": ReviewStatus.REJECTED,
}


class MatterQueryPort(Protocol):
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


def _require_context(context: TenantContext) -> None:
    if not isinstance(context, TenantContext):
        raise ValueError("matter repository requires a typed tenant context")
    require_uuid7(context.tenant_id, field="matter tenant_id")


def _matter(model: TenantMatterModel) -> Matter:
    return Matter(
        id=model.id,
        tenant_id=model.tenant_id,
        title=model.title,
        kind=_MATTER_KIND_MAP[model.kind],
        status=_MATTER_STATUS_MAP[model.status],
        description=model.description,
        created_by_user_id=model.created_by_user_id,
        created_by_membership_id=model.created_by_membership_id,
        owner_membership_id=model.owner_membership_id,
        version=model.version,
    )


def _matter_model(matter: Matter) -> TenantMatterModel:
    return TenantMatterModel(
        id=matter.id,
        tenant_id=matter.tenant_id,
        title=matter.title,
        kind=matter.kind.value,
        status=matter.status.value,
        description=matter.description,
        created_by_user_id=matter.created_by_user_id,
        created_by_membership_id=matter.created_by_membership_id,
        owner_membership_id=matter.owner_membership_id,
        version=matter.version,
    )


class SqlAlchemyMatterRepository:
    """Tenant-scoped Matter repository; get_by_id is impossible without tenant."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_matter(
        self, context: TenantContext, matter_id: UUID
    ) -> Matter | None:
        _require_context(context)
        require_uuid7(matter_id, field="matter_id")
        model = await self._session.scalar(
            select(TenantMatterModel).where(
                TenantMatterModel.tenant_id == context.tenant_id,
                TenantMatterModel.id == matter_id,
            )
        )
        return None if model is None else _matter(model)

    async def create_matter(
        self,
        *,
        tenant_id: UUID,
        title: str,
        kind: MatterKind,
        created_by_user_id: UUID,
        created_by_membership_id: UUID,
        description: str | None = None,
    ) -> Matter:
        matter = Matter(
            id=new_uuid7(),
            tenant_id=tenant_id,
            title=title,
            kind=kind,
            status=MatterStatus.OPEN,
            description=description,
            created_by_user_id=created_by_user_id,
            created_by_membership_id=created_by_membership_id,
            version=1,
        )
        self._session.add(_matter_model(matter))
        await self._session.flush()
        return matter

    async def add_party(
        self,
        *,
        tenant_id: UUID,
        matter_id: UUID,
        display_name: str,
        kind: str,
    ) -> MatterParty:
        require_uuid7(tenant_id, field="party tenant_id")
        require_uuid7(matter_id, field="party matter_id")
        party = MatterParty(
            id=new_uuid7(),
            tenant_id=tenant_id,
            matter_id=matter_id,
            display_name=display_name,
            kind=kind,
        )
        self._session.add(
            TenantMatterPartyModel(
                id=party.id,
                tenant_id=party.tenant_id,
                matter_id=party.matter_id,
                display_name=party.display_name,
                kind=party.kind,
                version=1,
            )
        )
        await self._session.flush()
        return party


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)
