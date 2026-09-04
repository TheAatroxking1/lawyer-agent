"""Deterministic (non-model) rule pack engine.

Runs tenant-owned regex rules over structured provision text and produces
candidate ``RiskIssue`` rows labelled ``rule_based``. It never claims legal
authority and never computes limitation periods; high-risk findings must be
confirmed by a human before they enter any conclusion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.rule_pack import (
    RiskIssue,
    RulePack,
    RulePackRule,
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
