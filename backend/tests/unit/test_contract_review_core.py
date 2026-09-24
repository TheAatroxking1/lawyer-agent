"""Core framework contracts; no customer material or external services."""

from importlib.util import find_spec

import pytest
from pydantic import ValidationError


def test_contract_review_framework_is_available() -> None:
    assert find_spec("lawyer_agent.application.contract_review") is not None, (
        "the approved contract review core has not been implemented"
    )


def test_review_contracts_reject_unbounded_or_forged_inputs() -> None:
    from lawyer_agent.application.contract_review.contracts import (
        ReadBlocksInput,
        ReviewCandidate,
    )

    with pytest.raises(ValidationError):
        ReadBlocksInput(document_version_id="doc", block_ids=[])
    with pytest.raises(ValidationError):
        ReadBlocksInput(document_version_id="doc", block_ids=["b"] * 21)
    with pytest.raises(ValidationError):
        ReadBlocksInput(document_version_id="doc", block_ids=["b"], tenant_id="other")
    with pytest.raises(ValidationError):
        ReviewCandidate(issues=[], reviewed_block_ids=[], reasoning="private")


def test_span_ranges_cannot_be_inverted() -> None:
    from lawyer_agent.application.contract_review.contracts import RiskIssueDraft

    with pytest.raises(ValidationError):
        RiskIssueDraft(
            block_id="b",
            anchor_id="a",
            start=8,
            end=3,
            quote="text",
            category="wording",
            severity="medium",
            problem="p",
            suggestion="s",
        )
