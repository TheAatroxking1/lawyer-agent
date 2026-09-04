from __future__ import annotations

import pytest
from pydantic import ValidationError

from lawyer_agent.api.v1.matter_documents import (
    CreateMatterBody,
    RegisterDocumentBody,
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
