"""Structured legal claim gate (non-model).

spec 6.5: a model emits structured ``claims[]`` and every legal conclusion must
bind one or more ``evidence_id`` from the current Evidence Bundle. Before
anything is shown, the backend must decide whether the whole claim set is
publishable: every claim allowed only when all its citations pass the citation
gate (in-bundle / authorized / date-consistent / status-known). This module
judges an already-parsed claim set; parsing model JSON into ``LegalClaim``
belongs to the QA/provider layer. Verdicts never carry claim text.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol
from uuid import UUID

from lawyer_agent.application.evidence import (
    Citation,
    CitationVerdict,
    EvidenceBundle,
)
from lawyer_agent.domain.common import is_uuid7


class LegalClaimGateError(ValueError):
    """A structured claim is malformed."""


@dataclass(frozen=True, slots=True)
class LegalClaim:
    """One structured model conclusion bound to evidence ids."""

    text: str
    evidence_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise LegalClaimGateError("claim text must be non-empty text")
        if not isinstance(self.evidence_ids, tuple) or not self.evidence_ids:
            raise LegalClaimGateError(
                "claim must bind at least one evidence id"
            )
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise LegalClaimGateError("claim evidence ids must be unique")
        for evidence_id in self.evidence_ids:
            if not is_uuid7(evidence_id):
                raise LegalClaimGateError("claim evidence id must be a UUIDv7")


@dataclass(frozen=True, slots=True)
class LegalClaimVerdict:
    claim_index: int
    allowed: bool
    reason: str
    evidence_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class LegalClaimGateResult:
    allowed: bool
    refused: bool
    reason: str
    verdicts: tuple[LegalClaimVerdict, ...]


class _CitationGatePort(Protocol):
    def verify(
        self,
        bundle: EvidenceBundle,
        citations: tuple[Citation, ...],
        *,
        target_date: date,
    ) -> tuple[CitationVerdict, ...]: ...


class LegalClaimGate:
    """Publishes a claim set only when every claim and citation is allowed."""

    def __init__(self, bundle: EvidenceBundle, gate: _CitationGatePort) -> None:
        if not isinstance(bundle, EvidenceBundle):
            raise ValueError("claim gate requires a typed evidence bundle")
        if not hasattr(gate, "verify"):
            raise ValueError("claim gate requires a citation gate")
        self._bundle = bundle
        self._gate = gate

    async def verify(
        self,
        *,
        claims: tuple[LegalClaim, ...],
        target_date: date,
    ) -> LegalClaimGateResult:
        if not isinstance(claims, tuple):
            raise LegalClaimGateError("claims must be a tuple of structured claims")
        for claim in claims:
            if not isinstance(claim, LegalClaim):
                raise LegalClaimGateError("claims must contain typed LegalClaim")
        if not isinstance(target_date, date):
            raise LegalClaimGateError("target_date must be a date")
        if not claims:
            return LegalClaimGateResult(
                allowed=False, refused=True, reason="no_claims", verdicts=()
            )

        citations: list[Citation] = []
        for claim_index, claim in enumerate(claims):
            for evidence_id in claim.evidence_ids:
                citations.append(
                    Citation(claim_index=claim_index, evidence_id=evidence_id)
                )
        verdicts = self._gate.verify(
            self._bundle, tuple(citations), target_date=target_date
        )

        by_claim: dict[int, list[CitationVerdict]] = {}
        for verdict in verdicts:
            index = verdict.claim_index
            if index is not None:
                by_claim.setdefault(index, []).append(verdict)

        claim_verdicts: list[LegalClaimVerdict] = []
        overall_allowed = True
        first_failure: str | None = None
        for claim_index in range(len(claims)):
            claim_verdicts_for_index = by_claim.get(claim_index, [])
            blocked = [v for v in claim_verdicts_for_index if not v.allowed]
            if blocked:
                overall_allowed = False
                first_blocked = blocked[0]
                if first_failure is None:
                    first_failure = first_blocked.reason
                claim_verdicts.append(
                    LegalClaimVerdict(
                        claim_index=claim_index,
                        allowed=False,
                        reason=first_blocked.reason,
                        evidence_id=first_blocked.evidence_id,
                    )
                )
            else:
                claim_verdicts.append(
                    LegalClaimVerdict(
                        claim_index=claim_index,
                        allowed=True,
                        reason="allowed",
                    )
                )

        if overall_allowed:
            return LegalClaimGateResult(
                allowed=True,
                refused=False,
                reason="allowed",
                verdicts=tuple(claim_verdicts),
            )
        return LegalClaimGateResult(
            allowed=False,
            refused=True,
            reason=f"claim_not_supported:{first_failure}",
            verdicts=tuple(claim_verdicts),
        )
