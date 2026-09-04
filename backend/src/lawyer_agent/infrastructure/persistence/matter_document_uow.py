from __future__ import annotations

from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lawyer_agent.application.documents import DocumentUploadService
from lawyer_agent.infrastructure.objects.object_store import LocalObjectStorePlaceholder
from lawyer_agent.infrastructure.persistence.repositories.audit import AuditRepository
from lawyer_agent.infrastructure.persistence.repositories.documents import (
    SqlAlchemyDocumentRepository,
)
from lawyer_agent.infrastructure.persistence.repositories.idempotency import (
    SqlAlchemyIdempotencyRepository,
)
from lawyer_agent.infrastructure.persistence.repositories.matters import (
    SqlAlchemyMatterRepository,
)


class SqlAlchemyMatterDocumentUnitOfWork:
    """One HTTP matter/document request maps to one session/transaction."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None
        self.matters: SqlAlchemyMatterRepository
        self.documents: SqlAlchemyDocumentRepository
        self.upload: DocumentUploadService
        self.audit: AuditRepository
        self.idempotency: SqlAlchemyIdempotencyRepository

    async def __aenter__(self) -> Self:
        if self._session is not None:
            raise RuntimeError("unit of work is already active")
        self._session = self._session_factory()
        self.documents = SqlAlchemyDocumentRepository(self._session)
        self.matters = SqlAlchemyMatterRepository(self._session)
        self.upload = DocumentUploadService(
            LocalObjectStorePlaceholder(), self.documents
        )
        self.audit = AuditRepository(self._session)
        self.idempotency = SqlAlchemyIdempotencyRepository(self._session)
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
