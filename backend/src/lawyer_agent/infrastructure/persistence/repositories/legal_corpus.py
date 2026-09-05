from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lawyer_agent.domain.common import require_uuid7
from lawyer_agent.domain.legal_corpus import (
    ChunkQuality,
    ChunkType,
    DatasetSnapshot,
    DatasetState,
    LegalChunk,
    LegalInstrument,
    LegalVersion,
    LegalVersionStatus,
    LoadBatch,
    LoadStatus,
    Provision,
    ProvisionLevel,
)
from lawyer_agent.infrastructure.persistence.models.legal_corpus import (
    LegalChunkModel,
    LegalDatasetSnapshotModel,
    LegalInstrumentModel,
    LegalLoadBatchModel,
    LegalProvisionModel,
    LegalVersionModel,
)

_DATASET_STATE_MAP = {
    "pending": DatasetState.PENDING,
    "published": DatasetState.PUBLISHED,
    "superseded": DatasetState.SUPERSEDED,
    "rejected": DatasetState.REJECTED,
}

_LOAD_STATUS_MAP = {
    "inventoried": LoadStatus.INVENTORIED,
    "parsing": LoadStatus.PARSING,
    "completed": LoadStatus.COMPLETED,
    "failed": LoadStatus.FAILED,
}

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


class LegalCorpusInstrumentListCursorInvalid(ValueError):
    """A legal instrument list cursor references a row that does not exist."""


class LegalCorpusLoadBatchCursorInvalid(ValueError):
    """A legal corpus load batch list cursor references a missing row."""


class LegalCorpusQueryPort(Protocol):
    """Explicit public-read boundary for national legal corpus data."""

    async def version_at(
        self, instrument_id: UUID, as_of: date
    ) -> LegalVersion | None: ...

    async def provisions_for_version(
        self, version_id: UUID
    ) -> tuple[Provision, ...]: ...

    async def instrument_exists(self, instrument_id: UUID) -> bool: ...

    async def instrument_by_id(
        self, instrument_id: UUID
    ) -> LegalInstrument | None: ...

    async def versions_for_instrument(
        self, instrument_id: UUID
    ) -> tuple[LegalVersion, ...]: ...

    async def list_instruments(
        self,
        *,
        limit: int,
        before_id: UUID | None = None,
        title: str | None = None,
        issuing_authority: str | None = None,
        jurisdiction: str | None = None,
        region_code: str | None = None,
    ) -> tuple[LegalInstrument, ...]: ...

    async def load_batches(
        self,
        *,
        limit: int,
        before_id: UUID | None = None,
    ) -> tuple[LoadBatch, ...]: ...

    async def load_batch_by_id(self, batch_id: UUID) -> LoadBatch | None: ...


class LegalCorpusChunkPort(Protocol):
    """Read/write boundary for derived legal corpus chunks (re-indexable)."""

    async def replace_chunks_for_version(
        self, version_id: UUID, chunks: tuple[LegalChunk, ...]
    ) -> None: ...

    async def chunks_for_version(
        self, version_id: UUID
    ) -> tuple[LegalChunk, ...]: ...


