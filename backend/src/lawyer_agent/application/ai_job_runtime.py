from __future__ import annotations

import base64
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from typing import Protocol
from uuid import UUID

from lawyer_agent.domain.ai_jobs import (
    ClaimedExecution,
    ClaimJobRequest,
    ClaimRejected,
    FencedFinalizeSuccess,
    FencedHeartbeat,
)
from lawyer_agent.infrastructure.messaging.envelope import AIJobEnvelope


class OutboxStatus(StrEnum):
    PENDING = "pending"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    BLOCKED = "blocked"
    SUPERSEDED = "superseded"


class PublisherErrorCode(StrEnum):
    RABBIT_UNROUTABLE = "rabbit_unroutable"
    RABBIT_NACK = "rabbit_nack"
    RABBIT_TIMEOUT = "rabbit_timeout"
    RABBIT_CHANNEL_CLOSED = "rabbit_channel_closed"
    RABBIT_CONNECTION_LOST = "rabbit_connection_lost"
    PUBLISH_EXHAUSTED = "publish_exhausted"


@dataclass(frozen=True, slots=True)
class ClaimCandidate:
    tenant_id: UUID
    job_id: UUID
    correlation_id: UUID
    schema_version: int


EnvelopeFactory = Callable[[ClaimCandidate], AIJobEnvelope]


@dataclass(frozen=True, slots=True)
class ClaimedOutbox:
    id: UUID
    tenant_id: UUID
    job_id: UUID
    routing_key: str
    envelope: AIJobEnvelope
    envelope_digest: bytes
    claim_token: UUID
    claim_fence: int
    version: int
    publish_attempt_count: int
    max_publish_attempts: int


@dataclass(frozen=True, slots=True)
class PublishReceipt:
    confirmed: bool
    error_code: PublisherErrorCode | None = None


@dataclass(frozen=True, slots=True)
class PublishBatchResult:
    claimed: int
    published: int
    failed: int


class AIJobRuntimeRepositoryPort(Protocol):
    async def claim_due_job(
        self, request: ClaimJobRequest
    ) -> ClaimedExecution | ClaimRejected: ...

    async def heartbeat(self, request: FencedHeartbeat) -> bool: ...

    async def finalize_success(self, request: FencedFinalizeSuccess) -> bool: ...


class OutboxClaimPort(Protocol):
    async def claim_outbox_batch(
        self,
        *,
        now: datetime,
        claim_expires_at: datetime,
        worker_ref: str,
        limit: int,
        envelope_factory: EnvelopeFactory,
    ) -> tuple[ClaimedOutbox, ...]: ...

    async def mark_published(
        self,
        claim: ClaimedOutbox,
        *,
        now: datetime,
    ) -> bool: ...

    async def release_or_block(
        self,
        claim: ClaimedOutbox,
        error_code: PublisherErrorCode,
        *,
        now: datetime,
    ) -> bool: ...


class MessagePublisherPort(Protocol):
    async def publish(
        self,
        envelope: AIJobEnvelope,
        routing_key: str,
    ) -> PublishReceipt: ...


class OutboxPublisherService:
    def __init__(
        self,
        *,
        outbox: OutboxClaimPort,
        publisher: MessagePublisherPort,
        envelope_factory: EnvelopeFactory,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._outbox = outbox
        self._publisher = publisher
        self._envelope_factory = envelope_factory
        self._clock = clock

    async def publish_batch(self, *, now: datetime, limit: int) -> PublishBatchResult:
        claimed = await self._outbox.claim_outbox_batch(
            now=now,
            claim_expires_at=now + timedelta(seconds=30),
            worker_ref="publisher",
            limit=limit,
            envelope_factory=self._envelope_factory,
        )
        published = 0
        failed = 0
        for claim in claimed:
            receipt = await self._publisher.publish(claim.envelope, claim.routing_key)
            if receipt.confirmed and await self._outbox.mark_published(claim, now=now):
                published += 1
            else:
                error_code = receipt.error_code or PublisherErrorCode.RABBIT_TIMEOUT
                await self._outbox.release_or_block(claim, error_code, now=now)
                failed += 1
        return PublishBatchResult(len(claimed), published, failed)


def new_nonce() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(16)).decode("ascii").rstrip("=")


def envelope_digest(envelope: AIJobEnvelope) -> bytes:
    return sha256(envelope.canonical_bytes()).digest()
