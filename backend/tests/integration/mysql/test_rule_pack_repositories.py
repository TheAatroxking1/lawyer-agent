from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import text, update
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_ai_job_migration import _alembic_config

from alembic import command
from lawyer_agent.domain.authorization import AuthorizationScope
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.rule_pack import RiskIssueStatus
from lawyer_agent.domain.tenancy import MembershipStatus, TenantContext, TenantStatus
from lawyer_agent.infrastructure.persistence.models.rule_pack import TenantRulePackModel
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


async def _seed_tenant_with_pack(
    mysql_url: URL, factory, name: str
) -> tuple[UUID, UUID, UUID, UUID]:
    """Seed one tenant with matter/document/active pack/rule/issue."""
    tenant_id = new_uuid7()
    pack_id = new_uuid7()
    rule_id = new_uuid7()
    issue_id = new_uuid7()
    async with factory() as session:
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
                "name": name,
                "norm": name.lower(),
                "user": user_id.bytes,
            },
        )
        matter_id = new_uuid7()
        await session.execute(
            text(
                "INSERT INTO tenant_matters "
                "(id,tenant_id,title,kind,status,created_by_user_id,"
                "created_by_membership_id,version) "
                "VALUES (:id,:tenant,:title,'contract_review','open',:u,:m,1)"
            ),
            {
                "id": matter_id.bytes,
                "tenant": tenant_id.bytes,
                "title": f"{name} matter",
                "u": new_uuid7().bytes,
                "m": new_uuid7().bytes,
            },
        )
        document_id = new_uuid7()
        await session.execute(
            text(
                "INSERT INTO tenant_documents "
                "(id,tenant_id,matter_id,display_name,current_version_no,version) "
                "VALUES (:id,:tenant,:matter,'doc',1,1)"
            ),
            {"id": document_id.bytes, "tenant": tenant_id.bytes, "matter": matter_id.bytes},
        )
        await session.execute(
            text(
                "INSERT INTO tenant_rule_packs "
                "(id,tenant_id,name,version,active) VALUES (:id,:tenant,:name,1,1)"
            ),
            {"id": pack_id.bytes, "tenant": tenant_id.bytes, "name": name},
        )
        await session.execute(
            text(
                "INSERT INTO tenant_rule_pack_rules "
                "(id,tenant_id,pack_id,trigger_kind,label,pattern,risk_level,"
                "suggestion_template,enabled) "
                "VALUES (:id,:tenant,:pack,'risk_phrase','x','违约金',"
                "'high','s',1)"
            ),
            {"id": rule_id.bytes, "tenant": tenant_id.bytes, "pack": pack_id.bytes},
        )
        await session.execute(
            text(
                "INSERT INTO tenant_contract_risk_issues "
                "(id,tenant_id,document_id,rule_id,pack_id,pack_version,provision_no,"
                "matched_text,risk_level,status,evidence_level) "
                "VALUES (:id,:tenant,:doc,:rule,:pack,1,'第一条','违约金30%',"
                "'high','open','rule_based')"
            ),
            {
                "id": issue_id.bytes,
                "tenant": tenant_id.bytes,
                "doc": document_id.bytes,
                "rule": rule_id.bytes,
                "pack": pack_id.bytes,
            },
        )
        await session.commit()
    return tenant_id, pack_id, rule_id, issue_id


async def _cleanup(mysql_url: URL, tenant_ids: list[UUID]) -> None:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM tenant_contract_risk_issues"))
            await connection.execute(text("DELETE FROM tenant_rule_pack_rules"))
            await connection.execute(text("DELETE FROM tenant_rule_packs"))
            await connection.execute(text("DELETE FROM tenant_document_versions"))
            await connection.execute(text("DELETE FROM tenant_documents"))
            await connection.execute(text("DELETE FROM tenant_matter_parties"))
            for tenant_id in tenant_ids:
                await connection.execute(
                    text("DELETE FROM tenant_matters WHERE tenant_id=:id"),
                    {"id": tenant_id.bytes},
                )
            for tenant_id in tenant_ids:
                await connection.execute(
                    text("DELETE FROM tenants WHERE id=:id"),
                    {"id": tenant_id.bytes},
                )
            await connection.execute(
                text(
                    "DELETE FROM users WHERE id NOT IN "
                    "(SELECT created_by_user_id FROM tenants)"
                )
            )
    finally:
        await engine.dispose()


async def _run_repository_checks(mysql_url: URL) -> None:
    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    tenant_a, pack_a, _rule_a, issue_a = await _seed_tenant_with_pack(
        mysql_url, factory, "pack-a"
    )
    tenant_b, pack_b, _rule_b, issue_b = await _seed_tenant_with_pack(
        mysql_url, factory, "pack-b"
    )
    try:
        async with factory() as session:
            repo = SqlAlchemyRulePackRepository(session)
            # Same-tenant reads succeed for each tenant's own active pack.
            own_a = await repo.get_active_pack(_context(tenant_a))
            assert own_a is not None and own_a.id == pack_a
            own_b = await repo.get_active_pack(_context(tenant_b))
            assert own_b is not None and own_b.id == pack_b
            # Rules are only visible for the tenant's own pack id.
            assert await repo.rules_for_pack(_context(tenant_a), pack_a)
            assert await repo.rules_for_pack(_context(tenant_a), pack_b) == ()
            # A tenant cannot read the other tenant's issue.
            assert await repo.find_issue(_context(tenant_a), issue_b) is None
            assert await repo.find_issue(_context(tenant_b), issue_a) is None
            assert (await repo.find_issue(_context(tenant_a), issue_a)) is not None
            # Cross-tenant disposition attempt must not touch the row.
            changed = await repo.dispose_issue(
                tenant_id=tenant_b,
                issue_id=issue_a,
                status=RiskIssueStatus.REJECTED,
                reason="x",
                now=datetime.now(UTC),
            )
            assert changed is False
            await session.commit()
            still_open = await repo.find_issue(_context(tenant_a), issue_a)
            assert still_open is not None and still_open.status is RiskIssueStatus.OPEN

            # Same-tenant disposition succeeds.
            disposed = await repo.dispose_issue(
                tenant_id=tenant_a,
                issue_id=issue_a,
                status=RiskIssueStatus.ACCEPTED,
                reason="律师确认",
                now=datetime.now(UTC),
            )
            assert disposed is True
            await session.commit()
            accepted = await repo.find_issue(_context(tenant_a), issue_a)
            assert accepted is not None and accepted.status is RiskIssueStatus.ACCEPTED

        # Deactivating a pack hides it, so no new issues can be raised on it.
        async with factory() as session:
            await session.execute(
                update(TenantRulePackModel)
                .where(
                    TenantRulePackModel.tenant_id == tenant_a,
                    TenantRulePackModel.id == pack_a,
                )
                .values(active=False)
            )
            await session.commit()
        async with factory() as session:
            repo = SqlAlchemyRulePackRepository(session)
            assert await repo.get_active_pack(_context(tenant_a)) is None
            # Tenant B's pack stays active and readable.
            own_b = await repo.get_active_pack(_context(tenant_b))
            assert own_b is not None and own_b.id == pack_b
    finally:
        await engine.dispose()
        await _cleanup(mysql_url, [tenant_a, tenant_b])


def test_rule_pack_repository_cross_tenant_reverse_isolation(mysql_url: URL) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    asyncio.run(_run_repository_checks(mysql_url))
