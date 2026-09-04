from __future__ import annotations

import pytest

from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.rule_pack import RiskLevel, RuleTriggerKind
from lawyer_agent.domain.rule_pack_management import (
    build_pack_rule,
    next_pack_version,
)


@pytest.mark.parametrize(
    ("versions", "expected"),
    [((), 1), ((1,), 2), ((1, 2, 3), 4), ((5,), 6)],
)
def test_next_pack_version_increments(versions: tuple[int, ...], expected: int) -> None:
    assert next_pack_version(versions) == expected


def test_next_pack_version_rejects_non_positive() -> None:
    with pytest.raises(ValueError, match="version"):
        next_pack_version((0, -1))


def _valid_rule() -> dict[str, object]:
    return {
        "tenant_id": new_uuid7(),
        "pack_id": new_uuid7(),
        "trigger_kind": RuleTriggerKind.RISK_PHRASE,
        "label": "违约金过高",
        "pattern": r"(?:百分之[一二三四五六七八九十百0-9]+|[0-9]+\s*%)",
        "risk_level": RiskLevel.HIGH,
        "suggestion": "违约金比例较高，请人工核验。",
        "enabled": True,
    }


def test_build_pack_rule_valid_case() -> None:
    rule = build_pack_rule(**_valid_rule())
    assert rule.enabled is True
    assert rule.risk_level is RiskLevel.HIGH


def test_build_pack_rule_rejects_invalid_regex() -> None:
    values = _valid_rule()
    values.update({"pattern": "("})
    with pytest.raises(ValueError, match="valid regex"):
        build_pack_rule(**values)


def test_build_pack_rule_rejects_unknown_trigger_kind() -> None:
    values = _valid_rule()
    values.update({"trigger_kind": "escalate"})
    with pytest.raises(ValueError, match="trigger kind"):
        build_pack_rule(**values)


def test_build_pack_rule_rejects_unknown_risk_level() -> None:
    values = _valid_rule()
    values.update({"risk_level": "critical"})
    with pytest.raises(ValueError, match="risk level"):
        build_pack_rule(**values)


def test_build_pack_rule_rejects_blank_label() -> None:
    values = _valid_rule()
    values.update({"label": "  "})
    with pytest.raises(ValueError, match="label"):
        build_pack_rule(**values)
