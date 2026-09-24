import json

import pytest

from lawyer_agent.application.contract_review.contracts import ReviewError, ReviewResult, RunLimits
from lawyer_agent.application.contract_review.research import BoundedContractResearch
from tests.unit.test_contract_review_research import (
    BRIEF,
    REQUEST,
    SCOPE,
    Model,
    Tools,
    candidate,
    page,
)


def findings():
    return {"reviewed_pages": [1], "issues": [
        {"page": 1, "quote": word, "category": "commercial", "severity": "medium",
         "problem": "需明确" + word, "suggestion": "约定双方确认的具体条件。", "citations": []}
        for word in ("租金", "押金", "维修")
    ]}


def test_omitted_excerpt_marker_is_not_a_citable_legal_passage():
    from lawyer_agent.application.contract_review.research import LegalDocument, research_passages

    raw = page("law-1", "规则", text="第一条原文\n\n[…未选入段落…]\n\n第二条原文")
    document = LegalDocument.model_validate({
        key: value for key, value in raw.model_dump().items() if key in LegalDocument.model_fields
    })
    assert research_passages((document,)) == {
        "law1.p1": ("law-1", "第一条原文"), "law1.p3": ("law-1", "第二条原文"),
    }


class Pdf:
    page_dimensions = ((200, 200),)

    async def register_visual_quote(self, scope, number, quote):
        assert scope == SCOPE and number == 1
        return "unlocated"


class DocumentTools:
    scope, limits, tools = SCOPE, RunLimits(), []

    def reset(self):
        pass

    async def prepare(self):
        return json.dumps({"document_version_id": SCOPE.document_version_id, "blocks": [
            {"block_id": "b1", "kind": "text", "text": "租金押金维修",
             "quality": "verified", "anchors": [
                 {"anchor_id": "a1", "page": 1, "start": 0, "end": 6,
                  "quad": [0, 0, 100, 0, 100, 20, 0, 20]}]}
        ]}, ensure_ascii=False)


class Gate:
    async def validate(self, scope, request, result):
        assert result.reviewed_block_ids == ("b1",)
        assert all(i.quote in {"租金", "押金", "维修"} for i in result.issues)
        assert all(i.anchor_id == "a1" and len(i.highlights) == 1 for i in result.issues)
        return ReviewResult(run_id=scope.run_id, document_version_id=scope.document_version_id,
                            issues=result.issues)


class Store:
    saved = None

    async def save(self, scope, result):
        self.saved = result


async def run(response):
    from lawyer_agent.infrastructure.contract_review.two_pass import TwoPassContractReviewWorkflow

    model, legal, store = Model([BRIEF, response]), Tools(), Store()
    legal.search_results["合同类型：采购合同\n内容总结：采购方购买设备并分期付款。"] = (
        candidate("law-1", "规则"),
    )
    legal.pages[("law-1", None)] = page("law-1", "规则", text="有效的来源片段")
    workflow = TwoPassContractReviewWorkflow(
        scope=SCOPE, model=model, tools=DocumentTools(), gate=Gate(), store=store, pdf=Pdf(),
        research=BoundedContractResearch(model=model, tools=legal, summary_driven=True),
        limits=RunLimits(),
    )
    return await workflow.review(REQUEST), model, store


@pytest.mark.asyncio
async def test_two_pass_reviews_more_than_two_issues_without_anchor_payload_or_extra_calls():
    result, model, store = await run(json.dumps(findings(), ensure_ascii=False))
    assert len(model.messages) == 2
    assert len(result.issues) == 3 and store.saved == result
    second = model.messages[1][-1].content
    assert "租金押金维修" in second and "有效的来源片段" in second
    assert "quad" not in second and "previous_issues" not in second


