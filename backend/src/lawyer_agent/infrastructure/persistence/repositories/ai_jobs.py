from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from lawyer_agent.application.ai_jobs import (
    JOB_SCOPE_BY_ROLE,
    JobAuthoritySnapshot,
    scope_from_code,
)
from lawyer_agent.domain.ai_jobs import (
    AIJob,
    AIJobPermission,
    AIJobStatus,
    ClaimedExecution,
    ClaimJobRequest,
    ClaimRejected,
    FencedFinalizeSuccess,
    FencedHeartbeat,
    JobAccess,
    JobAuthorizationScope,
    JobExecutionGrant,
    JobScopeCode,
    JobVisibility,
    NewAIJobGraph,
    ReleaseState,
    RiskClass,
)
from lawyer_agent.domain.common import require_uuid7
from lawyer_agent.domain.tenancy import (
    Membership,
    MembershipStatus,
    MemberType,
    TenantContext,
)
from lawyer_agent.infrastructure.persistence.models import (
    AIJobAccessGrantModel,
    AIJobAttemptModel,
    AIJobExecutionGrantModel,
    AIJobModel,
    AIJobOutboxModel,
    MembershipRoleAssignmentModel,
    PermissionModel,
    TenantMembershipModel,
    TenantModel,
    TenantRoleModel,
    TenantRolePermissionModel,
    UserModel,
)

_CLAIMABLE_STATUSES = (AIJobStatus.QUEUED.value, AIJobStatus.RETRY_SCHEDULED.value)
_LEASE_HOLDING_STATUSES = (AIJobStatus.RUNNING.value, AIJobStatus.CANCEL_REQUESTED.value)


class SqlAlchemyAIJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(
        self,
        context: TenantContext,
        job_id: UUID,
        *,
        for_update: bool = False,
    ) -> AIJob | None:
        _require_context(context)
        require_uuid7(job_id, field="job_id")
        statement = select(AIJobModel).where(
            AIJobModel.tenant_id == context.tenant_id,
            AIJobModel.id == job_id,
        )
        if for_update:
            statement = statement.with_for_update()
        model = await self._session.scalar(statement)
        return None if model is None else _job(model)

    async def add_job_graph(self, graph: NewAIJobGraph) -> None:
        if not isinstance(graph, NewAIJobGraph):
            raise ValueError("job graph must be strongly typed")
        self._session.add(_job_model(graph.job))
        await self._session.flush()
        self._session.add(
            AIJobExecutionGrantModel(
                id=graph.grant.id,
                tenant_id=graph.grant.tenant_id,
                job_id=graph.grant.job_id,
                user_id=graph.grant.user_id,
                membership_id=graph.grant.membership_id,
                permission_code=graph.grant.permission_code.value,
                auth_version_at_submit=graph.grant.auth_version_at_submit,
                authz_version_at_submit=graph.grant.authz_version_at_submit,
                policy_version=graph.grant.policy_version,
                job_scope_manifest_version=graph.grant.job_scope_manifest_version,
                job_scope_code=graph.grant.job_scope_code.value,
                issued_at=_naive(graph.grant.issued_at),
            )
        )
        self._session.add(
            AIJobOutboxModel(
                id=graph.outbox.id,
                tenant_id=graph.outbox.tenant_id,
                job_id=graph.outbox.job_id,
                dispatch_generation=graph.outbox.dispatch_generation,
                envelope_generation=graph.outbox.envelope_generation,
                max_envelope_generations=graph.outbox.max_envelope_generations,
                event_type=graph.outbox.event_type,
                routing_key=graph.outbox.routing_key,
                schema_version=graph.outbox.schema_version,
                available_at=_naive(graph.outbox.available_at),
            )
        )

    async def get_access(
        self,
        context: TenantContext,
        job_id: UUID,
        membership_id: UUID,
    ) -> JobAccess | None:
        _require_context(context)
        require_uuid7(job_id, field="job_id")
        require_uuid7(membership_id, field="membership_id")
        model = await self._session.scalar(
            select(AIJobAccessGrantModel).where(
                AIJobAccessGrantModel.tenant_id == context.tenant_id,
                AIJobAccessGrantModel.job_id == job_id,
                AIJobAccessGrantModel.membership_id == membership_id,
            )
        )
        return None if model is None else _access(model)

    async def claim_due_job(
        self, request: ClaimJobRequest
    ) -> ClaimedExecution | ClaimRejected:
        if not isinstance(request, ClaimJobRequest):
            raise ValueError("claim request must be strongly typed")
        started_at = _naive(request.started_at)
        changed = cast(
            CursorResult[Any],
            await self._session.execute(
                update(AIJobModel)
                .where(
                    AIJobModel.tenant_id == request.tenant_id,
                    AIJobModel.id == request.job_id,
                    AIJobModel.status.in_(_CLAIMABLE_STATUSES),
                    AIJobModel.lease_owner.is_(None),
                    AIJobModel.expires_at > started_at,
                    or_(
                        AIJobModel.next_attempt_at.is_(None),
                        AIJobModel.next_attempt_at <= started_at,
                    ),
                )
                .values(
                    status=AIJobStatus.RUNNING.value,
                    current_attempt_no=request.attempt_no,
                    lease_owner=request.worker_instance_ref,
                    lease_token=request.lease_token,
                    lease_fence=request.lease_fence,
                    lease_expires_at=_naive(request.lease_expires_at),
                    started_at=started_at,
                    version=AIJobModel.version + 1,
                    updated_at=started_at,
                )
            ),
        )
        if changed.rowcount != 1:
            return ClaimRejected("job_not_claimable")
        self._session.add(
            AIJobAttemptModel(
                id=request.attempt_id,
                tenant_id=request.tenant_id,
                job_id=request.job_id,
                attempt_no=request.attempt_no,
                status="claimed",
                trigger_message_id=request.trigger_message_id,
                worker_instance_ref=request.worker_instance_ref,
                lease_token=request.lease_token,
                lease_fence=request.lease_fence,
                handler_code=request.handler_code,
                handler_version=request.handler_version,
                started_at=started_at,
            )
        )
        await self._session.flush()
        model = await self._session.scalar(
            select(AIJobModel).where(
                AIJobModel.tenant_id == request.tenant_id,
                AIJobModel.id == request.job_id,
            )
        )
        if model is None:
            raise RuntimeError("claimed job disappeared")
        return ClaimedExecution(
            job=_job(model),
            attempt_id=request.attempt_id,
            attempt_no=request.attempt_no,
            lease_token=request.lease_token,
            lease_fence=request.lease_fence,
            trigger_message_id=request.trigger_message_id,
            worker_instance_ref=request.worker_instance_ref,
            lease_expires_at=request.lease_expires_at,
        )

    async def heartbeat(self, request: FencedHeartbeat) -> bool:
        if not isinstance(request, FencedHeartbeat):
            raise ValueError("heartbeat request must be strongly typed")
        changed = cast(
            CursorResult[Any],
            await self._session.execute(
                update(AIJobModel)
                .where(
                    AIJobModel.tenant_id == request.tenant_id,
                    AIJobModel.id == request.job_id,
                    AIJobModel.lease_token == request.lease_token,
                    AIJobModel.lease_fence == request.lease_fence,
                    AIJobModel.version == request.expected_version,
                    AIJobModel.status.in_(_LEASE_HOLDING_STATUSES),
                )
                .values(
                    lease_expires_at=_naive(request.lease_expires_at),
                    version=AIJobModel.version + 1,
                    updated_at=_naive(request.heartbeat_at),
                )
            ),
        )
        return changed.rowcount == 1

    async def finalize_success(self, request: FencedFinalizeSuccess) -> bool:
        if not isinstance(request, FencedFinalizeSuccess):
            raise ValueError("finalize request must be strongly typed")
        changed = cast(
            CursorResult[Any],
            await self._session.execute(
                update(AIJobModel)
                .where(
                    AIJobModel.tenant_id == request.tenant_id,
                    AIJobModel.id == request.job_id,
                    AIJobModel.lease_token == request.lease_token,
                    AIJobModel.lease_fence == request.lease_fence,
                    AIJobModel.version == request.expected_version,
                    AIJobModel.status == AIJobStatus.RUNNING.value,
                )
                .values(
                    status=AIJobStatus.SUCCEEDED.value,
                    result_json=request.result,
                    completed_at=_naive(request.now),
                    lease_owner=None,
                    lease_token=None,
                    lease_fence=None,
                    lease_expires_at=None,
                    next_attempt_at=None,
                    version=AIJobModel.version + 1,
                    updated_at=_naive(request.now),
                )
            ),
        )
        if changed.rowcount != 1:
            return False
        await self._session.execute(
            update(AIJobAttemptModel)
            .where(
                AIJobAttemptModel.tenant_id == request.tenant_id,
                AIJobAttemptModel.job_id == request.job_id,
                AIJobAttemptModel.lease_fence == request.lease_fence,
                AIJobAttemptModel.status == "claimed",
            )
            .values(
                status="succeeded",
                finished_at=_naive(request.now),
                version=AIJobAttemptModel.version + 1,
                updated_at=_naive(request.now),
            )
        )
        return True


