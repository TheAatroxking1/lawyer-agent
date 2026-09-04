from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol, cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from lawyer_agent.domain.rule_pack import (
    RiskIssue,
    RiskIssueStatus,
    RiskLevel,
    RulePack,
    RulePackRule,
    RuleTriggerKind,
)
from lawyer_agent.domain.tenancy import TenantContext
from lawyer_agent.infrastructure.persistence.models.rule_pack import (
    TenantContractRiskIssueModel,
    TenantRulePackModel,
    TenantRulePackRuleModel,
)

_KIND_MAP = {"clause_type": RuleTriggerKind.CLAUSE_TYPE, "risk_phrase": RuleTriggerKind.RISK_PHRASE}
_RISK_MAP = {"low": RiskLevel.LOW, "medium": RiskLevel.MEDIUM, "high": RiskLevel.HIGH}
_STATUS_MAP = {
    "open": RiskIssueStatus.OPEN,
    "accepted": RiskIssueStatus.ACCEPTED,
    "rejected": RiskIssueStatus.REJECTED,
    "modified": RiskIssueStatus.MODIFIED,
    "stale": RiskIssueStatus.STALE,
}


class RulePackStorePort(Protocol):
    async def get_active_pack(self, context: TenantContext) -> RulePack | None: ...

    async def rules_for_pack(
        self, context: TenantContext, pack_id: UUID
    ) -> tuple[RulePackRule, ...]: ...


class RulePackError(ValueError):
    pass


def _require_context(context: TenantContext) -> None:
    if not isinstance(context, TenantContext):
        raise ValueError("rule pack repository requires a typed tenant context")


class SqlAlchemyRulePackRepository:
    """Tenant-scoped rule packs; deactivated packs are read-only."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_active_pack(self, context: TenantContext) -> RulePack | None:
        _require_context(context)
        model = await self._session.scalar(
            select(TenantRulePackModel)
            .where(
                TenantRulePackModel.tenant_id == context.tenant_id,
                TenantRulePackModel.active.is_(True),
            )
            .order_by(TenantRulePackModel.version.desc())
        )
        return None if model is None else _rule_pack(model)

    async def rules_for_pack(
        self, context: TenantContext, pack_id: UUID
    ) -> tuple[RulePackRule, ...]:
        _require_context(context)
        rows = await self._session.scalars(
            select(TenantRulePackRuleModel).where(
                TenantRulePackRuleModel.tenant_id == context.tenant_id,
                TenantRulePackRuleModel.pack_id == pack_id,
                TenantRulePackRuleModel.enabled.is_(True),
            )
        )
        return tuple(_rule(row) for row in rows)

    async def find_issue(
        self, context: TenantContext, issue_id: UUID
    ) -> RiskIssue | None:
        _require_context(context)
        model = await self._session.scalar(
            select(TenantContractRiskIssueModel).where(
                TenantContractRiskIssueModel.tenant_id == context.tenant_id,
                TenantContractRiskIssueModel.id == issue_id,
            )
        )
        return None if model is None else _issue(model)

    async def save_issue(self, issue: RiskIssue) -> None:
        self._session.add(_issue_model(issue))
        await self._session.flush()

    async def issues_for_document(
        self, context: TenantContext, document_id: UUID
    ) -> tuple[RiskIssue, ...]:
        _require_context(context)
        rows = await self._session.scalars(
            select(TenantContractRiskIssueModel)
            .where(
                TenantContractRiskIssueModel.tenant_id == context.tenant_id,
                TenantContractRiskIssueModel.document_id == document_id,
            )
            .order_by(TenantContractRiskIssueModel.created_at)
        )
        return tuple(_issue(row) for row in rows)

    async def mark_open_issue_stale(
        self,
        *,
        tenant_id: UUID,
        issue_id: UUID,
        now: datetime,
    ) -> bool:
        """Move an open issue to stale; only open rows can be marked."""
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(TenantContractRiskIssueModel)
                .where(
                    TenantContractRiskIssueModel.tenant_id == tenant_id,
                    TenantContractRiskIssueModel.id == issue_id,
                    TenantContractRiskIssueModel.status == "open",
                )
                .values(status="stale", disposed_at=_naive(now))
            ),
        )
        return result.rowcount == 1

    async def dispose_issue(
        self,
        *,
        tenant_id: UUID,
        issue_id: UUID,
        status: RiskIssueStatus,
        reason: str,
        now: datetime,
    ) -> bool:
        if status not in {
            RiskIssueStatus.ACCEPTED,
            RiskIssueStatus.REJECTED,
            RiskIssueStatus.MODIFIED,
        }:
            raise RulePackError("issue disposition must accept, reject or modify")
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(TenantContractRiskIssueModel)
                .where(
                    TenantContractRiskIssueModel.tenant_id == tenant_id,
                    TenantContractRiskIssueModel.id == issue_id,
                    TenantContractRiskIssueModel.status == "open",
                )
                .values(
                    status=status.value,
                    disposition_reason=reason,
                    disposed_at=_naive(now),
                )
            ),
        )
        return result.rowcount == 1


def _rule_pack(model: TenantRulePackModel) -> RulePack:
    return RulePack(
        id=model.id,
        tenant_id=model.tenant_id,
        name=model.name,
        version=model.version,
        active=bool(model.active),
    )


def _rule(model: TenantRulePackRuleModel) -> RulePackRule:
    return RulePackRule(
        id=model.id,
        pack_id=model.pack_id,
        tenant_id=model.tenant_id,
        trigger_kind=_KIND_MAP[model.trigger_kind],
        label=model.label,
        pattern=model.pattern,
        risk_level=_RISK_MAP[model.risk_level],
        suggestion_template=model.suggestion_template,
        enabled=bool(model.enabled),
    )


def _issue(model: TenantContractRiskIssueModel) -> RiskIssue:
    if model.rule_id is None:
        raise RuntimeError("stored risk issue is missing its rule reference")
    return RiskIssue(
        id=model.id,
        tenant_id=model.tenant_id,
        rule_id=model.rule_id,
        pack_id=model.pack_id,
        pack_version=model.pack_version,
        document_id=model.document_id,
        provision_no=model.provision_no,
        matched_text=model.matched_text,
        risk_level=_RISK_MAP[model.risk_level],
        status=_STATUS_MAP[model.status],
        evidence_level=model.evidence_level,
        disposition_reason=model.disposition_reason,
        raised_at=_aware_optional(model.raised_at),
        disposed_at=_aware_optional(model.disposed_at),
    )


def _issue_model(issue: RiskIssue) -> TenantContractRiskIssueModel:
    return TenantContractRiskIssueModel(
        id=issue.id,
        tenant_id=issue.tenant_id,
        document_id=issue.document_id,
        rule_id=issue.rule_id,
        pack_id=issue.pack_id,
        pack_version=issue.pack_version,
        provision_no=issue.provision_no,
        matched_text=issue.matched_text,
        risk_level=issue.risk_level.value,
        status=issue.status.value,
        evidence_level=issue.evidence_level,
        disposition_reason=issue.disposition_reason,
        raised_at=_naive_optional(issue.raised_at),
        disposed_at=_naive_optional(issue.disposed_at),
    )


def _naive(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("rule pack timestamp must be timezone-aware")
    return value.astimezone(UTC).replace(tzinfo=None)


def _naive_optional(value: datetime | None) -> datetime | None:
    return None if value is None else _naive(value)


def _aware_optional(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)
