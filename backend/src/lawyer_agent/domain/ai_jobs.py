from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from typing import Any, Final, Protocol
from uuid import UUID

from lawyer_agent.domain.common import require_uuid7

# --- Fixed runtime invariants (spec section 3) ---------------------------------

MAX_ATTEMPTS: Final[int] = 10
DEFAULT_MAX_ATTEMPTS: Final[int] = 4
RETRY_BUCKET_SECONDS: Final[tuple[int, int, int]] = (5, 30, 120)
MAX_JOB_LIFETIME: Final[timedelta] = timedelta(hours=24)
DEFAULT_LEASE_SECONDS: Final[int] = 60
DEFAULT_HEARTBEAT_SECONDS: Final[int] = 20
MAX_MESSAGE_CLOCK_SKEW_BACKWARD: Final[timedelta] = timedelta(minutes=15)
MAX_MESSAGE_CLOCK_SKEW_FORWARD: Final[timedelta] = timedelta(seconds=60)

JOB_POLICY_VERSION: Final[str] = "ai-job-policy-v1"
JOB_SCOPE_MANIFEST_VERSION: Final[str] = "ai-job-scope-v1"
SYNTHETIC_HANDLER_KIND: Final[str] = "synthetic.v1"
SYNTHETIC_INPUT_SCHEMA_VERSION: Final[str] = "synthetic-input-v1"

AI_JOB_FEATURE_CODE: Final[str] = "ai_job_runtime_v1"
AI_JOB_BASE_MANIFEST_VERSION: Final[str] = "ai-job-base-v1"
AI_JOB_FEATURE_MANIFEST_VERSION: Final[str] = "ai-job-feature-v1"
AI_JOB_PERMISSION_CODES: Final[tuple[str, ...]] = (
    "ai_job.create",
    "ai_job.read",
    "ai_job.cancel",
)


def ai_job_manifest_digest(version: str, permission_codes: tuple[str, ...]) -> bytes:
    """Deterministic SHA-256 digest of a permission feature manifest."""
    if not isinstance(version, str) or not version:
        raise ValueError("manifest version must be a non-empty string")
    if not isinstance(permission_codes, tuple):
        raise ValueError("manifest permission codes must be a tuple")
    canonical = "|".join((version, *sorted(permission_codes)))
    return sha256(f"ai-job-manifest:v1:{canonical}".encode()).digest()


class AIJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRY_SCHEDULED = "retry_scheduled"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class InboxStatus(StrEnum):
    ACCEPTED = "accepted"
    PROCESSING = "processing"
    COMPLETED = "completed"
    REJECTED = "rejected"


class PublicAIJobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    WAITING_REVIEW = "WAITING_REVIEW"


class AIJobPermission(StrEnum):
    CREATE = "ai_job.create"
    READ = "ai_job.read"
    CANCEL = "ai_job.cancel"


class JobVisibility(StrEnum):
    OWNER_ONLY = "owner_only"


class RiskClass(StrEnum):
    SYNTHETIC = "synthetic"


class ReleaseState(StrEnum):
    NON_PUBLISHABLE = "non_publishable"


class FailureClass(StrEnum):
    VALIDATION = "validation"
    AUTHORIZATION = "authorization"
    SECURITY = "security"
    TRANSIENT_INFRASTRUCTURE = "transient_infrastructure"
    PERMANENT_HANDLER = "permanent_handler"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class JobScopeCode(StrEnum):
    OWNER_SHARED = "owner_shared"
    TENANT_WIDE = "tenant_wide"


class InvalidJobTransition(ValueError):
    def __init__(self, source: AIJobStatus, target: AIJobStatus) -> None:
        self.source = source
        self.target = target
        super().__init__(f"invalid job transition: {source.value} -> {target.value}")