def _job(model: AIJobModel) -> AIJob:
    return AIJob(
        id=model.id,
        tenant_id=model.tenant_id,
        handler_code=model.handler_code,
        handler_version=model.handler_version,
        input_schema_version=model.input_schema_version,
        input_json=model.input_json,
        input_fingerprint=bytes(model.input_fingerprint),
        created_by_user_id=model.created_by_user_id,
        created_by_membership_id=model.created_by_membership_id,
        created_by_session_id=model.created_by_session_id,
        actor_snapshot_json=model.actor_snapshot_json,
        policy_version_at_submit=model.policy_version_at_submit,
        visibility=JobVisibility(model.visibility),
        status=AIJobStatus(model.status),
        current_attempt_no=model.current_attempt_no,
        max_attempts=model.max_attempts,
        risk_class=RiskClass(model.risk_class),
        release_state=ReleaseState(model.release_state),
        idempotency_record_id=model.idempotency_record_id,
        correlation_id=model.correlation_id,
        submitted_at=_aware(model.submitted_at),
        expires_at=_aware(model.expires_at),
        version=model.version,
        next_attempt_at=_aware_optional(model.next_attempt_at),
        lease_owner=model.lease_owner,
        lease_token=model.lease_token,
        lease_fence=model.lease_fence,
        lease_expires_at=_aware_optional(model.lease_expires_at),
        cancel_requested_at=_aware_optional(model.cancel_requested_at),
        cancel_requested_by_user_id=model.cancel_requested_by_user_id,
        cancel_reason_code=model.cancel_reason_code,
        result_json=model.result_json,
        failure_class=model.failure_class,
        failure_code=model.failure_code,
        started_at=_aware_optional(model.started_at),
        completed_at=_aware_optional(model.completed_at),
    )


