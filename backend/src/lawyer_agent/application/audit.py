from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from lawyer_agent.domain.common import new_uuid7, require_uuid7

_BOOTSTRAP_ACTIONS = frozenset({"platform_admin.bootstrap"})
_GLOBAL_MAINTENANCE_ACTIONS = frozenset({"invitation.blind_index_legacy_reconcile"})
_TENANT_USER_PREFIXES = (
    "tenant.",
    "membership.",
    "department.",
    "role.",
    "invitation.",
    "ai_job.",
    "risk_issue.",
    "document.",
    "rule_pack.",
    "matter.",
)
_GLOBAL_USER_PREFIXES = ("identity.", "credential.", "account.")
_ANONYMOUS_ACTIONS = frozenset({"identity.authenticate", "identity.register"})
_PLATFORM_PREFIXES = ("platform.", "tenant_application.")
_FEATURE_ROLLOUT_ACTIONS = frozenset(
    {"permission_feature.phase_migrated", "permission_feature.tenant_activated"}
)


class AuditActorKind(StrEnum):
    TENANT_USER = "tenant_user"
    GLOBAL_USER = "global_user"
    ANONYMOUS = "anonymous"
    SYSTEM_IDENTITY_BOOTSTRAP = "system_identity_bootstrap"
    SYSTEM_GLOBAL_MAINTENANCE = "system_global_maintenance"
    SYSTEM_WORKER = "system_worker"
    SYSTEM_PUBLISHER = "system_publisher"
    SYSTEM_JOB_MAINTENANCE = "system_job_maintenance"
    SYSTEM_FEATURE_ROLLOUT = "system_feature_rollout"
    SYSTEM_GLOBAL_FEATURE_ROLLOUT = "system_global_feature_rollout"
    PLATFORM_OPERATOR = "platform_operator"


class StructuredAuditError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class StructuredAuditEvent:
    """Strongly typed audit event that maps to non-NULL actor_kind rows."""

    id: UUID
    actor_kind: AuditActorKind
    action: str
    result: str
    reason_code: str
    trace_id: str
    occurred_at: datetime
    tenant_id: UUID | None = None
    actor_user_id: UUID | None = None
    actor_membership_id: UUID | None = None
    on_behalf_of_user_id: UUID | None = None
    on_behalf_of_membership_id: UUID | None = None
    target_job_id: UUID | None = None
    attempt_id: UUID | None = None
    message_id: UUID | None = None
    rollout_feature_code: str | None = None
    rollout_generation: int | None = None
    target_type: str | None = None
    target_id: UUID | None = None
    client_ip_hash: bytes | None = None
    user_agent_hash: bytes | None = None

    def __post_init__(self) -> None:
        require_uuid7(self.id, field="audit id")
        if not isinstance(self.actor_kind, AuditActorKind):
            raise StructuredAuditError("audit actor kind must be strongly typed")
        if not isinstance(self.action, str) or not self.action:
            raise StructuredAuditError("audit action must be non-empty text")
        if self.result not in {"success", "failure", "denied"}:
            raise StructuredAuditError("audit result is invalid")
        if not isinstance(self.reason_code, str) or not self.reason_code:
            raise StructuredAuditError("audit reason code must be non-empty text")
        if not isinstance(self.trace_id, str) or not self.trace_id:
            raise StructuredAuditError("audit trace id must be non-empty text")
        _require_utc(self.occurred_at, "audit occurred_at")
        for value, name in (
            (self.tenant_id, "audit tenant_id"),
            (self.actor_user_id, "audit actor_user_id"),
            (self.actor_membership_id, "audit actor_membership_id"),
            (self.on_behalf_of_user_id, "audit on_behalf_of_user_id"),
            (self.on_behalf_of_membership_id, "audit on_behalf_of_membership_id"),
            (self.target_job_id, "audit target_job_id"),
            (self.attempt_id, "audit attempt_id"),
            (self.message_id, "audit message_id"),
            (self.target_id, "audit target_id"),
        ):
            if value is not None:
                require_uuid7(value, field=name)
        for hash_value, hash_name in (
            (self.client_ip_hash, "client ip hash"),
            (self.user_agent_hash, "user agent hash"),
        ):
            if hash_value is not None and len(hash_value) != 32:
                raise StructuredAuditError(f"{hash_name} must be 32 bytes")
        if self.rollout_generation is not None and (
            isinstance(self.rollout_generation, bool)
            or not isinstance(self.rollout_generation, int)
            or self.rollout_generation < 0
        ):
            raise StructuredAuditError("rollout generation must be a non-negative integer")
        _validate_common_grouping(self)
        _validate_actor_kind_matrix(self)
        _validate_action_allowlist(self)


