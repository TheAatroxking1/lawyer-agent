from __future__ import annotations

from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lawyer_agent.infrastructure.persistence.repositories.legal_corpus_read import (
    SqlAlchemyLegalCorpusReadRepository,
)


class SqlAlchemyLegalCorpusReadUnitOfWork:
    """One read-only legal corpus query maps to one short session."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None
        self.corpus: SqlAlchemyLegalCorpusReadRepository

    async def __aenter__(self) -> Self:
        if self._session is not None:
            raise RuntimeError("unit of work is already active")
        self._session = self._session_factory()
        self.corpus = SqlAlchemyLegalCorpusReadRepository(self._session)
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
                await self._session.rollback()
            else:
                await self._session.rollback()
        finally:
            await self._session.close()
            self._session = None
