from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid1, uuid4

import pytest

from lawyer_agent.domain.authorization import (
    POLICY_VERSION,
    Action,
    AuthorizationDecision,
    AuthorizationScope,
    ConfidentialityLevel,
    PolicyEngine,
    Principal,
    PrincipalAudience,
    ResourceAccessPath,
    ResourceAttributes,
    ResourceState,
)
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.tenancy import (
    MembershipStatus,
    TenantContext,
    TenantStatus,
    normalize_tenant_name,
)
from lawyer_agent.infrastructure.persistence.seed_authz import (
    PLATFORM_ROLES,
    TENANT_ROLE_TEMPLATES,
    RoleSeed,
)

NOW = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
USER_ID = new_uuid7()
SESSION_ID = new_uuid7()
TENANT_ID = new_uuid7()
OTHER_TENANT_ID = new_uuid7()
MEMBERSHIP_ID = new_uuid7()
DEPARTMENT_ID = new_uuid7()
OTHER_DEPARTMENT_ID = new_uuid7()


def test_tenant_name_normalization_is_nfkc_trimmed_casefolded_and_bounded() -> None:
    normalized = normalize_tenant_name("  ＬＡＷ　ＦＩＲＭ  ")
    assert normalized.display_value == "LAW FIRM"
    assert normalized.normalized_value == "law firm"
    with pytest.raises(ValueError, match="tenant name"):
        normalize_tenant_name("　 ")
    with pytest.raises(ValueError, match="tenant name"):
        normalize_tenant_name("甲" * 256)


def _principal(
    *,
    permissions: frozenset[str] = frozenset({Action.TENANT_READ.value}),
) -> Principal:
    return Principal(
        user_id=USER_ID,
        session_id=SESSION_ID,
        audience=PrincipalAudience.TENANT,
        tenant_id=TENANT_ID,
        membership_id=MEMBERSHIP_ID,
        user_status="active",
        session_valid=True,
        auth_version=3,
        session_auth_version=3,
        permissions=permissions,
        role_codes=frozenset({"lawyer_or_legal"}),
        authenticated_at=NOW - timedelta(minutes=1),
    )


def _context(
    *,
    scope: AuthorizationScope | None = None,
    platform: bool = False,
) -> TenantContext:
    return TenantContext(
        tenant_id=TENANT_ID,
        membership_id=None if platform else MEMBERSHIP_ID,
        membership_user_id=None if platform else USER_ID,
        department_id=None if platform else DEPARTMENT_ID,
        tenant_status=TenantStatus.ACTIVE,
        membership_status=None if platform else MembershipStatus.ACTIVE,
        valid_from=None if platform else NOW - timedelta(days=1),
        valid_until=None if platform else NOW + timedelta(days=1),
        authz_version=None if platform else 7,
        session_authz_version=None if platform else 7,
        scope=scope or AuthorizationScope(allow_tenant_wide=True),
    )


def _resource(**changes: object) -> ResourceAttributes:
    values: dict[str, object] = {
        "tenant_id": TENANT_ID,
        "state": ResourceState.ACTIVE,
    }
    values.update(changes)
    return ResourceAttributes(**values)  # type: ignore[arg-type]


def test_policy_fails_closed_in_fixed_order() -> None:
    policy = PolicyEngine()
    invalid_identity = replace(_principal(), user_status="disabled", session_valid=False)
    mismatched_context = replace(_context(), tenant_id=OTHER_TENANT_ID)

    assert policy.decide(
        invalid_identity,
        mismatched_context,
        Action.TENANT_READ,
        _resource(tenant_id=OTHER_TENANT_ID),
        NOW,
    ) == AuthorizationDecision(False, "identity_invalid", POLICY_VERSION, True)

    assert policy.decide(
        replace(_principal(), session_valid=False),
        mismatched_context,
        Action.TENANT_READ,
        _resource(tenant_id=OTHER_TENANT_ID),
        NOW,
    ).reason_code == "session_invalid"

    assert policy.decide(
        _principal(),
        mismatched_context,
        Action.TENANT_READ,
        _resource(tenant_id=OTHER_TENANT_ID),
        NOW,
    ).reason_code == "tenant_mismatch"


