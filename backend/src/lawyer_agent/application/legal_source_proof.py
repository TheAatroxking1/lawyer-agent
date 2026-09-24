"""Persist provenance once and require complete identity equality on replay."""

from typing import Protocol
from uuid import UUID

from lawyer_agent.application.legal_corpus_import import (
    LegalCorpusImportConflict,
    LegalImportResult,
)
from lawyer_agent.domain.legal_source_proof import (
    STATIC_CONVERTER_VERSION,
    LegalSourceProof,
    static_review_matches,
)


class LegalSourceProofPort(Protocol):
    async def find_for_version(self, version_id: UUID) -> LegalSourceProof | None: ...

    async def create_for_version(self, version_id: UUID, proof: LegalSourceProof) -> None: ...


class LegalSourceProofService:
    """Participates in the caller's import transaction; never commits or repairs history."""

    def __init__(self, repository: LegalSourceProofPort) -> None:
        self._repository = repository

    async def ensure(
        self, imported: LegalImportResult, proof: LegalSourceProof, *,
        static_review_sha256: str | None = None,
    ) -> None:
        if (proof.converter_version == STATIC_CONVERTER_VERSION
                and not static_review_matches(proof, static_review_sha256)):
            raise LegalCorpusImportConflict("static_recovery_requires_quality_review")
        existing = await self._repository.find_for_version(imported.version_id)
        if imported.replayed:
            if existing is None:
                raise LegalCorpusImportConflict("source_proof_missing_requires_review")
            if existing != proof:
                raise LegalCorpusImportConflict("source_proof_conflict")
            return
        if existing is not None:
            raise LegalCorpusImportConflict("source_proof_conflict")
        await self._repository.create_for_version(imported.version_id, proof)
