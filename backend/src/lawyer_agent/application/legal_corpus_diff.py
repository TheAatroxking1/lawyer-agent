"""Provision-level diff between two versions of the same legal instrument.

Compares the old and the new version of one instrument by ``provision_no``
and full text, returning added / removed / modified / unchanged groups. Diff
never writes, never merges similar statutes into one LegalInstrument and never
guesses candidate relationships: the caller decides any follow-up. This is the
first, non-model step of the semi-automatic update flow (spec 6.6).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, cast
from uuid import UUID

from lawyer_agent.domain.legal_corpus import LegalInstrument, LegalVersion, Provision


@dataclass(frozen=True, slots=True)
class ModifiedProvision:
    """One provision whose full text differs between the two versions."""

    provision_no: str
    previous: Provision
    current: Provision

    def __post_init__(self) -> None:
        if not isinstance(self.provision_no, str) or not self.provision_no:
            raise ValueError("modified provision number must be non-empty text")
        if not isinstance(self.previous, Provision):
            raise ValueError("modified previous provision must be typed")
        if not isinstance(self.current, Provision):
            raise ValueError("modified current provision must be typed")
        if self.previous.provision_no != self.provision_no:
            raise ValueError("modified previous provision number mismatch")
        if self.current.provision_no != self.provision_no:
            raise ValueError("modified current provision number mismatch")
        if self.previous.full_text == self.current.full_text:
            raise ValueError("modified provisions must differ in full text")


@dataclass(frozen=True, slots=True)
class LegalVersionDiff:
    instrument_id: UUID
    from_version_id: UUID
    to_version_id: UUID
    added: tuple[Provision, ...]
    removed: tuple[Provision, ...]
    modified: tuple[ModifiedProvision, ...]
    unchanged: tuple[Provision, ...]

    def __post_init__(self) -> None:
        from lawyer_agent.domain.common import require_uuid7

        require_uuid7(self.instrument_id, field="diff instrument id")
        require_uuid7(self.from_version_id, field="diff from version id")
        require_uuid7(self.to_version_id, field="diff to version id")
        if self.from_version_id == self.to_version_id:
            raise ValueError("diff requires two distinct versions")


class LegalVersionDiffQueryPort(Protocol):
    """Read-only access to both versions of one instrument."""

    async def version_with_instrument(
        self, version_id: UUID
    ) -> tuple[LegalVersion, LegalInstrument] | None: ...

    async def provisions_for_version(
        self, version_id: UUID
    ) -> tuple[Provision, ...]: ...


class LegalVersionDiffError(ValueError):
    """Versions are missing or belong to different instruments."""

    status: int = 500
    code: str = "legal_version_diff_error"
    title: str = "Legal version diff failed"


class LegalVersionDiffVersionNotFound(LegalVersionDiffError):
    status = 404
    code = "legal_corpus_version_not_found"
    title = "No legal version exists for diff"

    def __init__(self, version_id: UUID) -> None:
        super().__init__(f"version not found: {version_id}")


class LegalVersionDiffCrossInstrument(LegalVersionDiffError):
    status = 409
    code = "legal_version_diff_cross_instrument"
    title = "Versions belong to different instruments"


class LegalVersionDiffService:
    """Computes the provision-level diff between two versions."""

    def __init__(self, query: LegalVersionDiffQueryPort) -> None:
        if not hasattr(query, "version_with_instrument"):
            raise ValueError("version diff requires a corpus query port")
        self._query = query

    async def diff(
        self, *, from_version_id: UUID, to_version_id: UUID
    ) -> LegalVersionDiff:
        old = await self._load_version(from_version_id)
        new = await self._load_version(to_version_id)
        if old.instrument_id != new.instrument_id:
            raise LegalVersionDiffCrossInstrument(
                "versions belong to different instruments; "
                "cross-instrument diff is refused"
            )
        return version_diff(
            await self._query.provisions_for_version(from_version_id),
            await self._query.provisions_for_version(to_version_id),
            instrument_id=old.instrument_id,
            from_version_id=from_version_id,
            to_version_id=to_version_id,
        )

    async def _load_version(self, version_id: UUID) -> LegalVersion:
        loaded = await self._query.version_with_instrument(version_id)
        if loaded is None:
            raise LegalVersionDiffVersionNotFound(version_id)
        return loaded[0]


class LegalVersionDiffUnitOfWorkPort(Protocol):
    corpus: LegalVersionDiffQueryPort

    async def __aenter__(self) -> LegalVersionDiffUnitOfWorkPort: ...

    async def __aexit__(self, *exc: object) -> None: ...


class LegalVersionDiffReadService:
    """Composition facade: one diff runs on one read-only unit of work."""

    def __init__(self, uow_factory: Callable[[], object]) -> None:
        if not callable(uow_factory):
            raise ValueError("version diff service requires a unit of work factory")
        self._uow_factory = uow_factory

    async def diff(
        self, *, from_version_id: UUID, to_version_id: UUID
    ) -> LegalVersionDiff:
        async with cast(
            LegalVersionDiffUnitOfWorkPort, self._uow_factory()
        ) as uow:
            return await LegalVersionDiffService(uow.corpus).diff(
                from_version_id=from_version_id,
                to_version_id=to_version_id,
            )


def version_diff(
    old_provisions: tuple[Provision, ...],
    new_provisions: tuple[Provision, ...],
    *,
    instrument_id: UUID,
    from_version_id: UUID,
    to_version_id: UUID,
) -> LegalVersionDiff:
    """Pure diff over two provision sets keyed by ``provision_no`` + text."""
    if not isinstance(old_provisions, tuple) or not isinstance(new_provisions, tuple):
        raise ValueError("diff requires tuples of typed provisions")
    old_by_no = {p.provision_no: p for p in old_provisions}
    new_by_no = {p.provision_no: p for p in new_provisions}

    added: list[Provision] = []
    removed: list[Provision] = []
    modified: list[ModifiedProvision] = []
    unchanged: list[Provision] = []

    for number, old in old_by_no.items():
        new = new_by_no.get(number)
        if new is None:
            removed.append(old)
        elif old.full_text != new.full_text:
            modified.append(
                ModifiedProvision(provision_no=number, previous=old, current=new)
            )
        else:
            unchanged.append(new)
    for number, new in new_by_no.items():
        if number not in old_by_no:
            added.append(new)

    added.sort(key=lambda p: (p.char_start, p.provision_no))
    removed.sort(key=lambda p: (p.char_start, p.provision_no))
    modified.sort(key=lambda m: (m.current.char_start, m.provision_no))
    unchanged.sort(key=lambda p: (p.char_start, p.provision_no))

    return LegalVersionDiff(
        instrument_id=instrument_id,
        from_version_id=from_version_id,
        to_version_id=to_version_id,
        added=tuple(added),
        removed=tuple(removed),
        modified=tuple(modified),
        unchanged=tuple(unchanged),
    )
