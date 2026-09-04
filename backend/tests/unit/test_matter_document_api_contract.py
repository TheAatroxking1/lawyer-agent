from __future__ import annotations

import pytest
from pydantic import ValidationError

from lawyer_agent.api.v1.matter_documents import (
    AssignMatterOwnerBody,
    CreateMatterBody,
    PartyConflictCheckBody,
    RegisterDocumentBody,
    UpdatePartyBody,
)
from lawyer_agent.application.matter_document_api import (
    MatterDocumentNotFound,
)


def test_create_matter_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError, match="extra"):
        CreateMatterBody.model_validate(
            {"title": "租赁审查", "kind": "contract_review", "surprise": 1}
        )


def test_create_matter_rejects_blank_title() -> None:
    with pytest.raises(ValidationError):
        CreateMatterBody.model_validate({"title": "   ", "kind": "contract_review"})


@pytest.mark.parametrize("kind", ["litigation", "legal_advice", "compliance", "other"])
def test_create_matter_accepts_known_kinds(kind: str) -> None:
    body = CreateMatterBody.model_validate({"title": "事项", "kind": kind})
    assert body.kind == kind


def test_create_matter_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        CreateMatterBody.model_validate({"title": "事项", "kind": "eviction"})


def test_register_document_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError, match="extra"):
        RegisterDocumentBody.model_validate(
            {
                "file_name": "lease.docx",
                "mime_type": "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document",
                "payload_b64": "eA==",
                "surprise": True,
            }
        )


def test_register_document_rejects_bad_base64() -> None:
    with pytest.raises(ValidationError):
        RegisterDocumentBody.model_validate(
            {
                "file_name": "lease.docx",
                "mime_type": "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document",
                "payload_b64": "not base64 !!",
            }
        )


def test_register_document_rejects_empty_payload() -> None:
    with pytest.raises(ValidationError):
        RegisterDocumentBody.model_validate(
            {
                "file_name": "lease.docx",
                "mime_type": "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document",
                "payload_b64": "",
            }
        )


def test_register_document_valid_case() -> None:
    body = RegisterDocumentBody.model_validate(
        {
            "file_name": "lease.docx",
            "mime_type": "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document",
            "payload_b64": "eA==",
        }
    )
    assert body.file_name == "lease.docx"
    assert body.payload_b64 == "eA=="


def test_matter_document_not_found_carries_stable_code() -> None:
    error = MatterDocumentNotFound()
    assert error.code == "matter_document_not_found"
    assert error.status == 404


def test_update_party_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError, match="extra"):
        UpdatePartyBody.model_validate({"display_name": "甲公司", "surprise": True})


def test_update_party_rejects_empty_body() -> None:
    with pytest.raises(ValidationError):
        UpdatePartyBody.model_validate({})


def test_update_party_rejects_blank_display_name() -> None:
    with pytest.raises(ValidationError):
        UpdatePartyBody.model_validate({"display_name": "   "})


def test_update_party_rejects_blank_kind() -> None:
    with pytest.raises(ValidationError):
        UpdatePartyBody.model_validate({"kind": "  "})


def test_update_party_rejects_oversized_display_name() -> None:
    with pytest.raises(ValidationError):
        UpdatePartyBody.model_validate({"display_name": "甲" * 300})


def test_update_party_accepts_display_name_only() -> None:
    body = UpdatePartyBody.model_validate({"display_name": "新名称"})
    assert body.display_name == "新名称"
    assert body.kind is None


def test_update_party_accepts_kind_only() -> None:
    body = UpdatePartyBody.model_validate({"kind": "lawyer"})
    assert body.kind == "lawyer"
    assert body.display_name is None


def test_conflict_check_body_accepts_valid_input() -> None:
    body = PartyConflictCheckBody.model_validate(
        {"display_name": "甲公司", "kind": "tenant"}
    )
    assert body.display_name == "甲公司"
    assert body.kind == "tenant"
    assert body.exclude_matter_id is None


def test_conflict_check_body_rejects_blank_display_name() -> None:
    with pytest.raises(ValidationError):
        PartyConflictCheckBody.model_validate({"display_name": "   "})


def test_conflict_check_body_rejects_blank_kind() -> None:
    with pytest.raises(ValidationError):
        PartyConflictCheckBody.model_validate({"display_name": "甲", "kind": "  "})


def test_conflict_check_body_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError, match="extra"):
        PartyConflictCheckBody.model_validate(
            {"display_name": "甲公司", "surprise": True}
        )


def test_assign_owner_body_accepts_valid_membership_id() -> None:
    membership_id = "018f6f60-0000-7000-8000-000000000001"
    body = AssignMatterOwnerBody.model_validate(
        {"owner_membership_id": membership_id}
    )
    assert str(body.owner_membership_id) == membership_id


def test_assign_owner_body_rejects_missing_membership_id() -> None:
    with pytest.raises(ValidationError, match="owner_membership_id"):
        AssignMatterOwnerBody.model_validate({})


def test_assign_owner_body_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError, match="extra"):
        AssignMatterOwnerBody.model_validate(
            {"owner_membership_id": "018f6f60-0000-7000-8000-000000000001", "x": 1}
        )