def _job_model(job: AIJob) -> AIJobModel:
    return AIJobModel(
        id=job.id,
        tenant_id=job.tenant_id,
        handler_code=job.handler_code,
        handler_version=job.handler_version,
        input_schema_version=job.input_schema_version,
        input_json=job.input_json,
        input_fingerprint=job.input_fingerprint,
        created_by_user_id=job.created_by_user_id,
        created_by_membership_id=job.created_by_membership_id,
        created_by_session_id=job.created_by_session_id,
        actor_snapshot_json=job.actor_snapshot_json,
        policy_version_at_submit=job.policy_version_at_submit,
        visibility=job.visibility.value,
        status=job.status.value,
        current_attempt_no=job.current_attempt_no,
        max_attempts=job.max_attempts,
        risk_class=job.risk_class.value,
        release_state=job.release_state.value,
        idempotency_record_id=job.idempotency_record_id,
        correlation_id=job.correlation_id,
        submitted_at=_naive(job.submitted_at),
        expires_at=_naive(job.expires_at),
        version=job.version,
        next_attempt_at=_naive_optional(job.next_attempt_at),
        lease_owner=job.lease_owner,
        lease_token=job.lease_token,
        lease_fence=job.lease_fence,
        lease_expires_at=_naive_optional(job.lease_expires_at),
        cancel_requested_at=_naive_optional(job.cancel_requested_at),
        cancel_requested_by_user_id=job.cancel_requested_by_user_id,
        cancel_reason_code=job.cancel_reason_code,
        result_json=job.result_json,
        failure_class=job.failure_class,
        failure_code=job.failure_code,
        started_at=_naive_optional(job.started_at),
        completed_at=_naive_optional(job.completed_at),
    )


def _access(model: AIJobAccessGrantModel) -> JobAccess:
    return JobAccess(
        tenant_id=model.tenant_id,
        job_id=model.job_id,
        membership_id=model.membership_id,
        access_level=model.access_level,
        generation=model.generation,
        revoked_at=_aware_optional(model.revoked_at),
    )


def _grant(model: AIJobExecutionGrantModel) -> JobExecutionGrant:
    return JobExecutionGrant(
        id=model.id,
        tenant_id=model.tenant_id,
        job_id=model.job_id,
        user_id=model.user_id,
        membership_id=model.membership_id,
        permission_code=AIJobPermission(model.permission_code),
        auth_version_at_submit=model.auth_version_at_submit,
        authz_version_at_submit=model.authz_version_at_submit,
        policy_version=model.policy_version,
        job_scope_manifest_version=model.job_scope_manifest_version,
        job_scope_code=JobScopeCode(model.job_scope_code),
        issued_at=_aware(model.issued_at),
        revoked_at=_aware_optional(model.revoked_at),
        revocation_reason_code=model.revocation_reason_code,
        version=model.version,
    )


def _membership(model: TenantMembershipModel) -> Membership:
    return Membership(
        id=model.id,
        tenant_id=model.tenant_id,
        user_id=model.user_id,
        department_id=model.department_id,
        member_type=MemberType(model.member_type),
        status=MembershipStatus(model.status),
        valid_from=_aware(model.valid_from),
        valid_until=_aware_optional(model.valid_until),
        authz_version=model.authz_version,
        version=model.version,
    )


