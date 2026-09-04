"""Tenant Rule Pack, rules and rule-based Risk Issues (non-model).

Rules are deterministic text triggers over already-parsed provisions. They
produce candidate risk hints labelled `rule_based`; they never self-claim to be
official law. Any high-risk conclusion and all limitation-period numbers
require human confirmation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from lawyer_agent.domain.common import require_uuid7

_MAX_PATTERN_BYTES = 4 * 1024


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RiskIssueStatus(StrEnum):
    OPEN = "open"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    MODIFIED = "modified"
    STALE = "stale"


class RuleTriggerKind(StrEnum):
    CLAUSE_TYPE = "clause_type"
    RISK_PHRASE = "risk_phrase"


@dataclass(frozen=True, slots=True)
class RulePack:
    id: UUID
    tenant_id: UUID
    name: str
    version: int
    active: bool

    def __post_init__(self) -> None:
        require_uuid7(self.id, field="rule pack id")
        require_uuid7(self.tenant_id, field="rule pack tenant_id")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("rule pack name must be non-empty text")


@dataclass(frozen=True, slots=True)
class RulePackRule:
    id: UUID
    pack_id: UUID
    tenant_id: UUID
    trigger_kind: RuleTriggerKind
    label: str
    pattern: str
    risk_level: RiskLevel
    suggestion_template: str
    enabled: bool = True

    def __post_init__(self) -> None:
        require_uuid7(self.id, field="rule id")
        require_uuid7(self.pack_id, field="rule pack_id")
        require_uuid7(self.tenant_id, field="rule tenant_id")
        if not isinstance(self.trigger_kind, RuleTriggerKind):
            raise ValueError("rule trigger kind must be strongly typed")
        if not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("rule label must be non-empty text")
        if len(self.pattern.encode("utf-8")) > _MAX_PATTERN_BYTES:
            raise ValueError("rule pattern is too large")
        try:
            re.compile(self.pattern)
        except re.error as exc:
            raise ValueError("rule pattern must be valid regex") from exc
        if not isinstance(self.risk_level, RiskLevel):
            raise ValueError("rule risk level must be strongly typed")


@dataclass(frozen=True, slots=True)
class RiskIssue:
    id: UUID
    tenant_id: UUID
    rule_id: UUID
    pack_id: UUID
    pack_version: int
    document_id: UUID
    provision_no: str
    matched_text: str
    risk_level: RiskLevel
    status: RiskIssueStatus
    evidence_level: str = "rule_based"
    disposition_reason: str | None = None
    raised_at: datetime | None = None
    disposed_at: datetime | None = None

    def __post_init__(self) -> None:
        require_uuid7(self.id, field="risk issue id")
        require_uuid7(self.tenant_id, field="risk issue tenant_id")
        require_uuid7(self.rule_id, field="risk issue rule_id")
        require_uuid7(self.pack_id, field="risk issue pack_id")
        require_uuid7(self.document_id, field="risk issue document_id")
        if not isinstance(self.risk_level, RiskLevel):
            raise ValueError("risk issue level must be strongly typed")
        if not isinstance(self.status, RiskIssueStatus):
            raise ValueError("risk issue status must be strongly typed")
        if self.evidence_level != "rule_based":
            raise ValueError("non-model risk issues must stay rule_based")
        if not isinstance(self.provision_no, str) or not self.provision_no:
            raise ValueError("risk issue provision number must be non-empty")


def new_risk_issue(
    *,
    issue_id: UUID,
    tenant_id: UUID,
    rule_id: UUID,
    pack_id: UUID,
    pack_version: int,
    document_id: UUID,
    provision_no: str,
    matched_text: str,
    risk_level: RiskLevel,
) -> RiskIssue:
    return RiskIssue(
        id=issue_id,
        tenant_id=tenant_id,
        rule_id=rule_id,
        pack_id=pack_id,
        pack_version=pack_version,
        document_id=document_id,
        provision_no=provision_no,
        matched_text=matched_text,
        risk_level=risk_level,
        status=RiskIssueStatus.OPEN,
    )
