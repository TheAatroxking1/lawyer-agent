from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from test_ai_job_constraints import JobGraph, _insert_graph
from test_ai_job_migration import _alembic_config

from alembic import command
from lawyer_agent.domain.ai_jobs import (
    AIJobStatus,
    ClaimJobRequest,
    ClaimRejected,
    FencedFinalizeSuccess,
)
from lawyer_agent.domain.authorization import AuthorizationScope
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.tenancy import MembershipStatus, TenantContext, TenantStatus
from lawyer_agent.infrastructure.persistence.ai_jobs_uow import SqlAlchemyAIJobUnitOfWork
from lawyer_agent.infrastructure.persistence.models import (
    AIJobAttemptModel,
    AIJobExecutionGrantModel,
    AIJobModel,
    AIJobOutboxModel,
    AuthSessionModel,
    IdempotencyRecordModel,
    TenantMembershipModel,
    TenantModel,
    UserModel,
)

pytestmark = [pytest.mark.integration, pytest.mark.mysql]

_NOW = datetime(2026, 9, 3, 1, 0, tzinfo=UTC)


async def _delete_graphs(session: AsyncSession, graphs: tuple[JobGraph, ...]) -> None:
    for graph in graphs:
        await session.execute(
            delete(AIJobOutboxModel).where(AIJobOutboxModel.job_id == graph.job_id)
        )
        await session.execute(
            delete(AIJobAttemptModel).where(AIJobAttemptModel.job_id == graph.job_id)
        )
        await session.execute(
            delete(AIJobExecutionGrantModel).where(
                AIJobExecutionGrantModel.job_id == graph.job_id
            )
        )
        await session.execute(delete(AIJobModel).where(AIJobModel.id == graph.job_id))
        await session.execute(
            delete(IdempotencyRecordModel).where(
                IdempotencyRecordModel.id == graph.idempotency_id
            )
        )
        await session.execute(
            delete(AuthSessionModel).where(AuthSessionModel.id == graph.session_id)
        )
        await session.execute(
            delete(TenantMembershipModel).where(
                TenantMembershipModel.id == graph.membership_id
            )
        )
        await session.execute(delete(TenantModel).where(TenantModel.id == graph.tenant_id))
        await session.execute(delete(UserModel).where(UserModel.id == graph.user_id))
    await session.commit()


def _context(graph: JobGraph) -> TenantContext:
    return TenantContext(
        tenant_id=graph.tenant_id,
        membership_id=graph.membership_id,
        membership_user_id=graph.user_id,
        department_id=None,
        tenant_status=TenantStatus.ACTIVE,
        membership_status=MembershipStatus.ACTIVE,
        valid_from=_NOW - timedelta(days=1),
        valid_until=_NOW + timedelta(days=1),
        authz_version=1,
        session_authz_version=1,
        scope=AuthorizationScope(allow_tenant_wide=True),
    )


async def _lease_job(session: AsyncSession, graph: JobGraph) -> tuple[int, object]:
    fence = 2
    lease_token = new_uuid7()
    await session.execute(
        update(AIJobModel)
        .where(
            AIJobModel.tenant_id == graph.tenant_id,
            AIJobModel.id == graph.job_id,
        )
        .values(
            status="running",
            lease_owner="worker-1",
            lease_token=lease_token,
            lease_fence=fence,
            lease_expires_at=_NOW + timedelta(seconds=60),
            current_attempt_no=1,
            started_at=_NOW,
            version=AIJobModel.version + 1,
        )
    )
    await session.execute(
        update(AIJobAttemptModel)
        .where(
            AIJobAttemptModel.tenant_id == graph.tenant_id,
            AIJobAttemptModel.job_id == graph.job_id,
        )
        .values(
            status="claimed",
            lease_token=lease_token,
            lease_fence=fence,
        )
    )
    await session.flush()
    return fence, lease_token


async def _run_repository_checks(factory: async_sessionmaker[AsyncSession]) -> None:
    graph_a: JobGraph | None = None
    graph_b: JobGraph | None = None
    try:
        async with factory() as session:
            graph_a = await _insert_graph(session)
            graph_b = await _insert_graph(session)
            await session.commit()

        async with SqlAlchemyAIJobUnitOfWork(factory) as uow:
            assert await uow.jobs.get(_context(graph_a), graph_b.job_id) is None
            assert await uow.jobs.get(_context(graph_a), graph_a.job_id) is not None

        async with factory() as session:
            fence, lease_token = await _lease_job(session, graph_a)
            await session.commit()

        async with SqlAlchemyAIJobUnitOfWork(factory) as uow:
            stale = await uow.runtime.finalize_success(
                FencedFinalizeSuccess(
                    tenant_id=graph_a.tenant_id,
                    job_id=graph_a.job_id,
                    lease_token=lease_token,
                    lease_fence=fence - 1,
                    expected_version=2,
                    result={"result_code": "synthetic_success"},
                    now=_NOW + timedelta(seconds=10),
                )
            )
            assert stale is False
            current = await uow.runtime.finalize_success(
                FencedFinalizeSuccess(
                    tenant_id=graph_a.tenant_id,
                    job_id=graph_a.job_id,
                    lease_token=lease_token,
                    lease_fence=fence,
                    expected_version=2,
                    result={"result_code": "synthetic_success"},
                    now=_NOW + timedelta(seconds=10),
                )
            )
            assert current is True
    finally:
        async with factory() as session:
            await _delete_graphs(
                session,
                tuple(graph for graph in (graph_a, graph_b) if graph is not None),
            )


async def _run_claim_check(factory: async_sessionmaker[AsyncSession]) -> None:
    graph: JobGraph | None = None
    try:
        async with factory() as session:
            graph = await _insert_graph(session)
            await session.execute(
                delete(AIJobAttemptModel).where(AIJobAttemptModel.job_id == graph.job_id)
            )
            await session.commit()

        lease_token = new_uuid7()
        attempt_id = new_uuid7()
        message_id = new_uuid7()
        async with SqlAlchemyAIJobUnitOfWork(factory) as uow:
            outcome = await uow.runtime.claim_due_job(
                ClaimJobRequest(
                    tenant_id=graph.tenant_id,
                    job_id=graph.job_id,
                    worker_instance_ref="worker-9",
                    lease_token=lease_token,
                    lease_fence=7,
                    attempt_id=attempt_id,
                    attempt_no=1,
                    trigger_message_id=message_id,
                    handler_code="synthetic.v1",
                    handler_version="v1",
                    started_at=_NOW + timedelta(seconds=5),
                    lease_expires_at=_NOW + timedelta(seconds=65),
                )
            )
            assert not isinstance(outcome, ClaimRejected)
            assert outcome.lease_fence == 7

        async with factory() as session:
            attempt = await session.scalar(
                select(AIJobAttemptModel).where(AIJobAttemptModel.id == attempt_id)
            )
            assert attempt is not None
            job = await session.scalar(
                select(AIJobModel).where(AIJobModel.id == graph.job_id)
            )
            assert job is not None and job.status == AIJobStatus.RUNNING.value
    finally:
        async with factory() as session:
            await _delete_graphs(session, (graph,) if graph is not None else ())


def test_repository_tenant_boundaries_and_fencing(mysql_url) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    engine = create_async_engine(mysql_url.render_as_string(hide_password=False))
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    asyncio.run(_run_repository_checks(factory))
    asyncio.run(engine.dispose())


def test_claim_due_job_creates_attempt_and_leases(mysql_url) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    engine = create_async_engine(mysql_url.render_as_string(hide_password=False))
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    asyncio.run(_run_claim_check(factory))
    asyncio.run(engine.dispose())
