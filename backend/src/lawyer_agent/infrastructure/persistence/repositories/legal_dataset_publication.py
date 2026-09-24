"""Each journal mutation commits before returning to the cross-system protocol."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import LargeBinary, Select, func, select, update
from sqlalchemy import cast as sql_cast
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import defer

from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import DatasetSnapshot, DatasetState
from lawyer_agent.domain.legal_dataset_publication import (
    DatasetPublication,
    PublicationError,
    PublicationState,
    validate_candidate,
)
from lawyer_agent.infrastructure.persistence.json_documents import read_json_document
from lawyer_agent.infrastructure.persistence.models.legal_dataset_publication import (
    LegalDatasetPublicationModel,
)
from lawyer_agent.infrastructure.persistence.repositories.legal_corpus_inventory import (
    SqlAlchemyLegalCorpusInventoryRepository,
)


def _record(model: LegalDatasetPublicationModel, payload: dict[str, Any]) -> DatasetPublication:
    try:
        candidate = DatasetSnapshot(
            id=UUID(payload["id"]),
            dataset_name=payload["dataset_name"],
            parser_version=payload["parser_version"],
            state=DatasetState(payload["state"]),
            manifest=payload["manifest"],
            quality_metrics=payload["quality_metrics"],
        )
        validate_candidate(candidate)
        if (
            candidate.dataset_name != model.alias
            or candidate.manifest["index_name"] != model.index_name
            or payload.get("released_at") is not None
        ):
            raise ValueError("inconsistent publication")
        return DatasetPublication(
            model.id,
            candidate,
            model.previous_target,
            PublicationState(model.state),
        )
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        raise PublicationError("publication_record_invalid") from None


async def _load_publication(
    session: AsyncSession,
    statement: Select[tuple[LegalDatasetPublicationModel]],
) -> tuple[LegalDatasetPublicationModel, dict[str, Any]] | None:
    # Capture metadata and its exact JSON digest in one read. A concurrent change
    # during transport is rejected even when the caller uses READ COMMITTED.
    statement_with_digest = statement.options(
        defer(LegalDatasetPublicationModel.candidate_json)
    ).add_columns(
        func.sha2(sql_cast(LegalDatasetPublicationModel.candidate_json, LargeBinary), 256)
    )
    row = (
        await session.execute(statement_with_digest.execution_options(populate_existing=True))
    ).one_or_none()
    if row is None:
        return None
    model, digest = row
    try:
        payload = await read_json_document(
            session,
            select(LegalDatasetPublicationModel.candidate_json).where(
                LegalDatasetPublicationModel.id == model.id
            ),
            expected_sha256=digest,
        )
        if payload is None:
            raise ValueError
        return model, payload
    except ValueError:
        raise PublicationError("publication_record_invalid") from None


class SqlAlchemyDatasetPublicationStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        candidate: DatasetSnapshot,
        previous_target: str | None,
    ) -> DatasetPublication:
        # Domain construction copies nested data and validates the previous target too.
        record = DatasetPublication(new_uuid7(), candidate, previous_target, PublicationState.READY)
        candidate = record.candidate
        validate_candidate(candidate)
        model = LegalDatasetPublicationModel(
            id=record.id,
            active_alias=candidate.dataset_name,
            alias=candidate.dataset_name,
            index_name=candidate.manifest["index_name"],
            previous_target=previous_target,
            state=record.state.value,
            created_at=datetime.now(UTC).replace(tzinfo=None),
            candidate_json={
                "id": str(candidate.id),
                "dataset_name": candidate.dataset_name,
                "parser_version": candidate.parser_version,
                "state": candidate.state.value,
                "manifest": candidate.manifest,
                "quality_metrics": candidate.quality_metrics,
                "released_at": None,
            },
        )
        try:
            async with self._session_factory() as session, session.begin():
                session.add(model)
                await session.flush()
        except IntegrityError:
            raise PublicationError("publication_conflict") from None
        return record

    async def get(self, run_id: UUID) -> DatasetPublication | None:
        async with self._session_factory() as session:
            loaded = await _load_publication(
                session,
                select(LegalDatasetPublicationModel).where(
                    LegalDatasetPublicationModel.id == run_id
                ),
            )
            return None if loaded is None else _record(*loaded)

    async def find_active(self, alias: str) -> DatasetPublication | None:
        if not isinstance(alias, str) or re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", alias) is None:
            raise PublicationError("publication_invalid_alias")
        async with self._session_factory() as session:
            loaded = await _load_publication(
                session,
                select(LegalDatasetPublicationModel).where(
                    LegalDatasetPublicationModel.active_alias == alias,
                ),
            )
            return None if loaded is None else _record(*loaded)

    async def transition(
        self,
        run_id: UUID,
        expected: PublicationState,
        target: PublicationState,
    ) -> DatasetPublication:
        if (
            not isinstance(expected, PublicationState)
            or not isinstance(target, PublicationState)
            or (expected, target)
            not in {
                (PublicationState.READY, PublicationState.SWITCHING),
                (PublicationState.SWITCHING, PublicationState.ACKNOWLEDGED),
            }
        ):
            raise PublicationError("publication_invalid_transition")
        async with self._session_factory() as session, session.begin():
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(LegalDatasetPublicationModel)
                    .where(
                        LegalDatasetPublicationModel.id == run_id,
                        LegalDatasetPublicationModel.state == expected.value,
                    )
                    .values(state=target.value),
                ),
            )
            if result.rowcount != 1:
                raise PublicationError("publication_conflict")
            loaded = await _load_publication(
                session,
                select(LegalDatasetPublicationModel).where(
                    LegalDatasetPublicationModel.id == run_id
                ),
            )
            assert loaded is not None
            record = _record(*loaded)
        return record

    async def complete(self, run_id: UUID) -> DatasetPublication:
        async with self._session_factory() as session, session.begin():
            loaded = await _load_publication(
                session,
                select(LegalDatasetPublicationModel)
                .where(LegalDatasetPublicationModel.id == run_id)
                .with_for_update(),
            )
            if loaded is None:
                raise PublicationError("publication_not_found")
            model, payload = loaded
            record = _record(model, payload)
            if record.state == PublicationState.COMPLETED:
                # Historical retries must never overwrite a newer published snapshot.
                return record
            if record.state != PublicationState.ACKNOWLEDGED:
                raise PublicationError("publication_conflict")
            now = datetime.now(UTC)
            inventory = SqlAlchemyLegalCorpusInventoryRepository(session)
            candidate = record.candidate
            existing = await inventory.find_dataset(candidate.dataset_name)
            await inventory.upsert_dataset(
                replace(
                    candidate,
                    id=existing.id if existing is not None else candidate.id,
                    state=DatasetState.PUBLISHED,
                    released_at=now,
                )
            )
            model.state = PublicationState.COMPLETED.value
            model.active_alias = None
            model.completed_at = now.replace(tzinfo=None)
            await session.flush()
            record = _record(model, payload)
        return record
