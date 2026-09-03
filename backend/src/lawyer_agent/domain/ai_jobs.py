from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from typing import Final, Protocol
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
