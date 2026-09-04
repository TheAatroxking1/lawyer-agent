from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from lawyer_agent.application.idempotency import (
    IdempotencyFingerprintPayload,
    IdempotencyRepositoryPort,
    IdempotencyRequest,
    IdempotencyResultReference,
    IdempotencyScope,
    IdempotencyScopeType,
    IdempotencyService,
)
from lawyer_agent.domain.ai_jobs import (
    JOB_POLICY_VERSION,
    JOB_SCOPE_MANIFEST_VERSION,
    SYNTHETIC_HANDLER_KIND,
    SYNTHETIC_INPUT_SCHEMA_VERSION,
    AIJob,
    AIJobPermission,
    AIJobStatus,
    JobExecutionGrant,
    JobScopeCode,
    JobVisibility,
    NewAIJobGraph,
    NewAIJobOutbox,
    ReleaseState,
    RiskClass,
)
from lawyer_agent.domain.authorization import AuthorizationScope
from lawyer_agent.domain.common import new_uuid7, require_uuid7
from lawyer_agent.domain.tenancy import (
    MembershipStatus,
    TenantContext,
    TenantStatus,
)

_JOB_OPERATION = "ai_job.create"
_JOB_RESULT_TYPE = "ai_job.job"
_CREATE_ROUTE = "/api/v1/tenants/{tenant_id}/ai-jobs"


@dataclass(frozen=True, slots=True)
class CreateAIJobCommand:
    tenant_id: UUID
    membership_id: UUID
    user_id: UUID
    session_id: UUID
    auth_version: int
    authz_version: int
    idempotency_key: str = field(repr=False)
    kind: str = SYNTHETIC_HANDLER_KIND
    scenario: str = "success"
    work_units: int = 1

    def __post_init__(self) -> None:
        for value, name in (
            (self.tenant_id, "command tenant_id"),
            (self.membership_id, "command membership_id"),
            (self.user_id, "command user_id"),
            (self.session_id, "command session_id"),
        ):
            require_uuid7(value, field=name)
        if not isinstance(self.idempotency_key, str) or not self.idempotency_key:
            raise ValueError("idempotency key must be non-empty text")
        if self.kind != SYNTHETIC_HANDLER_KIND:
            raise ValueError("only the synthetic handler is currently available")
        if self.scenario not in {"success", "retry_once", "permanent_failure", "wait_for_cancel"}:
            raise ValueError("unknown synthetic scenario")
        if isinstance(self.work_units, bool) or not isinstance(self.work_units, int):
            raise ValueError("work units must be an integer")
        if not 1 <= self.work_units <= 10:
            raise ValueError("work units must be between 1 and 10")


@dataclass(frozen=True, slots=True)
class AcceptedAIJob:
    job_id: UUID
    tenant_id: UUID
    etag: str
    replayed: bool = False


class AIJobServiceUnavailable(Exception):
    code = "ai_job_handler_unavailable"


class AIJobAccessDenied(Exception):
    code = "ai_job_access_denied"


class AIJobNotFound(Exception):
    code = "ai_job_not_found"


class AIJobServiceRuntimePort(Protocol):
    async def cancel_claimable_job(
        self,
        *,
        tenant_id: UUID,
        job_id: UUID,
        now: datetime,
    ) -> bool: ...

    async def lookup_membership_permissions(
        self,
        tenant_id: UUID,
        membership_id: UUID,
    ) -> frozenset[str]: ...


class AIJobServiceJobsPort(Protocol):
    async def get(
        self,
        context: TenantContext,
        job_id: UUID,
        *,
        for_update: bool = False,
    ) -> AIJob | None: ...

    async def add_job_graph(self, graph: NewAIJobGraph) -> None: ...


class AIJobServiceUnitOfWork(Protocol):
    jobs: AIJobServiceJobsPort
    runtime: AIJobServiceRuntimePort
    idempotency: IdempotencyRepositoryPort

    async def __aenter__(self) -> AIJobServiceUnitOfWork: ...

    async def __aexit__(self, *exc: object) -> None: ...


class AIJobProjection:
    def __init__(self, job: AIJob) -> None:
        self.job = job


def _job_context(command: CreateAIJobCommand, now: datetime) -> TenantContext:
    return TenantContext(
        tenant_id=command.tenant_id,
        membership_id=command.membership_id,
        membership_user_id=command.user_id,
        department_id=None,
        tenant_status=TenantStatus.ACTIVE,
        membership_status=MembershipStatus.ACTIVE,
        valid_from=now - timedelta(minutes=1),
        valid_until=None,
        authz_version=command.authz_version,
        session_authz_version=command.authz_version,
        scope=AuthorizationScope(allow_tenant_wide=True),
    )


AIJobServiceUowFactory = Callable[..., Any]


