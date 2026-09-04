from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lawyer_agent.application.ai_job_runtime import (
    ClaimedOutbox,
    EnvelopeFactory,
    OutboxClaimPort,
    PublisherErrorCode,
)
from lawyer_agent.infrastructure.persistence.ai_jobs_uow import SqlAlchemyAIJobUnitOfWork
from lawyer_agent.infrastructure.persistence.repositories.ai_jobs import (
    SqlAlchemyAIJobRepository,
)


class SqlAlchemyOutboxClaimAdapter(OutboxClaimPort):
    """Outbox claim port over a session factory.

    Each operation commits in its own transaction: the claim must be durable
    before the network publish, and mark/release must be fenced by the claim
    token so a late confirm cannot touch a superseded row.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def claim_outbox_batch(
        self,
        *,
        now: datetime,
        claim_expires_at: datetime,
        worker_ref: str,
        limit: int,
        envelope_factory: EnvelopeFactory,
    ) -> tuple[ClaimedOutbox, ...]:
        async with SqlAlchemyAIJobUnitOfWork(self._session_factory) as uow:
            repository: SqlAlchemyAIJobRepository = uow.runtime
            return await repository.claim_outbox_batch(
                now=now,
                claim_expires_at=claim_expires_at,
                worker_ref=worker_ref,
                limit=limit,
                envelope_factory=envelope_factory,
            )

    async def mark_published(
        self,
        claim: ClaimedOutbox,
        *,
        now: datetime,
    ) -> bool:
        async with SqlAlchemyAIJobUnitOfWork(self._session_factory) as uow:
            repository: SqlAlchemyAIJobRepository = uow.runtime
            return await repository.mark_published(claim, now=now)

    async def release_or_block(
        self,
        claim: ClaimedOutbox,
        error_code: PublisherErrorCode,
        *,
        now: datetime,
    ) -> bool:
        async with SqlAlchemyAIJobUnitOfWork(self._session_factory) as uow:
            repository: SqlAlchemyAIJobRepository = uow.runtime
            return await repository.release_or_block(claim, error_code, now=now)