@pytest.mark.asyncio
async def test_two_pass_preserves_full_quote_and_all_line_highlights():
    from lawyer_agent.infrastructure.contract_review.two_pass import TwoPassContractReviewWorkflow

    class MultiLineTools(DocumentTools):
        async def prepare(self):
            return json.dumps({
                "document_version_id": SCOPE.document_version_id,
                "blocks": [
                    {"block_id": f"b{index}", "kind": "text", "text": text,
                     "quality": "verified", "anchors": [{
                         "anchor_id": f"a{index}", "page": 1,
                         "start": 0, "end": len(text),
                         "quad": [0, index * 20, 100, index * 20,
                                  100, index * 20 + 10, 0, index * 20 + 10],
                     }]}
                    for index, text in enumerate(("甲方可", "单方修改", "服务规则"), 1)
                ],
            }, ensure_ascii=False)

    class MultiLineGate:
        async def validate(self, scope, request, draft):
            issue = draft.issues[0]
            assert issue.quote == "甲方可\n单方修改\n服务规则"
            assert [item.model_dump() for item in issue.highlights] == [
                {"block_id": "b1", "anchor_id": "a1", "start": 0, "end": 3},
                {"block_id": "b2", "anchor_id": "a2", "start": 0, "end": 4},
                {"block_id": "b3", "anchor_id": "a3", "start": 0, "end": 4},
            ]
            return ReviewResult(
                run_id=scope.run_id,
                document_version_id=scope.document_version_id,
                issues=draft.issues,
            )

    raw = json.dumps({"reviewed_pages": [1], "issues": [{
        "page": 1, "quote": "甲方可\n单方修改\n服务规则",
        "category": "commercial", "severity": "medium",
        "problem": "甲方可单方修改规则。", "suggestion": "重大变更需双方确认。",
        "citations": [],
    }]}, ensure_ascii=False)
    model, legal = Model([BRIEF, raw]), Tools()
    legal.search_results["合同类型：采购合同\n内容总结：采购方购买设备并分期付款。"] = (
        candidate("law-1", "规则"),
    )
    legal.pages[("law-1", None)] = page("law-1", "规则", text="有效的来源片段")
    result = await TwoPassContractReviewWorkflow(
        scope=SCOPE, model=model, tools=MultiLineTools(), gate=MultiLineGate(),
        store=Store(), pdf=Pdf(),
        research=BoundedContractResearch(model=model, tools=legal, summary_driven=True),
        limits=RunLimits(),
    ).review(REQUEST)
    assert len(result.issues) == 1


def test_quote_locator_recovers_unique_native_text_when_model_page_is_off_by_one():
    from lawyer_agent.infrastructure.contract_review.two_pass import _locate_quote

    blocks = [{
        "block_id": "p002-line0001", "kind": "text", "text": "唯一风险条款",
        "quality": "verified", "anchors": [{
            "anchor_id": "a2", "page": 2, "start": 0, "end": 6,
            "quad": [0, 0, 100, 0, 100, 10, 0, 10],
        }],
    }]
    assert [item.anchor_id for item in _locate_quote(blocks, 1, "唯一风险条款")] == ["a2"]

    duplicate = blocks + [{
        **blocks[0], "block_id": "p003-line0001",
        "anchors": [{**blocks[0]["anchors"][0], "anchor_id": "a3", "page": 3}],
    }]
    assert _locate_quote(duplicate, 1, "唯一风险条款") == ()


def test_quote_locator_marks_unique_confirmed_fragments_around_ellipsis():
    from lawyer_agent.infrastructure.contract_review.two_pass import _locate_quote

    blocks = [
        {"block_id": f"p001-line000{index}", "kind": "text", "text": text,
         "quality": "verified", "anchors": [{
             "anchor_id": f"a{index}", "page": 1, "start": 0, "end": len(text),
             "quad": [0, index * 10, 100, index * 10,
                      100, index * 10 + 8, 0, index * 10 + 8],
         }]}
        for index, text in enumerate((
            "9.1责任限制：", "（1）其他内容", "（4）政府征收时甲方可解除协议",
        ), 1)
    ]
    result = _locate_quote(
        blocks, 1, "9.1责任限制：...（4）政府征收时甲方可解除协议",
    )
    assert [item.anchor_id for item in result] == ["a1", "a3"]


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["extra_page", "duplicate_keys"])
async def test_two_pass_rejects_invalid_coverage_and_citations_without_retry(defect):
    output = findings()
    if defect == "extra_page":
        output["reviewed_pages"] = [1, 2]
    raw = json.dumps(output, ensure_ascii=False)
    if defect == "duplicate_keys":
        raw = '{"issues": [],' + raw[1:]
    with pytest.raises(ReviewError):
        await run(raw)