ALLOWED_TRANSITIONS: Final[frozenset[tuple[AIJobStatus, AIJobStatus]]] = frozenset(
    {
        (AIJobStatus.QUEUED, AIJobStatus.RUNNING),
        (AIJobStatus.QUEUED, AIJobStatus.CANCELLED),
        (AIJobStatus.QUEUED, AIJobStatus.FAILED),
        (AIJobStatus.RUNNING, AIJobStatus.SUCCEEDED),
        (AIJobStatus.RUNNING, AIJobStatus.RETRY_SCHEDULED),
        (AIJobStatus.RUNNING, AIJobStatus.FAILED),
        (AIJobStatus.RUNNING, AIJobStatus.CANCEL_REQUESTED),
        (AIJobStatus.CANCEL_REQUESTED, AIJobStatus.CANCELLED),
        (AIJobStatus.RETRY_SCHEDULED, AIJobStatus.RUNNING),
        (AIJobStatus.RETRY_SCHEDULED, AIJobStatus.CANCELLED),
        (AIJobStatus.RETRY_SCHEDULED, AIJobStatus.FAILED),
    }
)

TERMINAL_JOB_STATUSES: Final[frozenset[AIJobStatus]] = frozenset(
    {AIJobStatus.SUCCEEDED, AIJobStatus.FAILED, AIJobStatus.CANCELLED}
)

LEASE_HOLDING_STATUSES: Final[frozenset[AIJobStatus]] = frozenset(
    {AIJobStatus.RUNNING, AIJobStatus.CANCEL_REQUESTED}
)

_PUBLIC_STATUS_MAP: Final[dict[AIJobStatus, PublicAIJobStatus]] = {
    AIJobStatus.QUEUED: PublicAIJobStatus.QUEUED,
    AIJobStatus.RETRY_SCHEDULED: PublicAIJobStatus.QUEUED,
    AIJobStatus.RUNNING: PublicAIJobStatus.RUNNING,
    AIJobStatus.CANCEL_REQUESTED: PublicAIJobStatus.RUNNING,
    AIJobStatus.SUCCEEDED: PublicAIJobStatus.SUCCEEDED,
    AIJobStatus.FAILED: PublicAIJobStatus.FAILED,
    AIJobStatus.CANCELLED: PublicAIJobStatus.CANCELLED,
}


def require_transition(source: AIJobStatus, target: AIJobStatus) -> None:
    if not isinstance(source, AIJobStatus) or not isinstance(target, AIJobStatus):
        raise ValueError("job transition endpoints must be strongly typed")
    if (source, target) not in ALLOWED_TRANSITIONS:
        raise InvalidJobTransition(source, target)


def to_public_status(status: AIJobStatus) -> PublicAIJobStatus:
    if not isinstance(status, AIJobStatus):
        raise ValueError("job status must be strongly typed")
    return _PUBLIC_STATUS_MAP[status]


def retry_delay_seconds(attempt_no: int) -> int:
    """Return the fixed retry delay for the next attempt after *attempt_no* fails."""
    if isinstance(attempt_no, bool) or not isinstance(attempt_no, int) or attempt_no < 1:
        raise ValueError("attempt number must be a positive integer")
    index = min(attempt_no - 1, len(RETRY_BUCKET_SECONDS) - 1)
    return RETRY_BUCKET_SECONDS[index]


def validate_runtime_timing(*, lease: timedelta, heartbeat: timedelta) -> None:
    if not isinstance(lease, timedelta) or lease <= timedelta(0):
        raise ValueError("lease must be a positive duration")
    if not isinstance(heartbeat, timedelta) or heartbeat <= timedelta(0):
        raise ValueError("heartbeat must be a positive duration")
    if heartbeat >= lease / 2:
        raise ValueError("heartbeat must be less than half the lease")


def validate_job_lifetime(*, submitted_at: datetime, expires_at: datetime) -> None:
    _require_utc(submitted_at, "submitted_at")
    _require_utc(expires_at, "expires_at")
    if expires_at <= submitted_at:
        raise ValueError("job expires_at must be after submitted_at")
    if expires_at - submitted_at > MAX_JOB_LIFETIME:
        raise ValueError("job lifetime must not exceed 24 hours")


@dataclass(frozen=True, slots=True)
class JobAuthorizationScope:
    owner: bool
    shared: bool
    tenant_wide: bool

    def __post_init__(self) -> None:
        for name in ("owner", "shared", "tenant_wide"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"job authorization scope {name} must be a boolean")

    def scope_code(self) -> JobScopeCode:
        if self.tenant_wide:
            if not (self.owner and self.shared):
                raise ValueError("tenant-wide scope requires owner and shared access")
            return JobScopeCode.TENANT_WIDE
        if self.owner and self.shared:
            return JobScopeCode.OWNER_SHARED
        raise ValueError("job authorization scope has no supported mapping")


