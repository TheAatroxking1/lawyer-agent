"""OpenSearch legal-corpus retrieval values (non-model, BM25-first)."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from uuid import UUID

from lawyer_agent.domain.common import require_uuid7


def validate_version_scope(
    version_id: UUID | None, version_ids: tuple[UUID, ...] | None,
) -> None:
    """Validate the mutually exclusive public corpus version filters."""
    if version_id is not None and version_ids is not None:
        raise ValueError("version_id and version_ids are mutually exclusive")
    if version_id is not None:
        require_uuid7(version_id, field="version_id")
    if version_ids is not None:
        if not isinstance(version_ids, tuple) or not version_ids:
            raise ValueError("version_ids must be a non-empty tuple")
        for value in version_ids:
            require_uuid7(value, field="version_ids item")
        if len(set(version_ids)) != len(version_ids):
            raise ValueError("version_ids must be unique")


@dataclass(frozen=True, slots=True)
class LegalSearchHit:
    """One BM25 hit mapped back to its corpus chunk and parent provision."""

    chunk_id: UUID
    provision_id: UUID
    version_id: UUID
    score: float

    def __post_init__(self) -> None:
        require_uuid7(self.chunk_id, field="hit chunk_id")
        require_uuid7(self.provision_id, field="hit provision_id")
        require_uuid7(self.version_id, field="hit version_id")
        if not isinstance(self.score, float) or not isfinite(self.score):
            raise ValueError("hit score must be a finite float")