class SqlAlchemyAIJobAuthorityLoader:
    """Reloads the durable execution authority from MySQL without any browser session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load(
        self,
        tenant_id: UUID,
        job_id: UUID,
        *,
        for_update: bool = False,
    ) -> JobAuthoritySnapshot:
        require_uuid7(tenant_id, field="authority tenant_id")
        require_uuid7(job_id, field="authority job_id")
        job_statement = select(AIJobModel).where(
            AIJobModel.tenant_id == tenant_id,
            AIJobModel.id == job_id,
        )
        if for_update:
            job_statement = job_statement.with_for_update()
        job_model = await self._session.scalar(job_statement)
        if job_model is None:
            raise JobAuthorityUnavailable("job_unavailable")
        grant_statement = select(AIJobExecutionGrantModel).where(
            AIJobExecutionGrantModel.tenant_id == tenant_id,
            AIJobExecutionGrantModel.job_id == job_id,
        )
        if for_update:
            grant_statement = grant_statement.with_for_update()
        grant_model = await self._session.scalar(grant_statement)
        if grant_model is None:
            raise JobAuthorityUnavailable("grant_unavailable")

        user = await self._session.scalar(
            select(UserModel).where(UserModel.id == grant_model.user_id)
        )
        tenant = await self._session.scalar(
            select(TenantModel).where(TenantModel.id == tenant_id)
        )
        membership_model = await self._session.scalar(
            select(TenantMembershipModel).where(
                TenantMembershipModel.tenant_id == tenant_id,
                TenantMembershipModel.id == grant_model.membership_id,
            )
        )
        if user is None or tenant is None or membership_model is None:
            raise JobAuthorityUnavailable("authority_unavailable")

        role_rows = (
            await self._session.execute(
                select(TenantRoleModel.code, PermissionModel.code)
                .join(
                    MembershipRoleAssignmentModel,
                    and_(
                        MembershipRoleAssignmentModel.tenant_id == TenantRoleModel.tenant_id,
                        MembershipRoleAssignmentModel.tenant_role_id == TenantRoleModel.id,
                    ),
                )
                .outerjoin(
                    TenantRolePermissionModel,
                    and_(
                        TenantRolePermissionModel.tenant_id == TenantRoleModel.tenant_id,
                        TenantRolePermissionModel.tenant_role_id == TenantRoleModel.id,
                    ),
                )
                .outerjoin(
                    PermissionModel,
                    and_(
                        PermissionModel.id == TenantRolePermissionModel.permission_id,
                        PermissionModel.status == "active",
                    ),
                )
                .where(
                    TenantRoleModel.tenant_id == tenant_id,
                    TenantRoleModel.status == "active",
                    MembershipRoleAssignmentModel.membership_id == grant_model.membership_id,
                )
            )
        ).all()
        role_codes = frozenset(row[0] for row in role_rows)
        permission_codes = frozenset(row[1] for row in role_rows if row[1] is not None)
        resolved_scope, resolved_scope_code, unknown_or_mixed = _resolve_scope(role_codes)
        return JobAuthoritySnapshot(
            job=_job(job_model),
            grant=_grant(grant_model),
            user_active=user.status == "active",
            tenant_active=tenant.status == "active",
            membership=_membership(membership_model),
            role_codes=role_codes,
            permission_codes=permission_codes,
            auth_version=user.auth_version,
            authz_version=membership_model.authz_version,
            resolved_scope=resolved_scope,
            resolved_scope_code=resolved_scope_code,
            granted_scope=scope_from_code(JobScopeCode(grant_model.job_scope_code)),
            unknown_or_mixed_role_codes=unknown_or_mixed,
        )


class JobAuthorityUnavailable(Exception):
    code = "job_authority_unavailable"

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__("job authority is unavailable")


def _resolve_scope(
    role_codes: frozenset[str],
) -> tuple[JobAuthorizationScope, JobScopeCode, bool]:
    known_scopes = [
        JOB_SCOPE_BY_ROLE[role] for role in role_codes if role in JOB_SCOPE_BY_ROLE
    ]
    unknown_or_mixed = not role_codes or any(
        role not in JOB_SCOPE_BY_ROLE for role in role_codes
    )
    if known_scopes:
        resolved = JobAuthorizationScope(
            owner=any(scope.owner for scope in known_scopes),
            shared=any(scope.shared for scope in known_scopes),
            tenant_wide=any(scope.tenant_wide for scope in known_scopes),
        )
        return resolved, resolved.scope_code(), unknown_or_mixed
    return (
        JobAuthorizationScope(False, False, False),
        JobScopeCode.OWNER_SHARED,
        unknown_or_mixed,
    )


def _require_context(context: TenantContext) -> None:
    if not isinstance(context, TenantContext):
        raise ValueError("tenant context must be strongly typed")
    require_uuid7(context.tenant_id, field="tenant context tenant_id")
    if context.membership_id is not None:
        require_uuid7(context.membership_id, field="tenant context membership_id")
    if context.membership_user_id is not None:
        require_uuid7(context.membership_user_id, field="tenant context membership_user_id")


def _naive(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("database timestamp must be UTC-aware")
    return value.astimezone(UTC).replace(tzinfo=None)


def _naive_optional(value: datetime | None) -> datetime | None:
    return None if value is None else _naive(value)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _aware_optional(value: datetime | None) -> datetime | None:
    return None if value is None else _aware(value)
