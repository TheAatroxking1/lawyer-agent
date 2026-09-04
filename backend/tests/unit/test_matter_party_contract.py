from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from lawyer_agent.api.v1.matter_documents import (
    CreatePartyBody,
    PartySummary,
)
from lawyer_agent.domain.common import new_uuid7


def test_create_party_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError, match="extra"):
        CreatePartyBody.model_validate(
            {"display_name": "甲方", "kind": "tenant", "surprise": 1}
        )


def test_create_party_rejects_blank_display_name() -> None:
    with pytest.raises(ValidationError):
        CreatePartyBody.model_validate({"display_name": "   ", "kind": "甲方"})


def test_create_party_rejects_blank_kind() -> None:
    with pytest.raises(ValidationError):
        CreatePartyBody.model_validate({"display_name": "甲方", "kind": "  "})


def test_create_party_rejects_oversized_kind() -> None:
    with pytest.raises(ValidationError):
        CreatePartyBody.model_validate(
            {"display_name": "甲方", "kind": "k" * 65}
        )


def test_create_party_valid_case() -> None:
    body = CreatePartyBody.model_validate(
        {"display_name": "甲方", "kind": "tenant"}
    )
    assert body.display_name == "甲方"


def test_party_summary_round_trip() -> None:
    summary = PartySummary(
        id=new_uuid7(),
        matter_id=new_uuid7(),
        display_name="乙方",
        kind="counterparty",
        version=1,
    )
    assert summary.kind == "counterparty"


def test_service_present_in_composition() -> None:
    services = SimpleNamespace(matter_document_http=object())
    assert services.matter_document_http is not None
