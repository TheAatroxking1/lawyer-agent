from __future__ import annotations

import pytest

from lawyer_agent.domain.document_review import (
    InvalidReviewDecision,
    InvalidReviewTransition,
    ReviewDecision,
    require_review_reason,
    review_target_status,
)
from lawyer_agent.domain.matter_documents import ReviewStatus


@pytest.mark.parametrize(
    ("current", "decision", "expected"),
    [
        (None, ReviewDecision.SUBMIT, ReviewStatus.PENDING_REVIEW),
        (ReviewStatus.DRAFT, ReviewDecision.SUBMIT, ReviewStatus.PENDING_REVIEW),
        (
            ReviewStatus.CHANGES_REQUESTED,
            ReviewDecision.SUBMIT,
            ReviewStatus.PENDING_REVIEW,
        ),
        (
            ReviewStatus.PENDING_REVIEW,
            ReviewDecision.APPROVE,
            ReviewStatus.APPROVED,
        ),
        (
            ReviewStatus.PENDING_REVIEW,
            ReviewDecision.REJECT,
            ReviewStatus.REJECTED,
        ),
        (
            ReviewStatus.PENDING_REVIEW,
            ReviewDecision.REQUEST_CHANGES,
            ReviewStatus.CHANGES_REQUESTED,
        ),
    ],
)
def test_legal_review_transitions(
    current: ReviewStatus | None,
    decision: ReviewDecision,
    expected: ReviewStatus,
) -> None:
    assert review_target_status(current, decision) is expected


@pytest.mark.parametrize(
    ("current", "decision"),
    [
        (None, ReviewDecision.APPROVE),
        (None, ReviewDecision.REJECT),
        (None, ReviewDecision.REQUEST_CHANGES),
        (ReviewStatus.DRAFT, ReviewDecision.APPROVE),
        (ReviewStatus.APPROVED, ReviewDecision.SUBMIT),
        (ReviewStatus.APPROVED, ReviewDecision.REQUEST_CHANGES),
        (ReviewStatus.REJECTED, ReviewDecision.SUBMIT),
        (ReviewStatus.REJECTED, ReviewDecision.APPROVE),
        (ReviewStatus.PENDING_REVIEW, ReviewDecision.SUBMIT),
    ],
)
def test_illegal_review_transitions_raise(
    current: ReviewStatus | None,
    decision: ReviewDecision,
) -> None:
    with pytest.raises(InvalidReviewTransition):
        review_target_status(current, decision)


def test_require_review_reason_allows_non_empty() -> None:
    require_review_reason(ReviewDecision.REJECT, "原件疑似被篡改")
    require_review_reason(ReviewDecision.REQUEST_CHANGES, "需要补充授权签署页")


@pytest.mark.parametrize(
    "decision",
    [ReviewDecision.REJECT, ReviewDecision.REQUEST_CHANGES],
)
def test_require_review_reason_rejects_blank(decision: ReviewDecision) -> None:
    with pytest.raises(InvalidReviewDecision, match="reason"):
        require_review_reason(decision, "   ")


@pytest.mark.parametrize(
    "decision",
    [ReviewDecision.SUBMIT, ReviewDecision.APPROVE],
)
def test_approve_or_submit_may_omit_reason(decision: ReviewDecision) -> None:
    require_review_reason(decision, "")  # must not raise
