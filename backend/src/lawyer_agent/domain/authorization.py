from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from lawyer_agent.domain.common import is_uuid7
from lawyer_agent.domain.tenancy import (
    MembershipStatus,
    TenantContext,
    TenantStatus,
)

POLICY_VERSION = "identity-authz-v1"


class Action(StrEnum):
    TENANT_READ = "tenant.read"
    TENANT_UPDATE = "tenant.update"
    DEPARTMENT_READ = "department.read"
    DEPARTMENT_MANAGE = "department.manage"
    MEMBERSHIP_READ = "membership.read"
    MEMBERSHIP_INVITE = "membership.invite"
    MEMBERSHIP_UPDATE = "membership.update"
    MEMBERSHIP_REVOKE = "membership.revoke"
    ROLE_READ = "role.read"
    ROLE_ASSIGN = "role.assign"
    EXTERNAL_SERVICE_ENABLE = "external_service.enable"
    TENANT_APPLICATION_READ = "tenant_application.read"
    TENANT_APPLICATION_REVIEW = "tenant_application.review"
    PLATFORM_ADMIN_BOOTSTRAP = "platform_admin.bootstrap"

    @property
    def risk_level(self) -> str:
        return _ACTION_RISK[self]

    @property
    def audit_required(self) -> bool:
        return self in _AUDITED_ACTIONS


class PrincipalAudience(StrEnum):
    TENANT = "tenant"
    PLATFORM = "platform"


class ResourceAccessPath(StrEnum):
    DEPARTMENT = "department"
    OWNER = "owner"
    SHARED = "shared"
    MATTER_TEAM = "matter_team"
    CLASS = "class"
    CLIENT_DELEGATION = "client_delegation"


class ResourceState(StrEnum):
    ACTIVE = "active"
    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    DISABLED = "disabled"
    DELETED = "deleted"
    REVOKED = "revoked"
    CLOSED = "closed"


