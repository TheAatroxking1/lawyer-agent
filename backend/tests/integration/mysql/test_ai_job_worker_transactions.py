from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from test_ai_job_constraints import JobGraph, _insert_graph
from test_ai_job_migration import _alembic_config

from alembic import command
from lawyer_agent.application.ai_job_runtime import (
    AIJobWorkerService,
    DeliveryDecision,
)
from lawyer_agent.application.ai_jobs import DurableGrantPolicy
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.infrastructure.messaging.envelope import AIJobEnvelope
from lawyer_agent.infrastructure.messaging.signing import EnvelopeSigner
from lawyer_agent.infrastructure.persistence.ai_jobs_uow import SqlAlchemyAIJobUnitOfWork
from lawyer_agent.infrastructure.persistence.models import (
    AIJobAttemptModel,
    AIJobExecutionGrantModel,
    AIJobInboxModel,
    AIJobModel,
    AIJobOutboxModel,
    AuditEventModel,
    AuthSessionModel,
    IdempotencyRecordModel,
    MembershipRoleAssignmentModel,
    PermissionModel,
    RoleTemplateModel,
    TenantMembershipModel,
    TenantModel,
    TenantRoleModel,
    TenantRolePermissionModel,
    UserModel,
)
from lawyer_agent.infrastructure.persistence.repositories.ai_jobs import (
    SqlAlchemyAIJobRepository,
)
from lawyer_agent.infrastructure.persistence.seed_authz import seed_authorization_catalog

pytestmark = [pytest.mark.integration, pytest.mark.mysql]

_NONCE = base64.urlsafe_b64encode(b"n" * 16).decode("ascii").rstrip("=")
_SIGNATURE = "ed25519.dev." + base64.urlsafe_b64encode(b"x" * 64).decode("ascii").rstrip("=")


def _keypair() -> EnvelopeSigner:
    private_key = Ed25519PrivateKey.generate()
    seed = private_key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    return EnvelopeSigner({"dev": seed}, active_kid="dev")


def _sign(
    signer: EnvelopeSigner,
    *,
    tenant_id: object,
    job_id: object,
    issued_at: datetime,
) -> AIJobEnvelope:
    unsigned = AIJobEnvelope(
        schema_version=1,
        message_id=new_uuid7(),
        job_id=job_id,
        tenant_id=tenant_id,
        correlation_id=new_uuid7(),
        issued_at=issued_at,
        nonce=_NONCE,
        signature=_SIGNATURE,
    )
    return signer.sign(unsigned)


async def _delete_graph(session: AsyncSession, graph: JobGraph) -> None:
    await session.execute(
        delete(AuditEventModel).where(AuditEventModel.target_job_id == graph.job_id)
    )
    await session.execute(
        delete(AIJobInboxModel).where(AIJobInboxModel.tenant_id == graph.tenant_id)
    )
    await session.execute(delete(AIJobOutboxModel).where(AIJobOutboxModel.job_id == graph.job_id))
    await session.execute(delete(AIJobAttemptModel).where(AIJobAttemptModel.job_id == graph.job_id))
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


async def _assign_create_role(session: AsyncSession, graph: JobGraph) -> None:
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


async def _run_first_claim(factory: async_sessionmaker[AsyncSession]) -> None:
    graph: JobGraph | None = None
    try:
        async with factory() as session:
            graph = await _insert_graph(session)
            await session.execute(
                delete(AIJobAttemptModel).where(AIJobAttemptModel.job_id == graph.job_id)
            )
            await _assign_create_role(session, graph)
            await session.commit()

        now = datetime.now(UTC)
        signer = _keypair()
        envelope = _sign(signer, tenant_id=graph.tenant_id, job_id=graph.job_id, issued_at=now)

        async with SqlAlchemyAIJobUnitOfWork(factory) as uow:
            repository: SqlAlchemyAIJobRepository = uow.runtime
            service = AIJobWorkerService(
                store=repository,
                runtime=repository,
                authority=uow.authority,
                audit=uow.audit,
                policy=DurableGrantPolicy(),
                worker_instance_ref="worker-1",
                lease_seconds=60,
            )
            decision = await service.accept_delivery(
                tenant_id=graph.tenant_id,
                message_id=envelope.message_id,
                job_id=graph.job_id,
                envelope=envelope,
                now=now,
            )
            assert decision is DeliveryDecision.ACK_CLAIMED

        async with factory() as session:
            attempts = (
                await session.scalars(
                    select(AIJobAttemptModel).where(
                        AIJobAttemptModel.job_id == graph.job_id
                    )
                )
            ).all()
            assert len(attempts) == 1
            job = await session.scalar(select(AIJobModel).where(AIJobModel.id == graph.job_id))
            assert job is not None
            assert job.status == "running"
            assert job.lease_owner == "worker-1"
            assert job.lease_fence is not None
            inbox = await session.scalar(
                select(AIJobInboxModel).where(
                    AIJobInboxModel.tenant_id == graph.tenant_id,
                    AIJobInboxModel.message_id == envelope.message_id,
                )
            )
            assert inbox is not None
            assert inbox.status == "processing"
            assert inbox.handling_attempt_id == attempts[0].id
            assert inbox.handling_fence == job.lease_fence
    finally:
        async with factory() as session:
            if graph is not None:
                await _delete_graph(session, graph)