@pytest.mark.parametrize(
    ("action", "permission_codes", "allowed"),
    [
        (action, frozenset({action.value}), True) for action in Action
    ]
    + [
        (action, frozenset(), False) for action in Action
    ],
)
def test_permission_code_matrix_uses_codes_not_client_role_names(
    action: Action,
    permission_codes: frozenset[str],
    allowed: bool,
) -> None:
    principal = replace(
        _principal(permissions=permission_codes),
        audience=(
            PrincipalAudience.PLATFORM
            if action
            in {
                Action.TENANT_APPLICATION_READ,
                Action.TENANT_APPLICATION_REVIEW,
                Action.PLATFORM_ADMIN_BOOTSTRAP,
            }
            else PrincipalAudience.TENANT
        ),
        tenant_id=(
            None
            if action
            in {
                Action.TENANT_APPLICATION_READ,
                Action.TENANT_APPLICATION_REVIEW,
                Action.PLATFORM_ADMIN_BOOTSTRAP,
            }
            else TENANT_ID
        ),
        membership_id=(
            None
            if action
            in {
                Action.TENANT_APPLICATION_READ,
                Action.TENANT_APPLICATION_REVIEW,
                Action.PLATFORM_ADMIN_BOOTSTRAP,
            }
            else MEMBERSHIP_ID
        ),
        role_codes=(
            frozenset({"content_operator"})
            if action
            in {
                Action.TENANT_APPLICATION_READ,
                Action.TENANT_APPLICATION_REVIEW,
                Action.PLATFORM_ADMIN_BOOTSTRAP,
            }
            else frozenset({"external_client"})
        ),
    )

    decision = PolicyEngine().decide(
        principal,
        _context(platform=principal.audience is PrincipalAudience.PLATFORM),
        action,
        _resource(),
        NOW,
    )

    assert decision.allowed is allowed
    assert decision.reason_code == ("allowed" if allowed else "permission_denied")
    assert decision.audit_required is (not allowed or action.audit_required)


@pytest.mark.parametrize("role", TENANT_ROLE_TEMPLATES)
def test_every_tenant_role_permission_and_denial_matrix(role: RoleSeed) -> None:
    principal = replace(
        _principal(permissions=role.permission_codes),
        role_codes=frozenset({role.code}),
    )
    platform_actions = {
        Action.TENANT_APPLICATION_READ,
        Action.TENANT_APPLICATION_REVIEW,
        Action.PLATFORM_ADMIN_BOOTSTRAP,
    }
    for action in Action:
        if action in platform_actions:
            continue
        decision = PolicyEngine().decide(
            principal, _context(), action, _resource(), NOW
        )
        assert decision.allowed is (action.value in role.permission_codes), (
            role.code,
            action,
        )


@pytest.mark.parametrize("role", PLATFORM_ROLES)
def test_every_platform_role_permission_and_denial_matrix(role: RoleSeed) -> None:
    principal = replace(
        _principal(permissions=role.permission_codes),
        audience=PrincipalAudience.PLATFORM,
        tenant_id=None,
        membership_id=None,
        role_codes=frozenset({role.code}),
    )
    for action in (
        Action.TENANT_APPLICATION_READ,
        Action.TENANT_APPLICATION_REVIEW,
        Action.PLATFORM_ADMIN_BOOTSTRAP,
    ):
        decision = PolicyEngine().decide(
            principal, _context(platform=True), action, _resource(), NOW
        )
        assert decision.allowed is (action.value in role.permission_codes), (
            role.code,
            action,
        )


@pytest.mark.parametrize(
    ("context", "expected"),
    [
        (replace(_context(), tenant_status=TenantStatus.SUSPENDED), "tenant_status_denied"),
        (replace(_context(), membership_status=MembershipStatus.SUSPENDED), "membership_inactive"),
        (replace(_context(), valid_from=NOW + timedelta(seconds=1)), "membership_not_yet_valid"),
        (replace(_context(), valid_until=NOW), "membership_expired"),
        (replace(_context(), session_authz_version=8), "session_invalid"),
    ],
)
def test_status_time_and_version_checks_fail_closed(
    context: TenantContext,
    expected: str,
) -> None:
    decision = PolicyEngine().decide(
        _principal(), context, Action.TENANT_READ, _resource(), NOW
    )
    assert decision == AuthorizationDecision(False, expected, POLICY_VERSION, True)


def test_pending_tenant_allows_internal_setup_but_not_external_service() -> None:
    context = replace(_context(), tenant_status=TenantStatus.PENDING_VERIFICATION)
    principal = _principal(
        permissions=frozenset(
            {Action.DEPARTMENT_MANAGE.value, Action.EXTERNAL_SERVICE_ENABLE.value}
        )
    )

    assert PolicyEngine().decide(
        principal, context, Action.DEPARTMENT_MANAGE, _resource(), NOW
    ).allowed
    assert PolicyEngine().decide(
        principal, context, Action.EXTERNAL_SERVICE_ENABLE, _resource(), NOW
    ).reason_code == "tenant_status_denied"


