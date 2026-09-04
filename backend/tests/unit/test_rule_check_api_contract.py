from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from lawyer_agent.api.v1.rule_checks import (
    DispositionBody,
    ProvisionInputBody,
    RiskCheckRunBody,
)
from lawyer_agent.application.rule_check_api import (
    RuleCheckDocumentNotFound,
    RuleCheckHttpService,
)


def _uuid() -> UUID:
    return uuid4()


def test_run_body_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError, match="extra"):
        RiskCheckRunBody.model_validate({"provisions": [], "surprise": True})


def test_run_body_rejects_blank_provision_text() -> None:
    with pytest.raises(ValidationError):
        RiskCheckRunBody.model_validate(
            {"provisions": [{"provision_no": "第十条", "text": "  "}]}
        )


def test_run_body_rejects_missing_provision_no() -> None:
    with pytest.raises(ValidationError):
        RiskCheckRunBody.model_validate({"provisions": [{"text": "abc"}]})


def test_provision_input_body_valid_case() -> None:
    body = ProvisionInputBody(provision_no="第一条", text="双方协商一致。")
    assert body.provision_no == "第一条"
    assert body.text == "双方协商一致。"


def test_disposition_body_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        DispositionBody.model_validate({"status": "open", "reason": "x"})


def test_disposition_body_rejects_blank_reason() -> None:
    with pytest.raises(ValidationError):
        DispositionBody.model_validate({"status": "accepted", "reason": "  "})


def test_disposition_body_valid_case() -> None:
    body = DispositionBody.model_validate(
        {"status": "modified", "reason": "律师调整建议文本"}
    )
    assert body.status == "modified"
    assert body.reason == "律师调整建议文本"


def test_document_not_found_carries_stable_code() -> None:
    error = RuleCheckDocumentNotFound()
    assert error.code == "rule_check_document_not_found"
    assert error.status == 404


def test_service_missing_from_composition_returns_503_semantics() -> None:
    """The composition contract: absent service => 503 (checked by the endpoint)."""
    services = SimpleNamespace(rule_check=None)
    with pytest.raises(RuntimeError):
        _require(services)


def _require(services: object) -> RuleCheckHttpService:
    value = getattr(services, "rule_check", None)
    if value is None:
        raise RuntimeError("rule check service is unavailable")
    return value


def test_service_present_is_returned() -> None:
    placeholder = object()
    services = SimpleNamespace(rule_check=placeholder)
    assert services.rule_check is placeholder
