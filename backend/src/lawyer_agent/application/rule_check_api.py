"""HTTP-facing rule check orchestration (non-model).

Wraps the deterministic rule engine, issue store and report export behind a
unit of work so HTTP endpoints only handle tenant actors, commands and
projections. Every read/write stays inside the tenant context; there is no
un-scoped ``get_by_id``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol, cast
from uuid import UUID

from lawyer_agent.application.report_export import (
    ReportDocumentPort,
    RiskReportService,
)
from lawyer_agent.application.rules import (
    ProvisionInput,
    RiskIssueStorePort,
    RuleCheckService,
    RuleEngine,
    TenantScoped,
)
from lawyer_agent.domain.common import require_uuid7
from lawyer_agent.domain.rule_pack import (
    RiskIssue,
    RiskIssueStatus,
    dispose_risk_issue,
)


class RuleCheckError(Exception):
    """Base class carrying a stable Problem Details code."""

    status: int = 500
    code: str = "rule_check_error"
    title: str = "Rule check failed"


class RuleCheckDocumentNotFound(RuleCheckError):
    status = 404
    code = "rule_check_document_not_found"
    title = "Document not found for this tenant"


class RuleCheckIssueNotFound(RuleCheckError):
    status = 404
    code = "rule_check_issue_not_found"
    title = "Risk issue not found for this tenant"


class RuleCheckUnavailable(RuleCheckError):
    status = 409
    code = "rule_check_unavailable"
    title = "No active rule pack is available for this tenant"


class RuleCheckConflict(RuleCheckError):
    status = 409
    code = "rule_check_conflict"
    title = "Risk issue is no longer open"


class RuleCheckInvalidRequest(RuleCheckError):
    status = 422
    code = "rule_check_invalid_request"
    title = "Rule check request is invalid"


class RuleCheckDocumentPort(ReportDocumentPort, Protocol):
    async def document_exists(self, tenant_id: UUID, document_id: UUID) -> bool: ...


class RuleCheckUnitOfWorkPort(Protocol):
    rule_check: RiskIssueStorePort
    documents: RuleCheckDocumentPort

    async def __aenter__(self) -> RuleCheckUnitOfWorkPort: ...

    async def __aexit__(self, *exc: object) -> None: ...


class RuleCheckHttpService:
    """Composition facade used by the tenant HTTP endpoints."""

    def __init__(self, uow_factory: Callable[[], Any]) -> None:
        if not callable(uow_factory):
            raise ValueError("rule check http service requires a unit of work factory")
        self._uow_factory = uow_factory

    async def run_checks(
        self,
        *,
        context: TenantScoped,
        document_id: UUID,
        provisions: tuple[ProvisionInput, ...],
    ) -> tuple[RiskIssue, ...]:
        require_uuid7(document_id, field="document_id")
        async with cast(RuleCheckUnitOfWorkPort, self._uow_factory()) as uow:
            await self._require_document(uow, context.tenant_id, document_id)
            service = RuleCheckService(
                rule_store=uow.rule_check,
                engine=RuleEngine(),
            )
            try:
                created = await service.run_checks(
                    context=context,
                    document_id=document_id,
                    provisions=provisions,
                )
            except ValueError as exc:
                message = str(exc)
                if "active rule pack" in message or "enabled rules" in message:
                    raise RuleCheckUnavailable from exc
                raise RuleCheckInvalidRequest from exc
            return tuple(created)

    async def list_issues(
        self,
        *,
        context: TenantScoped,
        document_id: UUID,
    ) -> tuple[RiskIssue, ...]:
        require_uuid7(document_id, field="document_id")
        async with cast(RuleCheckUnitOfWorkPort, self._uow_factory()) as uow:
            await self._require_document(uow, context.tenant_id, document_id)
            return tuple(
                await uow.rule_check.issues_for_document(context, document_id)
            )

    async def dispose(
        self,
        *,
        context: TenantScoped,
        issue_id: UUID,
        status: RiskIssueStatus,
        reason: str,
        now: datetime,
    ) -> RiskIssue:
        require_uuid7(issue_id, field="issue_id")
        async with cast(RuleCheckUnitOfWorkPort, self._uow_factory()) as uow:
            issue = await uow.rule_check.find_issue(context, issue_id)
            if issue is None:
                raise RuleCheckIssueNotFound
            service = RuleCheckService(
                rule_store=uow.rule_check,
                engine=RuleEngine(),
            )
            try:
                disposed = await service.dispose(
                    context=context,
                    issue_id=issue_id,
                    status=status,
                    reason=reason,
                    now=now,
                )
            except ValueError as exc:
                raise RuleCheckInvalidRequest from exc
            if not disposed:
                raise RuleCheckConflict
            # Project the committed disposition without relying on a second read.
            return dispose_risk_issue(
                issue=issue,
                status=status,
                reason=reason,
                now=now,
            )

    async def export_report(
        self,
        *,
        context: TenantScoped,
        document_id: UUID,
        now: datetime,
    ) -> bytes:
        require_uuid7(document_id, field="document_id")
        async with cast(RuleCheckUnitOfWorkPort, self._uow_factory()) as uow:
            await self._require_document(uow, context.tenant_id, document_id)
            service = RiskReportService(
                documents=uow.documents,
                issues=uow.rule_check,
            )
            return await service.export(
                context=context,
                document_id=document_id,
                now=now,
            )

    async def _require_document(
        self,
        uow: RuleCheckUnitOfWorkPort,
        tenant_id: UUID,
        document_id: UUID,
    ) -> None:
        if not await uow.documents.document_exists(tenant_id, document_id):
            raise RuleCheckDocumentNotFound
