from __future__ import annotations

from math import isfinite
from types import TracebackType
from typing import Self

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from lawyer_agent.application.idempotency import IdempotencyRepositoryPort
from lawyer_agent.application.platform import (
    BootstrapCommittedWithCleanupWarning,
    PlatformRepositoryPort,
)
from lawyer_agent.application.security_locks import SecurityWriteLockRepositoryPort
from lawyer_agent.application.tenancy import TenantAuditRepositoryPort
from lawyer_agent.infrastructure.persistence.repositories.audit import AuditRepository
from lawyer_agent.infrastructure.persistence.repositories.idempotency import (
    SqlAlchemyIdempotencyRepository,
)
from lawyer_agent.infrastructure.persistence.repositories.platform import PlatformRepository
from lawyer_agent.infrastructure.persistence.repositories.security_locks import (
    SecurityWriteLockRepository,
)

_BOOTSTRAP_LOCK_NAME = "lawyer_platform_bootstrap_admin:v1"
_MAX_LOCK_TIMEOUT_SECONDS = 5.0


class SqlAlchemyPlatformWorkflowUnitOfWork:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        lock_timeout_seconds: float = _MAX_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        if (
            isinstance(lock_timeout_seconds, bool)
            or not isfinite(lock_timeout_seconds)
            or not 0 <= lock_timeout_seconds <= _MAX_LOCK_TIMEOUT_SECONDS
        ):
            raise ValueError("bootstrap lock timeout must be between 0 and 5 seconds")
        bind = session_factory.kw.get("bind")
        if not isinstance(bind, AsyncEngine):
            raise TypeError("platform unit of work requires an AsyncEngine-bound factory")
        self._session_factory = session_factory
        self._lock_engine = bind
        self._lock_timeout_seconds = lock_timeout_seconds
        self._session: AsyncSession | None = None
        self._lock_connection: AsyncConnection | None = None
        self.platform: PlatformRepositoryPort
        self.security_locks: SecurityWriteLockRepositoryPort
        self.idempotency: IdempotencyRepositoryPort
        self.audit: TenantAuditRepositoryPort

    async def __aenter__(self) -> Self:
        if self._session is not None:
            raise RuntimeError("unit of work is already active")
        self._session = self._session_factory()
        self.platform = PlatformRepository(self._session)
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
        committed = False
        try:
            if exc_type is None:
                await self._session.commit()
                committed = True
            else:
                await self._session.rollback()
        except BaseException:
            await self._session.rollback()
            raise
        finally:
            cleanup_failed = False
            try:
                await self._release_bootstrap_lock()
            except BaseException:
                cleanup_failed = True
            try:
                await self._session.close()
            except BaseException:
                cleanup_failed = True
            finally:
                self._session = None
            if committed and cleanup_failed:
                raise BootstrapCommittedWithCleanupWarning from None

    async def acquire_bootstrap_lock(self) -> None:
        if self._session is None:
            raise RuntimeError("unit of work has not been entered")
        if self._lock_connection is not None:
            raise RuntimeError("bootstrap lock is already held")
        connection = await self._lock_engine.connect()
        acquisition_failed = False
        acquired: object = None
        try:
            acquired = await connection.scalar(
                select(text("GET_LOCK(:lock_name, :lock_timeout)")).params(
                    lock_name=_BOOTSTRAP_LOCK_NAME,
                    lock_timeout=self._lock_timeout_seconds,
                )
            )
        except BaseException:
            acquisition_failed = True
        if acquisition_failed or acquired != 1:
            try:
                await connection.close()
            except BaseException:
                acquisition_failed = True
            raise TimeoutError("platform bootstrap lock is unavailable")
        self._lock_connection = connection

    async def _release_bootstrap_lock(self) -> None:
        connection = self._lock_connection
        if connection is None:
            return
        try:
            await connection.scalar(
                select(text("RELEASE_LOCK(:lock_name)")).params(
                    lock_name=_BOOTSTRAP_LOCK_NAME
                )
            )
        finally:
            await connection.close()
            self._lock_connection = None
