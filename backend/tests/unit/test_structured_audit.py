from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from lawyer_agent.application.audit import (
    AuditActorKind,
    StructuredAuditError,
    StructuredAuditEvent,
)

_NOW = datetime(2026, 9, 3, 1, 0, tzinfo=UTC)
_ID = UUID("0192e1a0-0000-7000-8000-000000000001")
_ID2 = UUID("0192e1a0-0000-7000-8000-000000000002")
_ID3 = UUID("0192e1a0-0000-7000-8000-000000000003")


def _event(actor_kind: AuditActorKind, action: str, **overrides: object) -> StructuredAuditEvent:
    fields: dict[str, object] = {
        "id": _ID,
        "actor_kind": actor_kind,
        "action": action,
        "result": "success",
        "reason_code": "test",
        "trace_id": "trace",
        "occurred_at": _NOW,
    }
    fields.update(overrides)
    return StructuredAuditEvent(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("actor_kind", "action", "fields"),
    [
        (
            AuditActorKind.TENANT_USER,
            "membership.read",
            {"tenant_id": _ID, "actor_user_id": _ID2, "actor_membership_id": _ID3},
        ),
        (AuditActorKind.GLOBAL_USER, "identity.authenticate", {"actor_user_id": _ID2}),
        (AuditActorKind.ANONYMOUS, "identity.authenticate", {}),
        (AuditActorKind.SYSTEM_IDENTITY_BOOTSTRAP, "platform_admin.bootstrap", {}),
        (
            AuditActorKind.SYSTEM_GLOBAL_MAINTENANCE,
            "invitation.blind_index_legacy_reconcile",
            {},
        ),
        (
            AuditActorKind.SYSTEM_WORKER,
            "ai_job.execution_started",
            {
                "tenant_id": _ID,
                "on_behalf_of_user_id": _ID2,
                "on_behalf_of_membership_id": _ID3,
                "target_job_id": _ID,
            },
        ),
        (
            AuditActorKind.SYSTEM_PUBLISHER,
            "ai_job.outbox_published",
            {"tenant_id": _ID, "target_job_id": _ID2, "message_id": _ID3},
        ),
        (
            AuditActorKind.SYSTEM_JOB_MAINTENANCE,
            "ai_job.lease_expired",
            {"tenant_id": _ID, "target_job_id": _ID2},
        ),
        (
            AuditActorKind.SYSTEM_FEATURE_ROLLOUT,
            "permission_feature.tenant_activated",
            {
                "tenant_id": _ID,
                "rollout_feature_code": "ai_job_runtime_v1",
                "rollout_generation": 1,
            },
        ),
        (
            AuditActorKind.SYSTEM_GLOBAL_FEATURE_ROLLOUT,
            "permission_feature.phase_migrated",
            {"rollout_feature_code": "ai_job_runtime_v1", "rollout_generation": 1},
        ),
        (
            AuditActorKind.PLATFORM_OPERATOR,
            "tenant_application.list",
            {"actor_user_id": _ID2},
        ),
    ],
)
def test_valid_structured_audit_matrix(
    actor_kind: AuditActorKind, action: str, fields: dict[str, object]
) -> None:
    _event(actor_kind, action, **fields)


def test_global_maintenance_writer_cannot_claim_bootstrap_kind() -> None:
    with pytest.raises(StructuredAuditError, match="actor kind/action mismatch"):
        _event(AuditActorKind.SYSTEM_GLOBAL_MAINTENANCE, "platform_admin.bootstrap")


def test_bootstrap_writer_cannot_claim_global_maintenance_kind() -> None:
    with pytest.raises(StructuredAuditError, match="actor kind/action mismatch"):
        _event(AuditActorKind.SYSTEM_IDENTITY_BOOTSTRAP, "invitation.blind_index_legacy_reconcile")


def test_on_behalf_of_fields_must_be_set_together() -> None:
    with pytest.raises(StructuredAuditError, match="on-behalf-of"):
        _event(
            AuditActorKind.SYSTEM_WORKER,
            "ai_job.execution_started",
            tenant_id=_ID,
            on_behalf_of_user_id=_ID2,
            target_job_id=_ID,
        )


def test_rollout_fields_must_be_set_together() -> None:
    with pytest.raises(StructuredAuditError, match="rollout"):
        _event(
            AuditActorKind.SYSTEM_GLOBAL_FEATURE_ROLLOUT,
            "permission_feature.phase_migrated",
            rollout_feature_code="ai_job_runtime_v1",
        )


def test_system_worker_requires_target_job() -> None:
    with pytest.raises(StructuredAuditError, match="system_worker"):
        _event(
            AuditActorKind.SYSTEM_WORKER,
            "ai_job.execution_started",
            tenant_id=_ID,
            on_behalf_of_user_id=_ID2,
            on_behalf_of_membership_id=_ID3,
        )


def test_tenant_user_requires_actor_membership() -> None:
    with pytest.raises(StructuredAuditError, match="tenant_user"):
        _event(
            AuditActorKind.TENANT_USER,
            "membership.read",
            tenant_id=_ID,
            actor_user_id=_ID2,
        )


def test_attempt_target_requires_job() -> None:
    with pytest.raises(StructuredAuditError, match="attempt or message"):
        _event(
            AuditActorKind.TENANT_USER,
            "membership.read",
            tenant_id=_ID,
            actor_user_id=_ID2,
            actor_membership_id=_ID3,
            attempt_id=_ID3,
        )
