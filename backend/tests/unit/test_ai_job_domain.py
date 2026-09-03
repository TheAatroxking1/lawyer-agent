from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from lawyer_agent.domain.ai_jobs import (
    ALLOWED_TRANSITIONS,
    MAX_JOB_LIFETIME,
    RETRY_BUCKET_SECONDS,
    TERMINAL_JOB_STATUSES,
    ActorSnapshot,
    AIJobPermission,
    AIJobStatus,
    InvalidJobTransition,
    JobAuthorizationScope,
    JobExecutionGrant,
    JobExecutionPrincipal,
    JobScopeCode,
    PublicAIJobStatus,
    ReleaseState,
    RiskClass,
    retry_delay_seconds,
    to_public_status,
    validate_job_lifetime,
    validate_runtime_timing,
)

_NOW = datetime(2026, 9, 3, 1, 2, 3, tzinfo=UTC)
_ID = UUID("0192e1a0-0000-7000-8000-000000000001")
_ID2 = UUID("0192e1a0-0000-7000-8000-000000000002")
_ID3 = UUID("0192e1a0-0000-7000-8000-000000000003")


@pytest.mark.parametrize(
    ("source", "target"),
    [
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
    ],
)
def test_allowed_job_transitions(source: AIJobStatus, target: AIJobStatus) -> None:
    from lawyer_agent.domain.ai_jobs import require_transition

    require_transition(source, target)