class AIJobService:
    """HTTP-facing AI Job application service (Owner-only synthetic creation)."""

    def __init__(
        self,
        uow_factory: AIJobServiceUowFactory,
        idempotency: IdempotencyService,
        *,
        now: datetime | None = None,
    ) -> None:
        if not callable(uow_factory):
            raise ValueError("ai job service requires a unit of work factory")
        if not isinstance(idempotency, IdempotencyService):
            raise ValueError("ai job service requires an idempotency service")
        self._uow_factory = uow_factory
        self._idempotency = idempotency
        self._now = now

    def _clock(self) -> datetime:
        return self._now if self._now is not None else datetime.now(UTC)

    async def create(self, command: CreateAIJobCommand) -> AcceptedAIJob:
        if not isinstance(command, CreateAIJobCommand):
            raise ValueError("ai job command must be strongly typed")
        now = self._clock()

        async with self._uow_factory() as uow:
            reloaded = await uow.runtime.lookup_membership_permissions(
                command.tenant_id, command.membership_id
            )
            if AIJobPermission.CREATE.value not in reloaded:
                raise AIJobAccessDenied
            reservation = await self._idempotency.reserve(
                uow.idempotency,
                scope=IdempotencyScope(
                    IdempotencyScopeType.MEMBERSHIP,
                    command.membership_id,
                    tenant_id=command.tenant_id,
                ),
                operation=_JOB_OPERATION,
                request=IdempotencyRequest(
                    key=command.idempotency_key,
                    method="POST",
                    canonical_route=_CREATE_ROUTE,
                    body=IdempotencyFingerprintPayload(
                        values={
                            "kind": command.kind,
                            "scenario": command.scenario,
                            "work_units": command.work_units,
                        },
                        business_paths=frozenset(
                            {("kind",), ("scenario",), ("work_units",)}
                        ),
                    ),
                ),
                now=now,
            )
            if reservation.replay is not None:
                replay_job = await uow.jobs.get(
                    _job_context(command, now), reservation.replay.result_id
                )
                if replay_job is None:
                    raise AIJobNotFound
                return AcceptedAIJob(
                    job_id=replay_job.id,
                    tenant_id=command.tenant_id,
                    etag=f'"{replay_job.version}"',
                    replayed=True,
                )

            job_id = new_uuid7()
            submitted_at = now
            expires_at = now + timedelta(hours=1)
            job = AIJob(
                id=job_id,
                tenant_id=command.tenant_id,
                handler_code=command.kind,
                handler_version="v1",
                input_schema_version=SYNTHETIC_INPUT_SCHEMA_VERSION,
                input_json={"scenario": command.scenario, "work_units": command.work_units},
                input_fingerprint=b"i" * 32,
                created_by_user_id=command.user_id,
                created_by_membership_id=command.membership_id,
                created_by_session_id=command.session_id,
                actor_snapshot_json={
                    "user_id": str(command.user_id),
                    "membership_id": str(command.membership_id),
                    "session_id": str(command.session_id),
                    "auth_version": command.auth_version,
                    "authz_version": command.authz_version,
                },
                policy_version_at_submit=JOB_POLICY_VERSION,
                visibility=JobVisibility.OWNER_ONLY,
                status=AIJobStatus.QUEUED,
                current_attempt_no=0,
                max_attempts=4,
                risk_class=RiskClass.SYNTHETIC,
                release_state=ReleaseState.NON_PUBLISHABLE,
                idempotency_record_id=reservation.record_id,
                correlation_id=new_uuid7(),
                submitted_at=submitted_at,
                expires_at=expires_at,
                version=1,
            )
            grant = JobExecutionGrant(
                id=new_uuid7(),
                tenant_id=command.tenant_id,
                job_id=job_id,
                user_id=command.user_id,
                membership_id=command.membership_id,
                permission_code=AIJobPermission.CREATE,
                auth_version_at_submit=command.auth_version,
                authz_version_at_submit=command.authz_version,
                policy_version=JOB_POLICY_VERSION,
                job_scope_manifest_version=JOB_SCOPE_MANIFEST_VERSION,
                job_scope_code=JobScopeCode.OWNER_SHARED,
                issued_at=submitted_at,
                version=1,
            )
            outbox = NewAIJobOutbox(
                id=new_uuid7(),
                tenant_id=command.tenant_id,
                job_id=job_id,
                dispatch_generation=1,
                envelope_generation=1,
                max_envelope_generations=4,
                event_type="ai_job.execute_requested",
                routing_key="ai.job.execute",
                schema_version=1,
                available_at=submitted_at,
            )
            await uow.jobs.add_job_graph(
                NewAIJobGraph(job=job, grant=grant, outbox=outbox)
            )
            await self._idempotency.complete(
                uow.idempotency,
                reservation,
                IdempotencyResultReference(_JOB_RESULT_TYPE, job_id),
                now=now,
            )
        return AcceptedAIJob(
            job_id=job_id,
            tenant_id=command.tenant_id,
            etag='"{1}"',
        )

    async def get(self, context: TenantContext, job_id: UUID) -> AIJobProjection:
        async with self._uow_factory() as uow:
            job = await uow.jobs.get(context, job_id)
        if job is None:
            raise AIJobNotFound
        return AIJobProjection(job)

    async def cancel(self, context: TenantContext, job_id: UUID) -> None:
        async with self._uow_factory() as uow:
            if not await uow.runtime.cancel_claimable_job(
                tenant_id=context.tenant_id,
                job_id=job_id,
                now=self._clock(),
            ):
                raise AIJobNotFound
