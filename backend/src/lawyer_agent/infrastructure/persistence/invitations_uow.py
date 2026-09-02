from __future__ import annotations

from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lawyer_agent.application.idempotency import IdempotencyRepositoryPort
from lawyer_agent.application.invitations import InvitationRepositoryPort
from lawyer_agent.application.security_locks import SecurityWriteLockRepositoryPort
from lawyer_agent.application.tenancy import (
    TenantAuditRepositoryPort,
    TenantAuthorizationWorkflowRepositoryPort,
)
from lawyer_agent.infrastructure.persistence.repositories.audit import AuditRepository
from lawyer_agent.infrastructure.persistence.repositories.idempotency import (
    SqlAlchemyIdempotencyRepository,
)
from lawyer_agent.infrastructure.persistence.repositories.invitations import (
    InvitationRepository,
)
from lawyer_agent.infrastructure.persistence.repositories.security_locks import (
    SecurityWriteLockRepository,
)
from lawyer_agent.infrastructure.persistence.repositories.tenant_workflows import (
    TenantAuthorizationWorkflowRepository,
)


class SqlAlchemyInvitationWorkflowUnitOfWork:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None
        self.invitations: InvitationRepositoryPort
        self.authorization: TenantAuthorizationWorkflowRepositoryPort
        self.security_locks: SecurityWriteLockRepositoryPort
        self.idempotency: IdempotencyRepositoryPort
        self.audit: TenantAuditRepositoryPort

    async def __aenter__(self) -> Self:
        if self._session is not None:
            raise RuntimeError("unit of work is already active")
        self._session = self._session_factory()
        self.invitations = InvitationRepository(self._session)
        self.authorization = TenantAuthorizationWorkflowRepository(self._session)
        self.security_locks = SecurityWriteLockRepository(self._session)
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
