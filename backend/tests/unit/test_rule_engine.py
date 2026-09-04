from __future__ import annotations

import pytest

from lawyer_agent.application.rules import ProvisionInput, RuleEngine
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.rule_pack import (
    RiskIssueStatus,
    RiskLevel,
    RulePack,
    RulePackRule,
    RuleTriggerKind,
)

_TEXT = (
    "第十条 违约责任：甲方逾期交付租赁物的，每逾期一日应按日租金的"
    "百分之三向乙方支付违约金，但累计不得超过本合同总价款的百分之三十。"
)


def _pack() -> RulePack:
    return RulePack(
        id=new_uuid7(),
        tenant_id=new_uuid7(),
        name="租赁合同规则包",
        version=2,
        active=True,
    )


def _clause_rule(pack: RulePack) -> RulePackRule:
    return RulePackRule(
        id=new_uuid7(),
        pack_id=pack.id,
        tenant_id=pack.tenant_id,
        trigger_kind=RuleTriggerKind.CLAUSE_TYPE,
        label="违约责任条款",
        pattern=r"违约责任",
        risk_level=RiskLevel.LOW,
        suggestion_template="该条款约定违约责任。",
    )


def _phrase_rule(pack: RulePack) -> RulePackRule:
    return RulePackRule(
        id=new_uuid7(),
        pack_id=pack.id,
        tenant_id=pack.tenant_id,
        trigger_kind=RuleTriggerKind.RISK_PHRASE,
        label="高额违约金提示",
        pattern=r"(?:百分之[一二三四五六七八九十百0-9]+|[0-9]+\s*%)",
        risk_level=RiskLevel.HIGH,
        suggestion_template="违约金比例较高，请人工核验。",
    )


def test_clause_type_rule_hits_and_raises_issue() -> None:
    pack = _pack()
    rule = _clause_rule(pack)
    issues = RuleEngine().run(
        pack=pack,
        rules=(rule,),
        provisions=(ProvisionInput(provision_no="第十条", text=_TEXT),),
        tenant_id=pack.tenant_id,
        document_id=new_uuid7(),
    )
    assert len(issues) == 1
    issue = issues[0]
    assert issue.provision_no == "第十条"
    assert issue.rule_id == rule.id
    assert issue.risk_level is RiskLevel.LOW
    assert issue.evidence_level == "rule_based"
    assert issue.status is RiskIssueStatus.OPEN
    assert "违约责任" in issue.matched_text


def test_risk_phrase_rule_hits_with_quoted_percent() -> None:
    pack = _pack()
    rule = _phrase_rule(pack)
    issues = RuleEngine().run(
        pack=pack,
        rules=(rule,),
        provisions=(ProvisionInput(provision_no="第十条", text=_TEXT),),
        tenant_id=pack.tenant_id,
        document_id=new_uuid7(),
    )
    assert len(issues) == 1
    issue = issues[0]
    assert issue.rule_id == rule.id
    assert issue.risk_level is RiskLevel.HIGH
    assert "百分之" in issue.matched_text


def test_multiple_rules_matching_one_provision_raise_separate_issues() -> None:
    pack = _pack()
    clause = _clause_rule(pack)
    phrase = _phrase_rule(pack)
    issues = RuleEngine().run(
        pack=pack,
        rules=(clause, phrase),
        provisions=(ProvisionInput(provision_no="第十条", text=_TEXT),),
        tenant_id=pack.tenant_id,
        document_id=new_uuid7(),
    )
    assert len(issues) == 2
    assert {issue.rule_id for issue in issues} == {clause.id, phrase.id}


def test_no_rule_hit_returns_empty() -> None:
    pack = _pack()
    issues = RuleEngine().run(
        pack=pack,
        rules=(_clause_rule(pack),),
        provisions=(ProvisionInput(provision_no="第一条", text="本合同由双方协商一致订立。"),),
        tenant_id=pack.tenant_id,
        document_id=new_uuid7(),
    )
    assert issues == ()


def test_disabled_rule_is_skipped() -> None:
    pack = _pack()
    disabled = RulePackRule(
        id=new_uuid7(),
        pack_id=pack.id,
        tenant_id=pack.tenant_id,
        trigger_kind=RuleTriggerKind.RISK_PHRASE,
        label="停用规则",
        pattern=r"违约金",
        risk_level=RiskLevel.MEDIUM,
        suggestion_template="x",
        enabled=False,
    )
    issues = RuleEngine().run(
        pack=pack,
        rules=(disabled,),
        provisions=(ProvisionInput(provision_no="第十条", text=_TEXT),),
        tenant_id=pack.tenant_id,
        document_id=new_uuid7(),
    )
    assert issues == ()


def test_inactive_pack_is_rejected() -> None:
    pack = RulePack(
        id=new_uuid7(),
        tenant_id=new_uuid7(),
        name="停用包",
        version=1,
        active=False,
    )
    with pytest.raises(ValueError, match="active"):
        RuleEngine().run(
            pack=pack,
            rules=(_clause_rule(pack),),
            provisions=(ProvisionInput(provision_no="第十条", text=_TEXT),),
            tenant_id=pack.tenant_id,
            document_id=new_uuid7(),
        )


def test_foreign_rule_tenant_is_rejected() -> None:
    pack = _pack()
    foreign = RulePackRule(
        id=new_uuid7(),
        pack_id=pack.id,
        tenant_id=new_uuid7(),
        trigger_kind=RuleTriggerKind.RISK_PHRASE,
        label="跨租户规则",
        pattern=r"违约金",
        risk_level=RiskLevel.MEDIUM,
        suggestion_template="x",
    )
    with pytest.raises(ValueError, match="tenant"):
        RuleEngine().run(
            pack=pack,
            rules=(foreign,),
            provisions=(ProvisionInput(provision_no="第十条", text=_TEXT),),
            tenant_id=pack.tenant_id,
            document_id=new_uuid7(),
        )
