"""Transactional Outbox Publisher process.

Claims due AI Job outbox rows from MySQL, signs each reference envelope with the
private seed ring, publishes with Publisher Confirm + Mandatory to RabbitMQ and
only then marks the row Published (or Releases/Blocks it on failure). This
process never executes Handlers and never ACKs or Rejects consumer deliveries.

Run: ``python -m lawyer_agent.workers.ai_job_publisher``
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import NoReturn

from lawyer_agent.application.ai_job_runtime import (
    ClaimCandidate,
    EnvelopeFactory,
    OutboxPublisherService,
    PublishBatchResult,
    new_nonce,
)
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.infrastructure.messaging.envelope import AIJobEnvelope
from lawyer_agent.infrastructure.messaging.rabbitmq import AioPikaPublisher
from lawyer_agent.infrastructure.messaging.signing import EnvelopeSigner, decode_ring
from lawyer_agent.infrastructure.messaging.topology import RabbitTopologyV1
from lawyer_agent.infrastructure.persistence.engine import (
    create_engine_from_url,
    create_session_factory,
)
from lawyer_agent.infrastructure.persistence.outbox_claim import (
    SqlAlchemyOutboxClaimAdapter,
)
from lawyer_agent.runtime.settings import (
    AIJobPublisherSettings,
    read_json_secret_file,
    read_secret_file,
)


def _signing_envelope_factory(signer: EnvelopeSigner) -> EnvelopeFactory:
    def _build(candidate: ClaimCandidate) -> AIJobEnvelope:
        if candidate.schema_version != 1:
            raise ValueError("only ai job envelope schema version 1 is supported")
        unsigned = AIJobEnvelope(
            schema_version=1,
            message_id=new_uuid7(),
            job_id=candidate.job_id,
            tenant_id=candidate.tenant_id,
            correlation_id=candidate.correlation_id,
            issued_at=datetime.now(UTC),
            nonce=new_nonce(),
            signature="ed25519.dev." + "p" * 86,
        )
        return signer.sign(unsigned)

    return _build


class PublisherProcess:
    """Composition root for the outbox publisher loop."""

    def __init__(self, settings: AIJobPublisherSettings) -> None:
        if settings.environment not in {"development", "test", "staging"}:
            raise RuntimeError("ai job publisher is forbidden in production")
        amqp_url = read_secret_file(
            settings.rabbit_amqp_url_file, field_name="rabbit_amqp_url_file"
        )
        ring = decode_ring(
            read_json_secret_file(
                settings.message_private_seed_ring_file,
                field_name="message_private_seed_ring_file",
            ),
            field_name="message_private_seed_ring_file",
        )
        signer = EnvelopeSigner(ring, active_kid=settings.message_active_kid)
        topology = RabbitTopologyV1()
        publisher = AioPikaPublisher(
            amqp_url,
            topology=topology,
            confirm_timeout_seconds=settings.confirm_timeout_seconds,
        )
        self._session_factory = create_session_factory(
            create_engine_from_url(settings.database_url.get_secret_value())
        )
        self._service = OutboxPublisherService(
            outbox=SqlAlchemyOutboxClaimAdapter(self._session_factory),
            publisher=publisher,
            envelope_factory=_signing_envelope_factory(signer),
        )
        self._batch_size = settings.batch_size

    async def run_once(self, *, limit: int | None = None) -> PublishBatchResult:
        return await self._service.publish_batch(
            now=datetime.now(UTC), limit=limit or self._batch_size
        )

    async def close(self) -> None:
        await self._session_factory.close_all()  # type: ignore[attr-defined]


def main() -> NoReturn:
    settings = AIJobPublisherSettings()  # type: ignore[call-arg]
    process = PublisherProcess(settings)

    async def _loop() -> None:
        try:
            while True:
                result = await process.run_once()
                if result.claimed == 0:
                    await asyncio.sleep(1.0)
                elif result.failed:
                    await asyncio.sleep(0.25)
        finally:
            await process.close()

    try:
        asyncio.run(_loop())
    except KeyboardInterrupt:
        pass
    raise SystemExit(0)


if __name__ == "__main__":
    main()
