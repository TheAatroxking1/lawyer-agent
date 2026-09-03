from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID

import pytest
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from lawyer_agent.domain.common import new_uuid7

pytestmark = [pytest.mark.integration, pytest.mark.mysql]

AI_JOB_TABLES = {
    "ai_job_access_grants",
    "ai_job_attempts",
    "ai_job_execution_grants",
    "ai_job_inbox",
    "ai_job_outbox",
    "ai_job_step_effects",
    "ai_jobs",
    "message_security_rejections",
    "permission_feature_rollouts",
    "permission_feature_tenant_states",
}


def _alembic_config(mysql_url: URL) -> Config:
    backend_dir = Path(__file__).parents[3]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "alembic"))
    rendered_url = mysql_url.render_as_string(hide_password=False).replace("%", "%%")
    config.set_main_option("sqlalchemy.url", rendered_url)
    return config


async def _table_names(mysql_url: URL) -> set[str]:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(
                lambda sync_connection: set(inspect(sync_connection).get_table_names())
            )
    finally:
        await engine.dispose()


async def _seed_legacy_audit_rows(mysql_url: URL) -> tuple[UUID, ...]:
    engine = create_async_engine(mysql_url)
    user_id = new_uuid7()
    tenant_id = new_uuid7()
    membership_id = new_uuid7()
    global_user_id = new_uuid7()
    rows = (
        (new_uuid7(), None, global_user_id, None, "global.user.read", "global_user"),
        (new_uuid7(), tenant_id, user_id, membership_id, "membership.read", "tenant_membership"),
        (new_uuid7(), None, None, None, "anonymous.echo", "anonymous"),
        (new_uuid7(), None, None, None, "platform_admin.bootstrap", "identity_bootstrap"),
        (
            new_uuid7(),
            None,
            None,
            None,
            "invitation.blind_index_legacy_reconcile",
            "global_maintenance",
        ),
    )
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users (id,status,display_name,auth_version,version) "
                    "VALUES (:id,'active','Legacy Global',1,1)"
                ),
                {"id": global_user_id.bytes},
            )
            await connection.execute(
                text(
                    "INSERT INTO users (id,status,display_name,auth_version,version) "
                    "VALUES (:id,'active','Legacy Member',1,1)"
                ),
                {"id": user_id.bytes},
            )
            await connection.execute(
                text(
                    "INSERT INTO tenants "
                    "(id,name,normalized_name,tenant_type,status,created_by_user_id,"
                    "review_status,version) VALUES "
                    "(:id,'Legacy Tenant','legacy-tenant','enterprise','active',"
                    ":user_id,'approved',1)"
                ),
                {"id": tenant_id.bytes, "user_id": user_id.bytes},
            )
            await connection.execute(
                text(
                    "INSERT INTO tenant_memberships "
                    "(id,tenant_id,user_id,member_type,status,valid_from,authz_version,version) "
                    "VALUES (:id,:tenant_id,:user_id,'owner','active',UTC_TIMESTAMP(6),1,1)"
                ),
                {
                    "id": membership_id.bytes,
                    "tenant_id": tenant_id.bytes,
                    "user_id": user_id.bytes,
                },
            )
            for audit_id, tid, uid, mid, action, _kind in rows:
                await connection.execute(
                    text(
                        "INSERT INTO audit_events "
                        "(id,actor_user_id,tenant_id,actor_membership_id,action,result,"
                        "reason_code,trace_id,occurred_at) VALUES "
                        "(:id,:user_id,:tenant_id,:membership_id,:action,'success',"
                        "'legacy',:trace,UTC_TIMESTAMP(6))"
                    ),
                    {
                        "id": audit_id.bytes,
                        "user_id": None if uid is None else uid.bytes,
                        "tenant_id": None if tid is None else tid.bytes,
                        "membership_id": None if mid is None else mid.bytes,
                        "action": action,
                        "trace": "legacy-trace",
                    },
                )
        return tuple(audit_id for audit_id, *_ in rows)
    finally:
        await engine.dispose()


async def _legacy_audit_snapshot(mysql_url: URL, audit_ids: tuple[UUID, ...]) -> list[tuple]:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.connect() as connection:
            result = []
            for audit_id in audit_ids:
                row = (
                    await connection.execute(
                        text(
                            "SELECT actor_user_id, tenant_id, actor_membership_id "
                            "FROM audit_events WHERE id = :id"
                        ),
                        {"id": audit_id.bytes},
                    )
                ).one()
                result.append(tuple(row))
            return result
    finally:
        await engine.dispose()


async def _assert_legacy_new_columns_null(mysql_url: URL, audit_ids: tuple[UUID, ...]) -> None:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.connect() as connection:
            for audit_id in audit_ids:
                row = (
                    await connection.execute(
                        text(
                            "SELECT actor_kind, on_behalf_of_user_id, "
                            "on_behalf_of_membership_id, target_job_id, attempt_id, "
                            "message_id, rollout_feature_code, rollout_generation "
                            "FROM audit_events WHERE id = :id"
                        ),
                        {"id": audit_id.bytes},
                    )
                ).one()
                assert all(value is None for value in row)
    finally:
        await engine.dispose()


def test_ai_job_revision_extends_current_head(mysql_url: URL) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "20260902_05")
    audit_ids = asyncio.run(_seed_legacy_audit_rows(mysql_url))
    before = asyncio.run(_legacy_audit_snapshot(mysql_url, audit_ids))

    command.upgrade(config, "head")
    tables = asyncio.run(_table_names(mysql_url))
    assert AI_JOB_TABLES <= tables

    after = asyncio.run(_legacy_audit_snapshot(mysql_url, audit_ids))
    assert after == before
    asyncio.run(_assert_legacy_new_columns_null(mysql_url, audit_ids))

    command.downgrade(config, "20260902_05")
    assert not (AI_JOB_TABLES <= asyncio.run(_table_names(mysql_url)))
    command.upgrade(config, "head")
    assert AI_JOB_TABLES <= asyncio.run(_table_names(mysql_url))


def test_fresh_upgrade_round_trip_and_catalog_seed(mysql_url: URL) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    tables = asyncio.run(_table_names(mysql_url))
    assert AI_JOB_TABLES <= tables
    asyncio.run(_assert_catalog_seed(mysql_url))

    command.downgrade(config, "base")
    assert asyncio.run(_table_names(mysql_url)) == {"alembic_version"}
    command.upgrade(config, "head")
    assert AI_JOB_TABLES <= asyncio.run(_table_names(mysql_url))
    command.check(config)


async def _assert_catalog_seed(mysql_url: URL) -> None:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.connect() as connection:
            permission_codes = set(
                (
                    await connection.execute(
                        text(
                            "SELECT code FROM permission_catalog "
                            "WHERE code LIKE 'ai_job.%'"
                        )
                    )
                ).scalars()
            )
            assert permission_codes == {"ai_job.create", "ai_job.read", "ai_job.cancel"}
            feature = (
                await connection.execute(
                    text(
                        "SELECT phase, rollout_generation FROM permission_feature_rollouts "
                        "WHERE feature_code = 'ai_job_runtime_v1'"
                    )
                )
            ).one()
            assert tuple(feature) == ("catalog_only", 0)
    finally:
        await engine.dispose()
