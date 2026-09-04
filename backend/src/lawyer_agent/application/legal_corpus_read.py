"""HTTP-facing legal corpus read orchestration (non-model, public corpus).

Exposes the read-only legal corpus query port. Corpus data is public platform
fact (not tenant-private), so reads require an authenticated account but no
tenant membership.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Protocol, cast
from uuid import UUID

from lawyer_agent.domain.legal_corpus import LegalVersion, Provision


class LegalCorpusQueryError(Exception):
    status: int = 500
    code: str = "legal_corpus_query_error"
    title: str = "Legal corpus query failed"


class LegalCorpusVersionNotFound(LegalCorpusQueryError):
    status = 404
    code = "legal_corpus_version_not_found"
    title = "No legal version is effective at the requested date"


class LegalCorpusQueryPort(Protocol):
    async def version_at(
        self, instrument_id: UUID, as_of: date
    ) -> LegalVersion | None: ...

    async def provisions_for_version(
        self, version_id: UUID
    ) -> tuple[Provision, ...]: ...


class LegalCorpusReadUnitOfWorkPort(Protocol):
    corpus: LegalCorpusQueryPort

    async def __aenter__(self) -> LegalCorpusReadUnitOfWorkPort: ...

    async def __aexit__(self, *exc: object) -> None: ...


class LegalCorpusQueryService:
    """Composition facade used by the legal corpus endpoints."""

    def __init__(self, uow_factory: Callable[[], object]) -> None:
        if not callable(uow_factory):
            raise ValueError("legal corpus query service requires a unit of work factory")
        self._uow_factory = uow_factory

    async def version_at(
        self, *, instrument_id: UUID, as_of: date
    ) -> LegalVersion:
        async with cast(LegalCorpusReadUnitOfWorkPort, self._uow_factory()) as uow:
            version = await uow.corpus.version_at(instrument_id, as_of)
        if version is None:
            raise LegalCorpusVersionNotFound()
        return version

    async def provisions_for_version(
        self, *, version_id: UUID
    ) -> tuple[Provision, ...]:
        async with cast(LegalCorpusReadUnitOfWorkPort, self._uow_factory()) as uow:
            provisions = await uow.corpus.provisions_for_version(version_id)
        return tuple(provisions)
