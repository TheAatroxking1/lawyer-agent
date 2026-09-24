from datetime import date
from uuid import uuid4

import pytest

from lawyer_agent.application.contract_review.contracts import (
    Anchor,
    BlocksResult,
    DocumentBlock,
    ReviewCandidate,
    ReviewError,
    ReviewRequest,
    ReviewScope,
    RiskIssueDraft,
    StructureResult,
)


class Authority:
    async def require_allowed(self, scope, request, issue):
        if (
            issue.category != "wording"
            or issue.evidence_ids
            or issue.problem != "时间约定不明确"
            or issue.suggestion != "明确付款期限"
        ):
            raise ReviewError("content_rejected")

    async def require(self, scope, action, resource_id):
        pass

    async def structure(self, scope, request):
        return StructureResult(
            document_version_id=scope.document_version_id,
            block_ids=("b1",),
            recognition_complete=True,
        )

    async def read_blocks(self, scope, request):
        return BlocksResult(
            document_version_id=scope.document_version_id,
            blocks=(
                DocumentBlock(
                    block_id="b1",
                    kind="text",
                    text="及时付款",
                    quality="verified",
                    anchors=(
                        Anchor(
                            anchor_id="a1",
                            page=1,
                            start=0,
                            end=4,
                            quad=(0, 0, 40, 0, 40, 10, 0, 10),
                        ),
                    ),
                ),
            ),
        )


def fixtures():
    scope = ReviewScope(
        tenant_id=uuid4(), actor_id=uuid4(), run_id=uuid4(), document_version_id="doc-1"
    )
    request = ReviewRequest(instruction="审查", as_of=date(2026, 9, 16))
    issue = RiskIssueDraft(
        block_id="b1",
        anchor_id="a1",
        start=0,
        end=4,
        quote="及时付款",
        category="wording",
        severity="medium",
        problem="时间约定不明确",
        suggestion="明确付款期限",
    )
    return scope, request, issue


@pytest.mark.asyncio
async def test_authoritative_gate_checks_quote_anchor_and_coverage():
    from lawyer_agent.application.contract_review.gate import AnchoredReviewGate

    authority = Authority()
    scope, request, issue = fixtures()
    gate = AnchoredReviewGate(documents=authority, authorizer=authority, content_policy=authority)
    candidate = ReviewCandidate(reviewed_block_ids=("b1",), issues=(issue,))
    result = await gate.validate(scope, request, candidate)
    assert result.issues == (issue,)
    for changed in [
        candidate.model_copy(update={"reviewed_block_ids": ("other",)}),
        candidate.model_copy(update={"issues": (issue.model_copy(update={"quote": "虚构原文"}),)}),
        candidate.model_copy(update={"issues": (issue.model_copy(update={"anchor_id": "other"}),)}),
        candidate.model_copy(
            update={
                "issues": (
                    issue.model_copy(
                        update={
                            "category": "legal",
                            "evidence_ids": ("law-1",),
                        }
                    ),
                )
            }
        ),
    ]:
        with pytest.raises(ReviewError):
            await gate.validate(scope, request, changed)


@pytest.mark.asyncio
async def test_authoritative_gate_validates_multiblock_highlights_against_full_quote():
    from lawyer_agent.application.contract_review.gate import AnchoredReviewGate

    class MultiLineAuthority(Authority):
        async def structure(self, scope, request):
            return StructureResult(
                document_version_id=scope.document_version_id,
                block_ids=("b1", "b2"),
                recognition_complete=True,
            )

        async def read_blocks(self, scope, request):
            blocks = tuple(
                DocumentBlock(
                    block_id=f"b{index}", kind="text", text=text, quality="verified",
                    anchors=(Anchor(
                        anchor_id=f"a{index}", page=1, start=0, end=len(text),
                        quad=(0, index * 10, 40, index * 10, 40, index * 10 + 8,
                              0, index * 10 + 8),
                    ),),
                )
                for index, text in enumerate(("及时", "付款"), 1)
            )
            return BlocksResult(document_version_id=scope.document_version_id, blocks=blocks)

    scope, request, issue = fixtures()
    issue = RiskIssueDraft.model_validate({
        **issue.model_dump(),
        "quote": "及时\n付款",
        "end": 2,
        "highlights": [
            {"block_id": "b1", "anchor_id": "a1", "start": 0, "end": 2},
            {"block_id": "b2", "anchor_id": "a2", "start": 0, "end": 2},
        ],
    })
    authority = MultiLineAuthority()
    gate = AnchoredReviewGate(documents=authority, authorizer=authority, content_policy=authority)
    candidate = ReviewCandidate(reviewed_block_ids=("b1", "b2"), issues=(issue,))
    assert (await gate.validate(scope, request, candidate)).issues == (issue,)

    broken = issue.model_copy(update={
        "highlights": issue.highlights[:-1],
    })
    with pytest.raises(ReviewError, match="quote_mismatch"):
        await gate.validate(
            scope, request,
            candidate.model_copy(update={"issues": (broken,)}),
        )


