"""Legal corpus import onboarding: import a version and build its chunks.

The controlled-import service persists instruments/versions/provisions only;
nothing in production derives the retrieval chunk rows afterwards (tests used
to hand-derive them). This orchestration runs the existing import and then
derives + persists the PROVISION chunks for that version in one flow, so the
vector-indexing layer always has an in-DB chunk feed to consume. Re-running the
same command is safe: chunk replace is delete-then-insert (idempotent).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from lawyer_agent.application.legal_corpus_chunks import derive_chunks
from lawyer_agent.application.legal_corpus_import import (
    LegalImportCommand,
    LegalImportResult,
)
from lawyer_agent.domain.legal_corpus import LegalChunk, Provision


@dataclass(frozen=True, slots=True)
class OnboardingResult:
    instrument_id: UUID
    version_id: UUID
    chunk_count: int


class _ImportVersionCallable(Protocol):
    async def __call__(
        self, command: LegalImportCommand
    ) -> LegalImportResult: ...


class _ProvisionsCallable(Protocol):
    async def __call__(
        self, version_id: UUID
    ) -> tuple[Provision, ...]: ...


class _ChunkReplaceCallable(Protocol):
    async def __call__(
        self, version_id: UUID, chunks: tuple[LegalChunk, ...]
    ) -> None: ...


class LegalCorpusOnboardingService:
    """Imports one version and derives+persists its PROVISION chunk rows."""

    def __init__(
        self,
        *,
        import_version: _ImportVersionCallable,
        provisions: _ProvisionsCallable,
        chunks: _ChunkReplaceCallable,
    ) -> None:
        if not callable(import_version):
            raise ValueError("onboarding requires an import callable")
        if not callable(provisions):
            raise ValueError("onboarding requires a provisions callable")
        if not callable(chunks):
            raise ValueError("onboarding requires a chunk write callable")
        self._import_version = import_version
        self._provisions = provisions
        self._chunks = chunks

    async def onboard_version(
        self, command: LegalImportCommand
    ) -> OnboardingResult:
        result = await self._import_version(command)
        provisions = await self._provisions(result.version_id)
        if not provisions:
            return OnboardingResult(
                instrument_id=result.instrument_id,
                version_id=result.version_id,
                chunk_count=0,
            )
        chunks = derive_chunks(
            version_id=result.version_id,
            provisions=provisions,
            parser_version=command.parser_version,
        )
        await self._chunks(result.version_id, chunks)
        return OnboardingResult(
            instrument_id=result.instrument_id,
            version_id=result.version_id,
            chunk_count=len(chunks),
        )
