from __future__ import annotations

import pytest

from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.rule_pack import (
    RiskIssue,
    RiskIssueStatus,
    RiskLevel,
    RulePack,
    RulePackRule,
    RuleTriggerKind,
    new_risk_issue,
)


def _pack() -> RulePack:
    return RulePack(
        id=new_uuid7(),
        tenant_id=new_uuid7(),
        name="租赁规则包",
        version=1,
        active=True,
    )


def _rule(pack: RulePack) -> RulePackRule:
    return RulePackRule(
        id=new_uuid7(),
        pack_id=pack.id,
        tenant_id=pack.tenant_id,
        trigger_kind=RuleTriggerKind.RISK_PHRASE,
        label="违约金过高提示",
        pattern=r"违约金.{0,12}(?:百分之|%)[3-9]\d?",
        risk_level=RiskLevel.HIGH,
        suggestion_template="请人工核验违约金比例。",
    )


def test_pack_and_rule_round_trip() -> None:
    pack = _pack()
    rule = _rule(pack)
    assert rule.pack_id == pack.id
    assert rule.risk_level is RiskLevel.HIGH
    assert rule.enabled is True


def test_rule_rejects_invalid_regex() -> None:
    pack = _pack()
    with pytest.raises(ValueError, match="valid regex"):
        RulePackRule(
            id=new_uuid7(),
            pack_id=pack.id,
            tenant_id=pack.tenant_id,
            trigger_kind=RuleTriggerKind.CLAUSE_TYPE,
            label="bad",
            pattern="(",
            risk_level=RiskLevel.LOW,
            suggestion_template="x",
        )


def test_risk_issue_round_trip_and_defaults() -> None:
    pack = _pack()
    rule = _rule(pack)
    issue = new_risk_issue(
        issue_id=new_uuid7(),
        tenant_id=pack.tenant_id,
        rule_id=rule.id,
        pack_id=pack.id,
        pack_version=1,
        document_id=new_uuid7(),
        provision_no="第十二条",
        matched_text="违约金为30%",
        risk_level=rule.risk_level,
    )
    assert issue.status is RiskIssueStatus.OPEN
    assert issue.evidence_level == "rule_based"
    assert issue.risk_level is RiskLevel.HIGH


def test_risk_issue_rejects_non_rule_based_evidence() -> None:
    with pytest.raises(ValueError, match="rule_based"):
        RiskIssue(
            id=new_uuid7(),
            tenant_id=new_uuid7(),
            rule_id=new_uuid7(),
            pack_id=new_uuid7(),
            pack_version=1,
            document_id=new_uuid7(),
            provision_no="第一条",
            matched_text="x",
            risk_level=RiskLevel.MEDIUM,
            status=RiskIssueStatus.OPEN,
            evidence_level="llm",
        )