def _to_dataset_snapshot(model: LegalDatasetSnapshotModel) -> DatasetSnapshot:
    return DatasetSnapshot(
        id=model.id,
        dataset_name=model.dataset_name,
        parser_version=model.parser_version,
        state=_DATASET_STATE_MAP[model.state],
        manifest=model.manifest_json,
        quality_metrics=model.quality_metrics_json,
        released_at=_aware_utc(model.released_at),
    )


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _to_load_batch(model: LegalLoadBatchModel) -> LoadBatch:
    return LoadBatch(
        id=model.id,
        batch_no=model.batch_no,
        source_ref=model.source_ref,
        file_sha256=bytes(model.file_sha256),
        parser_version=model.parser_version,
        status=_LOAD_STATUS_MAP[model.status],
        item_counts=model.item_counts_json,
        started_at=_aware_utc(model.started_at),
        completed_at=_aware_utc(model.completed_at),
        error_message=model.error_message,
    )


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

    async def instrument_exists(self, instrument_id: UUID) -> bool:
        model = await self._session.scalar(
            select(LegalInstrumentModel.id).where(
                LegalInstrumentModel.id == instrument_id
            )
        )
        return model is not None

    async def instrument_by_id(
        self, instrument_id: UUID
    ) -> LegalInstrument | None:
        model = await self._session.scalar(
            select(LegalInstrumentModel).where(
                LegalInstrumentModel.id == instrument_id
            )
        )
        return None if model is None else _to_instrument(model)

    async def list_instruments(
        self,
        *,
        limit: int,
        before_id: UUID | None = None,
        title: str | None = None,
        issuing_authority: str | None = None,
        jurisdiction: str | None = None,
        region_code: str | None = None,
    ) -> tuple[LegalInstrument, ...]:
        """Keyset list of public legal instruments newest first (created_at, id).

        Optional substring filters (title/issuing_authority) and exact filters
        (jurisdiction/region_code) narrow the result before ordering. A cursor
        that does not exist raises ``LegalCorpusInstrumentListCursorInvalid``.
        """
        statement = select(LegalInstrumentModel)
        if title is not None:
            statement = statement.where(
                LegalInstrumentModel.title.contains(title)
            )
        if issuing_authority is not None:
            statement = statement.where(
                LegalInstrumentModel.issuing_authority.contains(issuing_authority)
            )
        if jurisdiction is not None:
            statement = statement.where(
                LegalInstrumentModel.jurisdiction == jurisdiction
            )
        if region_code is not None:
            statement = statement.where(
                LegalInstrumentModel.region_code == region_code
            )
        if before_id is not None:
            require_uuid7(before_id, field="before_id")
            anchor = await self._session.scalar(
                select(LegalInstrumentModel.created_at).where(
                    LegalInstrumentModel.id == before_id
                )
            )
            if anchor is None:
                raise LegalCorpusInstrumentListCursorInvalid(
                    "instrument list cursor does not exist"
                )
            statement = statement.where(
                or_(
                    LegalInstrumentModel.created_at < anchor,
                    and_(
                        LegalInstrumentModel.created_at == anchor,
                        LegalInstrumentModel.id < before_id,
                    ),
                )
            )
        models = (
            await self._session.scalars(
                statement.order_by(
                    LegalInstrumentModel.created_at.desc(),
                    LegalInstrumentModel.id.desc(),
                ).limit(limit)
            )
        ).all()
        return tuple(_to_instrument(model) for model in models)

    async def versions_for_instrument(
        self, instrument_id: UUID
    ) -> tuple[LegalVersion, ...]:
        # MySQL has no NULLS LAST; sort dated rows first, then by date desc.
        rows = await self._session.scalars(
            select(LegalVersionModel)
            .where(LegalVersionModel.instrument_id == instrument_id)
            .order_by(
                LegalVersionModel.published_on.is_not(None),
                LegalVersionModel.published_on.desc(),
                LegalVersionModel.effective_on.desc(),
                LegalVersionModel.id,
            )
        )
        return tuple(_to_version(model) for model in rows)

    async def dataset_snapshots(self) -> tuple[DatasetSnapshot, ...]:
        rows = await self._session.scalars(
            select(LegalDatasetSnapshotModel).order_by(
                LegalDatasetSnapshotModel.released_at.is_not(None).desc(),
                LegalDatasetSnapshotModel.released_at.desc(),
                LegalDatasetSnapshotModel.id.desc(),
            )
        )
        return tuple(_to_dataset_snapshot(model) for model in rows)

    async def dataset_snapshot_by_name(
        self, dataset_name: str
    ) -> DatasetSnapshot | None:
        model = await self._session.scalar(
            select(LegalDatasetSnapshotModel).where(
                LegalDatasetSnapshotModel.dataset_name == dataset_name
            )
        )
        return None if model is None else _to_dataset_snapshot(model)

    async def load_batches(
        self,
        *,
        limit: int,
        before_id: UUID | None = None,
    ) -> tuple[LoadBatch, ...]:
        """Keyset list of corpus load batches newest first (created_at, id)."""
        statement = select(LegalLoadBatchModel)
        if before_id is not None:
            require_uuid7(before_id, field="before_id")
            anchor = await self._session.scalar(
                select(LegalLoadBatchModel.created_at).where(
                    LegalLoadBatchModel.id == before_id
                )
            )
            if anchor is None:
                raise LegalCorpusLoadBatchCursorInvalid(
                    "load batch list cursor does not exist"
                )
            statement = statement.where(
                or_(
                    LegalLoadBatchModel.created_at < anchor,
                    and_(
                        LegalLoadBatchModel.created_at == anchor,
                        LegalLoadBatchModel.id < before_id,
                    ),
                )
            )
        models = (
            await self._session.scalars(
                statement.order_by(
                    LegalLoadBatchModel.created_at.desc(),
                    LegalLoadBatchModel.id.desc(),
                ).limit(limit)
            )
        ).all()
        return tuple(_to_load_batch(model) for model in models)

    async def load_batch_by_id(self, batch_id: UUID) -> LoadBatch | None:
        model = await self._session.scalar(
            select(LegalLoadBatchModel).where(
                LegalLoadBatchModel.id == batch_id
            )
        )
        return None if model is None else _to_load_batch(model)


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
