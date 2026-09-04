from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol, cast
from uuid import UUID

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.engine import CursorResult
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
    matter_owner_assignable,
    require_matter_status_transition,
    validate_matter_metadata,
)
from lawyer_agent.domain.tenancy import TenantContext
from lawyer_agent.infrastructure.persistence.models.matter_documents import (
    TenantMatterModel,
    TenantMatterPartyModel,
)
from lawyer_agent.infrastructure.persistence.models.tenancy import (
    TenantMembershipModel,
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


class MatterListCursorInvalid(ValueError):
    """Raised when a keyset cursor does not belong to the caller's tenant."""


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


def _party(model: TenantMatterPartyModel) -> MatterParty:
    return MatterParty(
        id=model.id,
        tenant_id=model.tenant_id,
        matter_id=model.matter_id,
        display_name=model.display_name,
        kind=model.kind,
        version=model.version,
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

    async def list_matters(
        self,
        context: TenantContext,
        *,
        limit: int,
        before_id: UUID | None = None,
    ) -> tuple[Matter, ...]:
        """Keyset list of the tenant's Matters newest first (created_at, id).

        ``before_id`` anchors the previous page's last item; a cursor that is
        not in this tenant raises a NotFound-style marker via None return
        semantics of the caller.
        """
        _require_context(context)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("matter list limit must be between 1 and 100")
        statement = select(TenantMatterModel).where(
            TenantMatterModel.tenant_id == context.tenant_id
        )
        if before_id is not None:
            require_uuid7(before_id, field="before_id")
            anchor = await self._session.scalar(
                select(TenantMatterModel.created_at).where(
                    TenantMatterModel.tenant_id == context.tenant_id,
                    TenantMatterModel.id == before_id,
                )
            )
            if anchor is None:
                raise MatterListCursorInvalid("matter list cursor is not in this tenant")
            statement = statement.where(
                or_(
                    TenantMatterModel.created_at < anchor,
                    and_(
                        TenantMatterModel.created_at == anchor,
                        TenantMatterModel.id < before_id,
                    ),
                )
            )
        models = (
            await self._session.scalars(
                statement.order_by(
                    TenantMatterModel.created_at.desc(), TenantMatterModel.id.desc()
                ).limit(limit)
            )
        ).all()
        return tuple(_matter(model) for model in models)

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

    async def transition_matter_status(
        self,
        context: TenantContext,
        matter_id: UUID,
        *,
        expected_version: int,
        target_status: MatterStatus,
    ) -> Matter | None:
        """Atomically transition one tenant Matter with a version CAS.

        Returns the updated Matter or None when the row does not exist / the
        version guard fails; callers map None to NotFound/Conflict by re-read.
        """
        _require_context(context)
        require_uuid7(matter_id, field="matter_id")
        if isinstance(expected_version, bool) or not isinstance(
            expected_version, int
        ) or expected_version < 1:
            raise ValueError("matter expected_version must be a positive integer")
        if not isinstance(target_status, MatterStatus):
            raise ValueError("matter target status must be strongly typed")
        model = await self._session.scalar(
            select(TenantMatterModel)
            .where(
                TenantMatterModel.tenant_id == context.tenant_id,
                TenantMatterModel.id == matter_id,
            )
            .with_for_update()
        )
        if model is None:
            return None
        current = _MATTER_STATUS_MAP[model.status]
        require_matter_status_transition(current, target_status)
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(TenantMatterModel)
                .where(
                    TenantMatterModel.tenant_id == context.tenant_id,
                    TenantMatterModel.id == matter_id,
                    TenantMatterModel.status == current.value,
                    TenantMatterModel.version == expected_version,
                )
                .values(
                    status=target_status.value,
                    version=TenantMatterModel.version + 1,
                )
            ),
        )
        if result.rowcount != 1:
            return None
        await self._session.flush()
        return _matter(model)

    async def update_matter_metadata(
        self,
        context: TenantContext,
        matter_id: UUID,
        *,
        expected_version: int,
        title: str | None,
        description: str | None,
    ) -> Matter | None:
        """CAS update of a Matter's editable text metadata (title/description)."""
        _require_context(context)
        require_uuid7(matter_id, field="matter_id")
        if isinstance(expected_version, bool) or not isinstance(
            expected_version, int
        ) or expected_version < 1:
            raise ValueError("matter expected_version must be a positive integer")
        validate_matter_metadata(title=title, description=description)
        model = await self._session.scalar(
            select(TenantMatterModel)
            .where(
                TenantMatterModel.tenant_id == context.tenant_id,
                TenantMatterModel.id == matter_id,
            )
            .with_for_update()
        )
        if model is None:
            return None
        values: dict[str, object] = {}
        if title is not None:
            values["title"] = title.strip()
        if description is not None:
            stripped = description.strip()
            values["description"] = None if not stripped else stripped
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(TenantMatterModel)
                .where(
                    TenantMatterModel.tenant_id == context.tenant_id,
                    TenantMatterModel.id == matter_id,
                    TenantMatterModel.version == expected_version,
                )
                .values(**values, version=TenantMatterModel.version + 1)
            ),
        )
        if result.rowcount != 1:
            return None
        await self._session.flush()
        return _matter(model)

    async def assign_owner(
        self,
        context: TenantContext,
        matter_id: UUID,
        *,
        expected_version: int,
        owner_membership_id: UUID,
    ) -> Matter | None:
        """Assign a tenant member as the Matter's responsible owner (version CAS).

        The owner must be an active own-staff membership (member_type owner or
        internal) of the SAME tenant. Any other reference - a foreign-tenant
        membership, an inactive member, a student/external client or a
        nonexistent id - is rejected with the same ValueError so callers cannot
        probe whether a membership exists elsewhere.
        """
        _require_context(context)
        require_uuid7(matter_id, field="matter_id")
        require_uuid7(owner_membership_id, field="owner_membership_id")
        if isinstance(expected_version, bool) or not isinstance(
            expected_version, int
        ) or expected_version < 1:
            raise ValueError("matter expected_version must be a positive integer")
        model = await self._session.scalar(
            select(TenantMatterModel)
            .where(
                TenantMatterModel.tenant_id == context.tenant_id,
                TenantMatterModel.id == matter_id,
            )
            .with_for_update()
        )
        if model is None:
            return None
        member = await self._session.scalar(
            select(TenantMembershipModel).where(
                TenantMembershipModel.tenant_id == context.tenant_id,
                TenantMembershipModel.id == owner_membership_id,
            )
        )
        if member is None or not matter_owner_assignable(
            member.member_type, member.status
        ):
            raise ValueError("owner membership is not an active own-staff member")
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(TenantMatterModel)
                .where(
                    TenantMatterModel.tenant_id == context.tenant_id,
                    TenantMatterModel.id == matter_id,
                    TenantMatterModel.version == expected_version,
                )
                .values(
                    owner_membership_id=owner_membership_id,
                    version=TenantMatterModel.version + 1,
                )
            ),
        )
        if result.rowcount != 1:
            return None
        await self._session.flush()
        return _matter(model)

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
        matter = await self._session.scalar(
            select(TenantMatterModel).where(
                TenantMatterModel.tenant_id == tenant_id,
                TenantMatterModel.id == matter_id,
            )
        )
        if matter is None:
            raise ValueError("matter does not belong to this tenant")
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

    async def list_parties(
        self, context: TenantContext, matter_id: UUID
    ) -> tuple[MatterParty, ...]:
        _require_context(context)
        require_uuid7(matter_id, field="matter_id")
        matter = await self._session.scalar(
            select(TenantMatterModel).where(
                TenantMatterModel.tenant_id == context.tenant_id,
                TenantMatterModel.id == matter_id,
            )
        )
        if matter is None:
            return ()
        rows = await self._session.scalars(
            select(TenantMatterPartyModel)
            .where(
                TenantMatterPartyModel.tenant_id == context.tenant_id,
                TenantMatterPartyModel.matter_id == matter_id,
            )
            .order_by(TenantMatterPartyModel.created_at)
        )
        return tuple(_party(model) for model in rows)

    async def update_party(
        self,
        *,
        tenant_id: UUID,
        matter_id: UUID,
        party_id: UUID,
        display_name: str | None,
        kind: str | None,
    ) -> MatterParty | None:
        """Update a party's mutable fields with an optimistic version bump."""
        require_uuid7(tenant_id, field="party tenant_id")
        require_uuid7(matter_id, field="party matter_id")
        require_uuid7(party_id, field="party_id")
        if display_name is not None and (
            not isinstance(display_name, str) or not display_name.strip()
        ):
            raise ValueError("party display name must be non-empty text")
        if kind is not None and (not isinstance(kind, str) or not kind.strip()):
            raise ValueError("party kind must be non-empty text")
        model = await self._session.scalar(
            select(TenantMatterPartyModel).where(
                TenantMatterPartyModel.tenant_id == tenant_id,
                TenantMatterPartyModel.matter_id == matter_id,
                TenantMatterPartyModel.id == party_id,
            )
        )
        if model is None:
            return None
        if display_name is not None:
            model.display_name = display_name
        if kind is not None:
            model.kind = kind
        model.version = model.version + 1
        await self._session.flush()
        return _party(model)

    async def remove_party(
        self,
        *,
        tenant_id: UUID,
        matter_id: UUID,
        party_id: UUID,
    ) -> bool:
        """Delete a party row that belongs to exactly (tenant, matter)."""
        require_uuid7(tenant_id, field="party tenant_id")
        require_uuid7(matter_id, field="party matter_id")
        require_uuid7(party_id, field="party_id")
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                delete(TenantMatterPartyModel).where(
                    TenantMatterPartyModel.tenant_id == tenant_id,
                    TenantMatterPartyModel.matter_id == matter_id,
                    TenantMatterPartyModel.id == party_id,
                )
            ),
        )
        return result.rowcount == 1

    async def count_other_party_matters(
        self,
        *,
        tenant_id: UUID,
        display_name: str,
        kind: str | None,
        exclude_matter_id: UUID | None,
    ) -> int:
        """Count other tenant Matters that name this party (no content leakage).

        Only returns how many distinct other Matters mention the party; the
        response never exposes which Matters they are or their contents.
        """
        require_uuid7(tenant_id, field="conflict tenant_id")
        if not isinstance(display_name, str) or not display_name.strip():
            raise ValueError("conflict party display name must be non-empty text")
        if exclude_matter_id is not None:
            require_uuid7(exclude_matter_id, field="conflict exclude_matter_id")
        statement = select(
            func.count(func.distinct(TenantMatterPartyModel.matter_id))
        ).where(
            TenantMatterPartyModel.tenant_id == tenant_id,
            TenantMatterPartyModel.display_name == display_name,
        )
        if kind is not None:
            if not isinstance(kind, str) or not kind.strip():
                raise ValueError("conflict party kind must be non-empty text")
            statement = statement.where(TenantMatterPartyModel.kind == kind)
        if exclude_matter_id is not None:
            statement = statement.where(
                TenantMatterPartyModel.matter_id != exclude_matter_id
            )
        value = await self._session.scalar(statement)
        return int(value or 0)


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)
