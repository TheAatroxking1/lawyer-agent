from __future__ import annotations

from datetime import date

import pytest

from lawyer_agent.application.evidence import (
    CitationGate,
    EvidenceBundle,
    EvidenceItem,
)
from lawyer_agent.application.legal_claim_gate import LegalClaimGate
from lawyer_agent.application.legal_claim_parse import (
    LegalClaimsParseError,
    parse_claims_text,
)
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import LegalVersionStatus

A = new_uuid7()
B = new_uuid7()


def _payload() -> dict[str, object]:
    return {
        "claims": [
            {"text": "结论一。", "evidence_ids": [str(A), str(B)]},
            {"text": "结论二。", "evidence_ids": [str(A)]},
        ]
    }


def _json(payload: dict[str, object]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False)


def test_parse_plain_json() -> None:
    claims = parse_claims_text(_json(_payload()))
    assert len(claims) == 2
    assert claims[0].text == "结论一。"
    assert claims[0].evidence_ids == (A, B)
    assert claims[1].evidence_ids == (A,)


def test_parse_strips_markdown_fence() -> None:
    text = f"```json\n{_json(_payload())}\n```"
    claims = parse_claims_text(text)
    assert len(claims) == 2


def test_parse_one_controlled_repair_for_prefix_prose() -> None:
    text = f"好的，这是依据：\n{_json(_payload())}"
    claims = parse_claims_text(text)
    assert len(claims) == 2


def test_parse_rejects_malformed_json() -> None:
    with pytest.raises(LegalClaimsParseError):
        parse_claims_text("这不是 JSON")


def test_parse_rejects_missing_claims_key() -> None:
    with pytest.raises(LegalClaimsParseError, match="claims"):
        parse_claims_text(_json({"answer": "x"}))


def test_parse_rejects_claims_not_list() -> None:
    with pytest.raises(LegalClaimsParseError, match="claims"):
        parse_claims_text(_json({"claims": {"text": "x"}}))


def test_parse_rejects_claim_missing_text() -> None:
    with pytest.raises(LegalClaimsParseError, match="text"):
        parse_claims_text(_json({"claims": [{"evidence_ids": [str(A)]}]}))


def test_parse_rejects_empty_text() -> None:
    with pytest.raises(LegalClaimsParseError, match="text"):
        parse_claims_text(_json({"claims": [{"text": " ", "evidence_ids": [str(A)]}]}))


def test_parse_rejects_unknown_claim_key() -> None:
    with pytest.raises(LegalClaimsParseError, match="key"):
        parse_claims_text(
            _json({"claims": [{"text": "x", "evidence_ids": [str(A)], "extra": 1}]})
        )


def test_parse_rejects_missing_evidence_ids() -> None:
    with pytest.raises(LegalClaimsParseError, match="evidence"):
        parse_claims_text(_json({"claims": [{"text": "x"}]}))


def test_parse_rejects_empty_evidence_ids() -> None:
    with pytest.raises(LegalClaimsParseError, match="evidence"):
        parse_claims_text(_json({"claims": [{"text": "x", "evidence_ids": []}]}))


def test_parse_rejects_non_uuid_evidence() -> None:
    with pytest.raises(LegalClaimsParseError, match="evidence"):
        parse_claims_text(_json({"claims": [{"text": "x", "evidence_ids": ["nope"]}]}))


def test_parse_rejects_duplicate_evidence() -> None:
    with pytest.raises(LegalClaimsParseError, match="evidence"):
        parse_claims_text(
            _json({"claims": [{"text": "x", "evidence_ids": [str(A), str(A)]}]})
        )


def test_parse_limits_claim_count() -> None:
    claims = [
        {"text": f"第{i}条结论。", "evidence_ids": [str(A)]} for i in range(3)
    ]
    with pytest.raises(LegalClaimsParseError, match="claims"):
        parse_claims_text(_json({"claims": claims}), max_claims=2)


def test_parse_limits_text_length() -> None:
    with pytest.raises(LegalClaimsParseError, match="text"):
        parse_claims_text(
            _json({"claims": [{"text": "长" * 2001, "evidence_ids": [str(A)]}]}),
            max_text_chars=2000,
        )


def test_parse_limits_evidence_count_per_claim() -> None:
    ids = [str(new_uuid7()) for _ in range(3)]
    with pytest.raises(LegalClaimsParseError, match="evidence"):
        parse_claims_text(
            _json({"claims": [{"text": "x", "evidence_ids": ids}]}),
            max_evidence_per_claim=2,
        )


async def test_parsed_claims_consume_claim_gate() -> None:
    item = EvidenceItem(
        evidence_id=A,
        instrument_title="中华人民共和国民法典",
        version_id=new_uuid7(),
        version_label="2020 公布版",
        status=LegalVersionStatus.CURRENT,
        published_on=None,
        effective_on=None,
        repealed_on=None,
        provision_no="第一条",
        provision_text="第一条 条文内容。",
        source_ref="object://corpus/sample.docx",
        dataset_version="dataset_v1",
        authorized=True,
    )
    bundle = EvidenceBundle(items=(item,))
    claims = parse_claims_text(_json(_payload()))
    result = await LegalClaimGate(bundle=bundle, gate=CitationGate()).verify(
        claims=claims, target_date=date(2023, 6, 1)
    )
    assert result.refused  # evidence B not in bundle
    assert "claim_not_supported:" in result.reason


def test_error_does_not_echo_full_input() -> None:
    secret_input = "用户机密原问内容" + "x" * 500
    with pytest.raises(LegalClaimsParseError) as exc_info:
        parse_claims_text(secret_input)
    assert "用户机密原问内容" not in str(exc_info.value)
