from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_ai_job_constraints import JobGraph, _insert_graph
from test_ai_job_migration import _alembic_config

from alembic import command
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.infrastructure.messaging.envelope import AIJobEnvelope
from lawyer_agent.infrastructure.messaging.rabbitmq import AioPikaPublisher
from lawyer_agent.infrastructure.messaging.signing import EnvelopeSigner
from lawyer_agent.infrastructure.messaging.topology import (
    RabbitTopologyV1,
    TopologyInitializer,
)
from lawyer_agent.infrastructure.persistence.ai_jobs_uow import SqlAlchemyAIJobUnitOfWork
from lawyer_agent.infrastructure.persistence.models import AIJobOutboxModel

pytestmark = [pytest.mark.integration, pytest.mark.mysql]


def _private_key() -> tuple[bytes, bytes]:
    private_key = Ed25519PrivateKey.generate()
    seed = private_key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return seed, public


def _placeholder_signature() -> str:
    encoded = base64.urlsafe_b64encode(b"x" * 64).decode("ascii").rstrip("=")
    return f"ed25519.dev.{encoded}"


def _signer_factory(signer: EnvelopeSigner):
    def _make(candidate) -> AIJobEnvelope:
        nonce = base64.urlsafe_b64encode(b"n" * 16).decode("ascii").rstrip("=")
        unsigned = AIJobEnvelope(
            schema_version=1,
            message_id=new_uuid7(),
            job_id=candidate.job_id,
            tenant_id=candidate.tenant_id,
            correlation_id=candidate.correlation_id,
            issued_at=datetime.now(UTC),
            nonce=nonce,
            signature=_placeholder_signature(),
        )
        return signer.sign(unsigned)

    return _make


async def _run_publisher_check(db_url: str, rabbit_vhost) -> None:
    seed, _public = _private_key()
    signer = EnvelopeSigner({"dev": seed}, active_kid="dev")
    topology = RabbitTopologyV1()
    initializer = TopologyInitializer(rabbit_vhost.management, rabbit_vhost.amqp_url)
    await initializer.apply_and_verify(topology)

    engine = create_async_engine(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    graph: JobGraph | None = None
    try:
        async with factory() as session:
            graph = await _insert_graph(session)
            await session.execute(
                update(AIJobOutboxModel)
                .where(AIJobOutboxModel.job_id == graph.job_id)
                .values(
                    status="pending",
                    message_id=None,
                    nonce=None,
                    signature=None,
                    envelope_digest=None,
                    correlation_id=new_uuid7(),
                )
            )
            await session.commit()

        async with SqlAlchemyAIJobUnitOfWork(factory) as uow:
            claimed = await uow.runtime.claim_outbox_batch(  # type: ignore[attr-defined]
                now=datetime.now(UTC),
                claim_expires_at=datetime.now(UTC) + timedelta(seconds=30),
                worker_ref="publisher",
                limit=10,
                envelope_factory=_signer_factory(signer),
            )
            assert len(claimed) == 1

        publisher = AioPikaPublisher(rabbit_vhost.amqp_url, topology=topology)
        claim = claimed[0]
        receipt = await publisher.publish(claim.envelope, claim.routing_key)
        assert receipt.confirmed is True

        # ai.job.retry.<key> maps to the retry exchange; no queue binds an
        # unknown bucket, so RabbitMQ must return it and never confirm it.
        unroutable = await publisher.publish(claim.envelope, "ai.job.retry.999s")
        assert unroutable.confirmed is False
        assert unroutable.error_code.value == "rabbit_unroutable"
    finally:
        async with factory() as session:
            if graph is not None:
                await session.execute(
                    update(AIJobOutboxModel)
                    .where(AIJobOutboxModel.job_id == graph.job_id)
                    .values(status="superseded")
                )
                await session.commit()
        await engine.dispose()


def test_publisher_confirm_and_mandatory_return(mysql_url, rabbit_vhost) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    asyncio.run(
        _run_publisher_check(mysql_url.render_as_string(hide_password=False), rabbit_vhost)
    )
