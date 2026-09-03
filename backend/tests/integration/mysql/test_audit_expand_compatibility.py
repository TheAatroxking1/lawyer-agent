from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from test_ai_job_constraints import _insert_graph
from test_ai_job_migration import _alembic_config

from alembic import command
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.infrastructure.persistence.models import (
    AIJobOutboxModel,
    AuditEventModel,
)

pytestmark = [pytest.mark.integration, pytest.mark.mysql]


def _naive_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _structured_audit(
    *,
    id: UUID,
    actor_kind: str,
    action: str,
    tenant_id: UUID | None = None,
    actor_user_id: UUID | None = None,
    actor_membership_id: UUID | None = None,
    on_behalf_of_user_id: UUID | None = None,
    on_behalf_of_membership_id: UUID | None = None,
    target_job_id: UUID | None = None,
    attempt_id: UUID | None = None,
    message_id: UUID | None = None,
    rollout_feature_code: str | None = None,
    rollout_generation: int | None = None,
) -> AuditEventModel:
    return AuditEventModel(
        id=id,
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        actor_membership_id=actor_membership_id,
        actor_kind=actor_kind,
        on_behalf_of_user_id=on_behalf_of_user_id,
        on_behalf_of_membership_id=on_behalf_of_membership_id,
        target_job_id=target_job_id,
        attempt_id=attempt_id,
        message_id=message_id,
        rollout_feature_code=rollout_feature_code,
        rollout_generation=rollout_generation,
        action=action,
        result="success",
        reason_code="test",
        trace_id="trace",
        occurred_at=_naive_now(),
    )


async def _exercise_audit_matrix(mysql_url: str) -> None:
    engine = create_async_engine(mysql_url)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        graph = await _insert_graph(session)
        other = await _insert_graph(session)
        now = _naive_now()

        # Audit message target must belong to the same job.
        with pytest.raises(DBAPIError):
            async with session.begin_nested():
                session.add(
                    _structured_audit(
                        id=new_uuid7(),
                        actor_kind="system_publisher",
                        action="ai_job.outbox_published",
                        tenant_id=graph.tenant_id,
                        target_job_id=other.job_id,
                        message_id=new_uuid7(),
                    )
                )
                await session.flush()

        # tenant_user must carry a full actor triple.
        with pytest.raises(DBAPIError):
            async with session.begin_nested():
                session.add(
                    _structured_audit(
                        id=new_uuid7(),
                        actor_kind="tenant_user",
                        action="ai_job.created",
                        tenant_id=None,
                        actor_user_id=None,
                        actor_membership_id=None,
                    )
                )
                await session.flush()

        # Unknown actor_kind is rejected by the closed matrix.
        with pytest.raises(DBAPIError):
            async with session.begin_nested():
                session.add(
                    _structured_audit(
                        id=new_uuid7(),
                        actor_kind="bogus_kind",
                        action="anything",
                    )
                )
                await session.flush()

        # Positive structured rows across the closed set.
        session.add(
            _structured_audit(
                id=new_uuid7(),
                actor_kind="tenant_user",
                action="ai_job.created",
                tenant_id=graph.tenant_id,
                actor_user_id=graph.user_id,
                actor_membership_id=graph.membership_id,
            )
        )
        session.add(
            _structured_audit(
                id=new_uuid7(),
                actor_kind="system_worker",
                action="ai_job.execution_started",
                tenant_id=graph.tenant_id,
                on_behalf_of_user_id=graph.user_id,
                on_behalf_of_membership_id=graph.membership_id,
                target_job_id=graph.job_id,
            )
        )
        session.add(
            _structured_audit(
                id=new_uuid7(),
                actor_kind="system_job_maintenance",
                action="ai_job.lease_expired",
                tenant_id=graph.tenant_id,
                target_job_id=graph.job_id,
            )
        )
        session.add(
            _structured_audit(
                id=new_uuid7(),
                actor_kind="system_global_feature_rollout",
                action="ai_job.feature_phase_migrated",
                rollout_feature_code="ai_job_runtime_v1",
                rollout_generation=1,
            )
        )
        session.add(
            _structured_audit(
                id=new_uuid7(),
                actor_kind="global_user",
                action="account.profile.update",
                actor_user_id=graph.user_id,
            )
        )
        session.add(
            _structured_audit(
                id=new_uuid7(),
                actor_kind="system_identity_bootstrap",
                action="platform_admin.bootstrap",
            )
        )
        session.add(
            _structured_audit(
                id=new_uuid7(),
                actor_kind="system_global_maintenance",
                action="invitation.blind_index_legacy_reconcile",
            )
        )
        session.add(
            _structured_audit(
                id=new_uuid7(),
                actor_kind="platform_operator",
                action="platform.review",
                actor_user_id=graph.user_id,
            )
        )
        await session.flush()

        # Legacy writer still writes NULL actor_kind rows with existing columns.
        session.add(
            AuditEventModel(
                id=new_uuid7(),
                actor_user_id=graph.user_id,
                tenant_id=graph.tenant_id,
                actor_membership_id=graph.membership_id,
                actor_kind=None,
                action="membership.read",
                result="success",
                reason_code="legacy",
                trace_id="legacy",
                occurred_at=now,
            )
        )
        await session.flush()
        await session.rollback()


