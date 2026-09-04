from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime

import aio_pika
import orjson
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_ai_job_constraints import JobGraph, _insert_graph
from test_ai_job_migration import _alembic_config

from alembic import command
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.infrastructure.messaging.envelope import AIJobEnvelope, envelope_to_json
from lawyer_agent.infrastructure.messaging.rabbitmq import AioPikaJobConsumer
from lawyer_agent.infrastructure.messaging.signing import EnvelopeSigner, EnvelopeVerifier
from lawyer_agent.infrastructure.messaging.topology import (
    RabbitTopologyV1,
    TopologyInitializer,
)
from lawyer_agent.infrastructure.persistence.models import AIJobAttemptModel

pytestmark = [pytest.mark.integration, pytest.mark.mysql]


def _keypair() -> tuple[EnvelopeSigner, EnvelopeVerifier]:
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
    return EnvelopeSigner({"dev": seed}, active_kid="dev"), EnvelopeVerifier({"dev": public})


def _sign(signer: EnvelopeSigner, *, tenant_id: object, job_id: object) -> AIJobEnvelope:
    nonce = base64.urlsafe_b64encode(b"n" * 16).decode("ascii").rstrip("=")
    signature = "ed25519.dev." + base64.urlsafe_b64encode(b"x" * 64).decode("ascii").rstrip("=")
    unsigned = AIJobEnvelope(
        schema_version=1,
        message_id=new_uuid7(),
        job_id=job_id,
        tenant_id=tenant_id,
        correlation_id=new_uuid7(),
        issued_at=datetime.now(UTC),
        nonce=nonce,
        signature=signature,
    )
    return signer.sign(unsigned)


async def _cleanup_graph(session, graph: JobGraph) -> None:
    from lawyer_agent.infrastructure.persistence.models import (
        AIJobExecutionGrantModel,
        AIJobInboxModel,
        AIJobModel,
        AIJobOutboxModel,
        AuthSessionModel,
        IdempotencyRecordModel,
        MembershipRoleAssignmentModel,
        TenantMembershipModel,
        TenantModel,
        TenantRoleModel,
        TenantRolePermissionModel,
        UserModel,
    )

    await session.execute(
        delete(AIJobInboxModel).where(AIJobInboxModel.tenant_id == graph.tenant_id)
    )
    await session.execute(delete(AIJobAttemptModel).where(AIJobAttemptModel.job_id == graph.job_id))
    await session.execute(delete(AIJobOutboxModel).where(AIJobOutboxModel.job_id == graph.job_id))
    await session.execute(
        delete(AIJobExecutionGrantModel).where(AIJobExecutionGrantModel.job_id == graph.job_id)
    )
    await session.execute(delete(AIJobModel).where(AIJobModel.id == graph.job_id))
    await session.execute(
        delete(IdempotencyRecordModel).where(IdempotencyRecordModel.id == graph.idempotency_id)
    )
    await session.execute(
        delete(AuthSessionModel).where(AuthSessionModel.id == graph.session_id)
    )
    await session.execute(
        delete(MembershipRoleAssignmentModel).where(
            MembershipRoleAssignmentModel.membership_id == graph.membership_id
        )
    )
    await session.execute(
        delete(TenantRolePermissionModel).where(
            TenantRolePermissionModel.tenant_id == graph.tenant_id
        )
    )
    await session.execute(
        delete(TenantRoleModel).where(TenantRoleModel.tenant_id == graph.tenant_id)
    )
    await session.execute(
        delete(TenantMembershipModel).where(TenantMembershipModel.id == graph.membership_id)
    )
    await session.execute(delete(TenantModel).where(TenantModel.id == graph.tenant_id))
    await session.execute(delete(UserModel).where(UserModel.id == graph.user_id))
    await session.commit()


