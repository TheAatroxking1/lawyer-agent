from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from lawyer_agent.application.ai_jobs import (
    DurableGrantPolicy,
    JobAuthoritySnapshot,
    JobAuthorizationDenied,
    JobAuthorizationResolver,
)
from lawyer_agent.application.tenancy import TenantAuthorizationSnapshot
from lawyer_agent.domain.ai_jobs import (
    JOB_POLICY_VERSION,
    JOB_SCOPE_MANIFEST_VERSION,
    AIJob,
    AIJobPermission,
    AIJobStatus,
    JobAuthorizationScope,
    JobExecutionGrant,
    JobScopeCode,
    JobVisibility,
    ReleaseState,
    RiskClass,
)
from lawyer_agent.domain.authorization import AuthorizationScope
from lawyer_agent.domain.tenancy import (
    Membership,
    MembershipStatus,
    MemberType,
    Tenant,
    TenantContext,
    TenantStatus,
)

_NOW = datetime(2026, 9, 3, 1, 0, tzinfo=UTC)
_ID = UUID("0192e1a0-0000-7000-8000-000000000001")
_ID2 = UUID("0192e1a0-0000-7000-8000-000000000002")
_ID3 = UUID("0192e1a0-0000-7000-8000-000000000003")
_ID4 = UUID("0192e1a0-0000-7000-8000-000000000004")
_ID5 = UUID("0192e1a0-0000-7000-8000-000000000005")


def _authority(
    *,
    role_codes: frozenset[str],
    permissions: frozenset[str] = frozenset(
        {"ai_job.create", "ai_job.read", "ai_job.cancel"}
    ),
    generic_tenant_wide: bool = True,
) -> TenantAuthorizationSnapshot:
    tenant = Tenant(
        id=_ID,
        name="tenant",
        normalized_name="tenant",
        tenant_type="enterprise",
        status=TenantStatus.ACTIVE,
        version=1,
        created_by_user_id=_ID2,
        review_status="approved",
    )
    membership = Membership(
        id=_ID3,
        tenant_id=_ID,
        user_id=_ID2,
        department_id=None,
        member_type=MemberType.OWNER,
        status=MembershipStatus.ACTIVE,
        valid_from=_NOW - timedelta(days=1),
        valid_until=None,
        authz_version=1,
        version=1,
    )
    context = TenantContext(
        tenant_id=_ID,
        membership_id=_ID3,
        membership_user_id=_ID2,
        department_id=None,
        tenant_status=TenantStatus.ACTIVE,
        membership_status=MembershipStatus.ACTIVE,
        valid_from=_NOW - timedelta(days=1),
        valid_until=None,
        authz_version=1,
        session_authz_version=1,
        scope=AuthorizationScope(allow_tenant_wide=generic_tenant_wide),
    )
    return TenantAuthorizationSnapshot(
        tenant=tenant,
        actor_membership=membership,
        context=context,
        user_status="active",
        user_auth_version=1,
        permissions=permissions,
        role_codes=role_codes,
        target_memberships={},
        actor_session=None,
    )


def _job(**overrides: object) -> AIJob:
    fields = {
        "id": _ID,
        "tenant_id": _ID,
        "handler_code": "synthetic.v1",
        "handler_version": "v1",
        "input_schema_version": "synthetic-input-v1",
        "input_json": {},
        "input_fingerprint": b"i" * 32,
        "created_by_user_id": _ID2,
        "created_by_membership_id": _ID3,
        "created_by_session_id": _ID4,
        "actor_snapshot_json": {},
        "policy_version_at_submit": JOB_POLICY_VERSION,
        "visibility": JobVisibility.OWNER_ONLY,
        "status": AIJobStatus.QUEUED,
        "current_attempt_no": 0,
        "max_attempts": 4,
        "risk_class": RiskClass.SYNTHETIC,
        "release_state": ReleaseState.NON_PUBLISHABLE,
        "idempotency_record_id": _ID5,
        "correlation_id": _ID5,
        "submitted_at": _NOW,
        "expires_at": _NOW + timedelta(hours=1),
        "version": 1,
    }
    fields.update(overrides)
    return AIJob(**fields)  # type: ignore[arg-type]


def _grant(**overrides: object) -> JobExecutionGrant:
    fields = {
        "id": _ID5,
        "tenant_id": _ID,
        "job_id": _ID,
        "user_id": _ID2,
        "membership_id": _ID3,
        "permission_code": AIJobPermission.CREATE,
        "auth_version_at_submit": 1,
        "authz_version_at_submit": 1,
        "policy_version": JOB_POLICY_VERSION,
        "job_scope_manifest_version": JOB_SCOPE_MANIFEST_VERSION,
        "job_scope_code": JobScopeCode.OWNER_SHARED,
        "issued_at": _NOW,
        "revoked_at": None,
        "version": 1,
    }
    fields.update(overrides)
    return JobExecutionGrant(**fields)  # type: ignore[arg-type]


