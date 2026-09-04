from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from lawyer_agent.api.v1.reviews import (
    DocumentReviewBody,
    DocumentVersionResponse,
)
from lawyer_agent.domain.document_review import (
    InvalidReviewDecision,
    ReviewDecision,
    require_review_reason,
)


def test_version_read_response_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError, match="extra"):
        DocumentVersionResponse.model_validate(
            {
                "document_id": "018f6f60-0000-7000-8000-000000000001",
                "version_no": 1,
                "kind": "original",
                "upload_status": "accepted",
                "surprise": True,
            }
        )


def test_version_read_response_white_list_projection() -> None:
    body = DocumentVersionResponse.model_validate(
        {
            "document_id": "018f6f60-0000-7000-8000-000000000001",
            "version_no": 1,
            "kind": "original",
            "file_name": "租赁合同.docx",
            "upload_status": "accepted",
            "review_status": "pending_review",
            "review_reason": None,
        }
    )
    assert body.file_name == "租赁合同.docx"
    assert body.review_status == "pending_review"


def test_review_body_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError, match="extra"):
        DocumentReviewBody.model_validate(
            {"decision": "approve", "surprise": True}
        )


def test_review_body_rejects_unknown_decision() -> None:
    with pytest.raises(ValidationError):
        DocumentReviewBody.model_validate({"decision": "escalate"})


@pytest.mark.parametrize(
    "decision",
    ["submit", "approve", "reject", "request_changes"],
)
def test_review_body_accepts_all_decisions(decision: str) -> None:
    reason = "人工核对" if decision in {"reject", "request_changes"} else None
    body = DocumentReviewBody.model_validate(
        {"decision": decision, "reason": reason}
    )
    assert body.decision == decision


def test_reject_blank_reason_raises_domain_error() -> None:
    with pytest.raises(InvalidReviewDecision, match="reason"):
        require_review_reason(ReviewDecision.REJECT, "  ")


def test_service_present_in_composition() -> None:
    placeholder = object()
    services = SimpleNamespace(review_http=placeholder)
    assert services.review_http is placeholder
