"""Rule Pack administration helpers (non-model).

Pure functions that decide the next pack version and build strongly validated
rules. Pattern text is a regex whitelist only: it is compiled for validation,
never executed, and never treated as code.
"""

from __future__ import annotations

from uuid import UUID

from lawyer_agent.domain.common import new_uuid7, require_uuid7
from lawyer_agent.domain.rule_pack import (
    RiskLevel,
    RulePackRule,
    RuleTriggerKind,
)

_MAX_PATTERN_BYTES = 4 * 1024


def next_pack_version(existing_versions: tuple[int, ...] | list[int]) -> int:
    """Return the next pack version for a name scoped to one tenant."""
    if not isinstance(existing_versions, (tuple, list)):
        raise ValueError("existing versions must be a sequence")
    for value in existing_versions:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("pack versions must be positive integers")
    if not existing_versions:
        return 1
    return max(existing_versions) + 1


def build_pack_rule(
    *,
    tenant_id: UUID | str,
    pack_id: UUID | str,
    trigger_kind: RuleTriggerKind | str,
    label: str,
    pattern: str,
    risk_level: RiskLevel | str,
    suggestion: str,
    enabled: bool = True,
) -> RulePackRule:
    """Construct a validated rule for a tenant pack (regex whitelist only)."""
    tenant = require_uuid7(tenant_id, field="rule tenant_id")
    pack = require_uuid7(pack_id, field="rule pack_id")
    if not isinstance(trigger_kind, RuleTriggerKind):
        try:
            trigger_kind = RuleTriggerKind(str(trigger_kind))
        except ValueError as exc:
            raise ValueError("rule trigger kind must be a known kind") from exc
    if not isinstance(risk_level, RiskLevel):
        try:
            risk_level = RiskLevel(str(risk_level))
        except ValueError as exc:
            raise ValueError("rule risk level must be a known level") from exc
    if not isinstance(label, str) or not label.strip():
        raise ValueError("rule label must be non-empty text")
    if not isinstance(suggestion, str) or not suggestion.strip():
        raise ValueError("rule suggestion must be non-empty text")
    if not isinstance(pattern, str) or not pattern.strip():
        raise ValueError("rule pattern must be non-empty text")
    if len(pattern.encode("utf-8")) > _MAX_PATTERN_BYTES:
        raise ValueError("rule pattern is too large")
    return RulePackRule(
        id=new_uuid7(),
        pack_id=pack,
        tenant_id=tenant,
        trigger_kind=trigger_kind,
        label=label,
        pattern=pattern,
        risk_level=risk_level,
        suggestion_template=suggestion,
        enabled=enabled,
    )