@pytest.mark.asyncio
@pytest.mark.parametrize("citations", [[], ["law99.p1"]])
async def test_invalid_legal_citation_is_withheld_explicitly_without_losing_other_drafts(citations):
    output = findings()
    output["issues"][0].update(category="legal", citations=citations)
    result, model, store = await run(json.dumps(output, ensure_ascii=False))
    assert len(result.issues) == 2
    assert len(result.withheld_issues) == 1
    assert result.withheld_issues[0].index == 1
    assert result.withheld_issues[0].reason == "unverified_citation"
    assert len(model.messages) == 2 and store.saved == result


@pytest.mark.asyncio
async def test_two_pass_binds_passage_reference_to_original_text_without_model_requotation():
    output = findings()
    output["issues"][0]["category"] = "legal"
    output["issues"][0]["citations"] = ["law1.p1"]
    result, model, _ = await run(json.dumps(output, ensure_ascii=False))
    issue = result.issues[0]
    assert issue.evidence_ids == ("law-1",)
    assert issue.evidence_passages[0].quote == "有效的来源片段"
    assert issue.evidence_passages[0].document_id == "law-1"
    assert len(model.messages) == 2


@pytest.mark.asyncio
async def test_source_only_draft_policy_is_run_bound_and_does_not_claim_semantic_review():
    from uuid import uuid4

    from lawyer_agent.application.contract_review.contracts import ReviewCandidate, RiskIssueDraft
    from lawyer_agent.application.contract_review.gate import SourceReferenceDraftPolicy
    from tests.unit.test_contract_review_research import research_result

    research = research_result()
    model = Model([])
    policy = SourceReferenceDraftPolicy(model=model, scope=SCOPE)
    issue = RiskIssueDraft(block_id="b1", anchor_id="a1", start=0, end=2, quote="租金",
                           category="legal", severity="medium", problem="待核对约定的适用条件",
                           suggestion="请律师核对相关法律的现行效力。",
                           evidence_ids=(research.evidence_ids[0],))
    draft = ReviewCandidate(reviewed_block_ids=("b1",), issues=(issue,))
    await policy.prepare(REQUEST, draft, research)
    await policy.require_allowed(SCOPE, REQUEST, issue)
    assert not model.messages
    with pytest.raises(ReviewError, match="resource_unavailable"):
        await policy.require_allowed(
            SCOPE.model_copy(update={"tenant_id": uuid4()}), REQUEST, issue,
        )
    with pytest.raises(ReviewError, match="evidence_not_allowed"):
        await policy.prepare(REQUEST, ReviewCandidate(reviewed_block_ids=("b1",), issues=(
            issue.model_copy(update={"evidence_ids": ("unselected",)}),
        )), research)


@pytest.mark.asyncio
async def test_two_pass_keeps_unlocatable_image_quote_without_fabricating_highlight():
    output = findings()
    output["issues"] = [output["issues"][0] | {"quote": "仅图片上有此条款"}]

    from lawyer_agent.infrastructure.contract_review.two_pass import TwoPassContractReviewWorkflow

    class UnlocatedGate:
        async def validate(self, scope, request, draft):
            assert draft.reviewed_block_ids == ("b1", "unlocated")
            assert draft.issues[0].anchor_id is None
            assert draft.issues[0].quote == "仅图片上有此条款"
            return ReviewResult(run_id=scope.run_id, document_version_id=scope.document_version_id,
                                issues=draft.issues)

    from tests.unit.test_contract_review_research import Research, research_result

    workflow = TwoPassContractReviewWorkflow(
        scope=SCOPE, model=Model([json.dumps(output)]), tools=DocumentTools(),
        gate=UnlocatedGate(), store=Store(), pdf=Pdf(), research=Research(research_result()),
        limits=RunLimits(),
    )
    assert len((await workflow.review(REQUEST)).issues) == 1