@pytest.mark.asyncio
async def test_authoritative_gate_accepts_only_ordered_confirmed_partial_highlights():
    from lawyer_agent.application.contract_review.gate import AnchoredReviewGate

    class PartialHighlightAuthority(Authority):
        async def read_blocks(self, scope, request):
            return BlocksResult(
                document_version_id=scope.document_version_id,
                blocks=(DocumentBlock(
                    block_id="b1", kind="text", text="责任限制", quality="verified",
                    anchors=(Anchor(
                        anchor_id="a1", page=1, start=0, end=4,
                        quad=(0, 0, 40, 0, 40, 10, 0, 10),
                    ),),
                ),),
            )

    scope, request, issue = fixtures()
    issue = RiskIssueDraft.model_validate({
        **issue.model_dump(),
        "block_id": "b1", "anchor_id": "a1", "start": 0, "end": 4,
        "quote": "责任限制……中间省略……不得单方免责",
        "highlights_complete": False,
        "highlights": [{"block_id": "b1", "anchor_id": "a1", "start": 0, "end": 4}],
    })
    authority = PartialHighlightAuthority()
    gate = AnchoredReviewGate(documents=authority, authorizer=authority, content_policy=authority)
    candidate = ReviewCandidate(reviewed_block_ids=("b1",), issues=(issue,))
    assert (await gate.validate(scope, request, candidate)).issues == (issue,)

    with pytest.raises(ReviewError, match="quote_mismatch"):
        await gate.validate(
            scope, request,
            candidate.model_copy(update={"issues": (
                issue.model_copy(update={"quote": "完全无关文字"}),
            )}),
        )


@pytest.mark.asyncio
async def test_model_cannot_bypass_content_gate_using_wording_category():
    from lawyer_agent.application.contract_review.gate import AnchoredReviewGate

    authority = Authority()
    scope, request, issue = fixtures()
    gate = AnchoredReviewGate(documents=authority, authorizer=authority, content_policy=authority)
    candidate = ReviewCandidate(
        reviewed_block_ids=("b1",),
        issues=(
            issue.model_copy(
                update={
                    "problem": "根据法律，本合同无效。",
                    "suggestion": "无需履行义务。",
                }
            ),
        ),
    )
    with pytest.raises(ReviewError):
        await gate.validate(scope, request, candidate)


@pytest.mark.asyncio
async def test_partial_review_allows_unlocated_transcription_but_not_fake_native_anchors():
    from lawyer_agent.application.contract_review.gate import AnchoredReviewGate

    class PartialAuthority(Authority):
        async def structure(self, scope, request):
            return (await super().structure(scope, request)).model_copy(
                update={"recognition_complete": False})

        async def read_blocks(self, scope, request):
            result = await super().read_blocks(scope, request)
            return result.model_copy(update={"blocks": (result.blocks[0].model_copy(
                update={"anchors": (), "quality": "needs_review"}),)})

    scope, request, issue = fixtures()
    authority = PartialAuthority()
    candidate = ReviewCandidate(reviewed_block_ids=("b1",),
        issues=(issue.model_copy(update={"anchor_id": None}),))
    gate = AnchoredReviewGate(documents=authority, authorizer=authority,
        content_policy=authority, allow_partial_document=True)
    assert (await gate.validate(scope, request, candidate)).issues[0].anchor_id is None
    with pytest.raises(ReviewError, match="anchor_mismatch"):
        await gate.validate(scope, request, candidate.model_copy(update={"issues": (issue,)}))
    strict = AnchoredReviewGate(documents=authority, authorizer=authority, content_policy=authority)
    with pytest.raises(ReviewError, match="document_incomplete"):
        await strict.validate(scope, request, candidate)
    native = Authority()
    with pytest.raises(ReviewError, match="anchor_mismatch"):
        await AnchoredReviewGate(documents=native, authorizer=native, content_policy=native,
            allow_partial_document=True).validate(scope, request, candidate)
