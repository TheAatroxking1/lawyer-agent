from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from lawyer_agent.domain.legal_corpus import (
    ChunkQuality,
    ChunkType,
    LegalChunk,
    LegalInstrument,
    LegalVersion,
    LegalVersionStatus,
    Provision,
    ProvisionLevel,
)
from lawyer_agent.infrastructure.persistence.models.legal_corpus import (
    LegalChunkModel,
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

_CHUNK_TYPE_MAP = {
    "provision": ChunkType.PROVISION,
    "sub_item": ChunkType.SUB_ITEM,
    "table": ChunkType.TABLE,
    "attachment": ChunkType.ATTACHMENT,
}

_CHUNK_QUALITY_MAP = {
    "ok": ChunkQuality.OK,
    "degraded": ChunkQuality.DEGRADED,
    "failed": ChunkQuality.FAILED,
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


class LegalCorpusChunkPort(Protocol):
    """Read/write boundary for derived legal corpus chunks (re-indexable)."""

    async def replace_chunks_for_version(
        self, version_id: UUID, chunks: tuple[LegalChunk, ...]
    ) -> None: ...

    async def chunks_for_version(
        self, version_id: UUID
    ) -> tuple[LegalChunk, ...]: ...


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


def _to_chunk(model: LegalChunkModel) -> LegalChunk:
    return LegalChunk(
        id=model.id,
        version_id=model.version_id,
        provision_id=model.provision_id,
        chunk_type=_CHUNK_TYPE_MAP[model.chunk_type],
        quality=_CHUNK_QUALITY_MAP[model.quality],
        content=model.content,
        content_hash=bytes(model.content_hash),
        parent_chunk_id=model.parent_chunk_id,
        parser_version=model.parser_version,
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

    async def version_with_instrument(
        self, version_id: UUID
    ) -> tuple[LegalVersion, LegalInstrument] | None:
        row = (
            await self._session.execute(
                select(LegalVersionModel, LegalInstrumentModel)
                .join(
                    LegalInstrumentModel,
                    LegalVersionModel.instrument_id == LegalInstrumentModel.id,
                )
                .where(LegalVersionModel.id == version_id)
            )
        ).first()
        if row is None:
            return None
        return _to_version(row[0]), _to_instrument(row[1])


class SqlAlchemyLegalCorpusChunkRepository:
    """Derived chunk rows; fully replaceable per version (re-index friendly)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def replace_chunks_for_version(
        self, version_id: UUID, chunks: tuple[LegalChunk, ...]
    ) -> None:
        await self._session.execute(
            delete(LegalChunkModel).where(LegalChunkModel.version_id == version_id)
        )
        for chunk in chunks:
            if chunk.version_id != version_id:
                raise ValueError("chunk does not belong to the target version")
            self._session.add(_chunk_model(chunk))
        await self._session.flush()

    async def chunks_for_version(
        self, version_id: UUID
    ) -> tuple[LegalChunk, ...]:
        rows = (
            await self._session.execute(
                select(LegalChunkModel, LegalProvisionModel.char_start)
                .join(
                    LegalProvisionModel,
                    LegalChunkModel.provision_id == LegalProvisionModel.id,
                )
                .where(LegalChunkModel.version_id == version_id)
                .order_by(LegalProvisionModel.char_start, LegalChunkModel.id)
            )
        ).all()
        return tuple(_to_chunk(row[0]) for row in rows)


def _chunk_model(chunk: LegalChunk) -> LegalChunkModel:
    return LegalChunkModel(
        id=chunk.id,
        version_id=chunk.version_id,
        provision_id=chunk.provision_id,
        parent_chunk_id=chunk.parent_chunk_id,
        chunk_type=chunk.chunk_type.value,
        quality=chunk.quality.value,
        content=chunk.content,
        content_hash=chunk.content_hash,
        parser_version=chunk.parser_version,
    )


def _instrument_model(instrument: LegalInstrument) -> LegalInstrumentModel:
    return LegalInstrumentModel(
        id=instrument.id,
        title=instrument.title,
        issuing_authority=instrument.issuing_authority,
        jurisdiction=instrument.jurisdiction,
        region_code=instrument.region_code,
    )


def _version_model(version: LegalVersion) -> LegalVersionModel:
    return LegalVersionModel(
        id=version.id,
        instrument_id=version.instrument_id,
        version_label=version.version_label,
        law_number=version.law_number,
        status=version.status.value,
        published_on=version.published_on,
        effective_on=version.effective_on,
        repealed_on=version.repealed_on,
        content_hash=version.content_hash,
        source_ref=version.source_ref,
        dataset_version=version.dataset_version,
        parser_version=version.parser_version,
    )


def _provision_model(provision: Provision) -> LegalProvisionModel:
    return LegalProvisionModel(
        id=provision.id,
        version_id=provision.version_id,
        provision_no=provision.provision_no,
        level=provision.level.value,
        structure_path_json=list(provision.structure_path),
        title=provision.title,
        full_text=provision.full_text,
        content_hash=provision.content_hash,
        char_start=provision.char_start,
        char_end=provision.char_end,
    )


class SqlAlchemyLegalCorpusImportRepository:
    """Write path for imported corpus instruments, versions and provisions.

    All calls flush within the caller's session/transaction; no implicit commit.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_instrument_by_identity(
        self, title: str, jurisdiction: str
    ) -> LegalInstrument | None:
        model = await self._session.scalar(
            select(LegalInstrumentModel).where(
                LegalInstrumentModel.title == title,
                LegalInstrumentModel.jurisdiction == jurisdiction,
            )
        )
        return None if model is None else _to_instrument(model)

    async def create_instrument(self, instrument: LegalInstrument) -> None:
        self._session.add(_instrument_model(instrument))
        await self._session.flush()

    async def find_version(
        self, instrument_id: UUID, version_label: str
    ) -> LegalVersion | None:
        model = await self._session.scalar(
            select(LegalVersionModel).where(
                LegalVersionModel.instrument_id == instrument_id,
                LegalVersionModel.version_label == version_label,
            )
        )
        return None if model is None else _to_version(model)

    async def create_version(self, version: LegalVersion) -> None:
        self._session.add(_version_model(version))
        await self._session.flush()

    async def create_provisions(self, provisions: tuple[Provision, ...]) -> None:
        for provision in provisions:
            self._session.add(_provision_model(provision))
        await self._session.flush()