async def _run_consumer_claim(db_url: str, rabbit_vhost) -> None:
    seed = _private_key_seed()
    signer, verifier = _keypair_from_seed(seed)
    topology = RabbitTopologyV1()
    initializer = TopologyInitializer(rabbit_vhost.management, rabbit_vhost.amqp_url)
    await initializer.apply_and_verify(topology)

    engine = create_async_engine(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    graph: JobGraph | None = None
    claimed = asyncio.Event()

    from lawyer_agent.application.ai_job_runtime import (
        AIJobWorkerService,
        DeliveryDecision,
    )
    from lawyer_agent.application.ai_jobs import DurableGrantPolicy
    from lawyer_agent.infrastructure.persistence.ai_jobs_uow import SqlAlchemyAIJobUnitOfWork

    async def accept(envelope: AIJobEnvelope, received_at: datetime) -> DeliveryDecision:
        del received_at
        async with SqlAlchemyAIJobUnitOfWork(factory) as uow:
            service = AIJobWorkerService(
                store=uow.runtime,
                runtime=uow.runtime,
                authority=uow.authority,
                audit=uow.audit,
                policy=DurableGrantPolicy(),
                worker_instance_ref="consumer-test",
                lease_seconds=60,
            )
            decision = await service.accept_delivery(
                tenant_id=envelope.tenant_id,
                message_id=envelope.message_id,
                job_id=envelope.job_id,
                envelope=envelope,
                now=datetime.now(UTC),
            )
            if decision is DeliveryDecision.ACK_CLAIMED:
                claimed.set()
            return decision

    consumer = AioPikaJobConsumer(
        rabbit_vhost.amqp_url,
        topology,
        verifier=verifier,
        accept=accept,
        prefetch=8,
    )
    task = asyncio.create_task(consumer.run())
    try:
        async with factory() as session:
            graph = await _insert_graph(session)
            await session.execute(
                delete(AIJobAttemptModel).where(AIJobAttemptModel.job_id == graph.job_id)
            )
            await _assign_create_role(session, graph)
            await session.commit()

        envelope = _sign(signer, tenant_id=graph.tenant_id, job_id=graph.job_id)
        connection = await aio_pika.connect_robust(rabbit_vhost.amqp_url)
        try:
            channel = await connection.channel(publisher_confirms=True)
            exchange = await channel.get_exchange(topology.main_exchange)
            message = aio_pika.Message(
                body=orjson.dumps(envelope_to_json(envelope)),
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                content_type="application/json",
            )
            await exchange.publish(message, routing_key=topology.execute_key)
        finally:
            await connection.close()

        await asyncio.wait_for(claimed.wait(), timeout=20)
        async with factory() as session:
            attempts = (
                await session.scalars(
                    select(AIJobAttemptModel).where(
                        AIJobAttemptModel.job_id == graph.job_id
                    )
                )
            ).all()
            assert len(attempts) == 1
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        async with factory() as session:
            if graph is not None:
                await _cleanup_graph(session, graph)
        await engine.dispose()


def _private_key_seed() -> bytes:
    private_key = Ed25519PrivateKey.generate()
    return private_key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )


def _keypair_from_seed(seed: bytes) -> tuple[EnvelopeSigner, EnvelopeVerifier]:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private_key = Ed25519PrivateKey.from_private_bytes(seed)
    public = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return EnvelopeSigner({"dev": seed}, active_kid="dev"), EnvelopeVerifier({"dev": public})


async def _assign_create_role(session, graph: JobGraph) -> None:
    from lawyer_agent.domain.common import new_uuid7
    from lawyer_agent.infrastructure.persistence.models import (
        MembershipRoleAssignmentModel,
        PermissionModel,
        RoleTemplateModel,
        TenantRoleModel,
        TenantRolePermissionModel,
    )
    from lawyer_agent.infrastructure.persistence.seed_authz import seed_authorization_catalog

    await seed_authorization_catalog(session)
    template = await session.scalar(
        select(RoleTemplateModel).where(RoleTemplateModel.code == "assistant")
    )
    assert template is not None
    role = TenantRoleModel(
        id=new_uuid7(),
        tenant_id=graph.tenant_id,
        role_template_id=template.id,
        code="assistant",
        name="助理",
        is_custom=False,
        status="active",
    )
    session.add(role)
    await session.flush()
    permission = await session.scalar(
        select(PermissionModel).where(PermissionModel.code == "ai_job.create")
    )
    assert permission is not None
    session.add(
        TenantRolePermissionModel(
            tenant_id=graph.tenant_id,
            tenant_role_id=role.id,
            permission_id=permission.id,
        )
    )
    session.add(
        MembershipRoleAssignmentModel(
            tenant_id=graph.tenant_id,
            membership_id=graph.membership_id,
            tenant_role_id=role.id,
            assigned_by_membership_id=graph.membership_id,
        )
    )
    await session.flush()


def test_consumer_claims_published_envelope(mysql_url, rabbit_vhost) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    asyncio.run(
        _run_consumer_claim(
            mysql_url.render_as_string(hide_password=False), rabbit_vhost
        )
    )