def test_transition_table_is_exhaustive_for_expected_set() -> None:
    expected = {
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
    assert ALLOWED_TRANSITIONS == expected


@pytest.mark.parametrize("target", [AIJobStatus.SUCCEEDED, AIJobStatus.FAILED])
def test_cancel_requested_has_only_cancelled_successor(target: AIJobStatus) -> None:
    from lawyer_agent.domain.ai_jobs import require_transition

    with pytest.raises(InvalidJobTransition):
        require_transition(AIJobStatus.CANCEL_REQUESTED, target)


@pytest.mark.parametrize(
    "status",
    list(AIJobStatus),
)
def test_terminal_states_have_no_successors(status: AIJobStatus) -> None:
    from lawyer_agent.domain.ai_jobs import require_transition

    if status not in TERMINAL_JOB_STATUSES:
        return
    for target in AIJobStatus:
        with pytest.raises(InvalidJobTransition):
            require_transition(status, target)


def test_runtime_timing_requires_heartbeat_below_half_lease() -> None:
    with pytest.raises(ValueError, match="less than half"):
        validate_runtime_timing(lease=timedelta(seconds=60), heartbeat=timedelta(seconds=30))


def test_runtime_timing_accepts_heartbeat_below_half_lease() -> None:
    validate_runtime_timing(lease=timedelta(seconds=60), heartbeat=timedelta(seconds=20))


def test_runtime_timing_rejects_non_positive_durations() -> None:
    with pytest.raises(ValueError):
        validate_runtime_timing(lease=timedelta(0), heartbeat=timedelta(seconds=1))
    with pytest.raises(ValueError):
        validate_runtime_timing(lease=timedelta(seconds=60), heartbeat=timedelta(0))


def test_public_status_maps_internal_statuses() -> None:
    assert to_public_status(AIJobStatus.QUEUED) is PublicAIJobStatus.QUEUED
    assert to_public_status(AIJobStatus.RETRY_SCHEDULED) is PublicAIJobStatus.QUEUED
    assert to_public_status(AIJobStatus.RUNNING) is PublicAIJobStatus.RUNNING
    assert to_public_status(AIJobStatus.CANCEL_REQUESTED) is PublicAIJobStatus.RUNNING
    assert to_public_status(AIJobStatus.SUCCEEDED) is PublicAIJobStatus.SUCCEEDED
    assert to_public_status(AIJobStatus.FAILED) is PublicAIJobStatus.FAILED
    assert to_public_status(AIJobStatus.CANCELLED) is PublicAIJobStatus.CANCELLED


def test_retry_buckets_are_fixed_and_capped() -> None:
    assert RETRY_BUCKET_SECONDS == (5, 30, 120)
    assert retry_delay_seconds(1) == 5
    assert retry_delay_seconds(2) == 30
    assert retry_delay_seconds(3) == 120
    assert retry_delay_seconds(4) == 120
    with pytest.raises(ValueError):
        retry_delay_seconds(0)


def test_scope_code_mapping() -> None:
    assert (
        JobAuthorizationScope(owner=True, shared=True, tenant_wide=True).scope_code()
        is JobScopeCode.TENANT_WIDE
    )
    assert (
        JobAuthorizationScope(owner=True, shared=True, tenant_wide=False).scope_code()
        is JobScopeCode.OWNER_SHARED
    )


def test_tenant_wide_scope_requires_owner_and_shared() -> None:
    with pytest.raises(ValueError):
        JobAuthorizationScope(owner=True, shared=False, tenant_wide=True).scope_code()


def test_unsupported_scope_has_no_mapping() -> None:
    with pytest.raises(ValueError):
        JobAuthorizationScope(owner=True, shared=False, tenant_wide=False).scope_code()


def test_job_lifetime_must_not_exceed_24_hours() -> None:
    validate_job_lifetime(submitted_at=_NOW, expires_at=_NOW + MAX_JOB_LIFETIME)
    with pytest.raises(ValueError, match="24 hours"):
        validate_job_lifetime(
            submitted_at=_NOW,
            expires_at=_NOW + MAX_JOB_LIFETIME + timedelta(seconds=1),
        )
    with pytest.raises(ValueError):
        validate_job_lifetime(submitted_at=_NOW, expires_at=_NOW)


def test_fixed_risk_and_release_values_are_stable() -> None:
    assert RiskClass.SYNTHETIC.value == "synthetic"
    assert ReleaseState.NON_PUBLISHABLE.value == "non_publishable"


def test_grant_permission_must_be_create() -> None:
    with pytest.raises(ValueError, match="ai_job.create"):
        JobExecutionGrant(
            id=_ID,
            tenant_id=_ID,
            job_id=_ID2,
            user_id=_ID3,
            membership_id=_ID3,
            permission_code=AIJobPermission.READ,
            auth_version_at_submit=1,
            authz_version_at_submit=1,
            policy_version="ai-job-policy-v1",
            job_scope_manifest_version="ai-job-scope-v1",
            job_scope_code=JobScopeCode.OWNER_SHARED,
            issued_at=_NOW,
        )


def test_grant_revocation_fields_must_match() -> None:
    with pytest.raises(ValueError, match="set together"):
        JobExecutionGrant(
            id=_ID,
            tenant_id=_ID,
            job_id=_ID2,
            user_id=_ID3,
            membership_id=_ID3,
            permission_code=AIJobPermission.CREATE,
            auth_version_at_submit=1,
            authz_version_at_submit=1,
            policy_version="ai-job-policy-v1",
            job_scope_manifest_version="ai-job-scope-v1",
            job_scope_code=JobScopeCode.OWNER_SHARED,
            issued_at=_NOW,
            revoked_at=_NOW,
        )


def test_actor_snapshot_rejects_non_utc_timestamps() -> None:
    with pytest.raises(ValueError, match="UTC-aware"):
        ActorSnapshot(
            user_id=_ID,
            membership_id=_ID2,
            session_id=_ID3,
            auth_version_at_submit=1,
            authz_version_at_submit=1,
            role_codes_at_submit=frozenset({"assistant"}),
            permission_codes_at_submit=frozenset({"ai_job.create"}),
            policy_version_at_submit="ai-job-policy-v1",
            authenticated_at=_NOW.replace(tzinfo=None),
            submitted_at=_NOW,
        )


def test_job_execution_principal_requires_typed_sets() -> None:
    with pytest.raises(ValueError, match="frozensets"):
        JobExecutionPrincipal(
            user_id=_ID,
            tenant_id=_ID,
            membership_id=_ID2,
            role_codes={"assistant"},  # type: ignore[arg-type]
            permission_codes=frozenset(),
        )