def test_worker_first_claim_leases_and_binds_inbox(mysql_url) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    engine = create_async_engine(mysql_url.render_as_string(hide_password=False))
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    asyncio.run(_run_first_claim(factory))
    asyncio.run(engine.dispose())


async def _run_revoked_no_attempt(factory: async_sessionmaker[AsyncSession]) -> None:
    graph: JobGraph | None = None
    try:
        async with factory() as session:
            graph = await _insert_graph(session)
            await session.execute(
                delete(AIJobAttemptModel).where(AIJobAttemptModel.job_id == graph.job_id)
            )
            await session.execute(
                update(AIJobExecutionGrantModel)
                .where(AIJobExecutionGrantModel.job_id == graph.job_id)
                .values(
                    revoked_at=datetime.now(UTC),
                    revocation_reason_code="admin_revocation",
                )
            )
            await session.commit()

        now = datetime.now(UTC)
        signer = _keypair()
        envelope = _sign(signer, tenant_id=graph.tenant_id, job_id=graph.job_id, issued_at=now)

        async with SqlAlchemyAIJobUnitOfWork(factory) as uow:
            repository: SqlAlchemyAIJobRepository = uow.runtime
            service = AIJobWorkerService(
                store=repository,
                runtime=repository,
                authority=uow.authority,
                audit=uow.audit,
                policy=DurableGrantPolicy(),
                worker_instance_ref="worker-1",
                lease_seconds=60,
            )
            decision = await service.accept_delivery(
                tenant_id=graph.tenant_id,
                message_id=envelope.message_id,
                job_id=graph.job_id,
                envelope=envelope,
                now=now,
            )
            assert decision is DeliveryDecision.ACK_TERMINAL

        async with factory() as session:
            attempts = (
                await session.scalars(
                    select(AIJobAttemptModel).where(
                        AIJobAttemptModel.job_id == graph.job_id
                    )
                )
            ).all()
            assert len(attempts) == 0
            job = await session.scalar(select(AIJobModel).where(AIJobModel.id == graph.job_id))
            assert job is not None
            assert job.status == "failed"
            assert job.failure_code == "grant_revoked"
            inbox = await session.scalar(
                select(AIJobInboxModel).where(
                    AIJobInboxModel.tenant_id == graph.tenant_id,
                    AIJobInboxModel.message_id == envelope.message_id,
                )
            )
            assert inbox is not None
            assert inbox.status == "completed"
            assert inbox.handling_attempt_id is None
            assert inbox.handling_fence is None
    finally:
        async with factory() as session:
            if graph is not None:
                await _delete_graph(session, graph)


def test_worker_revoked_grant_never_creates_attempt(mysql_url) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    engine = create_async_engine(mysql_url.render_as_string(hide_password=False))
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    asyncio.run(_run_revoked_no_attempt(factory))
    asyncio.run(engine.dispose())


async def _run_exact_digest_duplicate(factory: async_sessionmaker[AsyncSession]) -> None:
    graph: JobGraph | None = None
    try:
        async with factory() as session:
            graph = await _insert_graph(session)
            await session.execute(
                delete(AIJobAttemptModel).where(AIJobAttemptModel.job_id == graph.job_id)
            )
            await _assign_create_role(session, graph)
            await session.commit()

        now = datetime.now(UTC)
        signer = _keypair()
        envelope = _sign(signer, tenant_id=graph.tenant_id, job_id=graph.job_id, issued_at=now)

        async with SqlAlchemyAIJobUnitOfWork(factory) as uow:
            repository: SqlAlchemyAIJobRepository = uow.runtime
            service = AIJobWorkerService(
                store=repository,
                runtime=repository,
                authority=uow.authority,
                audit=uow.audit,
                policy=DurableGrantPolicy(),
                worker_instance_ref="worker-1",
                lease_seconds=60,
            )
            first = await service.accept_delivery(
                tenant_id=graph.tenant_id,
                message_id=envelope.message_id,
                job_id=graph.job_id,
                envelope=envelope,
                now=now,
            )
            assert first is DeliveryDecision.ACK_CLAIMED

        # Exact-digest duplicate (same signed envelope redelivered much later).
        async with SqlAlchemyAIJobUnitOfWork(factory) as uow:
            repository: SqlAlchemyAIJobRepository = uow.runtime
            service = AIJobWorkerService(
                store=repository,
                runtime=repository,
                authority=uow.authority,
                audit=uow.audit,
                policy=DurableGrantPolicy(),
                worker_instance_ref="worker-1",
                lease_seconds=60,
            )
            duplicate = await service.accept_delivery(
                tenant_id=graph.tenant_id,
                message_id=envelope.message_id,
                job_id=graph.job_id,
                envelope=envelope,
                now=now + timedelta(hours=1),
            )
            assert duplicate is DeliveryDecision.ACK_DUPLICATE

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
        async with factory() as session:
            if graph is not None:
                await _delete_graph(session, graph)


def test_worker_exact_digest_duplicate_is_acked_without_reclaim(mysql_url) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    engine = create_async_engine(mysql_url.render_as_string(hide_password=False))
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    asyncio.run(_run_exact_digest_duplicate(factory))
    asyncio.run(engine.dispose())
