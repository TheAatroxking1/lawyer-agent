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

from lawyer_agent.domain.common import require_uuid7
from lawyer_agent.domain.legal_corpus import (
    LegalInstrument,
    LegalVersion,
    Provision,
)


def _nonempty_filter(value: str | None, *, max_chars: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise LegalCorpusInvalidRequest()
    stripped = value.strip()
    if len(stripped) > max_chars:
        raise LegalCorpusInvalidRequest()
    return stripped


class LegalCorpusQueryError(Exception):
    status: int = 500
    code: str = "legal_corpus_query_error"
    title: str = "Legal corpus query failed"


class LegalCorpusVersionNotFound(LegalCorpusQueryError):
    status = 404
    code = "legal_corpus_version_not_found"
    title = "No legal version is effective at the requested date"


class LegalCorpusInstrumentNotFound(LegalCorpusQueryError):
    status = 404
    code = "legal_corpus_instrument_not_found"
    title = "Legal instrument not found"


class LegalCorpusInvalidRequest(LegalCorpusQueryError):
    status = 422
    code = "legal_corpus_invalid_request"
    title = "Legal corpus request is invalid"


class LegalCorpusInstrumentCursorInvalid(LegalCorpusQueryError):
    status = 404
    code = "legal_corpus_instrument_cursor_invalid"
    title = "Legal instrument list cursor is invalid"


class LegalCorpusQueryPort(Protocol):
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

    async def version_with_instrument(
        self, version_id: UUID
    ) -> tuple[LegalVersion, LegalInstrument] | None: ...

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

    async def versions_for_instrument(
        self, *, instrument_id: UUID
    ) -> tuple[LegalVersion, ...]:
        async with cast(LegalCorpusReadUnitOfWorkPort, self._uow_factory()) as uow:
            exists = await uow.corpus.instrument_exists(instrument_id)
            if not exists:
                raise LegalCorpusInstrumentNotFound()
            versions = await uow.corpus.versions_for_instrument(instrument_id)
        return tuple(versions)

    async def instrument(self, *, instrument_id: UUID) -> LegalInstrument:
        async with cast(LegalCorpusReadUnitOfWorkPort, self._uow_factory()) as uow:
            instrument = await uow.corpus.instrument_by_id(instrument_id)
        if instrument is None:
            raise LegalCorpusInstrumentNotFound()
        return instrument

    async def version(self, *, version_id: UUID) -> LegalVersion:
        async with cast(LegalCorpusReadUnitOfWorkPort, self._uow_factory()) as uow:
            loaded = await uow.corpus.version_with_instrument(version_id)
        if loaded is None:
            raise LegalCorpusVersionNotFound()
        return loaded[0]

    async def instruments(
        self,
        *,
        limit: int,
        before_id: UUID | None = None,
        title: str | None = None,
        issuing_authority: str | None = None,
        jurisdiction: str | None = None,
        region_code: str | None = None,
    ) -> tuple[LegalInstrument, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise LegalCorpusInvalidRequest()
        if before_id is not None:
            try:
                require_uuid7(before_id, field="before_id")
            except ValueError as exc:
                raise LegalCorpusInvalidRequest() from exc
        title_filter = _nonempty_filter(title, max_chars=512)
        authority_filter = _nonempty_filter(issuing_authority, max_chars=256)
        jurisdiction_filter = _nonempty_filter(jurisdiction, max_chars=64)
        region_filter = _nonempty_filter(region_code, max_chars=16)
        async with cast(LegalCorpusReadUnitOfWorkPort, self._uow_factory()) as uow:
            from lawyer_agent.infrastructure.persistence.repositories.legal_corpus import (
                LegalCorpusInstrumentListCursorInvalid,
            )

            try:
                return await uow.corpus.list_instruments(
                    limit=limit,
                    before_id=before_id,
                    title=title_filter,
                    issuing_authority=authority_filter,
                    jurisdiction=jurisdiction_filter,
                    region_code=region_filter,
                )
            except LegalCorpusInstrumentListCursorInvalid as exc:
                raise LegalCorpusInstrumentCursorInvalid() from exc
