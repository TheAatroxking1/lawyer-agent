from __future__ import annotations

from datetime import UTC, datetime

import pytest

from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.rule_pack import (
    RiskIssue,
    RiskIssueStatus,
    RiskLevel,
    RulePack,
    RulePackRule,
    RuleTriggerKind,
    dispose_risk_issue,
    new_risk_issue,
)

_NOW = datetime(2026, 9, 5, 8, 0, 0, tzinfo=UTC)


def _open_issue() -> RiskIssue:
    pack = RulePack(
        id=new_uuid7(), tenant_id=new_uuid7(), name="p", version=1, active=True
    )
    rule = RulePackRule(
        id=new_uuid7(),
        pack_id=pack.id,
        tenant_id=pack.tenant_id,
        trigger_kind=RuleTriggerKind.RISK_PHRASE,
        label="x",
        pattern=r"违约金",
        risk_level=RiskLevel.HIGH,
        suggestion_template="s",
    )
    return new_risk_issue(
        issue_id=new_uuid7(),
        tenant_id=pack.tenant_id,
        rule_id=rule.id,
        pack_id=pack.id,
        pack_version=1,
        document_id=new_uuid7(),
        provision_no="第十条",
        matched_text="违约金30%",
        risk_level=RiskLevel.HIGH,
    )


def test_open_issue_can_be_accepted() -> None:
    issue = _open_issue()
    disposed = dispose_risk_issue(
        issue=issue, status=RiskIssueStatus.ACCEPTED, reason="律师确认", now=_NOW
    )
    assert disposed.status is RiskIssueStatus.ACCEPTED
    assert disposed.disposition_reason == "律师确认"
    assert disposed.disposed_at == _NOW
    # The original instance is unchanged (immutable value semantics).
    assert issue.status is RiskIssueStatus.OPEN


@pytest.mark.parametrize(
    "status", [RiskIssueStatus.REJECTED, RiskIssueStatus.MODIFIED]
)
def test_open_issue_can_be_rejected_or_modified(status: RiskIssueStatus) -> None:
    issue = _open_issue()
    disposed = dispose_risk_issue(
        issue=issue, status=status, reason="文本已按律师意见修改", now=_NOW
    )
    assert disposed.status is status
    assert disposed.disposition_reason == "文本已按律师意见修改"


def test_non_open_issue_cannot_be_disposed() -> None:
    accepted = dispose_risk_issue(
        issue=_open_issue(),
        status=RiskIssueStatus.ACCEPTED,
        reason="律师确认",
        now=_NOW,
    )
    with pytest.raises(ValueError, match="open"):
        dispose_risk_issue(
            issue=accepted,
            status=RiskIssueStatus.REJECTED,
            reason="再改",
            now=_NOW,
        )


def test_disposition_reason_is_required() -> None:
    with pytest.raises(ValueError, match="reason"):
        dispose_risk_issue(
            issue=_open_issue(),
            status=RiskIssueStatus.ACCEPTED,
            reason="   ",
            now=_NOW,
        )


def test_back_to_open_is_not_a_disposition() -> None:
    with pytest.raises(ValueError, match="disposition"):
        dispose_risk_issue(
            issue=_open_issue(),
            status=RiskIssueStatus.OPEN,
            reason="reopen",
            now=_NOW,
        )
