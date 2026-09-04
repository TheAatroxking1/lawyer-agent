from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from test_ai_job_constraints import JobGraph, _insert_graph
from test_ai_job_migration import _alembic_config

from alembic import command
from lawyer_agent.application.ai_job_service import (
    AIJobAccessDenied,
    AIJobNotFound,
    AIJobService,
    CreateAIJobCommand,
)
from lawyer_agent.application.idempotency import IdempotencyService
from lawyer_agent.domain.ai_jobs import AIJobStatus
from lawyer_agent.domain.authorization import AuthorizationScope
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.tenancy import MembershipStatus, TenantContext, TenantStatus
from lawyer_agent.infrastructure.persistence.ai_jobs_uow import SqlAlchemyAIJobUnitOfWork
from lawyer_agent.infrastructure.persistence.models import (
    AIJobExecutionGrantModel,
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

pytestmark = [pytest.mark.integration, pytest.mark.mysql]


async def _assign_create_role(session: AsyncSession, graph: JobGraph) -> None:
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


def _context(graph: JobGraph) -> TenantContext:
    return TenantContext(
        tenant_id=graph.tenant_id,
        membership_id=graph.membership_id,
        membership_user_id=graph.user_id,
        department_id=None,
        tenant_status=TenantStatus.ACTIVE,
        membership_status=MembershipStatus.ACTIVE,
        valid_from=datetime.now(UTC),
        valid_until=None,
        authz_version=1,
        session_authz_version=1,
        scope=AuthorizationScope(allow_tenant_wide=True),
    )


async def _delete_graph(
    session: AsyncSession, graph: JobGraph, *, extra_job_ids: tuple = ()
) -> None:
    from lawyer_agent.infrastructure.persistence.models import AIJobAttemptModel

    job_ids = (graph.job_id, *extra_job_ids)
    await session.execute(
        delete(AIJobOutboxModel).where(AIJobOutboxModel.job_id.in_(job_ids))
    )
    await session.execute(
        delete(AIJobAttemptModel).where(AIJobAttemptModel.job_id.in_(job_ids))
    )
    await session.execute(
        delete(AIJobExecutionGrantModel).where(
            AIJobExecutionGrantModel.job_id.in_(job_ids)
        )
    )
    await session.execute(delete(AIJobModel).where(AIJobModel.id.in_(job_ids)))
    await session.execute(
        delete(IdempotencyRecordModel).where(
            IdempotencyRecordModel.tenant_id == graph.tenant_id,
            IdempotencyRecordModel.scope_id == graph.membership_id,
        )
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


_KEY_SECRET = b"k" * 32


def _idempotency() -> IdempotencyService:
    return IdempotencyService(key_hash_secret=_KEY_SECRET)


async def _run_create_get_cancel(factory: async_sessionmaker[AsyncSession]) -> None:
    graph: JobGraph | None = None
    accepted = None
    try:
        async with factory() as session:
            graph = await _insert_graph(session)
            await _assign_create_role(session, graph)
            await session.commit()

        def uow_factory() -> SqlAlchemyAIJobUnitOfWork:
            return SqlAlchemyAIJobUnitOfWork(factory)

        service = AIJobService(uow_factory, _idempotency(), now=datetime.now(UTC))
        command = CreateAIJobCommand(
            tenant_id=graph.tenant_id,
            membership_id=graph.membership_id,
            user_id=graph.user_id,
            session_id=graph.session_id,
            auth_version=1,
            authz_version=1,
            idempotency_key="create-job-000000000001",
            scenario="success",
            work_units=2,
        )
        accepted = await service.create(command)
        assert accepted.tenant_id == graph.tenant_id

        async with factory() as session:
            job = await session.scalar(
                select(AIJobModel).where(AIJobModel.id == accepted.job_id)
            )
            assert job is not None
            assert job.status == AIJobStatus.QUEUED.value
            outbox = await session.scalar(
                select(AIJobOutboxModel).where(AIJobOutboxModel.job_id == accepted.job_id)
            )
            assert outbox is not None
            assert outbox.routing_key == "ai.job.execute"

        projection = await service.get(_context(graph), accepted.job_id)
        assert projection.job.id == accepted.job_id

        await service.cancel(_context(graph), accepted.job_id)
        async with factory() as session:
            job = await session.scalar(
                select(AIJobModel).where(AIJobModel.id == accepted.job_id)
            )
            assert job is not None
            assert job.status == AIJobStatus.CANCELLED.value
    finally:
        async with factory() as session:
            if graph is not None:
                created_ids = (
                    (accepted.job_id,) if accepted is not None else ()
                )
                await _delete_graph(session, graph, extra_job_ids=created_ids)


async def _run_denied_without_permission(factory: async_sessionmaker[AsyncSession]) -> None:
    graph: JobGraph | None = None
    try:
        async with factory() as session:
            graph = await _insert_graph(session)
            await session.commit()

        def uow_factory() -> SqlAlchemyAIJobUnitOfWork:
            return SqlAlchemyAIJobUnitOfWork(factory)

        service = AIJobService(uow_factory, _idempotency(), now=datetime.now(UTC))
        denied = CreateAIJobCommand(
            tenant_id=graph.tenant_id,
            membership_id=graph.membership_id,
            user_id=graph.user_id,
            session_id=graph.session_id,
            auth_version=1,
            authz_version=1,
            idempotency_key="denied-job-000000000001",
        )
        with pytest.raises(AIJobAccessDenied):
            await service.create(denied)

        missing = new_uuid7()
        with pytest.raises(AIJobNotFound):
            await service.get(_context(graph), missing)
    finally:
        async with factory() as session:
            if graph is not None:
                await _delete_graph(session, graph)


def test_ai_job_service_create_get_cancel(mysql_url) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    engine = create_async_engine(mysql_url.render_as_string(hide_password=False))
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    asyncio.run(_run_create_get_cancel(factory))
    asyncio.run(engine.dispose())


def test_ai_job_service_denies_without_create_permission(mysql_url) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    engine = create_async_engine(mysql_url.render_as_string(hide_password=False))
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    asyncio.run(_run_denied_without_permission(factory))
    asyncio.run(engine.dispose())
