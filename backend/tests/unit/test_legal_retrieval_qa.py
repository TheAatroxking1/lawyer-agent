from __future__ import annotations

import json
from datetime import date

import pytest

from lawyer_agent.application.evidence import EvidenceBundle, EvidenceItem
from lawyer_agent.application.legal_retrieval_qa import (
    REFUSAL_NO_EVIDENCE,
    LegalRetrievalQaService,
    build_claims_system_prompt,
)
from lawyer_agent.application.model_gateway import ModelProviderUnavailable
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import LegalVersionStatus
from lawyer_agent.domain.model_gateway import TokenUsage


def _item(
    *,
    provision_text: str = "承租人应当按照约定的期限支付租金。",
    status: LegalVersionStatus = LegalVersionStatus.CURRENT,
    effective_on: date | None = date(2021, 1, 1),
    repealed_on: date | None = None,
    authorized: bool = True,
    provision_no: str = "第七十七条",
) -> tuple[EvidenceItem, ...]:
    version_id = new_uuid7()
    return (
        EvidenceItem(
            evidence_id=new_uuid7(),
            instrument_title="《民法典》",
            version_id=version_id,
            version_label="2020",
            status=status,
            published_on=date(2020, 5, 28),
            effective_on=effective_on,
            repealed_on=repealed_on,
            provision_no=provision_no,
            provision_text=provision_text,
            source_ref="corpus://civil-code-2020.docx",
            dataset_version="dataset_v1",
            authorized=authorized,
        ),
    )


def _bundle(*items: EvidenceItem) -> EvidenceBundle:
    return EvidenceBundle(tuple(items))


def _claims_json(*claims: dict[str, object]) -> str:
    return json.dumps({"claims": list(claims)}, ensure_ascii=False)


class _FakeDatasetEvidence:
    def __init__(self, bundle: EvidenceBundle | None) -> None:
        self._bundle = bundle
        self.calls: list[dict[str, object]] = []

    async def search_evidence(self, **kwargs: object) -> EvidenceBundle | None:
        self.calls.append(kwargs)
        return self._bundle


class _FakeChat:
    def __init__(self, text: str = "") -> None:
        self._text = text
        self.calls: list[object] = []

    async def chat(self, **kwargs: object) -> tuple[str, TokenUsage]:
        self.calls.append(kwargs)
        if isinstance(self._text, Exception):
            raise self._text
        return self._text, TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)


def _service(
    *,
    bundle: EvidenceBundle | None,
    chat_text: str = "",
    chat_error: Exception | None = None,
) -> tuple[LegalRetrievalQaService, _FakeDatasetEvidence, _FakeChat]:
    evidence = _FakeDatasetEvidence(bundle)
    chat = _FakeChat(chat_text if chat_error is None else chat_error)
    service = LegalRetrievalQaService(evidence, chat)
    return service, evidence, chat


@pytest.mark.asyncio
async def test_allowed_answer_returns_numbered_claims_and_citations() -> None:
    (item,) = _item(provision_text="承租人应当按照约定的期限支付租金。")
    claim_text = "承租人逾期支付租金构成违约，出租人可以请求其承担违约责任。"
    service, evidence, chat = _service(
        bundle=_bundle(item),
        chat_text=_claims_json(
            {"text": claim_text, "evidence_ids": [str(item.evidence_id)]}
        ),
    )
    answer = await service.answer(
        alias="dataset_v1",
        question="承租人逾期支付租金怎么办？",
        model_ref="deepseek-chat",
        dimension=512,
        target_date=date(2024, 1, 1),
    )
    assert answer.refused is False
    assert answer.reason == "allowed"
    assert answer.text == f"1. {claim_text}"
    assert len(answer.claims) == 1
    assert answer.claims[0].evidence_ids == (item.evidence_id,)
    assert answer.citations == (item,)
    assert answer.usage is not None
    assert answer.usage.total_tokens == 15
    assert len(chat.calls) == 1
    messages = chat.calls[0]["messages"]
    assert [message.role for message in messages] == ["system", "user"]
    assert messages[1].content == "承租人逾期支付租金怎么办？"
    assert evidence.calls[0]["query"] == "承租人逾期支付租金怎么办？"