def test_structured_audit_matrix_and_legacy_compatibility(mysql_url) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    asyncio.run(_exercise_audit_matrix(mysql_url.render_as_string(hide_password=False)))


async def _exercise_outbox_message_candidate_key(mysql_url: str) -> None:
    engine = create_async_engine(mysql_url)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        graph = await _insert_graph(session)
        await session.execute(
            text("DELETE FROM ai_job_outbox WHERE id = :id"),
            {"id": graph.outbox_id.bytes},
        )
        message_id = new_uuid7()
        session.add(
            AIJobOutboxModel(
                id=new_uuid7(),
                tenant_id=graph.tenant_id,
                job_id=graph.job_id,
                dispatch_generation=2,
                envelope_generation=1,
                max_envelope_generations=4,
                event_type="ai_job.execute_requested",
                routing_key="ai.job.execute",
                schema_version=1,
                message_id=message_id,
                available_at=_naive_now(),
            )
        )
        await session.flush()
        with pytest.raises(DBAPIError):
            async with session.begin_nested():
                session.add(
                    AIJobOutboxModel(
                        id=new_uuid7(),
                        tenant_id=graph.tenant_id,
                        job_id=graph.job_id,
                        dispatch_generation=3,
                        envelope_generation=1,
                        max_envelope_generations=4,
                        event_type="ai_job.execute_requested",
                        routing_key="ai.job.execute",
                        schema_version=1,
                        message_id=message_id,
                        available_at=_naive_now(),
                    )
                )
                await session.flush()
        await session.rollback()


def test_outbox_message_id_is_unique_per_tenant(mysql_url) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    asyncio.run(
        _exercise_outbox_message_candidate_key(mysql_url.render_as_string(hide_password=False))
    )


async def _exercise_structured_writer(mysql_url: str) -> None:
    from sqlalchemy import select as sa_select

    from lawyer_agent.application.audit import AuditActorKind, StructuredAuditEvent
    from lawyer_agent.infrastructure.persistence.repositories.audit import AuditRepository

    engine = create_async_engine(mysql_url)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    event = StructuredAuditEvent(
        id=new_uuid7(),
        actor_kind=AuditActorKind.SYSTEM_IDENTITY_BOOTSTRAP,
        action="platform_admin.bootstrap",
        result="success",
        reason_code="test",
        trace_id="trace",
        occurred_at=datetime.now(UTC),
    )
    try:
        async with factory() as session:
            await AuditRepository(session).append_structured(event)
            await session.commit()
        async with factory() as session:
            row = await session.scalar(
                sa_select(AuditEventModel).where(AuditEventModel.id == event.id)
            )
            assert row is not None
            assert row.actor_kind == "system_identity_bootstrap"
    finally:
        await engine.dispose()


def test_structured_writer_persists_non_null_actor_kind(mysql_url) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    asyncio.run(
        _exercise_structured_writer(mysql_url.render_as_string(hide_password=False))
    )