def test_platform_policy_uses_target_tenant_without_fabricating_membership() -> None:
    principal = replace(
        _principal(permissions=frozenset({Action.TENANT_APPLICATION_REVIEW.value})),
        audience=PrincipalAudience.PLATFORM,
        tenant_id=None,
        membership_id=None,
        role_codes=frozenset({"super_admin"}),
    )
    context = _context(platform=True)
    assert context.membership_id is None and context.membership_user_id is None
    assert PolicyEngine().decide(
        principal, context, Action.TENANT_APPLICATION_REVIEW, _resource(), NOW
    ).allowed


@pytest.mark.parametrize(
    ("scope", "resource", "allowed"),
    [
        (
            AuthorizationScope(department_ids=frozenset({DEPARTMENT_ID})),
            _resource(
                access_paths=frozenset({ResourceAccessPath.DEPARTMENT}),
                department_id=DEPARTMENT_ID,
            ),
            True,
        ),
        (
            AuthorizationScope(department_ids=frozenset({DEPARTMENT_ID})),
            _resource(
                access_paths=frozenset({ResourceAccessPath.DEPARTMENT}),
                department_id=OTHER_DEPARTMENT_ID,
            ),
            False,
        ),
        (
            AuthorizationScope(allow_owned=True),
            _resource(
                access_paths=frozenset({ResourceAccessPath.OWNER}), owner_user_id=USER_ID
            ),
            True,
        ),
        (
            AuthorizationScope(allow_shared=True),
            _resource(
                access_paths=frozenset({ResourceAccessPath.SHARED}),
                shared_with_membership_ids=frozenset({MEMBERSHIP_ID}),
            ),
            True,
        ),
        (
            AuthorizationScope(allow_matter_team=True),
            _resource(
                access_paths=frozenset({ResourceAccessPath.MATTER_TEAM}),
                matter_team_membership_ids=frozenset({MEMBERSHIP_ID}),
            ),
            True,
        ),
        (
            AuthorizationScope(allow_class=True),
            _resource(
                access_paths=frozenset({ResourceAccessPath.CLASS}),
                class_membership_ids=frozenset({MEMBERSHIP_ID}),
            ),
            True,
        ),
        (
            AuthorizationScope(allow_client_delegation=True),
            _resource(
                access_paths=frozenset({ResourceAccessPath.CLIENT_DELEGATION}),
                delegated_client_membership_ids=frozenset({MEMBERSHIP_ID}),
            ),
            True,
        ),
    ],
)
def test_abac_department_owner_shared_team_class_and_client_delegation(
    scope: AuthorizationScope,
    resource: ResourceAttributes,
    allowed: bool,
) -> None:
    decision = PolicyEngine().decide(
        _principal(), _context(scope=scope), Action.TENANT_READ, resource, NOW
    )
    assert decision.allowed is allowed
    assert decision.reason_code == ("allowed" if allowed else "resource_scope_denied")


@pytest.mark.parametrize(
    "state",
    [
        ResourceState.DISABLED,
        ResourceState.DELETED,
        ResourceState.REVOKED,
        ResourceState.CLOSED,
    ],
)
def test_terminal_resource_states_are_denied(state: ResourceState) -> None:
    decision = PolicyEngine().decide(
        _principal(), _context(), Action.TENANT_READ, _resource(state=state), NOW
    )
    assert decision.reason_code == "resource_state_denied"


def test_confidentiality_scope_is_enforced_before_resource_state() -> None:
    context = _context(
        scope=AuthorizationScope(
            allow_tenant_wide=True,
            maximum_confidentiality=ConfidentialityLevel.INTERNAL,
        )
    )
    resource = _resource(
        confidentiality=ConfidentialityLevel.RESTRICTED,
        state=ResourceState.DELETED,
    )
    decision = PolicyEngine().decide(
        _principal(), context, Action.TENANT_READ, resource, NOW
    )
    assert decision.reason_code == "resource_scope_denied"


def test_non_uuid7_identity_and_session_are_rejected_in_order() -> None:
    assert PolicyEngine().decide(
        replace(_principal(), user_id=uuid4()),
        _context(),
        Action.TENANT_READ,
        _resource(),
        NOW,
    ).reason_code == "identity_invalid"
    assert PolicyEngine().decide(
        replace(_principal(), session_id=uuid4()),
        _context(),
        Action.TENANT_READ,
        _resource(),
        NOW,
    ).reason_code == "session_invalid"


