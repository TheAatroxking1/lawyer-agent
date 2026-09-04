from __future__ import annotations

from datetime import date

import pytest

from lawyer_agent.application.evidence import (
    Citation,
    CitationGate,
    EvidenceBundle,
    EvidenceBundleError,
    EvidenceItem,
)
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import LegalVersionStatus


def _item(
    *,
    status: LegalVersionStatus = LegalVersionStatus.CURRENT,
    effective_on: date | None = date(2021, 1, 1),
    repealed_on: date | None = None,
    authorized: bool = True,
) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=new_uuid7(),
        instrument_title="中华人民共和国民法典",
        version_id=new_uuid7(),
        version_label="2020 公布版",
        status=status,
        published_on=date(2020, 5, 28),
        effective_on=effective_on,
        repealed_on=repealed_on,
        provision_no="第一条",
        provision_text="第一条 为了保护民事主体的合法权益，制定本法。",
        source_ref="object://corpus/civil-code.docx",
        dataset_version="dataset_v1",
        authorized=authorized,
    )


def test_bundle_rejects_empty_or_duplicate() -> None:
    with pytest.raises(EvidenceBundleError, match="empty"):
        EvidenceBundle(items=())
    item = _item()
    with pytest.raises(EvidenceBundleError, match="unique"):
        EvidenceBundle(items=(item, item))


def test_gate_allows_citation_in_bundle() -> None:
    item = _item()
    bundle = EvidenceBundle(items=(item,))
    verdict = CitationGate().verify(
        bundle,
        (Citation(claim_index=0, evidence_id=item.evidence_id),),
        target_date=date(2023, 6, 1),
    )[0]
    assert verdict.allowed
    assert verdict.reason == "allowed"


def test_gate_rejects_citation_outside_bundle() -> None:
    bundle = EvidenceBundle(items=(_item(),))
    outsider = new_uuid7()
    verdict = CitationGate().verify(
        bundle,
        (Citation(claim_index=0, evidence_id=outsider),),
        target_date=date(2023, 6, 1),
    )[0]
    assert not verdict.allowed
    assert verdict.reason == "evidence_not_in_bundle"


def test_gate_rejects_status_unknown() -> None:
    item = _item(status=LegalVersionStatus.STATUS_UNKNOWN)
    bundle = EvidenceBundle(items=(item,))
    verdict = CitationGate().verify(
        bundle,
        (Citation(claim_index=0, evidence_id=item.evidence_id),),
        target_date=date(2023, 6, 1),
    )[0]
    assert not verdict.allowed
    assert verdict.reason == "effective_status_unknown"


def test_gate_rejects_before_effective_and_after_repeal() -> None:
    item = _item(
        status=LegalVersionStatus.REPEALED,
        effective_on=date(2020, 1, 1),
        repealed_on=date(2022, 1, 1),
    )
    bundle = EvidenceBundle(items=(item,))
    gate = CitationGate()
    before = gate.verify(
        bundle,
        (Citation(0, item.evidence_id),),
        target_date=date(2019, 1, 1),
    )[0]
    after = gate.verify(
        bundle,
        (Citation(0, item.evidence_id),),
        target_date=date(2023, 1, 1),
    )[0]
    assert before.reason == "not_effective_on_target_date"
    assert after.reason == "repealed_on_target_date"


def test_gate_rejects_unauthorized_evidence() -> None:
    item = _item(authorized=False)
    bundle = EvidenceBundle(items=(item,))
    verdict = CitationGate().verify(
        bundle,
        (Citation(0, item.evidence_id),),
        target_date=date(2023, 6, 1),
    )[0]
    assert not verdict.allowed
    assert verdict.reason == "evidence_not_authorized"