def _validate_common_grouping(event: StructuredAuditEvent) -> None:
    if event.actor_membership_id is not None and (
        event.tenant_id is None or event.actor_user_id is None
    ):
        raise StructuredAuditError("actor membership requires tenant and actor user")
    if (event.on_behalf_of_user_id is None) != (event.on_behalf_of_membership_id is None):
        raise StructuredAuditError("on-behalf-of fields must be set together")
    if event.on_behalf_of_user_id is not None and event.tenant_id is None:
        raise StructuredAuditError("on-behalf-of requires tenant")
    if (event.attempt_id is not None or event.message_id is not None) and (
        event.target_job_id is None
    ):
        raise StructuredAuditError("attempt or message target requires a target job")
    if (
        event.target_job_id is not None
        or event.attempt_id is not None
        or event.message_id is not None
    ) and event.tenant_id is None:
        raise StructuredAuditError("job targets require tenant")
    if (event.rollout_feature_code is None) != (event.rollout_generation is None):
        raise StructuredAuditError("rollout feature and generation must be set together")


def _validate_actor_kind_matrix(event: StructuredAuditEvent) -> None:
    kind = event.actor_kind
    actor_absent = event.actor_user_id is None and event.actor_membership_id is None
    on_behalf_absent = (
        event.on_behalf_of_user_id is None and event.on_behalf_of_membership_id is None
    )
    rollout_absent = event.rollout_feature_code is None and event.rollout_generation is None
    target_absent = (
        event.target_job_id is None and event.attempt_id is None and event.message_id is None
    )
    if kind is AuditActorKind.TENANT_USER:
        if (
            event.tenant_id is None
            or event.actor_user_id is None
            or event.actor_membership_id is None
            or not on_behalf_absent
            or not rollout_absent
        ):
            raise StructuredAuditError("tenant_user audit matrix is invalid")
    elif kind is AuditActorKind.SYSTEM_WORKER:
        if (
            event.tenant_id is None
            or on_behalf_absent
            or event.target_job_id is None
            or not actor_absent
            or not rollout_absent
        ):
            raise StructuredAuditError("system_worker audit matrix is invalid")
    elif kind is AuditActorKind.SYSTEM_PUBLISHER:
        if (
            event.tenant_id is None
            or event.target_job_id is None
            or event.message_id is None
            or not actor_absent
            or not rollout_absent
        ):
            raise StructuredAuditError("system_publisher audit matrix is invalid")
    elif kind is AuditActorKind.SYSTEM_JOB_MAINTENANCE:
        if (
            event.tenant_id is None
            or event.target_job_id is None
            or not actor_absent
            or not rollout_absent
        ):
            raise StructuredAuditError("system_job_maintenance audit matrix is invalid")
    elif kind is AuditActorKind.SYSTEM_FEATURE_ROLLOUT:
        if (
            event.tenant_id is None
            or rollout_absent
            or not actor_absent
            or not on_behalf_absent
            or not target_absent
        ):
            raise StructuredAuditError("system_feature_rollout audit matrix is invalid")
    elif kind is AuditActorKind.SYSTEM_GLOBAL_FEATURE_ROLLOUT:
        if (
            event.tenant_id is not None
            or not actor_absent
            or not on_behalf_absent
            or not target_absent
            or rollout_absent
        ):
            raise StructuredAuditError("system_global_feature_rollout audit matrix is invalid")
    elif kind is AuditActorKind.GLOBAL_USER:
        if (
            event.tenant_id is not None
            or event.actor_user_id is None
            or event.actor_membership_id is not None
            or not on_behalf_absent
            or not target_absent
            or not rollout_absent
        ):
            raise StructuredAuditError("global_user audit matrix is invalid")
    elif kind in {
        AuditActorKind.ANONYMOUS,
        AuditActorKind.SYSTEM_IDENTITY_BOOTSTRAP,
        AuditActorKind.SYSTEM_GLOBAL_MAINTENANCE,
    }:
        if (
            event.tenant_id is not None
            or not actor_absent
            or not on_behalf_absent
            or not target_absent
            or not rollout_absent
        ):
            raise StructuredAuditError(f"{kind.value} audit matrix is invalid")
    elif kind is AuditActorKind.PLATFORM_OPERATOR:
        if (
            event.actor_user_id is None
            or event.actor_membership_id is not None
            or not on_behalf_absent
            or not rollout_absent
        ):
            raise StructuredAuditError("platform_operator audit matrix is invalid")