@pytest.mark.parametrize(
    "invalid",
    [uuid1(), uuid4(), UUID(int=0), True, "01990f00-0000-7000-8000-000000000501", object()],
)
def test_every_tenant_context_department_id_type_fails_closed(invalid: object) -> None:
    decision = PolicyEngine().decide(
        _principal(),
        replace(_context(), department_id=invalid),  # type: ignore[arg-type]
        Action.TENANT_READ,
        _resource(),
        NOW,
    )
    assert decision.reason_code == "tenant_mismatch"


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("tenant_id", "tenant_mismatch"),
        ("membership_id", "tenant_mismatch"),
        ("membership_user_id", "tenant_mismatch"),
    ],
)
def test_all_other_tenant_context_ids_are_checked(field: str, expected: str) -> None:
    decision = PolicyEngine().decide(
        _principal(),
        replace(_context(), **{field: uuid4()}),
        Action.TENANT_READ,
        _resource(),
        NOW,
    )
    assert decision.reason_code == expected


@pytest.mark.parametrize(
    "changes",
    [
        {"department_id": uuid4()},
        {"owner_user_id": uuid4()},
        {"shared_with_membership_ids": frozenset({uuid4()})},
        {"matter_team_membership_ids": frozenset({uuid4()})},
        {"class_membership_ids": frozenset({uuid4()})},
        {"delegated_client_membership_ids": frozenset({uuid4()})},
    ],
)
def test_every_resource_identifier_path_fails_closed(changes: dict[str, object]) -> None:
    resource = _resource(**changes)
    decision = PolicyEngine().decide(
        _principal(), _context(), Action.TENANT_READ, resource, NOW
    )
    assert decision.reason_code == "unknown_resource_attributes"


def test_unknown_or_non_uuid7_resource_attributes_fail_closed() -> None:
    malformed = _resource(
        access_paths=frozenset({ResourceAccessPath.SHARED}),
        shared_with_membership_ids=frozenset({uuid4()}),
    )
    assert PolicyEngine().decide(
        _principal(), _context(), Action.TENANT_READ, malformed, NOW
    ).reason_code == "unknown_resource_attributes"


@pytest.mark.parametrize(
    ("principal", "action", "resource", "expected"),
    [
        (
            replace(_principal(), permissions=frozenset({"future.permission"})),
            Action.TENANT_READ,
            _resource(),
            "unknown_permission",
        ),
        (
            replace(_principal(), role_codes=frozenset({"client-supplied-admin"})),
            Action.TENANT_READ,
            _resource(),
            "unknown_role",
        ),
        (
            _principal(),
            cast(Action, "future.action"),
            _resource(),
            "unknown_action",
        ),
        (
            _principal(),
            Action.TENANT_READ,
            _resource(unrecognized_fields=frozenset({"future_field"})),
            "unknown_resource_attributes",
        ),
    ],
)
def test_unknown_action_permission_role_or_resource_field_is_denied(
    principal: Principal,
    action: Action,
    resource: ResourceAttributes,
    expected: str,
) -> None:
    assert PolicyEngine().decide(
        principal, _context(), action, resource, NOW
    ).reason_code == expected


def test_unknown_action_does_not_skip_earlier_tenant_match_gate() -> None:
    decision = PolicyEngine().decide(
        _principal(),
        replace(_context(), tenant_id=OTHER_TENANT_ID),
        cast(Action, "future.action"),
        _resource(tenant_id=OTHER_TENANT_ID),
        NOW,
    )
    assert decision.reason_code == "tenant_mismatch"


def test_unknown_scope_and_policy_exception_fail_closed_without_leaking_details() -> None:
    bad_scope = replace(
        _context().scope,
        unrecognized_scope_codes=frozenset({"database.superuser"}),
    )
    assert PolicyEngine().decide(
        _principal(), _context(scope=bad_scope), Action.TENANT_READ, _resource(), NOW
    ).reason_code == "unknown_scope"

    malformed = replace(_context(), valid_from=cast(datetime, object()))
    decision = PolicyEngine().decide(
        _principal(), malformed, Action.TENANT_READ, _resource(), NOW
    )
    assert decision == AuthorizationDecision(False, "policy_error", POLICY_VERSION, True)
    assert "object" not in repr(decision).lower()


def test_domain_policy_has_no_framework_or_persistence_imports() -> None:

    import lawyer_agent.domain.authorization as authorization_module
    import lawyer_agent.domain.tenancy as tenancy_module

    source = inspect.getsource(authorization_module) + inspect.getsource(tenancy_module)
    assert "fastapi" not in source.lower()
    assert "sqlalchemy" not in source.lower()
    assert "redis" not in source.lower()
