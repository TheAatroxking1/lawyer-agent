from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_ai_job_migration import _alembic_config

from alembic import command
from lawyer_agent.domain.authorization import AuthorizationScope
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.rule_pack import RiskLevel, RuleTriggerKind
from lawyer_agent.domain.rule_pack_management import build_pack_rule
from lawyer_agent.domain.tenancy import MembershipStatus, TenantContext, TenantStatus
from lawyer_agent.infrastructure.persistence.repositories.rule_pack import (
    SqlAlchemyRulePackRepository,
)

pytestmark = [pytest.mark.integration, pytest.mark.mysql]


def _context(tenant_id: UUID) -> TenantContext:
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


async def _seed_tenants(mysql_url: URL, factory) -> tuple[UUID, UUID]:
    tenant_a = new_uuid7()
    tenant_b = new_uuid7()
    async with factory() as session:
        for tenant_id in (tenant_a, tenant_b):
            user_id = new_uuid7()
            await session.execute(
                text(
                    "INSERT INTO users (id,status,display_name,auth_version,version) "
                    "VALUES (:id,'active','u',1,1)"
                ),
                {"id": user_id.bytes},
            )
            await session.execute(
                text(
                    "INSERT INTO tenants "
                    "(id,name,normalized_name,tenant_type,status,review_status,"
                    "created_by_user_id,version) "
                    "VALUES (:id,:name,:norm,'enterprise','active','approved',:user,1)"
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


async def _cleanup(mysql_url: URL, tenant_ids: tuple[UUID, UUID]) -> None:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM tenant_contract_risk_issues"))
            await connection.execute(text("DELETE FROM tenant_rule_pack_rules"))
            await connection.execute(text("DELETE FROM tenant_rule_packs"))
            for tenant_id in tenant_ids:
                await connection.execute(
                    text("DELETE FROM tenants WHERE id=:id"), {"id": tenant_id.bytes}
                )
            await connection.execute(
                text(
                    "DELETE FROM users WHERE id NOT IN "
                    "(SELECT created_by_user_id FROM tenants)"
                )
            )
    finally:
        await engine.dispose()


async def _run_admin_checks(mysql_url: URL) -> None:
    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    tenant_a, tenant_b = await _seed_tenants(mysql_url, factory)
    try:
        async with factory() as session:
            repo = SqlAlchemyRulePackRepository(session)
            # Create two same-name packs -> version increments.
            pack_v1 = await repo.create_pack(_context(tenant_a), name="租赁合同")
            pack_v2 = await repo.create_pack(_context(tenant_a), name="租赁合同")
            await session.commit()
            assert pack_v1.version == 1 and pack_v1.active is False
            assert pack_v2.version == 2 and pack_v2.active is False

            # list_packs returns both, newest first.
            listed = await repo.list_packs(_context(tenant_a))
            assert [pack.id for pack in listed] == [pack_v2.id, pack_v1.id]

            # Add a rule to v2 and see it via rules_for_pack.
            rule = build_pack_rule(
                tenant_id=tenant_a,
                pack_id=pack_v2.id,
                trigger_kind=RuleTriggerKind.RISK_PHRASE,
                label="违约金过高",
                pattern=r"(?:百分之[一二三四五六七八九十百0-9]+|[0-9]+\s*%)",
                risk_level=RiskLevel.HIGH,
                suggestion="请人工核验违约金比例。",
                enabled=True,
            )
            stored_rule = await repo.add_rule(_context(tenant_a), rule)
            await session.commit()
            assert stored_rule.id == rule.id
            rules = await repo.rules_for_pack(_context(tenant_a), pack_v2.id)
            assert len(rules) == 1 and rules[0].label == "违约金过高"

            # Activate v2 -> unique active for tenant A.
            assert await repo.activate_pack(_context(tenant_a), pack_v2.id) is True
            await session.commit()
            active = await repo.get_active_pack(_context(tenant_a))
            assert active is not None and active.id == pack_v2.id

            # Disable the rule -> no enabled rules remain.
            assert (
                await repo.set_rule_enabled(
                    _context(tenant_a), pack_v2.id, rule.id, enabled=False
                )
                is True
            )
            await session.commit()
            assert await repo.rules_for_pack(_context(tenant_a), pack_v2.id) == ()

            # Tenant B cannot see A's packs, cannot add into them, cannot
            # activate them, and list is empty.
            assert await repo.list_packs(_context(tenant_b)) == ()
            assert await repo.get_active_pack(_context(tenant_b)) is None
            with pytest.raises(ValueError):
                await repo.add_rule(
                    _context(tenant_b),
                    build_pack_rule(
                        tenant_id=tenant_b,
                        pack_id=pack_v2.id,
                        trigger_kind=RuleTriggerKind.CLAUSE_TYPE,
                        label="越权",
                        pattern="x",
                        risk_level=RiskLevel.LOW,
                        suggestion="x",
                    ),
                )
            assert await repo.activate_pack(_context(tenant_b), pack_v2.id) is False
    finally:
        await engine.dispose()
        await _cleanup(mysql_url, (tenant_a, tenant_b))


def test_rule_pack_admin_repository_over_mysql(mysql_url: URL) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    asyncio.run(_run_admin_checks(mysql_url))
