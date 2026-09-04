from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lawyer_agent.domain.legal_corpus import (
    LegalInstrument,
    LegalVersion,
    LegalVersionStatus,
    Provision,
    ProvisionLevel,
)
from lawyer_agent.infrastructure.persistence.models.legal_corpus import (
    LegalInstrumentModel,
    LegalProvisionModel,
    LegalVersionModel,
)

_VERSION_STATUS_MAP = {
    "current": LegalVersionStatus.CURRENT,
    "repealed": LegalVersionStatus.REPEALED,
    "status_unknown": LegalVersionStatus.STATUS_UNKNOWN,
    "historical": LegalVersionStatus.HISTORICAL,
    "draft": LegalVersionStatus.DRAFT,
}

_LEVEL_MAP = {
    "part": ProvisionLevel.PART,
    "chapter": ProvisionLevel.CHAPTER,
    "section": ProvisionLevel.SECTION,
    "article": ProvisionLevel.ARTICLE,
    "paragraph": ProvisionLevel.PARAGRAPH,
    "item": ProvisionLevel.ITEM,
    "sub_item": ProvisionLevel.SUB_ITEM,
}


@dataclass(frozen=True, slots=True)
class VersionProvisions:
    version: LegalVersion
    provisions: tuple[Provision, ...]


class LegalCorpusQueryPort(Protocol):
    """Explicit public-read boundary for national legal corpus data."""

    async def version_at(
        self, instrument_id: UUID, as_of: date
    ) -> LegalVersion | None: ...

    async def provisions_for_version(
        self, version_id: UUID
    ) -> tuple[Provision, ...]: ...


def _to_instrument(model: LegalInstrumentModel) -> LegalInstrument:
    return LegalInstrument(
        id=model.id,
        title=model.title,
        issuing_authority=model.issuing_authority,
        jurisdiction=model.jurisdiction,
        region_code=model.region_code,
    )


def _to_version(model: LegalVersionModel) -> LegalVersion:
    return LegalVersion(
        id=model.id,
        instrument_id=model.instrument_id,
        version_label=model.version_label,
        status=_VERSION_STATUS_MAP[model.status],
        published_on=model.published_on,
        effective_on=model.effective_on,
        repealed_on=model.repealed_on,
        law_number=model.law_number,
        content_hash=bytes(model.content_hash) if model.content_hash is not None else None,
        source_ref=model.source_ref,
        dataset_version=model.dataset_version,
        parser_version=model.parser_version,
    )


def _to_provision(model: LegalProvisionModel) -> Provision:
    return Provision(
        id=model.id,
        version_id=model.version_id,
        provision_no=model.provision_no,
        level=_LEVEL_MAP[model.level],
        structure_path=tuple(model.structure_path_json or ()),
        title=model.title,
        full_text=model.full_text,
        content_hash=bytes(model.content_hash),
        char_start=model.char_start,
        char_end=model.char_end,
    )


class SqlAlchemyLegalCorpusRepository:
    """Read-only public corpus repository; never exposes a raw global get_by_id."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def version_at(
        self, instrument_id: UUID, as_of: date
    ) -> LegalVersion | None:
        model = await self._session.scalar(
            select(LegalVersionModel)
            .where(
                LegalVersionModel.instrument_id == instrument_id,
                LegalVersionModel.effective_on.is_not(None),
                LegalVersionModel.effective_on <= as_of,
            )
            .order_by(
                LegalVersionModel.effective_on.desc(),
                LegalVersionModel.published_on.desc(),
            )
        )
        return None if model is None else _to_version(model)

    async def provisions_for_version(
        self, version_id: UUID
    ) -> tuple[Provision, ...]:
        rows = await self._session.scalars(
            select(LegalProvisionModel)
            .where(LegalProvisionModel.version_id == version_id)
            .order_by(LegalProvisionModel.char_start)
        )
        return tuple(_to_provision(model) for model in rows)
