from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from test_ai_job_migration import _alembic_config

from alembic import command
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.infrastructure.persistence.models import (
    AIJobAccessGrantModel,
    AIJobAttemptModel,
    AIJobExecutionGrantModel,
    AIJobInboxModel,
    AIJobModel,
    AIJobOutboxModel,
    AIJobStepEffectModel,
    AuthSessionModel,
    IdempotencyRecordModel,
    TenantMembershipModel,
    TenantModel,
    UserModel,
)

pytestmark = [pytest.mark.integration, pytest.mark.mysql]


@dataclass(frozen=True, slots=True)
class JobGraph:
    user_id: UUID
    tenant_id: UUID
    membership_id: UUID
    session_id: UUID
    idempotency_id: UUID
    job_id: UUID
    grant_id: UUID
    outbox_id: UUID
    attempt_id: UUID


def _naive_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _insert_graph(session: AsyncSession) -> JobGraph:
    now = _naive_now()
    user_id = new_uuid7()
    tenant_id = new_uuid7()
    membership_id = new_uuid7()
    session_id = new_uuid7()
    idempotency_id = new_uuid7()
    job_id = new_uuid7()
    grant_id = new_uuid7()
    outbox_id = new_uuid7()
    attempt_id = new_uuid7()
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
    await session.flush()
    session.add(
        TenantMembershipModel(
            id=membership_id,
            tenant_id=tenant_id,
            user_id=user_id,
            department_id=None,
            member_type="owner",
            status="active",
            valid_from=now,
            valid_until=None,
            authz_version=1,
        )
    )
    await session.flush()
    session.add(
        AuthSessionModel(
            id=session_id,
            user_id=user_id,
            tenant_id=tenant_id,
            membership_id=membership_id,
            current_family_id=new_uuid7(),
            auth_version_at_issue=1,
            authz_version_at_issue=1,
            revoked_at=None,
            revocation_reason=None,
            last_seen_at=now,
            expires_at=now + timedelta(days=30),
        )
    )
    await session.flush()
    session.add(
        IdempotencyRecordModel(
            id=idempotency_id,
            tenant_id=tenant_id,
            scope_type="membership",
            scope_id=membership_id,
            operation="ai_job.create",
            key_hash=b"k" * 32,
            request_fingerprint=b"f" * 32,
            status="completed",
            expires_at=now + timedelta(days=1),
        )
    )
    await session.flush()
    session.add(
        AIJobModel(
            id=job_id,
            tenant_id=tenant_id,
            handler_code="synthetic.v1",
            handler_version="v1",
            input_schema_version="synthetic-input-v1",
            input_json={"scenario": "success", "work_units": 1},
            input_fingerprint=b"i" * 32,
            created_by_user_id=user_id,
            created_by_membership_id=membership_id,
            created_by_session_id=session_id,
            actor_snapshot_json={},
            policy_version_at_submit="ai-job-policy-v1",
            idempotency_record_id=idempotency_id,
            correlation_id=new_uuid7(),
            submitted_at=now,
            expires_at=now + timedelta(hours=1),
        )
    )
    await session.flush()
    session.add(
        AIJobExecutionGrantModel(
            id=grant_id,
            tenant_id=tenant_id,
            job_id=job_id,
            user_id=user_id,
            membership_id=membership_id,
            permission_code="ai_job.create",
            auth_version_at_submit=1,
            authz_version_at_submit=1,
            policy_version="ai-job-policy-v1",
            job_scope_manifest_version="ai-job-scope-v1",
            job_scope_code="owner_shared",
            issued_at=now,
        )
    )
    session.add(
        AIJobOutboxModel(
            id=outbox_id,
            tenant_id=tenant_id,
            job_id=job_id,
            dispatch_generation=1,
            envelope_generation=1,
            max_envelope_generations=4,
            event_type="ai_job.execute_requested",
            routing_key="ai.job.execute",
            schema_version=1,
            message_id=new_uuid7(),
            available_at=now,
        )
    )
    await session.flush()
    session.add(
        AIJobAttemptModel(
            id=attempt_id,
            tenant_id=tenant_id,
            job_id=job_id,
            attempt_no=1,
            status="claimed",
            trigger_message_id=new_uuid7(),
            lease_token=new_uuid7(),
            lease_fence=1,
            handler_code="synthetic.v1",
            handler_version="v1",
            started_at=now,
        )
    )
    await session.flush()
    return JobGraph(
        user_id, tenant_id, membership_id, session_id, idempotency_id, job_id, grant_id,
        outbox_id, attempt_id,
    )


