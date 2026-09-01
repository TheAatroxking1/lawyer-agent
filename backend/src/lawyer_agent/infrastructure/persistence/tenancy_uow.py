from __future__ import annotations

from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lawyer_agent.infrastructure.persistence.repositories.audit import AuditRepository
from lawyer_agent.infrastructure.persistence.repositories.idempotency import (
    SqlAlchemyIdempotencyRepository,
)
from lawyer_agent.infrastructure.persistence.repositories.tenant_workflows import (
    MembershipWorkflowRepository,
    TenantApplicationRepository,
    TenantAuthorizationWorkflowRepository,
    TenantRoleWorkflowRepository,
    TenantSessionRevocationRepository,
)


class SqlAlchemyTenantWorkflowUnitOfWork:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None
        self.tenants: TenantApplicationRepository
        self.memberships: MembershipWorkflowRepository
        self.roles: TenantRoleWorkflowRepository
        self.authorization: TenantAuthorizationWorkflowRepository
        self.sessions: TenantSessionRevocationRepository
        self.idempotency: SqlAlchemyIdempotencyRepository
        self.audit: AuditRepository

    async def __aenter__(self) -> Self:
        if self._session is not None:
            raise RuntimeError("unit of work is already active")
        self._session = self._session_factory()
        self.tenants = TenantApplicationRepository(self._session)
        self.memberships = MembershipWorkflowRepository(self._session)
        self.roles = TenantRoleWorkflowRepository(self._session)
        self.authorization = TenantAuthorizationWorkflowRepository(self._session)
        self.sessions = TenantSessionRevocationRepository(self._session)
        self.idempotency = SqlAlchemyIdempotencyRepository(self._session)
        self.audit = AuditRepository(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_value, traceback
        if self._session is None:
            raise RuntimeError("unit of work has not been entered")
        try:
            if exc_type is None:
                await self._session.commit()
            else:
                await self._session.rollback()
        except BaseException:
            await self._session.rollback()
            raise
        finally:
            await self._session.close()
            self._session = None
