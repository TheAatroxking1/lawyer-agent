"""Restore authoritative provisions from retrieval hits and assemble evidence.

Given ordered retrieval hits (already fused/truncated by the hybrid search
service), this service reloads the authoritative version + instrument + full
provision text from the legal corpus read side and builds the typed
``EvidenceBundle`` consumed by the Citation Gate. It never fabricates: a hit
whose version or provision is absent, or whose version lacks traceable source
metadata, is refused instead of being dropped or guessed.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from lawyer_agent.application.evidence import (
    EvidenceAccessPort,
    EvidenceBundle,
    EvidenceItem,
)
from lawyer_agent.domain.legal_corpus import LegalInstrument, LegalVersion, Provision
from lawyer_agent.domain.legal_search import LegalSearchHit


class LegalEvidenceQueryPort(Protocol):
    """Read-only authoritative corpus access used to restore evidence."""

    async def version_with_instrument(
        self, version_id: UUID
    ) -> tuple[LegalVersion, LegalInstrument] | None: ...

    async def provisions_for_version(
        self, version_id: UUID
    ) -> tuple[Provision, ...]: ...


class LegalEvidenceAssemblyError(ValueError):
    """A retrieval hit could not be restored into authoritative evidence."""


class LegalEvidenceVersionNotFound(LegalEvidenceAssemblyError):
    def __init__(self, version_id: UUID) -> None:
        super().__init__(f"evidence version not found: {version_id}")


class LegalEvidenceProvisionNotFound(LegalEvidenceAssemblyError):
    def __init__(self, version_id: UUID, provision_id: UUID) -> None:
        super().__init__(
            f"evidence provision {provision_id} not found in version {version_id}"
        )


class LegalEvidenceSourceMissing(LegalEvidenceAssemblyError):
    def __init__(self, version_id: UUID, field: str) -> None:
        super().__init__(
            f"evidence version {version_id} lacks required provenance field {field}"
        )


class LegalEvidenceAssemblyService:
    """Assembles an EvidenceBundle from ordered retrieval hits (public corpus)."""

    def __init__(
        self,
        query: LegalEvidenceQueryPort,
        access: EvidenceAccessPort | None = None,
    ) -> None:
        if not hasattr(query, "version_with_instrument"):
            raise ValueError("evidence assembly requires a corpus query port")
        self._query = query
        self._access = access

    async def assemble(
        self, *, hits: Sequence[LegalSearchHit]
    ) -> EvidenceBundle | None:
        typed_hits = tuple(hits)
        if not typed_hits:
            return None

        # One lookup per referenced version; keep first-seen hit order and dedupe
        # chunk hits that resolve to the same provision.
        seen_provisions: set[tuple[UUID, UUID]] = set()
        ordered: list[tuple[LegalVersion, LegalInstrument, Provision]] = []
        per_version: dict[UUID, tuple[LegalVersion, LegalInstrument, dict[UUID, Provision]]] = {}

        for hit in typed_hits:
            key = (hit.version_id, hit.provision_id)
            if key in seen_provisions:
                continue
            seen_provisions.add(key)
            version_id = hit.version_id
            if version_id not in per_version:
                loaded = await self._query.version_with_instrument(version_id)
                if loaded is None:
                    raise LegalEvidenceVersionNotFound(version_id)
                version, instrument = loaded
                provisions = {
                    provision.id: provision
                    for provision in await self._query.provisions_for_version(version_id)
                }
                per_version[version_id] = (version, instrument, provisions)
            version, instrument, provisions = per_version[version_id]
            provision = provisions.get(hit.provision_id)
            if provision is None:
                raise LegalEvidenceProvisionNotFound(version_id, hit.provision_id)
            ordered.append((version, instrument, provision))

        items: list[EvidenceItem] = []
        for version, instrument, provision in ordered:
            if not version.source_ref:
                raise LegalEvidenceSourceMissing(version.id, "source_ref")
            if not version.dataset_version:
                raise LegalEvidenceSourceMissing(version.id, "dataset_version")
            authorized = True
            if self._access is not None:
                authorized = await self._access.authorize_evidence(provision.id)
            items.append(
                EvidenceItem(
                    evidence_id=provision.id,
                    instrument_title=instrument.title,
                    version_id=version.id,
                    version_label=version.version_label,
                    status=version.status,
                    published_on=version.published_on,
                    effective_on=version.effective_on,
                    repealed_on=version.repealed_on,
                    provision_no=provision.provision_no,
                    provision_text=provision.full_text,
                    source_ref=version.source_ref,
                    dataset_version=version.dataset_version,
                    authorized=authorized,
                )
            )
        return EvidenceBundle(items=tuple(items))
