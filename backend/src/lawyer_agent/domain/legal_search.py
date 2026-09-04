"""OpenSearch legal-corpus retrieval values (non-model, BM25-first)."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from lawyer_agent.domain.common import require_uuid7


@dataclass(frozen=True, slots=True)
class LegalSearchHit:
    """One BM25 hit mapped back to its corpus chunk and parent provision."""

    chunk_id: object
    provision_id: object
    version_id: object
    score: float

    def __post_init__(self) -> None:
        require_uuid7(self.chunk_id, field="hit chunk_id")
        require_uuid7(self.provision_id, field="hit provision_id")
        require_uuid7(self.version_id, field="hit version_id")
        if not isinstance(self.score, float) or not isfinite(self.score):
            raise ValueError("hit score must be a finite float")
