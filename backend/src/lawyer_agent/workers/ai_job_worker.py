"""AI Job Worker process.

Consumes signed AI Job reference envelopes from the main RabbitMQ queue,
verifies each signature with the public verify ring, then runs the fixed-order
worker transaction (Inbox dedup, authoritative Claim) in MySQL. It never reads
private message keys and never executes a Handler.

Run: ``python -m lawyer_agent.workers.ai_job_worker``
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import NoReturn

from lawyer_agent.application.ai_job_runtime import (
    AIJobWorkerService,
    DeliveryDecision,
)
from lawyer_agent.application.ai_jobs import DurableGrantPolicy
from lawyer_agent.infrastructure.messaging.delivery import EnvelopeDeliveryCodec, RawDelivery
from lawyer_agent.infrastructure.messaging.envelope import AIJobEnvelope
from lawyer_agent.infrastructure.messaging.rabbitmq import AioPikaJobConsumer
from lawyer_agent.infrastructure.messaging.signing import EnvelopeVerifier, decode_ring
from lawyer_agent.infrastructure.messaging.topology import RabbitTopologyV1
from lawyer_agent.infrastructure.persistence.ai_jobs_uow import SqlAlchemyAIJobUnitOfWork
from lawyer_agent.infrastructure.persistence.engine import (
    create_engine_from_url,
    create_session_factory,
)
from lawyer_agent.infrastructure.persistence.repositories.message_security import (
    RejectionCapacity,
    SecurityRejection,
    observability_ref,
)
from lawyer_agent.runtime.settings import (
    AIJobWorkerSettings,
    read_json_secret_file,
    read_secret_file,
)

_CAPACITY = RejectionCapacity(max_rows=10_000, max_rows_per_day=2_000, sample_rate=100)


def _observability_key(settings: AIJobWorkerSettings) -> bytes:
    raw = read_secret_file(
        settings.observability_reference_key_file,
        field_name="observability_reference_key_file",
    ).encode("utf-8")
    if len(raw) < 32:
        raise ValueError("worker observability reference key must contain at least 32 bytes")
    return raw[:32]


class WorkerProcess:
    """Composition root for the verified AI Job worker loop."""

    def __init__(self, settings: AIJobWorkerSettings) -> None:
        if settings.environment not in {"development", "test", "staging"}:
            raise RuntimeError("ai job worker is forbidden in production")
        amqp_url = read_secret_file(
            settings.rabbit_amqp_url_file, field_name="rabbit_amqp_url_file"
        )
        ring = decode_ring(
            read_json_secret_file(
                settings.message_public_verify_ring_file,
                field_name="message_public_verify_ring_file",
            ),
            field_name="message_public_verify_ring_file",
        )
        self._observability_key = _observability_key(settings)
        self._session_factory = create_session_factory(
            create_engine_from_url(settings.database_url.get_secret_value())
        )
        verifier = EnvelopeVerifier(ring)
        self._consumer = AioPikaJobConsumer(
            amqp_url,
            RabbitTopologyV1(),
            verifier=verifier,
            codec=EnvelopeDeliveryCodec(),
            accept=self._accept,
            on_reject=self._observe_reject,
            prefetch=settings.prefetch,
        )

    async def _accept(self, envelope: AIJobEnvelope, received_at: datetime) -> DeliveryDecision:
        async with SqlAlchemyAIJobUnitOfWork(self._session_factory) as uow:
            service = AIJobWorkerService(
                store=uow.runtime,
                runtime=uow.runtime,
                authority=uow.authority,
                audit=uow.audit,
                policy=DurableGrantPolicy(),
                worker_instance_ref="worker",
                lease_seconds=60,
            )
            return await service.accept_delivery(
                tenant_id=envelope.tenant_id,
                message_id=envelope.message_id,
                job_id=envelope.job_id,
                envelope=envelope,
                now=received_at,
            )

    async def _observe_reject(self, raw: RawDelivery, code: str) -> None:
        rejection = SecurityRejection(
            observability_ref=observability_ref(self._observability_key, raw.body),
            rejection_code=code,
            source_channel="worker",
            received_at=raw.received_at,
        )
        async with SqlAlchemyAIJobUnitOfWork(self._session_factory) as uow:
            await uow.security_rejections.record(rejection, capacity=_CAPACITY)

    async def run(self) -> None:
        await self._consumer.run()

    async def close(self) -> None:
        await self._session_factory.close_all()  # type: ignore[attr-defined]


def main() -> NoReturn:
    settings = AIJobWorkerSettings()  # type: ignore[call-arg]
    process = WorkerProcess(settings)

    async def _run() -> None:
        try:
            await process.run()
        finally:
            await process.close()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    raise SystemExit(0)


if __name__ == "__main__":
    main()
