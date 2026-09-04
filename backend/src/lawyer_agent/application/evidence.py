"""Evidence Bundle and Citation Gate (backend only, no model calls).

A legal conclusion in this slice is only "supported" when every cited
evidence_id exists in the current bundle, belongs to the requesting access
scope, and does not conflict with the target date. Unknown effective status can
never be presented as current.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol
from uuid import UUID

from lawyer_agent.domain.common import require_uuid7
from lawyer_agent.domain.legal_corpus import LegalVersionStatus


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    evidence_id: UUID
    instrument_title: str
    version_id: UUID
    version_label: str
    status: LegalVersionStatus
    published_on: date | None
    effective_on: date | None
    repealed_on: date | None
    provision_no: str
    provision_text: str
    source_ref: str
    dataset_version: str
    authorized: bool

    def __post_init__(self) -> None:
        require_uuid7(self.evidence_id, field="evidence id")
        require_uuid7(self.version_id, field="evidence version id")
        if not isinstance(self.instrument_title, str) or not self.instrument_title:
            raise ValueError("evidence instrument title must be non-empty")
        if not isinstance(self.provision_no, str) or not self.provision_no:
            raise ValueError("evidence provision number must be non-empty")
        if not isinstance(self.provision_text, str) or not self.provision_text:
            raise ValueError("evidence provision text must be non-empty")
        if not isinstance(self.status, LegalVersionStatus):
            raise ValueError("evidence status must be strongly typed")


@dataclass(frozen=True, slots=True)
class Citation:
    claim_index: int
    evidence_id: UUID


@dataclass(frozen=True, slots=True)
class CitationVerdict:
    allowed: bool
    reason: str
    claim_index: int | None = None
    evidence_id: UUID | None = None


class EvidenceAccessPort(Protocol):
    async def authorize_evidence(
        self, evidence_id: UUID
    ) -> bool: ...


class EvidenceBundleError(ValueError):
    pass


class EvidenceBundle:
    def __init__(self, items: tuple[EvidenceItem, ...]) -> None:
        if not isinstance(items, tuple) or not items:
            raise EvidenceBundleError("an evidence bundle must not be empty")
        ids = [item.evidence_id for item in items]
        if len(set(ids)) != len(ids):
            raise EvidenceBundleError("evidence ids must be unique within a bundle")
        self.items = items
        self._by_id = {item.evidence_id: item for item in items}

    def __contains__(self, evidence_id: object) -> bool:
        return evidence_id in self._by_id

    def item(self, evidence_id: UUID) -> EvidenceItem:
        try:
            return self._by_id[evidence_id]
        except KeyError as exc:
            raise EvidenceBundleError("evidence id is not in this bundle") from exc


class CitationGate:
    """Refuses conclusions citing evidence outside the current bundle."""

    def verify(
        self,
        bundle: EvidenceBundle,
        citations: tuple[Citation, ...],
        *,
        target_date: date,
    ) -> tuple[CitationVerdict, ...]:
        if not isinstance(bundle, EvidenceBundle):
            raise ValueError("citation gate requires a typed evidence bundle")
        verdicts: list[CitationVerdict] = []
        for citation in citations:
            verdicts.append(
                self._verify_one(bundle, citation, target_date=target_date)
            )
        return tuple(verdicts)

    def _verify_one(
        self,
        bundle: EvidenceBundle,
        citation: Citation,
        *,
        target_date: date,
    ) -> CitationVerdict:
        if citation.evidence_id not in bundle:
            return CitationVerdict(
                False,
                "evidence_not_in_bundle",
                citation.claim_index,
                citation.evidence_id,
            )
        item = bundle.item(citation.evidence_id)
        if not item.authorized:
            return CitationVerdict(
                False,
                "evidence_not_authorized",
                citation.claim_index,
                citation.evidence_id,
            )
        if item.status is LegalVersionStatus.STATUS_UNKNOWN:
            return CitationVerdict(
                False,
                "effective_status_unknown",
                citation.claim_index,
                citation.evidence_id,
            )
        if target_date < (item.effective_on or date.min):
            return CitationVerdict(
                False,
                "not_effective_on_target_date",
                citation.claim_index,
                citation.evidence_id,
            )
        if (
            item.status is LegalVersionStatus.REPEALED
            and item.repealed_on is not None
            and target_date > item.repealed_on
        ):
            return CitationVerdict(
                False,
                "repealed_on_target_date",
                citation.claim_index,
                citation.evidence_id,
            )
        return CitationVerdict(True, "allowed", citation.claim_index, citation.evidence_id)
