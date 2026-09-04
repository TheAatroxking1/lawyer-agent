from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from lawyer_agent.api.v1.rule_packs import (
    CreateRulePackBody,
    PatchRuleBody,
    RulePackRuleBody,
)
from lawyer_agent.application.rule_pack_admin_api import RulePackAdminNotFound


def test_create_pack_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError, match="extra"):
        CreateRulePackBody.model_validate({"name": "租赁合同", "surprise": 1})


def test_create_pack_rejects_blank_name() -> None:
    with pytest.raises(ValidationError):
        CreateRulePackBody.model_validate({"name": "   "})


def test_rule_body_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError, match="extra"):
        RulePackRuleBody.model_validate(
            {
                "trigger_kind": "risk_phrase",
                "label": "违约金",
                "pattern": "违约金",
                "risk_level": "high",
                "suggestion": "请核验",
                "surprise": 1,
            }
        )


def test_rule_body_rejects_unknown_trigger_kind() -> None:
    with pytest.raises(ValidationError):
        RulePackRuleBody.model_validate(
            {
                "trigger_kind": "escalate",
                "label": "违约金",
                "pattern": "违约金",
                "risk_level": "high",
                "suggestion": "请核验",
            }
        )


def test_rule_body_rejects_unknown_risk_level() -> None:
    with pytest.raises(ValidationError):
        RulePackRuleBody.model_validate(
            {
                "trigger_kind": "risk_phrase",
                "label": "违约金",
                "pattern": "违约金",
                "risk_level": "critical",
                "suggestion": "请核验",
            }
        )


def test_rule_body_rejects_blank_label() -> None:
    with pytest.raises(ValidationError):
        RulePackRuleBody.model_validate(
            {
                "trigger_kind": "clause_type",
                "label": "  ",
                "pattern": "违约责任",
                "risk_level": "low",
                "suggestion": "请核验",
            }
        )


def test_patch_rule_body_accepts_enabled_flag() -> None:
    body = PatchRuleBody.model_validate({"enabled": False})
    assert body.enabled is False


def test_patch_rule_body_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError, match="extra"):
        PatchRuleBody.model_validate({"enabled": True, "label": "x"})


def test_admin_not_found_carries_stable_code() -> None:
    error = RulePackAdminNotFound()
    assert error.code == "rule_pack_admin_not_found"
    assert error.status == 404


def test_service_present_in_composition() -> None:
    placeholder = object()
    services = SimpleNamespace(rule_pack_admin_http=placeholder)
    assert services.rule_pack_admin_http is placeholder
