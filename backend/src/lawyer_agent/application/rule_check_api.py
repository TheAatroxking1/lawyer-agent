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

from lawyer_agent.application.audit import new_tenant_user_audit_event
from lawyer_agent.application.idempotency import (
    IdempotencyFingerprintPayload,
    IdempotencyRepositoryPort,
    IdempotencyRequest,
    IdempotencyReservation,
    IdempotencyResultReference,
    IdempotencyScope,
    IdempotencyScopeType,
    IdempotencyService,
)
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

_DISPOSE_OPERATION = "risk_issue.dispose"
_DISPOSE_ROUTE = "/api/v1/tenants/{tenant_id}/risk-issues/{issue_id}/disposition"
_DISPOSE_RESULT_TYPE = "risk_issue"


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
    idempotency: IdempotencyRepositoryPort

    async def __aenter__(self) -> RuleCheckUnitOfWorkPort: ...

    async def __aexit__(self, *exc: object) -> None: ...


class RuleCheckHttpService:
    """Composition facade used by the tenant HTTP endpoints."""

    def __init__(
        self,
        uow_factory: Callable[[], Any],
        idempotency: IdempotencyService | None = None,
    ) -> None:
        if not callable(uow_factory):
            raise ValueError("rule check http service requires a unit of work factory")
        if idempotency is not None and not isinstance(idempotency, IdempotencyService):
            raise ValueError("rule check http service requires an idempotency service")
        self._uow_factory = uow_factory
        self._idempotency = idempotency

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
        idempotency_key: str | None = None,
        trace_id: str | None = None,
    ) -> RiskIssue:
        require_uuid7(issue_id, field="issue_id")
        membership_id = getattr(context, "membership_id", None)
        reservation = None
        try:
            async with cast(RuleCheckUnitOfWorkPort, self._uow_factory()) as uow:
                if idempotency_key and self._idempotency is not None:
                    if membership_id is None:
                        raise RuleCheckConflict
                    reservation = await self._reserve_dispose(
                        uow,
                        tenant_id=context.tenant_id,
                        membership_id=membership_id,
                        issue_id=issue_id,
                        status=status,
                        reason=reason,
                        idempotency_key=idempotency_key,
                        now=now,
                    )
                    if reservation.is_replay:
                        assert reservation.replay is not None
                        issue = await uow.rule_check.find_issue(context, issue_id)
                        if issue is None:
                            raise RuleCheckConflict
                        return issue
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
                    if "only open risk issues can be disposed" in str(exc):
                        raise RuleCheckConflict from exc
                    raise RuleCheckInvalidRequest from exc
                if not disposed:
                    raise RuleCheckConflict
                if reservation is not None:
                    assert self._idempotency is not None
                    await self._idempotency.complete(
                        uow.idempotency,
                        reservation,
                        IdempotencyResultReference(
                            _DISPOSE_RESULT_TYPE, issue_id
                        ),
                        now=now,
                    )
                await _append_tenant_audit(
                    uow,
                    context=context,
                    action="risk_issue.dispose",
                    reason_code="disposed",
                    target_type="risk_issue",
                    target_id=issue_id,
                    trace_id=trace_id,
                    now=now,
                )
                # Project the committed disposition without relying on a second read.
                return dispose_risk_issue(
                    issue=issue,
                    status=status,
                    reason=reason,
                    now=now,
                )
        except RuleCheckIssueNotFound as exc:
            await _append_rejected_audit(
                self._uow_factory,
                context=context,
                action="risk_issue.dispose",
                result="denied",
                reason_code=exc.code,
                target_type="risk_issue",
                target_id=issue_id,
                trace_id=trace_id,
                now=now,
            )
            raise
        except RuleCheckConflict as exc:
            await _append_rejected_audit(
                self._uow_factory,
                context=context,
                action="risk_issue.dispose",
                result="denied",
                reason_code=exc.code,
                target_type="risk_issue",
                target_id=issue_id,
                trace_id=trace_id,
                now=now,
            )
            raise
        except RuleCheckInvalidRequest as exc:
            await _append_rejected_audit(
                self._uow_factory,
                context=context,
                action="risk_issue.dispose",
                result="failure",
                reason_code=exc.code,
                target_type="risk_issue",
                target_id=issue_id,
                trace_id=trace_id,
                now=now,
            )
            raise

    async def _reserve_dispose(
        self,
        uow: RuleCheckUnitOfWorkPort,
        *,
        tenant_id: UUID,
        membership_id: UUID,
        issue_id: UUID,
        status: RiskIssueStatus,
        reason: str,
        idempotency_key: str,
        now: datetime,
    ) -> IdempotencyReservation:
        assert self._idempotency is not None
        return await self._idempotency.reserve(
            uow.idempotency,
            scope=IdempotencyScope(
                IdempotencyScopeType.MEMBERSHIP,
                membership_id,
                tenant_id=tenant_id,
            ),
            operation=_DISPOSE_OPERATION,
            request=IdempotencyRequest(
                key=idempotency_key,
                method="POST",
                canonical_route=_DISPOSE_ROUTE,
                body=IdempotencyFingerprintPayload(
                    values={
                        "issue_id": str(issue_id),
                        "status": status.value,
                        "reason": reason,
                    },
                    business_paths=frozenset(
                        {("issue_id",), ("status",), ("reason",)}
                    ),
                ),
            ),
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


async def _append_tenant_audit(
    uow: Any,
    *,
    context: TenantScoped,
    action: str,
    reason_code: str,
    target_type: str,
    target_id: UUID,
    trace_id: str | None,
    now: datetime,
    result: str = "success",
) -> None:
    audit = getattr(uow, "audit", None)
    user_id = getattr(context, "membership_user_id", None)
    membership_id = getattr(context, "membership_id", None)
    if audit is None or user_id is None or membership_id is None:
        return
    if trace_id is None:
        trace_id = "http"
    await audit.append_structured(
        new_tenant_user_audit_event(
            tenant_id=context.tenant_id,
            actor_user_id=user_id,
            actor_membership_id=membership_id,
            action=action,
            reason_code=reason_code,
            trace_id=trace_id,
            target_type=target_type,
            target_id=target_id,
            result=result,
            occurred_at=now,
        )
    )


async def _append_rejected_audit(
    uow_factory: Callable[[], Any],
    *,
    context: TenantScoped,
    action: str,
    result: str,
    reason_code: str,
    target_type: str,
    target_id: UUID,
    trace_id: str | None,
    now: datetime,
) -> None:
    """Record a rejected/failed write attempt in its own committed transaction.

    The failing business UoW has already rolled back when an error handler calls
    this, so the audit append opens a fresh short-lived UoW whose clean exit
    commits only the audit row.
    """
    async with cast(RuleCheckUnitOfWorkPort, uow_factory()) as uow:
        await _append_tenant_audit(
            uow,
            context=context,
            action=action,
            reason_code=reason_code,
            target_type=target_type,
            target_id=target_id,
            trace_id=trace_id,
            now=now,
            result=result,
        )