async def _exercise_negative_constraints(mysql_url: str) -> None:
    engine = create_async_engine(mysql_url)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    now = _naive_now()
    async with factory() as session:
        graph = await _insert_graph(session)
        other = await _insert_graph(session)

        # Cross-tenant membership on the job actor FK.
        with pytest.raises(DBAPIError):
            async with session.begin_nested():
                session.add(
                    AIJobModel(
                        id=new_uuid7(),
                        tenant_id=graph.tenant_id,
                        handler_code="synthetic.v1",
                        handler_version="v1",
                        input_schema_version="synthetic-input-v1",
                        input_json={},
                        input_fingerprint=b"x" * 32,
                        created_by_user_id=graph.user_id,
                        created_by_membership_id=other.membership_id,
                        created_by_session_id=graph.session_id,
                        actor_snapshot_json={},
                        policy_version_at_submit="ai-job-policy-v1",
                        idempotency_record_id=graph.idempotency_id,
                        correlation_id=new_uuid7(),
                        submitted_at=now,
                        expires_at=now + timedelta(hours=1),
                    )
                )
                await session.flush()

        # Attempt referencing the other tenant's job.
        with pytest.raises(DBAPIError):
            async with session.begin_nested():
                session.add(
                    AIJobAttemptModel(
                        id=new_uuid7(),
                        tenant_id=graph.tenant_id,
                        job_id=other.job_id,
                        attempt_no=1,
                        status="claimed",
                        trigger_message_id=new_uuid7(),
                        lease_token=new_uuid7(),
                        lease_fence=1,
                        handler_code="synthetic.v1",
                        handler_version="v1",
                        started_at=now,
                    )
                )
                await session.flush()

        # Grant referencing the other tenant's membership.
        with pytest.raises(DBAPIError):
            async with session.begin_nested():
                session.add(
                    AIJobExecutionGrantModel(
                        id=new_uuid7(),
                        tenant_id=graph.tenant_id,
                        job_id=graph.job_id,
                        user_id=other.user_id,
                        membership_id=other.membership_id,
                        permission_code="ai_job.create",
                        auth_version_at_submit=1,
                        authz_version_at_submit=1,
                        policy_version="ai-job-policy-v1",
                        job_scope_manifest_version="ai-job-scope-v1",
                        job_scope_code="owner_shared",
                        issued_at=now,
                    )
                )
                await session.flush()

        # Attempt fence must belong to the job (cross-job fence).
        with pytest.raises(DBAPIError):
            async with session.begin_nested():
                session.add(
                    AIJobStepEffectModel(
                        id=new_uuid7(),
                        tenant_id=graph.tenant_id,
                        job_id=graph.job_id,
                        attempt_id=other.attempt_id,
                        step_code="unit",
                        effect_key="stable-1",
                        input_digest=b"d" * 32,
                        status="applied",
                        lease_fence=1,
                        started_at=now,
                        applied_at=now,
                    )
                )
                await session.flush()

        # Access grant supersedes chain cannot cross jobs.
        with pytest.raises(DBAPIError):
            async with session.begin_nested():
                session.add(
                    AIJobAccessGrantModel(
                        id=new_uuid7(),
                        tenant_id=graph.tenant_id,
                        job_id=graph.job_id,
                        membership_id=graph.membership_id,
                        access_level="read",
                        generation=2,
                        supersedes_generation=1,
                        supersedes_grant_id=other.grant_id,
                        granted_by_user_id=graph.user_id,
                        granted_by_membership_id=graph.membership_id,
                        granted_at=now,
                    )
                )
                await session.flush()

        # Inbox handling fence must reference the same job's attempt.
        with pytest.raises(DBAPIError):
            async with session.begin_nested():
                session.add(
                    AIJobInboxModel(
                        id=new_uuid7(),
                        tenant_id=graph.tenant_id,
                        message_id=new_uuid7(),
                        job_id=graph.job_id,
                        envelope_digest=b"e" * 32,
                        nonce_digest=b"n" * 32,
                        status="processing",
                        first_received_at=now,
                        last_received_at=now,
                        handling_attempt_id=other.attempt_id,
                        handling_fence=1,
                    )
                )
                await session.flush()

        await session.rollback()


def test_cross_tenant_composite_constraints_rejected(mysql_url) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    asyncio.run(_exercise_negative_constraints(mysql_url.render_as_string(hide_password=False)))


def test_access_grant_active_marker_enforces_single_active(mysql_url) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    asyncio.run(_exercise_active_grant_uniqueness(mysql_url.render_as_string(hide_password=False)))


async def _exercise_active_grant_uniqueness(mysql_url: str) -> None:
    engine = create_async_engine(mysql_url)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    now = _naive_now()
    async with factory() as session:
        graph = await _insert_graph(session)
        session.add(
            AIJobAccessGrantModel(
                id=new_uuid7(),
                tenant_id=graph.tenant_id,
                job_id=graph.job_id,
                membership_id=graph.membership_id,
                access_level="read",
                generation=1,
                granted_by_user_id=graph.user_id,
                granted_by_membership_id=graph.membership_id,
                granted_at=now,
            )
        )
        await session.flush()
        with pytest.raises(DBAPIError):
            async with session.begin_nested():
                session.add(
                    AIJobAccessGrantModel(
                        id=new_uuid7(),
                        tenant_id=graph.tenant_id,
                        job_id=graph.job_id,
                        membership_id=graph.membership_id,
                        access_level="read",
                        generation=2,
                        granted_by_user_id=graph.user_id,
                        granted_by_membership_id=graph.membership_id,
                        granted_at=now,
                    )
                )
                await session.flush()
        await session.rollback()
