from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest

from lawyer_agent.application.rules import (
    ProvisionInput,
    RuleCheckService,
    RuleEngine,
)
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.rule_pack import (
    RiskIssue,
    RiskIssueStatus,
    RiskLevel,
    RulePack,
    RulePackRule,
    RuleTriggerKind,
)

_NOW = datetime(2026, 9, 5, 9, 0, 0, tzinfo=UTC)
_TEXT = (
    "第十条 违约责任：甲方逾期交付租赁物的，每逾期一日应按日租金的"
    "百分之三向乙方支付违约金，但累计不得超过本合同总价款的百分之三十。"
)


class FakeRuleStore:
    """In-memory store covering every RuleCheckService persistence call."""

    def __init__(self) -> None:
        self.pack: RulePack | None = None
        self.rules: tuple[RulePackRule, ...] = ()
        self.issues: list[RiskIssue] = []
        self.saved: list[RiskIssue] = []
        self.staled: list[UUID] = []

    def seed(self, *, active: bool = True) -> tuple[RulePack, RulePackRule]:
        pack = RulePack(
            id=new_uuid7(),
            tenant_id=new_uuid7(),
            name="租赁合同规则包",
            version=2,
            active=active,
        )
        rule = RulePackRule(
            id=new_uuid7(),
            pack_id=pack.id,
            tenant_id=pack.tenant_id,
            trigger_kind=RuleTriggerKind.RISK_PHRASE,
            label="高额违约金提示",
            pattern=r"(?:百分之[一二三四五六七八九十百0-9]+|[0-9]+\s*%)",
            risk_level=RiskLevel.HIGH,
            suggestion_template="违约金比例较高，请人工核验。",
        )
        self.pack = pack
        self.rules = (rule,)
        return pack, rule

    async def get_active_pack(self, context) -> RulePack | None:
        del context
        return self.pack

    async def rules_for_pack(self, context, pack_id: UUID):
        del context, pack_id
        return self.rules

    async def find_issue(self, context, issue_id: UUID) -> RiskIssue | None:
        del context
        return next((i for i in self.issues if i.id == issue_id), None)

    async def issues_for_document(self, context, document_id: UUID):
        del context
        return tuple(i for i in self.issues if i.document_id == document_id)

    async def save_issue(self, issue: RiskIssue) -> None:
        self.issues.append(issue)
        self.saved.append(issue)

    async def dispose_issue(
        self, *, tenant_id, issue_id, status, reason, now
    ) -> bool:
        del tenant_id, issue_id, status, reason, now
        return True

    async def mark_open_issue_stale(self, *, tenant_id, issue_id, now) -> bool:
        del tenant_id, now
        self.staled.append(issue_id)
        return True


class _Context:
    def __init__(self, tenant_id: UUID) -> None:
        self.tenant_id = tenant_id


def _service(store: FakeRuleStore) -> RuleCheckService:
    return RuleCheckService(
        rule_store=store,
        engine=RuleEngine(),
    )


async def _run_once(store: FakeRuleStore, document_id: UUID, text: str):
    return await _service(store).run_checks(
        context=_Context(store.pack.tenant_id),
        document_id=document_id,
        provisions=(ProvisionInput(provision_no="第十条", text=text),),
    )


def test_service_saves_new_matches_as_open() -> None:
    store = FakeRuleStore()
    store.seed()
    document_id = new_uuid7()
    issues = asyncio.run(_run_once(store, document_id, _TEXT))
    assert len(issues) == 1
    issue = issues[0]
    assert issue.status is RiskIssueStatus.OPEN
    assert issue.document_id == document_id
    assert issue.risk_level is RiskLevel.HIGH
    assert issue.evidence_level == "rule_based"
    assert len(store.saved) == 1


def test_service_rerun_is_idempotent_when_text_is_unchanged() -> None:
    store = FakeRuleStore()
    store.seed()
    document_id = new_uuid7()

    async def scenario() -> tuple[int, int]:
        first = await _run_once(store, document_id, _TEXT)
        second = await _run_once(store, document_id, _TEXT)
        return len(first), len(second)

    first_count, second_count = asyncio.run(scenario())
    assert first_count == 1
    assert second_count == 0
    assert len(store.saved) == 1
    assert store.staled == []


def test_service_marks_old_open_issue_stale_when_text_changes() -> None:
    store = FakeRuleStore()
    store.seed()
    document_id = new_uuid7()
    changed = _TEXT.replace("百分之三", "百分之十五")

    async def scenario() -> tuple[RiskIssue, RiskIssue]:
        first = await _run_once(store, document_id, _TEXT)
        second = await _run_once(store, document_id, changed)
        return first[0], second[0]

    first, second = asyncio.run(scenario())
    assert second.id != first.id
    assert first.id in store.staled
    assert second.status is RiskIssueStatus.OPEN


def test_dispose_accepts_open_issue_with_reason() -> None:
    store = FakeRuleStore()
    store.seed()
    document_id = new_uuid7()
    issues = asyncio.run(_run_once(store, document_id, _TEXT))
    issue = issues[0]

    async def scenario() -> bool:
        return await _service(store).dispose(
            context=_Context(store.pack.tenant_id),
            issue_id=issue.id,
            status=RiskIssueStatus.ACCEPTED,
            reason="律师确认该比例可接受",
            now=_NOW,
        )

    assert asyncio.run(scenario()) is True


def test_dispose_rejects_blank_reason() -> None:
    store = FakeRuleStore()
    store.seed()
    document_id = new_uuid7()
    issues = asyncio.run(_run_once(store, document_id, _TEXT))
    issue = issues[0]

    async def scenario() -> bool:
        return await _service(store).dispose(
            context=_Context(store.pack.tenant_id),
            issue_id=issue.id,
            status=RiskIssueStatus.REJECTED,
            reason="  ",
            now=_NOW,
        )

    with pytest.raises(ValueError, match="reason"):
        asyncio.run(scenario())


def test_dispose_rejects_non_open_issue() -> None:
    store = FakeRuleStore()
    store.seed()
    document_id = new_uuid7()
    issues = asyncio.run(_run_once(store, document_id, _TEXT))
    issue = issues[0]
    # Make the stored issue already disposed.
    store.issues = [
        RiskIssue(
            id=issue.id,
            tenant_id=issue.tenant_id,
            rule_id=issue.rule_id,
            pack_id=issue.pack_id,
            pack_version=issue.pack_version,
            document_id=issue.document_id,
            provision_no=issue.provision_no,
            matched_text=issue.matched_text,
            risk_level=issue.risk_level,
            status=RiskIssueStatus.ACCEPTED,
            disposition_reason="已确认",
        )
    ]

    async def scenario() -> bool:
        return await _service(store).dispose(
            context=_Context(store.pack.tenant_id),
            issue_id=issue.id,
            status=RiskIssueStatus.REJECTED,
            reason="改为驳回",
            now=_NOW,
        )

    with pytest.raises(ValueError, match="open"):
        asyncio.run(scenario())