@dataclass(frozen=True, slots=True)
class ActorSnapshot:
    """Immutable audit-only snapshot captured at submission (never authoritative)."""

    user_id: UUID
    membership_id: UUID
    session_id: UUID
    auth_version_at_submit: int
    authz_version_at_submit: int
    role_codes_at_submit: frozenset[str]
    permission_codes_at_submit: frozenset[str]
    policy_version_at_submit: str
    authenticated_at: datetime
    submitted_at: datetime

    def __post_init__(self) -> None:
        for value, name in (
            (self.user_id, "actor user_id"),
            (self.membership_id, "actor membership_id"),
            (self.session_id, "actor session_id"),
        ):
            require_uuid7(value, field=name)
        _require_positive_int(self.auth_version_at_submit, "auth_version_at_submit")
        _require_positive_int(self.authz_version_at_submit, "authz_version_at_submit")
        if not isinstance(self.role_codes_at_submit, frozenset) or not isinstance(
            self.permission_codes_at_submit, frozenset
        ):
            raise ValueError("actor snapshot role and permission codes must be frozensets")
        if not isinstance(self.policy_version_at_submit, str) or not self.policy_version_at_submit:
            raise ValueError("actor snapshot policy version is invalid")
        _require_utc(self.authenticated_at, "authenticated_at")
        _require_utc(self.submitted_at, "submitted_at")


@dataclass(frozen=True, slots=True)
class JobExecutionGrant:
    """Non-transferable durable execution grant bound to one job and membership."""

    id: UUID
    tenant_id: UUID
    job_id: UUID
    user_id: UUID
    membership_id: UUID
    permission_code: AIJobPermission
    auth_version_at_submit: int
    authz_version_at_submit: int
    policy_version: str
    job_scope_manifest_version: str
    job_scope_code: JobScopeCode
    issued_at: datetime
    revoked_at: datetime | None = None
    revocation_reason_code: str | None = None
    version: int = 1

    def __post_init__(self) -> None:
        for value, name in (
            (self.id, "grant id"),
            (self.tenant_id, "grant tenant_id"),
            (self.job_id, "grant job_id"),
            (self.user_id, "grant user_id"),
            (self.membership_id, "grant membership_id"),
        ):
            require_uuid7(value, field=name)
        if not isinstance(self.permission_code, AIJobPermission):
            raise ValueError("grant permission code must be strongly typed")
        if self.permission_code is not AIJobPermission.CREATE:
            raise ValueError("durable grant permission code must be ai_job.create")
        _require_positive_int(self.auth_version_at_submit, "grant auth_version_at_submit")
        _require_positive_int(self.authz_version_at_submit, "grant authz_version_at_submit")
        if not isinstance(self.policy_version, str) or not self.policy_version:
            raise ValueError("grant policy version is invalid")
        if (
            not isinstance(self.job_scope_manifest_version, str)
            or not self.job_scope_manifest_version
        ):
            raise ValueError("grant job scope manifest version is invalid")
        if not isinstance(self.job_scope_code, JobScopeCode):
            raise ValueError("grant job scope code must be strongly typed")
        _require_utc(self.issued_at, "grant issued_at")
        _require_positive_int(self.version, "grant version")
        if self.revoked_at is not None:
            _require_utc(self.revoked_at, "grant revoked_at")
        if (self.revoked_at is None) != (self.revocation_reason_code is None):
            raise ValueError("grant revocation timestamp and reason must be set together")


@dataclass(frozen=True, slots=True)
class JobExecutionPrincipal:
    """Worker-side limited continuing authorization, rebuilt from MySQL each boundary."""

    user_id: UUID
    tenant_id: UUID
    membership_id: UUID
    role_codes: frozenset[str]
    permission_codes: frozenset[str]

    def __post_init__(self) -> None:
        for value, name in (
            (self.user_id, "principal user_id"),
            (self.tenant_id, "principal tenant_id"),
            (self.membership_id, "principal membership_id"),
        ):
            require_uuid7(value, field=name)
        if not isinstance(self.role_codes, frozenset) or not isinstance(
            self.permission_codes, frozenset
        ):
            raise ValueError("principal role and permission codes must be frozensets")