@pytest.mark.asyncio
async def test_allowed_parses_claims_json() -> None:
    (item,) = _item()
    claim_text = "承租人应当按照约定的期限支付租金。"
    service, _, _ = _service(
        bundle=_bundle(item),
        chat_text=_claims_json({"text": claim_text, "evidence_ids": [str(item.evidence_id)]}),
    )
    answer = await service.answer(
        alias="dataset_v1",
        question="租金支付期限？",
        model_ref="deepseek-chat",
        dimension=512,
        target_date=date(2024, 1, 1),
    )
    assert answer.refused is False
    assert answer.reason == "allowed"
    assert answer.claims[0].text == claim_text
    assert answer.citations == (item,)


@pytest.mark.asyncio
async def test_no_evidence_refuses_without_calling_chat() -> None:
    service, _, chat = _service(bundle=None)
    answer = await service.answer(
        alias="dataset_v1",
        question="如何计算违约金的起算日？",
        model_ref="deepseek-chat",
        dimension=512,
        target_date=date(2024, 1, 1),
    )
    assert answer.refused is True
    assert answer.reason == REFUSAL_NO_EVIDENCE
    assert answer.usage is None
    assert chat.calls == []


@pytest.mark.asyncio
async def test_unparsable_claims_fail_safe() -> None:
    (item,) = _item()
    service, _, chat = _service(
        bundle=_bundle(item),
        chat_text="抱歉我无法找到依据。",
    )
    answer = await service.answer(
        alias="dataset_v1",
        question="承租人逾期支付租金怎么办？",
        model_ref="deepseek-chat",
        dimension=512,
        target_date=date(2024, 1, 1),
    )
    assert answer.refused is True
    assert answer.reason == "claim_parse_failed"
    assert len(chat.calls) == 1
    assert answer.usage is not None


@pytest.mark.asyncio
async def test_out_of_bundle_citation_refused() -> None:
    (item,) = _item()
    foreign = new_uuid7()
    service, _, _ = _service(
        bundle=_bundle(item),
        chat_text=_claims_json(
            {"text": "结论A", "evidence_ids": [str(item.evidence_id)]},
            {"text": "结论B", "evidence_ids": [str(foreign)]},
        ),
    )
    answer = await service.answer(
        alias="dataset_v1",
        question="租金逾期？",
        model_ref="deepseek-chat",
        dimension=512,
        target_date=date(2024, 1, 1),
    )
    assert answer.refused is True
    assert answer.reason == "claim_not_supported:evidence_not_in_bundle"
    assert answer.claims == ()


@pytest.mark.asyncio
async def test_unauthorized_evidence_refused() -> None:
    (item,) = _item(authorized=False)
    service, _, _ = _service(
        bundle=_bundle(item),
        chat_text=_claims_json({"text": "结论", "evidence_ids": [str(item.evidence_id)]}),
    )
    answer = await service.answer(
        alias="dataset_v1",
        question="租金逾期？",
        model_ref="deepseek-chat",
        dimension=512,
        target_date=date(2024, 1, 1),
    )
    assert answer.refused is True
    assert answer.reason == "claim_not_supported:evidence_not_authorized"


@pytest.mark.asyncio
async def test_not_effective_on_target_date_refused() -> None:
    (item,) = _item(effective_on=date(2024, 6, 1))
    service, _, _ = _service(
        bundle=_bundle(item),
        chat_text=_claims_json({"text": "结论", "evidence_ids": [str(item.evidence_id)]}),
    )
    answer = await service.answer(
        alias="dataset_v1",
        question="租金逾期？",
        model_ref="deepseek-chat",
        dimension=512,
        target_date=date(2024, 1, 1),
    )
    assert answer.refused is True
    assert answer.reason == "claim_not_supported:not_effective_on_target_date"