def _membership(**overrides: object) -> Membership:
    fields = {
        "id": _ID3,
        "tenant_id": _ID,
        "user_id": _ID2,
        "department_id": None,
        "member_type": MemberType.OWNER,
        "status": MembershipStatus.ACTIVE,
        "valid_from": _NOW - timedelta(days=1),
        "valid_until": None,
        "authz_version": 1,
        "version": 1,
    }
    fields.update(overrides)
    return Membership(**fields)  # type: ignore[arg-type]


def _snapshot(**overrides: object) -> JobAuthoritySnapshot:
    fields = {
        "job": _job(),
        "grant": _grant(),
        "user_active": True,
        "tenant_active": True,
        "membership": _membership(),
        "role_codes": frozenset({"assistant"}),
        "permission_codes": frozenset({"ai_job.create", "ai_job.read"}),
        "auth_version": 1,
        "authz_version": 1,
        "resolved_scope": JobAuthorizationScope(True, True, False),
        "resolved_scope_code": JobScopeCode.OWNER_SHARED,
        "granted_scope": JobAuthorizationScope(True, True, False),
        "unknown_or_mixed_role_codes": False,
    }
    fields.update(overrides)
    return JobAuthoritySnapshot(**fields)  # type: ignore[arg-type]


def test_resolver_ignores_generic_tenant_wide_scope() -> None:
    snapshot = _authority(role_codes=frozenset({"assistant"}), generic_tenant_wide=True)
    scope = JobAuthorizationResolver().resolve(snapshot, AIJobPermission.READ)
    assert scope == JobAuthorizationScope(owner=True, shared=True, tenant_wide=False)


def test_tenant_owner_gets_tenant_wide_scope() -> None:
    snapshot = _authority(role_codes=frozenset({"tenant_owner"}))
    scope = JobAuthorizationResolver().resolve(snapshot, AIJobPermission.READ)
    assert scope == JobAuthorizationScope(owner=True, shared=True, tenant_wide=True)


def test_custom_or_unknown_role_fails_closed() -> None:
    with pytest.raises(JobAuthorizationDenied, match="job_scope_unmapped"):
        JobAuthorizationResolver().resolve(
            _authority(role_codes=frozenset({"custom_partner"})),
            AIJobPermission.READ,
        )


def test_student_role_fails_closed() -> None:
    with pytest.raises(JobAuthorizationDenied, match="job_scope_unmapped"):
        JobAuthorizationResolver().resolve(
            _authority(role_codes=frozenset({"student"})),
            AIJobPermission.READ,
        )


def test_resolver_requires_permission() -> None:
    with pytest.raises(JobAuthorizationDenied, match="permission_denied"):
        JobAuthorizationResolver().resolve(
            _authority(role_codes=frozenset({"assistant"}), permissions=frozenset()),
            AIJobPermission.READ,
        )


def test_policy_allows_valid_authority() -> None:
    assert DurableGrantPolicy().authorize(_snapshot(), now=_NOW).allowed


def test_authz_version_bump_invalidates_durable_grant() -> None:
    authority = _snapshot()
    changed = authority.with_current_authz_version(authority.authz_version + 1)
    decision = DurableGrantPolicy().authorize(changed, now=_NOW)
    assert not decision.allowed
    assert decision.reason_code == "authz_version_changed"


def test_auth_version_bump_invalidates_durable_grant() -> None:
    snapshot = _snapshot(auth_version=2)
    decision = DurableGrantPolicy().authorize(snapshot, now=_NOW)
    assert not decision.allowed
    assert decision.reason_code == "auth_version_changed"


def test_revoked_grant_rejects() -> None:
    snapshot = _snapshot(grant=_grant(revoked_at=_NOW, revocation_reason_code="revoked"))
    decision = DurableGrantPolicy().authorize(snapshot, now=_NOW)
    assert not decision.allowed
    assert decision.reason_code == "grant_revoked"


def test_expired_job_rejects() -> None:
    snapshot = _snapshot(job=_job(expires_at=_NOW - timedelta(seconds=1)))
    decision = DurableGrantPolicy().authorize(snapshot, now=_NOW)
    assert not decision.allowed
    assert decision.reason_code == "job_expired"


def test_inactive_membership_rejects() -> None:
    snapshot = _snapshot(
        membership=_membership(status=MembershipStatus.SUSPENDED)
    )
    decision = DurableGrantPolicy().authorize(snapshot, now=_NOW)
    assert not decision.allowed
    assert decision.reason_code == "membership_inactive"


def test_unknown_role_flag_rejects() -> None:
    snapshot = _snapshot(unknown_or_mixed_role_codes=True)
    decision = DurableGrantPolicy().authorize(snapshot, now=_NOW)
    assert not decision.allowed
    assert decision.reason_code == "unknown_role"


def test_scope_mismatch_rejects() -> None:
    snapshot = _snapshot(
        granted_scope=JobAuthorizationScope(True, True, True),
        resolved_scope=JobAuthorizationScope(True, True, False),
    )
    decision = DurableGrantPolicy().authorize(snapshot, now=_NOW)
    assert not decision.allowed
    assert decision.reason_code == "scope_mismatch"
