from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from test_ai_job_migration import _alembic_config

from alembic import command
from lawyer_agent.application.permission_rollout import (
    AI_JOB_PERMISSION_MANIFEST,
    PermissionFeatureRolloutService,
    RolloutDirection,
    TenantFeaturePhase,
    WritersDrainedEvidence,
)
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.infrastructure.persistence.models import (
    PermissionFeatureTenantStateModel,
    TenantModel,
    UserModel,
)
from lawyer_agent.infrastructure.persistence.permission_rollout_uow import (
    SqlAlchemyPermissionRolloutUnitOfWork,
)
from lawyer_agent.infrastructure.persistence.seed_authz import seed_authorization_catalog

pytestmark = [pytest.mark.integration, pytest.mark.mysql]

_NOW = datetime(2026, 9, 3, 1, 0, tzinfo=UTC)


def _service(factory: async_sessionmaker[AsyncSession]) -> PermissionFeatureRolloutService:
    return PermissionFeatureRolloutService(
        uow_factory=lambda: SqlAlchemyPermissionRolloutUnitOfWork(factory),
        environment="test",
        manifest=AI_JOB_PERMISSION_MANIFEST,
    )


def _evidence() -> WritersDrainedEvidence:
    return WritersDrainedEvidence("deploy-test-1", datetime.now(UTC))


async def _run_round_trip(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session:
        await seed_authorization_catalog(session)
        await session.commit()
    async with SqlAlchemyPermissionRolloutUnitOfWork(factory) as uow:
        base_gen = (await uow.rollout.load_global()).rollout_generation
    service = _service(factory)
    gen1 = await service.begin_activation(_evidence())
    assert gen1 == base_gen + 1
    await service.finalize(direction=RolloutDirection.ACTIVATE, generation=gen1)
    gen2 = await service.begin_deactivation()
    assert gen2 == gen1 + 1
    await service.finalize(direction=RolloutDirection.DEACTIVATE, generation=gen2)
    gen3 = await service.begin_activation(_evidence())
    assert gen3 == gen2 + 1
    # Return the shared database to catalog_only so later tests start clean.
    await service.finalize(direction=RolloutDirection.ACTIVATE, generation=gen3)
    gen4 = await service.begin_deactivation()
    assert gen4 == gen3 + 1
    await service.finalize(direction=RolloutDirection.DEACTIVATE, generation=gen4)


async def _run_claim_keeps_applied(factory: async_sessionmaker[AsyncSession]) -> None:
    user_id = new_uuid7()
    tenant_id = new_uuid7()
    async with factory() as session:
        await seed_authorization_catalog(session)
        session.add(UserModel(id=user_id, status="active", display_name="u", auth_version=1))
        await session.flush()
        session.add(
            TenantModel(
                id=tenant_id,
                name="t",
                normalized_name=f"t-{tenant_id}",
                tenant_type="enterprise",
                status="active",
                created_by_user_id=user_id,
                review_status="approved",
            )
        )
        await session.commit()

    service = _service(factory)
    proof = await service.reconcile_expand_window(_evidence())
    assert proof.balanced

    async with SqlAlchemyPermissionRolloutUnitOfWork(factory) as uow:
        snapshot = await uow.rollout.claim_tenant(
            tenant_id,
            generation=1,
            manifest=AI_JOB_PERMISSION_MANIFEST,
            direction=RolloutDirection.ACTIVATE,
        )
        assert snapshot.applied_manifest_version == AI_JOB_PERMISSION_MANIFEST.base_version
        assert snapshot.phase is TenantFeaturePhase.CATALOG_ONLY

    # cleanup
    async with factory() as session:
        state = await session.scalar(
            select(PermissionFeatureTenantStateModel).where(
                PermissionFeatureTenantStateModel.tenant_id == tenant_id
            )
        )
        if state is not None:
            await session.delete(state)
        await session.flush()
        tenant = await session.get(TenantModel, tenant_id)
        if tenant is not None:
            await session.delete(tenant)
        await session.flush()
        user = await session.get(UserModel, user_id)
        if user is not None:
            await session.delete(user)
        await session.commit()


def test_same_manifest_can_activate_deactivate_and_activate_again(mysql_url) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    engine = create_async_engine(mysql_url.render_as_string(hide_password=False))
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    asyncio.run(_run_round_trip(factory))
    asyncio.run(engine.dispose())


def test_claim_never_changes_applied_fact(mysql_url) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    engine = create_async_engine(mysql_url.render_as_string(hide_password=False))
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    asyncio.run(_run_claim_keeps_applied(factory))
    asyncio.run(engine.dispose())
