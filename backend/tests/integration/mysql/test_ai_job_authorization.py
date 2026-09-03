from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from test_ai_job_constraints import JobGraph, _insert_graph
from test_ai_job_migration import _alembic_config

from alembic import command
from lawyer_agent.application.ai_jobs import DurableGrantPolicy
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.infrastructure.persistence.models import (
    AIJobAttemptModel,
    AIJobExecutionGrantModel,
    AIJobModel,
    AIJobOutboxModel,
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
    SqlAlchemyAIJobAuthorityLoader,
)
from lawyer_agent.infrastructure.persistence.seed_authz import seed_authorization_catalog

pytestmark = [pytest.mark.integration, pytest.mark.mysql]

_NOW = datetime(2026, 9, 3, 1, 0, tzinfo=UTC)


def _clock() -> datetime:
    return datetime.now(UTC)


async def _assign_assistant_role(session: AsyncSession, graph: JobGraph) -> None:
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


async def _delete_graph(session: AsyncSession, graph: JobGraph) -> None:
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
    await session.flush()
    await session.execute(delete(TenantModel).where(TenantModel.id == graph.tenant_id))
    await session.flush()
    await session.execute(delete(UserModel).where(UserModel.id == graph.user_id))
    await session.commit()


async def _run_authority_checks(factory: async_sessionmaker[AsyncSession]) -> None:
    graph: JobGraph | None = None
    try:
        async with factory() as session:
            graph = await _insert_graph(session)
            await _assign_assistant_role(session, graph)
            await session.commit()

        async with factory() as session:
            loader = SqlAlchemyAIJobAuthorityLoader(session)
            authority = await loader.load(graph.tenant_id, graph.job_id)
            assert DurableGrantPolicy().authorize(authority, now=_clock()).allowed

        # Revoke the browser session; durable grant must remain valid.
        async with factory() as session:
            await session.execute(
                update(AuthSessionModel)
                .where(AuthSessionModel.id == graph.session_id)
                .values(revoked_at=_NOW, revocation_reason="logout")
            )
            await session.commit()
        async with factory() as session:
            loader = SqlAlchemyAIJobAuthorityLoader(session)
            authority = await loader.load(graph.tenant_id, graph.job_id)
            assert DurableGrantPolicy().authorize(authority, now=_clock()).allowed

        # Bumping the membership authz_version invalidates the durable grant.
        async with factory() as session:
            await session.execute(
                update(TenantMembershipModel)
                .where(TenantMembershipModel.id == graph.membership_id)
                .values(authz_version=TenantMembershipModel.authz_version + 1)
            )
            await session.commit()
        async with factory() as session:
            loader = SqlAlchemyAIJobAuthorityLoader(session)
            authority = await loader.load(graph.tenant_id, graph.job_id)
            decision = DurableGrantPolicy().authorize(authority, now=_clock())
            assert not decision.allowed
            assert decision.reason_code == "authz_version_changed"
    finally:
        async with factory() as session:
            await _delete_graph(session, graph)


def test_durable_grant_authority_ignores_session_and_tracks_authz_version(mysql_url) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    engine = create_async_engine(mysql_url.render_as_string(hide_password=False))
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    asyncio.run(_run_authority_checks(factory))
    asyncio.run(engine.dispose())
