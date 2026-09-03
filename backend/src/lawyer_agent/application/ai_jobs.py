from __future__ import annotations

from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from lawyer_agent.application.idempotency import IdempotencyRepositoryPort
from lawyer_agent.application.tenancy import TenantAuditRepositoryPort
from lawyer_agent.domain.ai_jobs import AIJob, JobAccess, NewAIJobGraph
from lawyer_agent.domain.tenancy import TenantContext


class AIJobRepositoryPort(Protocol):
    async def get(
        self,
        context: TenantContext,
        job_id: UUID,
        *,
        for_update: bool = False,
    ) -> AIJob | None: ...

    async def add_job_graph(self, graph: NewAIJobGraph) -> None: ...

    async def get_access(
        self,
        context: TenantContext,
        job_id: UUID,
        membership_id: UUID,
    ) -> JobAccess | None: ...


class AIJobUnitOfWork(Protocol):
    jobs: AIJobRepositoryPort
    runtime: object
    idempotency: IdempotencyRepositoryPort
    audit: TenantAuditRepositoryPort

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...
