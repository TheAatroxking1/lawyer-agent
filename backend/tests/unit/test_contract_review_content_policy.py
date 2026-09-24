from datetime import date
from uuid import uuid4

import pytest

from lawyer_agent.application.contract_review.contracts import (
    ReviewCandidate,
    ReviewError,
    ReviewRequest,
    ReviewScope,
    RiskIssueDraft,
)
from lawyer_agent.application.contract_review.gate import ResearchDraftPolicy
from lawyer_agent.application.contract_review.research import (
    ContractBrief,
    LegalCandidate,
    LegalDocument,
    ResearchResult,
    serialize_research_documents,
)


class Judge:
    def __init__(self, response: str) -> None:
        self.response = response

    async def chat(self, messages):
        return self.response


def bundle():
    documents = (LegalDocument(document_id="law1", title="测试法规", version="v1",
                 content_hash="a"*64, text="测试法规文本", source_ref="synthetic"),)
    return ResearchResult(
        status="ready", brief=ContractBrief(is_contract=True, contract_type="合同",
        fact_summary="合成内容", queries=("测试",)),
        candidates=(LegalCandidate(document_id="law1", title="测试法规", snippets=("测试",)),),
        documents=documents,
        evidence_ids=("law1",), evidence_titles=("测试法规",),
        context=serialize_research_documents(documents),
    )


def issue(problem="付款时间未明确", category="commercial", evidence_ids=()):
    return RiskIssueDraft(block_id="b1", anchor_id="a1", start=0, end=4, quote="及时付款",
                          category=category, severity="medium", problem=problem,
                          suggestion="建议双方明确付款期限", evidence_ids=evidence_ids)


def scope():
    return ReviewScope(tenant_id=uuid4(), actor_id=uuid4(), run_id=uuid4(),
                       document_version_id="test")


@pytest.mark.asyncio
async def test_commercial_draft_requires_separate_judge_and_same_scope():
    current = scope()
    policy = ResearchDraftPolicy(model=Judge(
        '{"verdicts":[{"index":0,"legal_claim":false,"supported":true}]}'),
        scope=current)
    request = ReviewRequest(instruction="审阅", as_of=date(2026, 9, 16))
    candidate = ReviewCandidate(reviewed_block_ids=("b1",), issues=(issue(),))
    await policy.prepare(request, candidate, bundle())
    await policy.require_allowed(current, request, candidate.issues[0])
    with pytest.raises(ReviewError, match="resource_unavailable"):
        await policy.require_allowed(scope(), request, candidate.issues[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("problem,legal", [("此条款违法无效", "false"),
                                           ("可单方解除且无需赔偿", "true")])
async def test_unknown_validity_cannot_be_hidden_under_commercial_category(problem, legal):
    policy = ResearchDraftPolicy(model=Judge(
        '{"verdicts":[{"index":0,"legal_claim":'+legal+',"supported":true}]}'),
        scope=scope())
    with pytest.raises(ReviewError, match="legal_metadata_unverified"):
        await policy.prepare(
            ReviewRequest(instruction="审阅", as_of=date(2026, 9, 16)),
            ReviewCandidate(reviewed_block_ids=("b1",), issues=(issue(problem),)), bundle(),
        )


@pytest.mark.asyncio
async def test_foreign_evidence_and_incomplete_verdicts_are_rejected():
    policy = ResearchDraftPolicy(model=Judge('{"verdicts":[]}'), scope=scope())
    request = ReviewRequest(instruction="审阅", as_of=date(2026, 9, 16))
    with pytest.raises(ReviewError, match="evidence_not_allowed"):
        await policy.prepare(request, ReviewCandidate(reviewed_block_ids=("b1",),
            issues=(issue(evidence_ids=("other",)),)), bundle())
    with pytest.raises(ReviewError, match="content_verification_failed"):
        await policy.prepare(request, ReviewCandidate(reviewed_block_ids=("b1",),
            issues=(issue(),)), bundle())