@pytest.mark.asyncio
async def test_repealed_on_target_date_refused() -> None:
    (item,) = _item(
        status=LegalVersionStatus.REPEALED,
        repealed_on=date(2023, 1, 1),
    )
    service, _, _ = _service(
        bundle=_bundle(item),
        chat_text=_claims_json({"text": "结论", "evidence_ids": [str(item.evidence_id)]}),
    )
    answer = await service.answer(
        alias="dataset_v1",
        question="租金逾期？",
        model_ref="deepseek-chat",
        dimension=512,
        target_date=date(2024, 1, 1),
    )
    assert answer.refused is True
    assert answer.reason == "claim_not_supported:repealed_on_target_date"


@pytest.mark.asyncio
async def test_status_unknown_refused() -> None:
    (item,) = _item(
        status=LegalVersionStatus.STATUS_UNKNOWN,
        effective_on=None,
    )
    service, _, _ = _service(
        bundle=_bundle(item),
        chat_text=_claims_json({"text": "结论", "evidence_ids": [str(item.evidence_id)]}),
    )
    answer = await service.answer(
        alias="dataset_v1",
        question="租金逾期？",
        model_ref="deepseek-chat",
        dimension=512,
        target_date=date(2024, 1, 1),
    )
    assert answer.refused is True
    assert answer.reason == "claim_not_supported:effective_status_unknown"


@pytest.mark.asyncio
async def test_gateway_error_propagates() -> None:
    (item,) = _item()
    service, _, _ = _service(
        bundle=_bundle(item),
        chat_error=ModelProviderUnavailable("provider down"),
    )
    with pytest.raises(ModelProviderUnavailable):
        await service.answer(
            alias="dataset_v1",
            question="租金逾期？",
            model_ref="deepseek-chat",
            dimension=512,
            target_date=date(2024, 1, 1),
        )


@pytest.mark.asyncio
async def test_invalid_inputs_rejected() -> None:
    (item,) = _item()
    service, _, _ = _service(bundle=_bundle(item))
    good = {
        "alias": "dataset_v1",
        "question": "租金逾期怎么办？",
        "model_ref": "deepseek-chat",
        "dimension": 512,
        "target_date": date(2024, 1, 1),
    }
    bad_cases: list[tuple[str, object]] = [
        ("alias", "  "),
        ("question", " "),
        ("dimension", 0),
        ("dimension", -3),
        ("target_date", "2024-01-01"),
        ("limit", 0),
        ("model_ref", ""),
    ]
    for field, value in bad_cases:
        kwargs = {**good, field: value}
        with pytest.raises(ValueError):
            await service.answer(**kwargs)  # type: ignore[arg-type]


def test_prompt_numbers_evidence_in_order() -> None:
    first, second = _item(), _item(provision_text="支付租金应当一次付清。")
    prompt = build_claims_system_prompt((first[0], second[0]))
    assert f"[1]（依据编号 {first[0].evidence_id}）" in prompt
    assert f"[2]（依据编号 {second[0].evidence_id}）" in prompt
    assert prompt.index("[1]") < prompt.index("[2]")


def test_prompt_caps_overlong_item_with_marker() -> None:
    (item,) = _item(provision_text="长" * 5_000)
    prompt = build_claims_system_prompt((item,), per_item_cap_chars=100)
    assert item.provision_text[:100] in prompt
    assert "（条文过长已截断" in prompt
    assert len(prompt) < 1_200


def test_prompt_respects_total_budget() -> None:
    first = _item(provision_text="甲" * 600)
    second = _item(provision_text="乙" * 600)
    prompt = build_claims_system_prompt(
        (first[0], second[0]),
        budget_chars=len(
            f"[1]（依据编号 {first[0].evidence_id}）《民法典》2020 第七十七条：{'甲' * 600}"
        )
        + 50,
    )
    assert "[2]" not in prompt


def test_prompt_rejects_bad_budget_args() -> None:
    (item,) = _item()
    with pytest.raises(ValueError):
        build_claims_system_prompt((item,), budget_chars=0)
    with pytest.raises(ValueError):
        build_claims_system_prompt((item,), per_item_cap_chars=-1)
