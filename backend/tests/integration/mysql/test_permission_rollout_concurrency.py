from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from test_ai_job_migration import _alembic_config

from alembic import command
from lawyer_agent.application.permission_rollout import (
    AI_JOB_PERMISSION_MANIFEST,
    PermissionFeatureRolloutService,
    RolloutDirection,
    WritersDrainedEvidence,
)
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.infrastructure.persistence.models import (
    PermissionFeatureTenantStateModel,
    PermissionModel,
    TenantModel,
    TenantRoleModel,
    TenantRolePermissionModel,
    UserModel,
)
from lawyer_agent.infrastructure.persistence.permission_rollout_uow import (
    SqlAlchemyPermissionRolloutUnitOfWork,
)
from lawyer_agent.infrastructure.persistence.repositories.tenant_workflows import (
    TenantRoleWorkflowRepository,
)
from lawyer_agent.infrastructure.persistence.seed_authz import seed_authorization_catalog

pytestmark = [pytest.mark.integration, pytest.mark.mysql]


def _service(factory: async_sessionmaker[AsyncSession]) -> PermissionFeatureRolloutService:
    return PermissionFeatureRolloutService(
        uow_factory=lambda: SqlAlchemyPermissionRolloutUnitOfWork(factory),
        environment="test",
        manifest=AI_JOB_PERMISSION_MANIFEST,
    )


def _evidence() -> WritersDrainedEvidence:
    return WritersDrainedEvidence("deploy-test-1", datetime.now(UTC))


async def _assistant_has_ai_job_create(session: AsyncSession, tenant_id) -> bool:
    assistant = await session.scalar(
        select(TenantRoleModel).where(
            TenantRoleModel.tenant_id == tenant_id,
            TenantRoleModel.code == "assistant",
        )
    )
    assert assistant is not None
    row = await session.scalar(
        select(TenantRolePermissionModel.permission_id)
        .join(
            PermissionModel,
            PermissionModel.id == TenantRolePermissionModel.permission_id,
        )
        .where(
            TenantRolePermissionModel.tenant_id == tenant_id,
            TenantRolePermissionModel.tenant_role_id == assistant.id,
            PermissionModel.code == "ai_job.create",
        )
    )
    return row is not None


async def _run_phase_aware_cloning(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session:
        await seed_authorization_catalog(session)
        await session.commit()
    service = _service(factory)
    activation_gen = await service.begin_activation(_evidence())
    await service.finalize(direction=RolloutDirection.ACTIVATE, generation=activation_gen)

    activated_tenant = new_uuid7()
    activated_user = new_uuid7()
    async with factory() as session:
        session.add(UserModel(id=activated_user, status="active", display_name="u", auth_version=1))
        await session.flush()
        session.add(
            TenantModel(
                id=activated_tenant,
                name="a",
                normalized_name=f"a-{activated_tenant}",
                tenant_type="enterprise",
                status="active",
                created_by_user_id=activated_user,
                review_status="approved",
            )
        )
        await session.flush()
        await TenantRoleWorkflowRepository(session).clone_templates_for_tenant(activated_tenant)
        assert await _assistant_has_ai_job_create(session, activated_tenant)
        await session.commit()

    deactivation_gen = await service.begin_deactivation()

    deactivating_tenant = new_uuid7()
    deactivating_user = new_uuid7()
    async with factory() as session:
        session.add(
            UserModel(id=deactivating_user, status="active", display_name="u", auth_version=1)
        )
        await session.flush()
        session.add(
            TenantModel(
                id=deactivating_tenant,
                name="d",
                normalized_name=f"d-{deactivating_tenant}",
                tenant_type="enterprise",
                status="active",
                created_by_user_id=deactivating_user,
                review_status="approved",
            )
        )
        await session.flush()
        await TenantRoleWorkflowRepository(session).clone_templates_for_tenant(
            deactivating_tenant
        )
        assert not await _assistant_has_ai_job_create(session, deactivating_tenant)
        await session.commit()

    async with factory() as session:
        for tenant_id in (activated_tenant, deactivating_tenant):
            await session.execute(
                delete(PermissionFeatureTenantStateModel).where(
                    PermissionFeatureTenantStateModel.tenant_id == tenant_id
                )
            )
            await session.execute(
                delete(TenantRolePermissionModel).where(
                    TenantRolePermissionModel.tenant_id == tenant_id
                )
            )
            await session.execute(
                delete(TenantRoleModel).where(TenantRoleModel.tenant_id == tenant_id)
            )
            await session.flush()
        for tenant_id in (activated_tenant, deactivating_tenant):
            tenant = await session.get(TenantModel, tenant_id)
            if tenant is not None:
                await session.delete(tenant)
        await session.flush()
        for user_id in (activated_user, deactivating_user):
            user = await session.get(UserModel, user_id)
            if user is not None:
                await session.delete(user)
        await session.commit()

    # Return the shared database to catalog_only so later tests start clean.
    await service.finalize(direction=RolloutDirection.DEACTIVATE, generation=deactivation_gen)


def test_phase_aware_tenant_cloning_filters_feature(mysql_url) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    engine = create_async_engine(mysql_url.render_as_string(hide_password=False))
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    asyncio.run(_run_phase_aware_cloning(factory))
    asyncio.run(engine.dispose())
