import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_ai_job_constraints import _insert_graph, _naive_now
from test_ai_job_migration import _alembic_config

from alembic import command
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.infrastructure.persistence.engine import create_engine_from_url
from lawyer_agent.infrastructure.persistence.models import AIJobModel

pytestmark = [pytest.mark.integration, pytest.mark.mysql]


async def seed_legacy(url):
    engine = create_engine_from_url(url.render_as_string(hide_password=False))
    try:
        async with async_sessionmaker(engine).begin() as session:
            graph = await _insert_graph(session)
            return graph.job_id
    finally:
        await engine.dispose()


async def exercise_device_binding(url, legacy_job_id):
    engine = create_engine_from_url(url.render_as_string(hide_password=False))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = _naive_now()
    try:
        async with factory.begin() as session:
            assert await session.get(AIJobModel, legacy_job_id) is not None
            owner = await _insert_graph(session, device_session=True)
            stranger = await _insert_graph(session, device_session=True)
            for bad_session, bad_member, expected_constraint in (
                (stranger.session_id, owner.membership_id, "fk_ai_jobs_user_session_actor"),
                (owner.session_id, stranger.membership_id, "fk_ai_jobs_tenant_membership_user"),
            ):
                with pytest.raises(IntegrityError, match=expected_constraint):
                    async with session.begin_nested():
                        session.add(AIJobModel(
                            id=new_uuid7(), tenant_id=owner.tenant_id,
                            handler_code="synthetic.v1", handler_version="v1",
                            input_schema_version="synthetic-input-v1", input_json={},
                            input_fingerprint=b"x" * 32, created_by_user_id=owner.user_id,
                            created_by_membership_id=bad_member, created_by_session_id=bad_session,
                            actor_snapshot_json={}, policy_version_at_submit="ai-job-policy-v1",
                            idempotency_record_id=owner.idempotency_id, correlation_id=new_uuid7(),
                            submitted_at=now, expires_at=now + timedelta(hours=1),
                        ))
                        await session.flush()
        async with factory() as session:
            assert (await session.scalar(select(AIJobModel).where(
                AIJobModel.id == owner.job_id,
            ))).created_by_session_id == owner.session_id
        async with engine.connect() as connection:
            foreign_keys = await connection.run_sync(
                lambda sync: inspect(sync).get_foreign_keys("ai_jobs"),
            )
            user_binding = next(fk for fk in foreign_keys
                                if fk["name"] == "fk_ai_jobs_user_session_actor")
            assert user_binding["constrained_columns"] == [
                "created_by_user_id", "created_by_session_id",
            ]
            assert user_binding["referred_columns"] == ["user_id", "id"]
    finally:
        await engine.dispose()


def test_upgrade_preserves_legacy_jobs_and_device_jobs_keep_owner_and_tenant_fks(mysql_url):
    config = _alembic_config(mysql_url)
    command.upgrade(config, "20260921_17")
    legacy = asyncio.run(seed_legacy(mysql_url))
    command.upgrade(config, "head")
    # Reversal is safe while every existing job still has its original tenant session.
    command.downgrade(config, "20260921_17")
    command.upgrade(config, "head")
    asyncio.run(exercise_device_binding(mysql_url, legacy))
    with pytest.raises(RuntimeError, match="device session"):
        command.downgrade(config, "20260921_17")
