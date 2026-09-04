from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_ai_job_migration import _alembic_config

from alembic import command
from lawyer_agent.domain.authorization import AuthorizationScope
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.matter_documents import MatterKind
from lawyer_agent.domain.tenancy import MembershipStatus, TenantContext, TenantStatus
from lawyer_agent.infrastructure.persistence.repositories.matters import (
    SqlAlchemyMatterRepository,
)

pytestmark = [pytest.mark.integration, pytest.mark.mysql]


def _context(tenant_id) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        membership_id=new_uuid7(),
        membership_user_id=new_uuid7(),
        department_id=None,
        tenant_status=TenantStatus.ACTIVE,
        membership_status=MembershipStatus.ACTIVE,
        valid_from=datetime.now(UTC),
        valid_until=None,
        authz_version=1,
        session_authz_version=1,
        scope=AuthorizationScope(allow_tenant_wide=False),
    )


async def _seed_tenants(mysql_url: URL, factory) -> tuple:
    tenant_a = new_uuid7()
    tenant_b = new_uuid7()
    async with factory() as session:
        for tenant_id in (tenant_a, tenant_b):
            user_id = new_uuid7()
            await session.execute(
                text(
                    "INSERT INTO users (id, status, display_name, auth_version, version) "
                    "VALUES (:id, 'active', 'u', 1, 1)"
                ),
                {"id": user_id.bytes},
            )
            await session.execute(
                text(
                    "INSERT INTO tenants "
                    "(id, name, normalized_name, tenant_type, status, review_status, "
                    "created_by_user_id, version) "
                    "VALUES (:id, :name, :norm, 'enterprise', 'active', 'approved', "
                    " :user, 1)"
                ),
                {
                    "id": tenant_id.bytes,
                    "name": f"t-{tenant_id}",
                    "norm": f"t-{tenant_id}".lower(),
                    "user": user_id.bytes,
                },
            )
        await session.commit()
    return tenant_a, tenant_b


async def _cleanup(mysql_url: URL, tenant_ids: tuple) -> None:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM tenant_document_versions"))
            await connection.execute(text("DELETE FROM tenant_documents"))
            await connection.execute(text("DELETE FROM tenant_matter_parties"))
            for tenant_id in tenant_ids:
                await connection.execute(
                    text("DELETE FROM tenant_matters WHERE tenant_id = :id"),
                    {"id": tenant_id.bytes},
                )
            for tenant_id in tenant_ids:
                await connection.execute(
                    text("DELETE FROM tenants WHERE id = :id"),
                    {"id": tenant_id.bytes},
                )
    finally:
        await engine.dispose()


async def _run_tenant_isolation_checks(mysql_url: URL) -> None:
    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    tenant_a, tenant_b = await _seed_tenants(mysql_url, factory)
    try:
        async with factory() as session:
            repo = SqlAlchemyMatterRepository(session)
            matter_a = await repo.create_matter(
                tenant_id=tenant_a,
                title="A 租户合同审查",
                kind=MatterKind.CONTRACT_REVIEW,
                created_by_user_id=new_uuid7(),
                created_by_membership_id=new_uuid7(),
            )
            matter_b = await repo.create_matter(
                tenant_id=tenant_b,
                title="B 租户诉讼",
                kind=MatterKind.LITIGATION,
                created_by_user_id=new_uuid7(),
                created_by_membership_id=new_uuid7(),
            )
            await session.commit()

        async with factory() as session:
            repo = SqlAlchemyMatterRepository(session)
            # Same-tenant reads succeed.
            own = await repo.get_matter(_context(tenant_a), matter_a.id)
            assert own is not None and own.title == "A 租户合同审查"
            # Cross-tenant read must be invisible (reverse isolation).
            foreign = await repo.get_matter(_context(tenant_a), matter_b.id)
            assert foreign is None
    finally:
        await _cleanup(mysql_url, (tenant_a, tenant_b))
        await engine.dispose()


def test_matter_repository_cross_tenant_reverse_isolation(mysql_url: URL) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    asyncio.run(_run_tenant_isolation_checks(mysql_url))