def _validate_action_allowlist(event: StructuredAuditEvent) -> None:
    kind = event.actor_kind
    action = event.action
    if kind is AuditActorKind.SYSTEM_IDENTITY_BOOTSTRAP:
        if action not in _BOOTSTRAP_ACTIONS:
            raise StructuredAuditError("actor kind/action mismatch")
    elif kind is AuditActorKind.SYSTEM_GLOBAL_MAINTENANCE:
        if action not in _GLOBAL_MAINTENANCE_ACTIONS:
            raise StructuredAuditError("actor kind/action mismatch")
    elif kind is AuditActorKind.TENANT_USER:
        if not action.startswith(_TENANT_USER_PREFIXES):
            raise StructuredAuditError("actor kind/action mismatch")
    elif kind is AuditActorKind.GLOBAL_USER:
        if not action.startswith(_GLOBAL_USER_PREFIXES):
            raise StructuredAuditError("actor kind/action mismatch")
    elif kind is AuditActorKind.ANONYMOUS:
        if action not in _ANONYMOUS_ACTIONS:
            raise StructuredAuditError("actor kind/action mismatch")
    elif kind in {
        AuditActorKind.SYSTEM_WORKER,
        AuditActorKind.SYSTEM_PUBLISHER,
        AuditActorKind.SYSTEM_JOB_MAINTENANCE,
    }:
        if not action.startswith("ai_job."):
            raise StructuredAuditError("actor kind/action mismatch")
    elif kind in {
        AuditActorKind.SYSTEM_FEATURE_ROLLOUT,
        AuditActorKind.SYSTEM_GLOBAL_FEATURE_ROLLOUT,
    }:
        if action not in _FEATURE_ROLLOUT_ACTIONS:
            raise StructuredAuditError("actor kind/action mismatch")
    elif kind is AuditActorKind.PLATFORM_OPERATOR:
        if not action.startswith(_PLATFORM_PREFIXES):
            raise StructuredAuditError("actor kind/action mismatch")


class StructuredAuditRepositoryPort(Protocol):
    async def append_structured(self, event: StructuredAuditEvent) -> None: ...


def new_tenant_user_audit_event(
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    actor_membership_id: UUID,
    action: str,
    reason_code: str,
    trace_id: str,
    target_type: str | None = None,
    target_id: UUID | None = None,
    result: str = "success",
    occurred_at: datetime | None = None,
) -> StructuredAuditEvent:
    """Build a TENANT_USER structured audit event for a tenant write action."""
    if result not in {"success", "failure", "denied"}:
        raise StructuredAuditError("audit result is invalid")
    return StructuredAuditEvent(
        id=new_uuid7(),
        actor_kind=AuditActorKind.TENANT_USER,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        actor_membership_id=actor_membership_id,
        action=action,
        result=result,
        reason_code=reason_code,
        trace_id=trace_id,
        target_type=target_type,
        target_id=target_id,
        occurred_at=occurred_at or datetime.now(UTC),
    )


def _require_utc(value: object, name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != UTC.utcoffset(value)
    ):
        raise StructuredAuditError(f"{name} must be UTC-aware")
