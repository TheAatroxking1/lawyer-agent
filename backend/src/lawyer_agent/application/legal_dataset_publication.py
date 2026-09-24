"""Conservative cross-system publication and explicit recovery protocol."""

from __future__ import annotations

from copy import deepcopy
from typing import Protocol
from uuid import UUID

from lawyer_agent.domain.legal_corpus import DatasetSnapshot
from lawyer_agent.domain.legal_dataset_publication import (
    DatasetPublication,
    PublicationError,
    PublicationState,
    validate_candidate,
    validate_reviewed_candidate,
)


class DatasetPublicationStore(Protocol):
    """Each mutation commits independently; complete atomically writes snapshot.

    Create excludes concurrent unfinished runs for an alias and index reuse.
    Transition uses state CAS, raising publication_conflict on lost ownership.
    Errors never release an unfinished run's alias reservation.
    """

    async def create(
        self, candidate: DatasetSnapshot, previous_target: str | None
    ) -> DatasetPublication: ...

    async def get(self, run_id: UUID) -> DatasetPublication | None: ...

    async def find_active(self, alias: str) -> DatasetPublication | None:
        """Find the occupied alias after a caller loses the committed run ID."""
        ...

    async def transition(
        self, run_id: UUID, expected: PublicationState, target: PublicationState
    ) -> DatasetPublication: ...

    async def complete(self, run_id: UUID) -> DatasetPublication: ...


class DatasetPublicationAlias(Protocol):
    async def publish_dataset(self, alias: str, index_name: str) -> str | None: ...

    async def active_dataset_index(self, alias: str) -> str | None: ...


class LegalDatasetPublicationService:
    def __init__(self, store: DatasetPublicationStore, alias: DatasetPublicationAlias) -> None:
        self._store = store
        self._alias = alias

    async def publish(self, candidate: DatasetSnapshot) -> DatasetPublication:
        candidate = deepcopy(candidate)
        validate_candidate(candidate)
        previous = await self._alias.active_dataset_index(candidate.dataset_name)
        record = await self._store.create(candidate, previous)
        return await self.resume(record.id)

    async def resume(self, run_id: UUID, *, require_review: bool = False) -> DatasetPublication:
        record = await self._store.get(run_id)
        if record is None:
            raise PublicationError("publication_not_found")
        if record.state == PublicationState.COMPLETED:
            return record
        if record.state == PublicationState.SWITCHING:
            # GET=target cannot establish whether an earlier request is still live.
            raise PublicationError("publication_outcome_unknown")
        candidate = record.candidate
        alias = candidate.dataset_name
        index_name = candidate.manifest["index_name"]
        if record.state == PublicationState.READY:
            if require_review:
                validate_reviewed_candidate(candidate)
            record = await self._store.transition(
                run_id, PublicationState.READY, PublicationState.SWITCHING
            )
            if await self._alias.active_dataset_index(alias) != record.previous_target:
                raise PublicationError("publication_alias_conflict")
            previous = await self._alias.publish_dataset(alias, index_name)
            if previous != record.previous_target:
                raise PublicationError("publication_alias_conflict")
            record = await self._store.transition(
                run_id, PublicationState.SWITCHING, PublicationState.ACKNOWLEDGED
            )
        if await self._alias.active_dataset_index(alias) != index_name:
            raise PublicationError("publication_alias_conflict")
        return await self._store.complete(run_id)
