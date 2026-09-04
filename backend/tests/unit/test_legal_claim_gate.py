from __future__ import annotations

from datetime import date
from uuid import UUID

import pytest

from lawyer_agent.application.evidence import (
    CitationGate,
    EvidenceBundle,
    EvidenceItem,
)
from lawyer_agent.application.legal_claim_gate import (
    LegalClaim,
    LegalClaimGate,
    LegalClaimGateError,
)
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import LegalVersionStatus

EVIDENCE_A = new_uuid7()
EVIDENCE_B = new_uuid7()
VERSION = new_uuid7()


def _item(
    evidence_id: UUID,
    *,
    status: LegalVersionStatus = LegalVersionStatus.CURRENT,
    effective_on: date | None = date(2021, 1, 1),
    repealed_on: date | None = None,
    authorized: bool = True,
) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        instrument_title="中华人民共和国民法典",
        version_id=VERSION,
        version_label="2020 公布版",
        status=status,
        published_on=date(2020, 5, 28),
        effective_on=effective_on,
        repealed_on=repealed_on,
        provision_no="第一条",
        provision_text="第一条 条文内容。",
        source_ref="object://corpus/sample.docx",
        dataset_version="dataset_v1",
        authorized=authorized,
    )


def _bundle(*items: EvidenceItem) -> EvidenceBundle:
    return EvidenceBundle(items=items)


def _gate(bundle: EvidenceBundle) -> LegalClaimGate:
    return LegalClaimGate(bundle=bundle, gate=CitationGate())


async def test_allowed_single_claim_multiple_citations() -> None:
    bundle = _bundle(_item(EVIDENCE_A), _item(EVIDENCE_B))
    result = await _gate(bundle).verify(
        claims=(
            LegalClaim(
                text="结论一。",
                evidence_ids=(EVIDENCE_A, EVIDENCE_B),
            ),
        ),
        target_date=date(2023, 6, 1),
    )
    assert result.allowed
    assert not result.refused
    assert result.reason == "allowed"
    assert len(result.verdicts) == 1
    assert result.verdicts[0].claim_index == 0
    assert result.verdicts[0].allowed


async def test_allowed_multiple_claims() -> None:
    bundle = _bundle(_item(EVIDENCE_A), _item(EVIDENCE_B))
    result = await _gate(bundle).verify(
        claims=(
            LegalClaim(text="结论一。", evidence_ids=(EVIDENCE_A,)),
            LegalClaim(text="结论二。", evidence_ids=(EVIDENCE_B,)),
        ),
        target_date=date(2023, 6, 1),
    )
    assert result.allowed
    assert len(result.verdicts) == 2
    assert all(v.allowed for v in result.verdicts)


async def test_refused_claim_citing_outside_bundle() -> None:
    bundle = _bundle(_item(EVIDENCE_A))
    result = await _gate(bundle).verify(
        claims=(LegalClaim(text="结论。", evidence_ids=(new_uuid7(),)),),
        target_date=date(2023, 6, 1),
    )
    assert result.refused
    assert not result.allowed
    assert result.reason == "claim_not_supported:evidence_not_in_bundle"
    assert not result.verdicts[0].allowed
    assert result.verdicts[0].reason == "evidence_not_in_bundle"


async def test_refused_mixed_claims_any_failure_blocks() -> None:
    bundle = _bundle(_item(EVIDENCE_A))
    outsider = new_uuid7()
    result = await _gate(bundle).verify(
        claims=(
            LegalClaim(text="好结论。", evidence_ids=(EVIDENCE_A,)),
            LegalClaim(text="坏结论。", evidence_ids=(outsider,)),
        ),
        target_date=date(2023, 6, 1),
    )
    assert result.refused
    assert not result.allowed
    assert result.verdicts[0].allowed
    assert not result.verdicts[1].allowed


async def test_refused_unauthorized_evidence() -> None:
    bundle = _bundle(_item(EVIDENCE_A, authorized=False))
    result = await _gate(bundle).verify(
        claims=(LegalClaim(text="结论。", evidence_ids=(EVIDENCE_A,)),),
        target_date=date(2023, 6, 1),
    )
    assert result.refused
    assert result.reason == "claim_not_supported:evidence_not_authorized"


async def test_refused_before_effective_and_repealed() -> None:
    repealed = _item(
        new_uuid7(),
        status=LegalVersionStatus.REPEALED,
        effective_on=date(2020, 1, 1),
        repealed_on=date(2022, 1, 1),
    )
    bundle = _bundle(_item(EVIDENCE_A), repealed)
    before = await _gate(bundle).verify(
        claims=(LegalClaim(text="结论。", evidence_ids=(EVIDENCE_A,)),),
        target_date=date(2019, 1, 1),
    )
    after = await _gate(bundle).verify(
        claims=(LegalClaim(text="结论。", evidence_ids=(repealed.evidence_id,)),),
        target_date=date(2023, 1, 1),
    )
    assert before.refused
    assert before.reason == "claim_not_supported:not_effective_on_target_date"
    assert after.refused
    assert after.reason == "claim_not_supported:repealed_on_target_date"


async def test_refused_unknown_status() -> None:
    bundle = _bundle(_item(EVIDENCE_A, status=LegalVersionStatus.STATUS_UNKNOWN))
    result = await _gate(bundle).verify(
        claims=(LegalClaim(text="结论。", evidence_ids=(EVIDENCE_A,)),),
        target_date=date(2023, 6, 1),
    )
    assert result.refused
    assert result.reason == "claim_not_supported:effective_status_unknown"


async def test_refused_empty_claims() -> None:
    bundle = _bundle(_item(EVIDENCE_A))
    result = await _gate(bundle).verify(
        claims=(),
        target_date=date(2023, 6, 1),
    )
    assert result.refused
    assert result.reason == "no_claims"
    assert result.verdicts == ()


async def test_invalid_claims_rejected() -> None:
    bundle = _bundle(_item(EVIDENCE_A))
    gate = _gate(bundle)
    with pytest.raises(LegalClaimGateError, match="text"):
        await gate.verify(
            claims=(LegalClaim(text="", evidence_ids=(EVIDENCE_A,)),),
            target_date=date(2023, 6, 1),
        )
    with pytest.raises(LegalClaimGateError, match="evidence"):
        await gate.verify(
            claims=(LegalClaim(text="结论。", evidence_ids=()),),
            target_date=date(2023, 6, 1),
        )
    with pytest.raises(LegalClaimGateError, match="evidence"):
        await gate.verify(
            claims=(LegalClaim(text="结论。", evidence_ids=(UUID(int=1),)),),
            target_date=date(2023, 6, 1),
        )


async def test_verdicts_never_contain_claim_text() -> None:
    bundle = _bundle(_item(EVIDENCE_A))
    result = await _gate(bundle).verify(
        claims=(LegalClaim(text="机密结论正文。", evidence_ids=(EVIDENCE_A,)),),
        target_date=date(2023, 6, 1),
    )
    text = str(result)
    assert "机密结论正文。" not in text
