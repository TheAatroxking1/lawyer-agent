"""HTTP-facing tenant audit query orchestration (non-model).

Read-only tenant audit listing; every row is filtered by the caller's tenant
and exposed through a safe allowlist (no IP/UA hashes, no metadata).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, cast
from uuid import UUID

from lawyer_agent.domain.common import require_uuid7
from lawyer_agent.domain.tenancy import TenantContext
from lawyer_agent.infrastructure.persistence.repositories.audit import (
    TenantAuditRow,
)


class AuditQueryError(Exception):
    status: int = 500
    code: str = "audit_query_error"
    title: str = "Audit query failed"


class AuditQueryInvalidRequest(AuditQueryError):
    status = 422
    code = "audit_query_invalid_request"
    title = "Audit query request is invalid"


class AuditQueryStorePort(Protocol):
    async def list_for_tenant(
        self,
        tenant_id: UUID,
        *,
        action_prefix: str | None = None,
        target_type: str | None = None,
        trace_id: str | None = None,
        limit: int = 20,
        before_id: UUID | None = None,
    ) -> tuple[TenantAuditRow, ...]: ...


class AuditQueryUnitOfWorkPort(Protocol):
    audit: AuditQueryStorePort

    async def __aenter__(self) -> AuditQueryUnitOfWorkPort: ...

    async def __aexit__(self, *exc: object) -> None: ...


class AuditQueryHttpService:
    """Composition facade used by the tenant audit endpoint."""

    def __init__(self, uow_factory: Callable[[], object]) -> None:
        if not callable(uow_factory):
            raise ValueError("audit query service requires a unit of work factory")
        self._uow_factory = uow_factory

    async def list_audit(
        self,
        *,
        context: TenantContext,
        action_prefix: str | None,
        target_type: str | None,
        trace_id: str | None,
        limit: int,
        before_id: UUID | None,
    ) -> tuple[TenantAuditRow, ...]:
        if action_prefix is not None and not _valid_action_prefix(action_prefix):
            raise AuditQueryInvalidRequest
        if before_id is not None:
            require_uuid7(before_id, field="audit before_id")
        async with cast(AuditQueryUnitOfWorkPort, self._uow_factory()) as uow:
            return await uow.audit.list_for_tenant(
                context.tenant_id,
                action_prefix=action_prefix,
                target_type=target_type,
                trace_id=trace_id,
                limit=limit,
                before_id=before_id,
            )


def _valid_action_prefix(value: str) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if value.endswith("."):
        value = value[:-1]
    allowed = {"tenant", "membership", "department", "role", "invitation", "ai_job",
               "risk_issue", "document", "rule_pack", "matter"}
    return value in allowed