class ConfidentialityLevel(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


@dataclass(frozen=True, slots=True)
class AuthorizationScope:
    department_ids: frozenset[UUID] = field(default_factory=frozenset)
    allow_tenant_wide: bool = False
    allow_owned: bool = False
    allow_shared: bool = False
    allow_matter_team: bool = False
    allow_class: bool = False
    allow_client_delegation: bool = False
    maximum_confidentiality: ConfidentialityLevel = ConfidentialityLevel.RESTRICTED
    unrecognized_scope_codes: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: UUID
    session_id: UUID
    audience: PrincipalAudience
    tenant_id: UUID | None
    membership_id: UUID | None
    user_status: str
    session_valid: bool
    auth_version: int
    session_auth_version: int
    permissions: frozenset[str]
    role_codes: frozenset[str]
    authenticated_at: datetime


@dataclass(frozen=True, slots=True)
class ResourceAttributes:
    tenant_id: UUID
    state: ResourceState
    access_paths: frozenset[ResourceAccessPath] = field(default_factory=frozenset)
    department_id: UUID | None = None
    owner_user_id: UUID | None = None
    shared_with_membership_ids: frozenset[UUID] = field(default_factory=frozenset)
    matter_team_membership_ids: frozenset[UUID] = field(default_factory=frozenset)
    class_membership_ids: frozenset[UUID] = field(default_factory=frozenset)
    delegated_client_membership_ids: frozenset[UUID] = field(default_factory=frozenset)
    confidentiality: ConfidentialityLevel = ConfidentialityLevel.INTERNAL
    approval_status: str | None = None
    unrecognized_fields: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    allowed: bool
    reason_code: str
    policy_version: str
    audit_required: bool


_ACTION_RISK = MappingProxyType(
    {
        Action.TENANT_READ: "medium",
        Action.TENANT_UPDATE: "high",
        Action.DEPARTMENT_READ: "low",
        Action.DEPARTMENT_MANAGE: "medium",
        Action.MEMBERSHIP_READ: "medium",
        Action.MEMBERSHIP_INVITE: "high",
        Action.MEMBERSHIP_UPDATE: "high",
        Action.MEMBERSHIP_REVOKE: "high",
        Action.ROLE_READ: "medium",
        Action.ROLE_ASSIGN: "high",
        Action.EXTERNAL_SERVICE_ENABLE: "high",
        Action.TENANT_APPLICATION_READ: "high",
        Action.TENANT_APPLICATION_REVIEW: "critical",
        Action.PLATFORM_ADMIN_BOOTSTRAP: "critical",
    }
)
_KNOWN_PERMISSION_CODES = frozenset(action.value for action in Action)
_KNOWN_ROLE_CODES = frozenset(
    {
        "tenant_owner",
        "tenant_admin",
        "department_admin",
        "lawyer_or_legal",
        "assistant",
        "teacher",
        "student",
        "external_client",
        "super_admin",
        "security_auditor",
        "operations_support",
        "content_operator",
    }
)
_PLATFORM_ACTIONS = frozenset(
    {
        Action.TENANT_APPLICATION_READ,
        Action.TENANT_APPLICATION_REVIEW,
        Action.PLATFORM_ADMIN_BOOTSTRAP,
    }
)
_AUDITED_ACTIONS = frozenset(
    {
        Action.TENANT_UPDATE,
        Action.DEPARTMENT_MANAGE,
        Action.MEMBERSHIP_INVITE,
        Action.MEMBERSHIP_UPDATE,
        Action.MEMBERSHIP_REVOKE,
        Action.ROLE_ASSIGN,
        Action.EXTERNAL_SERVICE_ENABLE,
        Action.TENANT_APPLICATION_READ,
        Action.TENANT_APPLICATION_REVIEW,
        Action.PLATFORM_ADMIN_BOOTSTRAP,
    }
)
_TERMINAL_RESOURCE_STATES = frozenset(
    {ResourceState.DISABLED, ResourceState.DELETED, ResourceState.REVOKED, ResourceState.CLOSED}
)
_CONFIDENTIALITY_RANK = MappingProxyType(
    {
        ConfidentialityLevel.PUBLIC: 0,
        ConfidentialityLevel.INTERNAL: 1,
        ConfidentialityLevel.CONFIDENTIAL: 2,
        ConfidentialityLevel.RESTRICTED: 3,
    }
)


class PolicyEngine:
    def decide(
        self,
        principal: Principal,
        context: TenantContext,
        action: Action,
        resource: ResourceAttributes,
        now: datetime,
    ) -> AuthorizationDecision:
        try:
            return self._decide(principal, context, action, resource, now)
        except Exception:
            return _deny("policy_error")

    def _decide(
        self,
        principal: Principal,
        context: TenantContext,
        action: Action,
        resource: ResourceAttributes,
        now: datetime,
    ) -> AuthorizationDecision:
        identity_reason = _identity_reason(principal)
        if identity_reason is not None:
            return _deny(identity_reason)

        session_reason = _session_reason(principal, context, now)
        if session_reason is not None:
            return _deny(session_reason)

        tenant_reason = _tenant_match_reason(principal, context, action, resource)
        if tenant_reason is not None:
            return _deny(tenant_reason)

        tenant_status_reason = _tenant_status_reason(context, action)
        if tenant_status_reason is not None:
            return _deny(tenant_status_reason)

        membership_reason = _membership_reason(principal, context, now)
        if membership_reason is not None:
            return _deny(membership_reason)

        if not isinstance(action, Action):
            return _deny("unknown_action")

        permission_reason = _permission_reason(principal, action)
        if permission_reason is not None:
            return _deny(permission_reason)

        scope_reason = _scope_reason(principal, context, resource)
        if scope_reason is not None:
            return _deny(scope_reason)

        state_reason = _resource_state_reason(resource)
        if state_reason is not None:
            return _deny(state_reason)

        return AuthorizationDecision(
            True,
            "allowed",
            POLICY_VERSION,
            action.audit_required,
        )


def _identity_reason(principal: Principal) -> str | None:
    if (
        not isinstance(principal, Principal)
        or not is_uuid7(principal.user_id)
        or principal.user_status != "active"
        or not _is_utc(principal.authenticated_at)
    ):
        return "identity_invalid"
    return None


def _session_reason(
    principal: Principal,
    context: TenantContext,
    now: datetime,
) -> str | None:
    if (
        not is_uuid7(principal.session_id)
        or principal.session_valid is not True
        or not _positive_int(principal.auth_version)
        or not _positive_int(principal.session_auth_version)
        or principal.auth_version != principal.session_auth_version
        or not _is_utc(now)
    ):
        return "session_invalid"
    if (
        principal.audience is PrincipalAudience.TENANT
        and (
            not _positive_int(context.authz_version)
            or not _positive_int(context.session_authz_version)
            or context.authz_version != context.session_authz_version
        )
    ):
        return "session_invalid"
    return None


def _tenant_match_reason(
    principal: Principal,
    context: TenantContext,
    action: Action,
    resource: ResourceAttributes,
) -> str | None:
    if (
        not isinstance(context, TenantContext)
        or not isinstance(resource, ResourceAttributes)
        or not is_uuid7(context.tenant_id)
        or (context.department_id is not None and not is_uuid7(context.department_id))
        or not is_uuid7(resource.tenant_id)
        or resource.tenant_id != context.tenant_id
    ):
        return "tenant_mismatch"
    if principal.audience is PrincipalAudience.TENANT:
        if (
            action in _PLATFORM_ACTIONS
            or not is_uuid7(principal.tenant_id)
            or not is_uuid7(principal.membership_id)
            or not is_uuid7(context.membership_id)
            or not is_uuid7(context.membership_user_id)
            or principal.tenant_id != context.tenant_id
            or principal.membership_id != context.membership_id
            or context.membership_user_id != principal.user_id
        ):
            return "tenant_mismatch"
    elif principal.audience is PrincipalAudience.PLATFORM:
        membership_values = (
            context.membership_id,
            context.membership_user_id,
            context.department_id,
            context.membership_status,
            context.valid_from,
            context.valid_until,
            context.authz_version,
            context.session_authz_version,
        )
        if (
            action not in _PLATFORM_ACTIONS
            or any(value is not None for value in (principal.tenant_id, principal.membership_id))
            or any(value is not None for value in membership_values)
        ):
            return "tenant_mismatch"
    else:
        return "tenant_mismatch"
    return None


def _tenant_status_reason(context: TenantContext, action: Action) -> str | None:
    if not isinstance(context.tenant_status, TenantStatus):
        return "tenant_status_denied"
    if context.tenant_status is TenantStatus.ACTIVE:
        return None
    if context.tenant_status is TenantStatus.PENDING_VERIFICATION:
        if action is Action.EXTERNAL_SERVICE_ENABLE:
            return "tenant_status_denied"
        return None
    return "tenant_status_denied"


def _membership_reason(
    principal: Principal,
    context: TenantContext,
    now: datetime,
) -> str | None:
    if principal.audience is PrincipalAudience.PLATFORM:
        return None
    if context.membership_status is not MembershipStatus.ACTIVE:
        return "membership_inactive"
    if not _is_utc(context.valid_from):
        raise ValueError("invalid membership timestamp")
    assert isinstance(context.valid_from, datetime)
    if context.valid_from > now:
        return "membership_not_yet_valid"
    if context.valid_until is not None:
        if not _is_utc(context.valid_until):
            raise ValueError("invalid membership timestamp")
        if context.valid_until <= now:
            return "membership_expired"
    return None


def _permission_reason(principal: Principal, action: Action) -> str | None:
    if not isinstance(principal.permissions, frozenset) or any(
        not isinstance(value, str) or value not in _KNOWN_PERMISSION_CODES
        for value in principal.permissions
    ):
        return "unknown_permission"
    if not isinstance(principal.role_codes, frozenset) or any(
        not isinstance(value, str) or value not in _KNOWN_ROLE_CODES
        for value in principal.role_codes
    ):
        return "unknown_role"
    if action.value not in principal.permissions:
        return "permission_denied"
    return None


def _scope_reason(
    principal: Principal,
    context: TenantContext,
    resource: ResourceAttributes,
) -> str | None:
    scope = context.scope
    if not isinstance(scope, AuthorizationScope):
        return "unknown_scope"
    if scope.unrecognized_scope_codes:
        return "unknown_scope"
    if resource.unrecognized_fields:
        return "unknown_resource_attributes"
    if not _valid_resource_attributes(resource):
        return "unknown_resource_attributes"
    if not _valid_uuid_set(scope.department_ids):
        return "unknown_scope"
    flags = (
        scope.allow_tenant_wide,
        scope.allow_owned,
        scope.allow_shared,
        scope.allow_matter_team,
        scope.allow_class,
        scope.allow_client_delegation,
    )
    if any(not isinstance(value, bool) for value in flags):
        return "unknown_scope"
    if (
        not isinstance(scope.maximum_confidentiality, ConfidentialityLevel)
        or not isinstance(resource.confidentiality, ConfidentialityLevel)
    ):
        return "unknown_scope"
    if (
        _CONFIDENTIALITY_RANK[resource.confidentiality]
        > _CONFIDENTIALITY_RANK[scope.maximum_confidentiality]
    ):
        return "resource_scope_denied"
    if not isinstance(resource.access_paths, frozenset) or any(
        not isinstance(path, ResourceAccessPath) for path in resource.access_paths
    ):
        return "unknown_scope"
    if principal.audience is PrincipalAudience.PLATFORM:
        return None
    if scope.allow_tenant_wide:
        return None
    if not resource.access_paths:
        return "resource_scope_denied"

    membership_id = context.membership_id
    checks = {
        ResourceAccessPath.DEPARTMENT: (
            resource.department_id is not None
            and resource.department_id in scope.department_ids
        ),
        ResourceAccessPath.OWNER: (
            scope.allow_owned and resource.owner_user_id == principal.user_id
        ),
        ResourceAccessPath.SHARED: (
            scope.allow_shared and membership_id in resource.shared_with_membership_ids
        ),
        ResourceAccessPath.MATTER_TEAM: (
            scope.allow_matter_team and membership_id in resource.matter_team_membership_ids
        ),
        ResourceAccessPath.CLASS: (
            scope.allow_class and membership_id in resource.class_membership_ids
        ),
        ResourceAccessPath.CLIENT_DELEGATION: (
            scope.allow_client_delegation
            and membership_id in resource.delegated_client_membership_ids
        ),
    }
    return None if any(checks[path] for path in resource.access_paths) else "resource_scope_denied"


def _resource_state_reason(resource: ResourceAttributes) -> str | None:
    if not isinstance(resource.state, ResourceState):
        return "resource_state_denied"
    if resource.state in _TERMINAL_RESOURCE_STATES:
        return "resource_state_denied"
    return None


def _deny(reason_code: str) -> AuthorizationDecision:
    return AuthorizationDecision(False, reason_code, POLICY_VERSION, True)


def _is_utc(value: object) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() == UTC.utcoffset(value)
    )


def _positive_int(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 1


def _valid_uuid_set(values: object) -> bool:
    return isinstance(values, frozenset) and all(is_uuid7(value) for value in values)


def _valid_resource_attributes(resource: ResourceAttributes) -> bool:
    optional_ids = (resource.department_id, resource.owner_user_id)
    if any(value is not None and not is_uuid7(value) for value in optional_ids):
        return False
    membership_sets = (
        resource.shared_with_membership_ids,
        resource.matter_team_membership_ids,
        resource.class_membership_ids,
        resource.delegated_client_membership_ids,
    )
    if any(not _valid_uuid_set(values) for values in membership_sets):
        return False
    if resource.approval_status not in {
        None,
        "draft",
        "pending_review",
        "changes_requested",
        "approved",
        "rejected",
    }:
        return False
    return isinstance(resource.unrecognized_fields, frozenset) and all(
        isinstance(value, str) for value in resource.unrecognized_fields
    )
