from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_ai_job_migration import _alembic_config

from alembic import command
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.infrastructure.persistence.models import (
    PermissionFeatureTenantStateModel,
    TenantModel,
    UserModel,
)
from lawyer_agent.infrastructure.persistence.repositories.security_locks import (
    SecurityWriteLockRepository,
)

pytestmark = [pytest.mark.integration, pytest.mark.mysql]


async def _run_feature_lock_checks(mysql_url: str) -> None:
    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    user_id = new_uuid7()
    tenant_id = new_uuid7()
    try:
        async with factory() as session:
            session.add(UserModel(id=user_id, status="active", display_name="u", auth_version=1))
            await session.flush()
            session.add(
                TenantModel(
                    id=tenant_id,
                    name="feature-tenant",
                    normalized_name=f"feature-{tenant_id}",
                    tenant_type="enterprise",
                    status="active",
                    created_by_user_id=user_id,
                    review_status="approved",
                )
            )
            await session.commit()

        async with factory() as session:
            locks = SecurityWriteLockRepository(session)
            snapshot = await locks.lock_global_feature_for_tenant_create()
            assert snapshot.feature_code == "ai_job_runtime_v1"
            assert snapshot.phase == "catalog_only"
            assert snapshot.rollout_generation == 0
            assert snapshot.manifest_version == "ai-job-base-v1"
            assert len(snapshot.manifest_digest) == 32
            assert snapshot.version == 1

        async with factory() as session:
            locks = SecurityWriteLockRepository(session)
            with pytest.raises(ValueError, match="missing"):
                await locks.lock_tenant_feature_shared(tenant_id)

        async with factory() as session:
            session.add(
                PermissionFeatureTenantStateModel(
                    id=new_uuid7(),
                    feature_code="ai_job_runtime_v1",
                    tenant_id=tenant_id,
                    phase="catalog_only",
                    target_manifest_version="ai-job-base-v1",
                    applied_manifest_version="ai-job-base-v1",
                    target_rollout_generation=0,
                    applied_rollout_generation=0,
                )
            )
            await session.commit()

        async with factory() as session:
            locks = SecurityWriteLockRepository(session)
            snapshot = await locks.lock_tenant_feature_shared(tenant_id)
            assert snapshot.tenant_id == tenant_id
            assert snapshot.phase == "catalog_only"
            assert snapshot.applied_manifest_version == "ai-job-base-v1"
            assert snapshot.applied_rollout_generation == 0
            assert snapshot.version == 1
    finally:
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
        await engine.dispose()


def test_feature_lock_ports_enforce_authoritative_snapshots(mysql_url) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    asyncio.run(_run_feature_lock_checks(mysql_url.render_as_string(hide_password=False)))