class CancellationProbe(Protocol):
    async def observe(self) -> bool: ...


class LeaseHeartbeat(Protocol):
    async def extend(self) -> None: ...


class StepEffectStore(Protocol):
    async def apply(self, step_code: str, effect_key: str, result: object) -> object: ...


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Read-only strong-typed execution context for a single Handler attempt."""

    job_id: UUID
    tenant_id: UUID
    handler_code: str
    handler_version: str
    actor_principal: JobExecutionPrincipal
    tenant_context: object
    grant: JobExecutionGrant
    scope: JobAuthorizationScope
    attempt_id: UUID
    attempt_no: int
    lease_token: UUID
    lease_fence: int
    lease_expires_at: datetime
    correlation_id: UUID
    trigger_message_id: UUID
    input: object
    cancellation_probe: CancellationProbe
    effect_store: StepEffectStore
    lease_heartbeat: LeaseHeartbeat

    def __post_init__(self) -> None:
        for value, name in (
            (self.job_id, "execution job_id"),
            (self.tenant_id, "execution tenant_id"),
            (self.attempt_id, "execution attempt_id"),
            (self.lease_token, "execution lease_token"),
            (self.correlation_id, "execution correlation_id"),
            (self.trigger_message_id, "execution trigger_message_id"),
        ):
            require_uuid7(value, field=name)
        if not isinstance(self.handler_code, str) or not self.handler_code:
            raise ValueError("execution handler code is invalid")
        if not isinstance(self.handler_version, str) or not self.handler_version:
            raise ValueError("execution handler version is invalid")
        _require_positive_int(self.attempt_no, "execution attempt_no")
        _require_positive_int(self.lease_fence, "execution lease_fence")
        _require_utc(self.lease_expires_at, "execution lease_expires_at")


def _require_utc(value: object, name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != UTC.utcoffset(value)
    ):
        raise ValueError(f"{name} must be UTC-aware")


def _require_positive_int(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class AIJob:
    """Authoritative job projection loaded from MySQL for HTTP and Worker boundaries."""

    id: UUID
    tenant_id: UUID
    handler_code: str
    handler_version: str
    input_schema_version: str
    input_json: dict[str, Any]
    input_fingerprint: bytes
    created_by_user_id: UUID
    created_by_membership_id: UUID
    created_by_session_id: UUID
    actor_snapshot_json: dict[str, Any]
    policy_version_at_submit: str
    visibility: JobVisibility
    status: AIJobStatus
    current_attempt_no: int
    max_attempts: int
    risk_class: RiskClass
    release_state: ReleaseState
    idempotency_record_id: UUID
    correlation_id: UUID
    submitted_at: datetime
    expires_at: datetime
    version: int
    next_attempt_at: datetime | None = None
    lease_owner: str | None = None
    lease_token: UUID | None = None
    lease_fence: int | None = None
    lease_expires_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    cancel_requested_by_user_id: UUID | None = None
    cancel_reason_code: str | None = None
    result_json: dict[str, Any] | None = None
    failure_class: str | None = None
    failure_code: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.id, "job id"),
            (self.tenant_id, "job tenant_id"),
            (self.created_by_user_id, "job created_by_user_id"),
            (self.created_by_membership_id, "job created_by_membership_id"),
            (self.created_by_session_id, "job created_by_session_id"),
            (self.idempotency_record_id, "job idempotency_record_id"),
            (self.correlation_id, "job correlation_id"),
        ):
            require_uuid7(value, field=name)
        if self.lease_token is not None:
            require_uuid7(self.lease_token, field="job lease_token")
        if self.cancel_requested_by_user_id is not None:
            require_uuid7(self.cancel_requested_by_user_id, field="job cancel_requested_by_user_id")
        if not isinstance(self.status, AIJobStatus):
            raise ValueError("job status must be strongly typed")
        if not isinstance(self.visibility, JobVisibility):
            raise ValueError("job visibility must be strongly typed")
        if not isinstance(self.risk_class, RiskClass) or not isinstance(
            self.release_state, ReleaseState
        ):
            raise ValueError("job risk/release must be strongly typed")
        if not isinstance(self.input_json, dict) or not isinstance(
            self.actor_snapshot_json, dict
        ):
            raise ValueError("job JSON payloads must be mappings")
        if not isinstance(self.input_fingerprint, bytes) or len(self.input_fingerprint) != 32:
            raise ValueError("job input fingerprint must be 32 bytes")
        _require_positive_int(self.version, "job version")
        _require_positive_int(self.max_attempts, "job max_attempts")
        if self.max_attempts > MAX_ATTEMPTS:
            raise ValueError("job max_attempts exceeds the limit")
        if (
            isinstance(self.current_attempt_no, bool)
            or not isinstance(self.current_attempt_no, int)
            or not 0 <= self.current_attempt_no <= self.max_attempts
        ):
            raise ValueError("job current_attempt_no is out of range")
        for timestamp, field_name in (
            (self.submitted_at, "job submitted_at"),
            (self.expires_at, "job expires_at"),
        ):
            _require_utc(timestamp, field_name)
        for optional_timestamp, optional_name in (
            (self.next_attempt_at, "job next_attempt_at"),
            (self.lease_expires_at, "job lease_expires_at"),
            (self.cancel_requested_at, "job cancel_requested_at"),
            (self.started_at, "job started_at"),
            (self.completed_at, "job completed_at"),
        ):
            if optional_timestamp is not None:
                _require_utc(optional_timestamp, optional_name)


@dataclass(frozen=True, slots=True)
class JobAccess:
    tenant_id: UUID
    job_id: UUID
    membership_id: UUID
    access_level: str
    generation: int
    revoked_at: datetime | None

    def __post_init__(self) -> None:
        for value, name in (
            (self.tenant_id, "access tenant_id"),
            (self.job_id, "access job_id"),
            (self.membership_id, "access membership_id"),
        ):
            require_uuid7(value, field=name)
        if self.access_level != "read":
            raise ValueError("job access level must be read")
        _require_positive_int(self.generation, "access generation")
        if self.revoked_at is not None:
            _require_utc(self.revoked_at, "access revoked_at")

    @property
    def active(self) -> bool:
        return self.revoked_at is None


@dataclass(frozen=True, slots=True)
class NewAIJobOutbox:
    id: UUID
    tenant_id: UUID
    job_id: UUID
    dispatch_generation: int
    envelope_generation: int
    max_envelope_generations: int
    event_type: str
    routing_key: str
    schema_version: int
    available_at: datetime

    def __post_init__(self) -> None:
        for value, name in (
            (self.id, "outbox id"),
            (self.tenant_id, "outbox tenant_id"),
            (self.job_id, "outbox job_id"),
        ):
            require_uuid7(value, field=name)
        _require_positive_int(self.dispatch_generation, "outbox dispatch_generation")
        _require_positive_int(self.envelope_generation, "outbox envelope_generation")
        _require_positive_int(self.max_envelope_generations, "outbox max_envelope_generations")
        if self.envelope_generation > self.max_envelope_generations:
            raise ValueError("outbox envelope generation exceeds its cap")
        if not isinstance(self.event_type, str) or not self.event_type:
            raise ValueError("outbox event type is invalid")
        if not isinstance(self.routing_key, str) or not self.routing_key:
            raise ValueError("outbox routing key is invalid")
        _require_positive_int(self.schema_version, "outbox schema_version")
        _require_utc(self.available_at, "outbox available_at")


@dataclass(frozen=True, slots=True)
class NewAIJobGraph:
    job: AIJob
    grant: JobExecutionGrant
    outbox: NewAIJobOutbox

    def __post_init__(self) -> None:
        if not isinstance(self.job, AIJob) or not isinstance(
            self.grant, JobExecutionGrant
        ) or not isinstance(self.outbox, NewAIJobOutbox):
            raise ValueError("job graph must contain strongly typed parts")
        if (
            self.job.tenant_id != self.grant.tenant_id
            or self.job.tenant_id != self.outbox.tenant_id
            or self.job.id != self.grant.job_id
            or self.job.id != self.outbox.job_id
        ):
            raise ValueError("job graph parts must reference the same tenant and job")


@dataclass(frozen=True, slots=True)
class ClaimJobRequest:
    tenant_id: UUID
    job_id: UUID
    worker_instance_ref: str
    lease_token: UUID
    lease_fence: int
    attempt_id: UUID
    attempt_no: int
    trigger_message_id: UUID
    handler_code: str
    handler_version: str
    started_at: datetime
    lease_expires_at: datetime

    def __post_init__(self) -> None:
        for value, name in (
            (self.tenant_id, "claim tenant_id"),
            (self.job_id, "claim job_id"),
            (self.lease_token, "claim lease_token"),
            (self.attempt_id, "claim attempt_id"),
            (self.trigger_message_id, "claim trigger_message_id"),
        ):
            require_uuid7(value, field=name)
        if not isinstance(self.worker_instance_ref, str) or not self.worker_instance_ref:
            raise ValueError("claim worker instance ref is invalid")
        _require_positive_int(self.lease_fence, "claim lease_fence")
        _require_positive_int(self.attempt_no, "claim attempt_no")
        if not isinstance(self.handler_code, str) or not self.handler_code:
            raise ValueError("claim handler code is invalid")
        if not isinstance(self.handler_version, str) or not self.handler_version:
            raise ValueError("claim handler version is invalid")
        _require_utc(self.started_at, "claim started_at")
        _require_utc(self.lease_expires_at, "claim lease_expires_at")
        if self.lease_expires_at <= self.started_at:
            raise ValueError("claim lease expiry must be after start")


@dataclass(frozen=True, slots=True)
class ClaimedExecution:
    job: AIJob
    attempt_id: UUID
    attempt_no: int
    lease_token: UUID
    lease_fence: int
    trigger_message_id: UUID
    worker_instance_ref: str
    lease_expires_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.job, AIJob):
            raise ValueError("claimed execution job must be strongly typed")
        for value, name in (
            (self.attempt_id, "claimed attempt_id"),
            (self.lease_token, "claimed lease_token"),
            (self.trigger_message_id, "claimed trigger_message_id"),
        ):
            require_uuid7(value, field=name)
        _require_positive_int(self.attempt_no, "claimed attempt_no")
        _require_positive_int(self.lease_fence, "claimed lease_fence")
        _require_utc(self.lease_expires_at, "claimed lease_expires_at")


@dataclass(frozen=True, slots=True)
class ClaimRejected:
    reason_code: str

    def __post_init__(self) -> None:
        if not isinstance(self.reason_code, str) or not self.reason_code:
            raise ValueError("claim rejection reason must be non-empty text")


@dataclass(frozen=True, slots=True)
class FencedHeartbeat:
    tenant_id: UUID
    job_id: UUID
    lease_token: UUID
    lease_fence: int
    expected_version: int
    heartbeat_at: datetime
    lease_expires_at: datetime

    def __post_init__(self) -> None:
        for value, name in (
            (self.tenant_id, "heartbeat tenant_id"),
            (self.job_id, "heartbeat job_id"),
            (self.lease_token, "heartbeat lease_token"),
        ):
            require_uuid7(value, field=name)
        _require_positive_int(self.lease_fence, "heartbeat lease_fence")
        _require_positive_int(self.expected_version, "heartbeat expected_version")
        _require_utc(self.heartbeat_at, "heartbeat heartbeat_at")
        _require_utc(self.lease_expires_at, "heartbeat lease_expires_at")
        if self.lease_expires_at <= self.heartbeat_at:
            raise ValueError("heartbeat lease expiry must be after heartbeat time")


@dataclass(frozen=True, slots=True)
class FencedFinalizeSuccess:
    tenant_id: UUID
    job_id: UUID
    lease_token: UUID
    lease_fence: int
    expected_version: int
    result: dict[str, Any]
    now: datetime

    def __post_init__(self) -> None:
        for value, name in (
            (self.tenant_id, "finalize tenant_id"),
            (self.job_id, "finalize job_id"),
            (self.lease_token, "finalize lease_token"),
        ):
            require_uuid7(value, field=name)
        _require_positive_int(self.lease_fence, "finalize lease_fence")
        _require_positive_int(self.expected_version, "finalize expected_version")
        if not isinstance(self.result, dict):
            raise ValueError("finalize result must be a mapping")
        _require_utc(self.now, "finalize now")
