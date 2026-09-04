"""Document Version review state machine (non-model, human-gated).

A lawyer submits an uploaded version for review, then approves, rejects or
asks for changes. ``None``/``DRAFT`` means not yet submitted; ``APPROVED`` and
``REJECTED`` are terminal. Approving never by itself authorises external
publication or a legal conclusion.
"""

from __future__ import annotations

from enum import StrEnum

from lawyer_agent.domain.matter_documents import ReviewStatus


class ReviewDecision(StrEnum):
    SUBMIT = "submit"
    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_CHANGES = "request_changes"


class InvalidReviewTransition(ValueError):
    pass


class InvalidReviewDecision(ValueError):
    pass


def review_target_status(
    current: ReviewStatus | None,
    decision: ReviewDecision,
) -> ReviewStatus:
    """Return the target review status for a decision, or raise.

    ``current=None`` and ``DRAFT`` are both treated as "not yet submitted";
    both may be submitted for review. Terminal statuses cannot change.
    """
    if not isinstance(decision, ReviewDecision):
        raise InvalidReviewDecision("review decision must be strongly typed")

    if decision is ReviewDecision.SUBMIT:
        if current in (None, ReviewStatus.DRAFT, ReviewStatus.CHANGES_REQUESTED):
            return ReviewStatus.PENDING_REVIEW
        raise InvalidReviewTransition(
            f"cannot submit for review from {current}"
        )
    if decision is ReviewDecision.APPROVE:
        if current is ReviewStatus.PENDING_REVIEW:
            return ReviewStatus.APPROVED
        raise InvalidReviewTransition(f"cannot approve from {current}")
    if decision is ReviewDecision.REJECT:
        if current is ReviewStatus.PENDING_REVIEW:
            return ReviewStatus.REJECTED
        raise InvalidReviewTransition(f"cannot reject from {current}")
    if decision is ReviewDecision.REQUEST_CHANGES:
        if current is ReviewStatus.PENDING_REVIEW:
            return ReviewStatus.CHANGES_REQUESTED
        raise InvalidReviewTransition(
            f"cannot request changes from {current}"
        )
    raise InvalidReviewDecision("review decision is unknown")


def require_review_reason(decision: ReviewDecision, reason: str | None) -> None:
    """Reject/request-changes require a non-blank human reason.

    Approve and submit may carry a reason but do not require one.
    """
    if not isinstance(decision, ReviewDecision):
        raise InvalidReviewDecision("review decision must be strongly typed")
    if decision in (ReviewDecision.REJECT, ReviewDecision.REQUEST_CHANGES):
        if not isinstance(reason, str) or not reason.strip():
            raise InvalidReviewDecision(
                f"{decision.value} requires a non-blank reason"
            )
