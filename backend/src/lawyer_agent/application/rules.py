"""Deterministic (non-model) rule pack engine.

Runs tenant-owned regex rules over structured provision text and produces
candidate ``RiskIssue`` rows labelled ``rule_based``. It never claims legal
authority and never computes limitation periods; high-risk findings must be
confirmed by a human before they enter any conclusion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.rule_pack import (
    RiskIssue,
    RiskIssueStatus,
    RulePack,
    RulePackRule,
    dispose_risk_issue,
    new_risk_issue,
)

_MAX_MATCHED_TEXT_CHARS = 400


@dataclass(frozen=True, slots=True)
class ProvisionInput:
    """One structured provision text of a document, with its number."""

    provision_no: str
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.provision_no, str) or not self.provision_no.strip():
            raise ValueError("provision number must be non-empty")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("provision text must be non-empty")


class RuleEnginePort(Protocol):
    def run(
        self,
        *,
        pack: RulePack,
        rules: tuple[RulePackRule, ...],
        provisions: tuple[ProvisionInput, ...],
        tenant_id: UUID,
        document_id: UUID,
    ) -> tuple[RiskIssue, ...]: ...


class RuleEngine:
    """Run whitelisted regex rules over provision text; no model involved."""

    def run(
        self,
        *,
        pack: RulePack,
        rules: tuple[RulePackRule, ...],
        provisions: tuple[ProvisionInput, ...],
        tenant_id: UUID,
        document_id: UUID,
    ) -> tuple[RiskIssue, ...]:
        if tenant_id != pack.tenant_id:
            raise ValueError("rule engine tenant does not match the rule pack tenant")
        if not pack.active:
            raise ValueError("rule engine requires an active rule pack")
        issues: list[RiskIssue] = []
        for rule in rules:
            if not rule.enabled:
                continue
            if rule.tenant_id != pack.tenant_id or rule.pack_id != pack.id:
                raise ValueError(
                    "rule engine cannot run a rule from another tenant or rule pack"
                )
            try:
                pattern = re.compile(rule.pattern)
            except re.error as exc:  # pragma: no cover - domain validates on write
                raise ValueError("rule pattern must be valid regex") from exc
            for provision in provisions:
                match = pattern.search(provision.text)
                if match is None:
                    continue
                issues.append(
                    new_risk_issue(
                        issue_id=new_uuid7(),
                        tenant_id=tenant_id,
                        rule_id=rule.id,
                        pack_id=pack.id,
                        pack_version=pack.version,
                        document_id=document_id,
                        provision_no=provision.provision_no,
                        matched_text=_clamp(match.group(0)),
                        risk_level=rule.risk_level,
                    )
                )
        return tuple(issues)


def _clamp(value: str) -> str:
    if len(value) <= _MAX_MATCHED_TEXT_CHARS:
        return value
    return value[: _MAX_MATCHED_TEXT_CHARS]


class TenantScoped(Protocol):
    @property
    def tenant_id(self) -> UUID: ...


class RiskIssueStorePort(Protocol):
    async def get_active_pack(self, context: TenantScoped) -> RulePack | None: ...

    async def rules_for_pack(
        self, context: TenantScoped, pack_id: UUID
    ) -> tuple[RulePackRule, ...]: ...

    async def issues_for_document(
        self, context: TenantScoped, document_id: UUID
    ) -> tuple[RiskIssue, ...]: ...

    async def find_issue(
        self, context: TenantScoped, issue_id: UUID
    ) -> RiskIssue | None: ...

    async def save_issue(self, issue: RiskIssue) -> None: ...

    async def dispose_issue(
        self,
        *,
        tenant_id: UUID,
        issue_id: UUID,
        status: RiskIssueStatus,
        reason: str,
        now: datetime,
    ) -> bool: ...

    async def mark_open_issue_stale(
        self, *, tenant_id: UUID, issue_id: UUID, now: datetime
    ) -> bool: ...


class RuleCheckService:
    """Run the deterministic engine for one document and track dispositions.

    ``run_checks`` only creates issues for the tenant's currently active pack.
    Re-running the same document is idempotent: an open issue whose matched
    text is unchanged is kept as-is; an open issue that no longer matches the
    new run is marked stale so the lawyer sees only current findings.
    """

    def __init__(
        self,
        *,
        rule_store: RiskIssueStorePort,
        engine: RuleEnginePort,
    ) -> None:
        self._store = rule_store
        self._engine = engine

    async def run_checks(
        self,
        *,
        context: TenantScoped,
        document_id: UUID,
        provisions: tuple[ProvisionInput, ...],
    ) -> tuple[RiskIssue, ...]:
        pack = await self._get_active_pack(context)
        rules = await self._get_rules(context, pack)
        fresh = self._engine.run(
            pack=pack,
            rules=rules,
            provisions=provisions,
            tenant_id=context.tenant_id,
            document_id=document_id,
        )
        existing = await self._store.issues_for_document(context, document_id)
        created: list[RiskIssue] = []
        for candidate in fresh:
            if _open_match_exists(existing, candidate):
                continue
            await self._store.save_issue(candidate)
            created.append(candidate)
        now = datetime.now(UTC)
        for old in existing:
            if old.status is not RiskIssueStatus.OPEN:
                continue
            if _open_match_exists(fresh, old):
                continue
            await self._store.mark_open_issue_stale(
                tenant_id=context.tenant_id,
                issue_id=old.id,
                now=now,
            )
        return tuple(created)

    async def dispose(
        self,
        *,
        context: TenantScoped,
        issue_id: UUID,
        status: RiskIssueStatus,
        reason: str,
        now: datetime,
    ) -> bool:
        issue = await self._store.find_issue(context, issue_id)
        if issue is None:
            return False
        dispose_risk_issue(issue=issue, status=status, reason=reason, now=now)
        return await self._store.dispose_issue(
            tenant_id=context.tenant_id,
            issue_id=issue_id,
            status=status,
            reason=reason,
            now=now,
        )

    async def _get_active_pack(self, context: TenantScoped) -> RulePack:
        pack = await self._store.get_active_pack(context)
        if pack is None:
            raise ValueError("no active rule pack for tenant")
        return pack

    async def _get_rules(
        self, context: TenantScoped, pack: RulePack
    ) -> tuple[RulePackRule, ...]:
        rules = await self._store.rules_for_pack(context, pack.id)
        if not rules:
            raise ValueError("active rule pack has no enabled rules")
        return rules


def _open_match_exists(
    issues: tuple[RiskIssue, ...],
    target: RiskIssue,
) -> bool:
    for issue in issues:
        if issue.status is not RiskIssueStatus.OPEN:
            continue
        if (
            issue.rule_id == target.rule_id
            and issue.provision_no == target.provision_no
            and issue.matched_text == target.matched_text
        ):
            return True
    return False
