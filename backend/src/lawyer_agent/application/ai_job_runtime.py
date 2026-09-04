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

from lawyer_agent.application.ai_jobs import DurableGrantPolicy, JobAuthoritySnapshot
from lawyer_agent.application.audit import (
    StructuredAuditRepositoryPort,
)
from lawyer_agent.domain.ai_jobs import (
    DEFAULT_LEASE_SECONDS,
    AIJobStatus,
    ClaimedExecution,
    ClaimJobRequest,
    ClaimRejected,
    FencedFinalizeSuccess,
    FencedHeartbeat,
)
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.infrastructure.messaging.envelope import AIJobEnvelope


class OutboxStatus(StrEnum):
    PENDING = "pending"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    BLOCKED = "blocked"
    SUPERSEDED = "superseded"


class DeliveryDecision(StrEnum):
    """Broker-facing outcome of one worker delivery (never requeues hot messages)."""

    ACK_DUPLICATE = "ack_duplicate"
    ACK_TERMINAL = "ack_terminal"
    ACK_CLAIMED = "ack_claimed"
    REJECT_NO_REQUEUE = "reject_no_requeue"


class InboxDisposition(StrEnum):
    """First-seen result before the authoritative Claim transaction."""

    NEW = "new"
    EXACT_DIGEST_DUPLICATE = "exact_digest_duplicate"
    DIGEST_CONFLICT = "digest_conflict"
    NONCE_CONFLICT = "nonce_conflict"
    CLOCK_OUT_OF_WINDOW = "clock_out_of_window"
    JOB_UNAVAILABLE = "job_unavailable"
    AUTHORIZATION_REVOKED = "authorization_revoked"


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


class AIJobWorkerCodecPort(Protocol):
    def require_size_and_content_type(self, delivery: object) -> object: ...

    def parse_and_require_canonical(self, delivery: object) -> AIJobEnvelope: ...


class AIJobEnvelopeVerifierPort(Protocol):
    def verify(self, envelope: AIJobEnvelope) -> AIJobEnvelope: ...


class WorkerInboxRow(Protocol):
    id: UUID
    status: str
    envelope_digest: bytes
    handling_attempt_id: UUID | None
    handling_fence: int | None


class AIJobWorkerStorePort(Protocol):
    async def inbox_row(
        self, tenant_id: UUID, message_id: UUID
    ) -> WorkerInboxRow | None: ...

    async def insert_inbox_accepted(
        self,
        inbox_id: UUID,
        *,
        tenant_id: UUID,
        message_id: UUID,
        job_id: UUID,
        envelope_digest: bytes,
        nonce_digest: bytes,
        now: datetime,
    ) -> None: ...

    async def complete_inbox_terminal(
        self,
        inbox_id: UUID,
        *,
        rejection_code: str | None,
        now: datetime,
    ) -> bool: ...

    async def bind_inbox_processing(
        self,
        inbox_id: UUID,
        *,
        attempt_id: UUID,
        lease_fence: int,
        now: datetime,
    ) -> bool: ...

    async def fail_job_without_attempt(
        self,
        *,
        tenant_id: UUID,
        job_id: UUID,
        failure_class: str,
        failure_code: str,
        now: datetime,
    ) -> bool: ...


class AIJobWorkerRuntimePort(Protocol):
    async def claim_due_job(
        self, request: ClaimJobRequest
    ) -> ClaimedExecution | ClaimRejected: ...


class AIJobAuthorityPort(Protocol):
    async def load(
        self, tenant_id: UUID, job_id: UUID, *, for_update: bool = False
    ) -> JobAuthoritySnapshot: ...


class AIJobWorkerService:
    """Fixed-order worker: bounded/canonical parse, verify, Inbox dedup, authoritative Claim.

    Never executes a Handler. Structural/canonical/signature failures are
    recorded by the caller-provided rejection hook and mapped to
    REJECT_NO_REQUEUE before any tenant data is read. Before the first Claim the
    durable authority is reloaded from MySQL; a denied grant fails the Job
    without creating an Attempt or Lease.
    """

    def __init__(
        self,
        *,
        store: AIJobWorkerStorePort,
        runtime: AIJobWorkerRuntimePort,
        authority: AIJobAuthorityPort,
        audit: StructuredAuditRepositoryPort,
        policy: DurableGrantPolicy,
        worker_instance_ref: str,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> None:
        if not isinstance(worker_instance_ref, str) or not worker_instance_ref:
            raise ValueError("worker instance ref must be non-empty text")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds < 1
        ):
            raise ValueError("worker lease seconds must be a positive integer")
        if not isinstance(policy, DurableGrantPolicy):
            raise ValueError("worker policy must be strongly typed")
        self._store = store
        self._runtime = runtime
        self._authority = authority
        self._audit = audit
        self._policy = policy
        self._worker_instance_ref = worker_instance_ref
        self._lease_seconds = lease_seconds

    async def accept_delivery(
        self,
        *,
        tenant_id: UUID,
        message_id: UUID,
        job_id: UUID,
        envelope: AIJobEnvelope,
        now: datetime,
    ) -> DeliveryDecision:
        existing = await self._store.inbox_row(tenant_id, message_id)
        if existing is not None:
            if existing.envelope_digest == envelope_digest(envelope):
                return DeliveryDecision.ACK_DUPLICATE
            return DeliveryDecision.REJECT_NO_REQUEUE

        first_seen_digest = envelope_digest(envelope)
        nonce_digest = sha256(envelope.nonce.encode("ascii")).digest()
        inbox_id = new_uuid7()
        await self._store.insert_inbox_accepted(
            inbox_id,
            tenant_id=tenant_id,
            message_id=message_id,
            job_id=job_id,
            envelope_digest=first_seen_digest,
            nonce_digest=nonce_digest,
            now=now,
        )

        authority = await self._authority.load(tenant_id, job_id, for_update=True)
        decision = self._policy.authorize(authority, now=now)
        if not decision.allowed:
            await self._store.complete_inbox_terminal(
                inbox_id,
                rejection_code=decision.reason_code,
                now=now,
            )
            if authority.job.status in (AIJobStatus.QUEUED, AIJobStatus.RETRY_SCHEDULED):
                await self._store.fail_job_without_attempt(
                    tenant_id=tenant_id,
                    job_id=job_id,
                    failure_class="authorization",
                    failure_code=decision.reason_code,
                    now=now,
                )
            return DeliveryDecision.ACK_TERMINAL

        lease_token = new_uuid7()
        attempt_id = new_uuid7()
        attempt_no = authority.job.current_attempt_no + 1
        lease_fence = (authority.job.lease_fence or 0) + 1
        outcome = await self._runtime.claim_due_job(
            ClaimJobRequest(
                tenant_id=tenant_id,
                job_id=job_id,
                worker_instance_ref=self._worker_instance_ref,
                lease_token=lease_token,
                lease_fence=lease_fence,
                attempt_id=attempt_id,
                attempt_no=attempt_no,
                trigger_message_id=message_id,
                handler_code=authority.job.handler_code,
                handler_version=authority.job.handler_version,
                started_at=now,
                lease_expires_at=now + timedelta(seconds=self._lease_seconds),
            )
        )
        if isinstance(outcome, ClaimRejected):
            await self._store.complete_inbox_terminal(
                inbox_id, rejection_code=outcome.reason_code, now=now
            )
            return DeliveryDecision.ACK_TERMINAL
        await self._store.bind_inbox_processing(
            inbox_id,
            attempt_id=outcome.attempt_id,
            lease_fence=outcome.lease_fence,
            now=now,
        )
        return DeliveryDecision.ACK_CLAIMED
